# -*- coding: utf-8 -*-
"""
MCP 工具模块 - 管理 MCP Server 连接、工具发现与调用

核心设计：
- MCPClientManager 是全局单例，多窗口共享连接池
- 所有 MCP 异步操作在一个专用后台线程的事件循环中执行
- 每个连接由一个持久 Task 管理，__aenter__/__aexit__ 在同一 Task 中
- 断开连接通过 Event 信号通知 Task 自然退出 async with 块
"""

import asyncio
import json
import os
import re
import threading
from typing import Dict, List, Optional, Tuple

from loguru import logger
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from app.tools.result import ToolResult


def _extract_real_error(exc: Exception) -> Exception:
    """
    从嵌套的 ExceptionGroup/BaseExceptionGroup 中提取最底层的真实错误。
    如果无法提取，返回原异常。
    """
    # Python 3.11+ ExceptionGroup
    if hasattr(exc, "exceptions"):
        group = exc
        # 遍历所有层级，找到第一个非 ExceptionGroup 的异常
        while hasattr(group, "exceptions") and group.exceptions:
            for e in group.exceptions:
                if not hasattr(e, "exceptions") or not e.exceptions:
                    return e
            # 所有子异常都是 ExceptionGroup → 继续深入第一支
            group = group.exceptions[0]
    return exc


class MCPServerConnection:
    """单个 MCP Server 的连接管理"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session: Optional[ClientSession] = None
        self.tools: List[mcp_types.Tool] = []
        # 持久 Task 管理（__aenter__/__aexit__ 在同一 Task 中）
        self._task: Optional[asyncio.Task] = None
        self._disconnect_event: Optional[asyncio.Event] = None
        self._ready_event: Optional[asyncio.Event] = None
        self._connect_error: Optional[Exception] = None

    @property
    def server_type(self) -> str:
        return self.config.get("type", "stdio")

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)


class MCPClientManager:
    """
    MCP 客户端管理器（全局单例）

    所有异步 MCP 操作在专用后台线程的持久事件循环中执行。
    每个服务器连接由一个持久 asyncio.Task 管理：
    - Task 内部用 async with 持有 transport + session
    - 断开时设置 Event 信号，Task 自然退出 async with 块
    - 确保 __aenter__ / __aexit__ 在同一 Task 中执行
    """

    TOOL_PREFIX = "mcp__"

    _instance = None
    _ref_count = 0

    @classmethod
    def get_instance(cls) -> "MCPClientManager":
        if cls._instance is None:
            cls._instance = MCPClientManager()
        return cls._instance

    def __init__(self):
        if MCPClientManager._instance is not None and MCPClientManager._instance is not self:
            raise RuntimeError("请使用 MCPClientManager.get_instance() 获取单例")

        self._connections: Dict[str, MCPServerConnection] = {}
        self._connected = False

        # 按 name 加锁：同一 server 只允许一个进行中的连接/断开操作
        self._busy_names: set = set()
        self._busy_lock = threading.Lock()

        # 专用后台线程 + 持久事件循环
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_loop()

    def _start_loop(self):
        """启动后台线程的持久事件循环"""
        self._loop_ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, name="mcp-eventloop", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5)
        logger.info("[MCP] 后台事件循环已启动")

    def _run_async(self, coro, timeout: float = 60):
        """在后台事件循环中执行协程，同步等待结果

        Args:
            coro: 协程对象
            timeout: 超时时间（秒），默认 60 秒

        Returns:
            协程执行结果

        Raises:
            TimeoutError: 执行超时
            RuntimeError: 事件循环未运行
        """
        if not self._loop or self._loop.is_closed():
            raise RuntimeError("MCP 事件循环未运行")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"协程执行超时（{timeout}秒）")
        except asyncio.CancelledError:
            # 🛡️ 透传取消信号，不拦截
            # 强制停止对话时 asyncio 会抛出 CancelledError（Python 3.11+ 是 BaseException），
            # 必须透传而非捕获，否则取消语义丢失并导致 Qt 事件循环收到未预期异常。
            raise
        except ExceptionGroup as e:
            # 处理 ExceptionGroup（Python 3.11+）
            raise _extract_real_error(e)
        except Exception as e:
            raise _extract_real_error(e)

    # ── 连接生命周期（持久 Task 模式）──────────────

    async def _server_lifespan(self, conn: MCPServerConnection) -> None:
        """
        连接生命周期 — 在专属 Task 中运行。

        用 async with 管理 transport 和 session，
        保证 __aenter__/__aexit__ 在同一 Task 中执行。
        断开时通过 _disconnect_event 信号自然退出。
        """
        server_type = conn.server_type
        try:
            if server_type == "stdio":
                await self._lifespan_stdio(conn)
            elif server_type == "sse":
                await self._lifespan_sse(conn)
            elif server_type == "http":
                # 如果 http 类型但含有 command 字段(无预设 URL)，需要先启动进程获取 URL
                if conn.config.get("command"):
                    await self._lifespan_http_from_stdio(conn)
                else:
                    await self._lifespan_http(conn)
            else:
                conn._connect_error = ValueError(f"不支持的 MCP 服务器类型: {server_type}")
                conn._ready_event.set()
        except asyncio.CancelledError:
            logger.debug(f"[MCP] 服务器 '{conn.name}' 的生命周期 Task 被取消")
        except Exception as e:
            if not conn._ready_event.is_set():
                # 尝试从 ExceptionGroup 中提取真实错误信息
                actual = _extract_real_error(e)
                conn._connect_error = actual
                conn._ready_event.set()
            else:
                logger.warning(f"[MCP] 服务器 '{conn.name}' 生命周期异常: {e}")
        finally:
            conn.session = None
            conn.tools = []
            logger.info(f"[MCP] 已断开服务器 '{conn.name}'")

    async def _lifespan_stdio(self, conn: MCPServerConnection) -> None:
        command = conn.config.get("command", "")
        args = conn.config.get("args", [])
        env = conn.config.get("env")

        params = StdioServerParameters(command=command, args=args, env=env)

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                conn.session = session
                conn.tools = result.tools
                conn._ready_event.set()
                await conn._disconnect_event.wait()

    async def _lifespan_sse(self, conn: MCPServerConnection) -> None:
        url = conn.config.get("url", "")
        headers = conn.config.get("headers")

        async with sse_client(url=url, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                conn.session = session
                conn.tools = result.tools
                conn._ready_event.set()
                await conn._disconnect_event.wait()

    async def _lifespan_http(self, conn: MCPServerConnection) -> None:
        url = conn.config.get("url", "")
        headers = conn.config.get("headers")

        async with streamablehttp_client(url=url, headers=headers) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                conn.session = session
                conn.tools = result.tools
                conn._ready_event.set()
                await conn._disconnect_event.wait()

    async def _lifespan_http_from_stdio(self, conn: MCPServerConnection) -> None:
        """
        两阶段连接：先通过 stdio 启动服务器进程 -> 捕获 stdout 中的 URL -> 切换 HTTP 连接。

        适用场景：服务器使用 --transport http 参数，启动后输出 URL 到 stdout
        （如 @brave/brave-search-mcp-server --transport http）
        """
        command = conn.config.get("command", "")
        args = conn.config.get("args", [])
        env = conn.config.get("env")
        merged_env = {**os.environ, **(env or {})}

        logger.info(f"[MCP] '{conn.name}' 启动进程中获取 URL...")

        # 阶段一：启动进程并捕获 stdout（寻找 URL）
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )

        found_url = None
        try:
            # 读取 stdout 寻找 URL（最大等待 15 秒）
            async def _find_url():
                nonlocal found_url
                assert process.stdout is not None
                url_pattern = re.compile(r'http[s]?://[^\s"\']+')
                while True:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace").strip()
                    logger.debug(f"[MCP] '{conn.name}' stdout: {decoded}")
                    # 尝试匹配 URL
                    match = url_pattern.search(decoded)
                    if match:
                        found_url = match.group(0)
                        return
                    # 也检查是否为纯 URL 行
                    if decoded.startswith("http://") or decoded.startswith("https://"):
                        found_url = decoded
                        return

            await _find_url()
        except asyncio.TimeoutError:
            # 超时未找到 URL，尝试杀掉进程后回退到 stdio
            try:
                process.kill()
            except Exception:
                pass
            raise TimeoutError(
                f"启动服务器 '{conn.name}' 后未能在 stdout 中找到 URL（15秒超时），"
                f"请确认服务器使用了正确的 --transport 参数"
            )

        if not found_url:
            try:
                process.kill()
            except Exception:
                pass
            raise RuntimeError(
                f"启动服务器 '{conn.name}' 后未能从输出中解析到 URL"
            )

        # 清理 URL 中的尾部分隔符
        found_url = found_url.rstrip("/,.;")
        # 如果 URL 是 0.0.0.0，替换为 localhost
        found_url = found_url.replace("0.0.0.0", "127.0.0.1")
        logger.info(f"[MCP] '{conn.name}' 获取到 URL: {found_url}")

        # 阶段二：用获取到的 URL 建立 HTTP 连接
        # 保持子进程运行（HTTP 服务器进程），连接关闭时杀掉进程
        try:
            async with streamablehttp_client(url=found_url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    conn.session = session
                    conn.tools = result.tools
                    conn._ready_event.set()
                    logger.info(f"[MCP] '{conn.name}' HTTP 连接成功，发现 {len(conn.tools)} 个工具")
                    await conn._disconnect_event.wait()
        finally:
            # 断开连接时杀掉子进程
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            except Exception:
                pass

    # ── 连接操作 ──────────────────────────────────────

    def connect_all_sync(self, servers_config: List[dict]) -> None:
        """同步连接所有 MCP 服务器（阻塞调用线程，慎用）"""
        self._run_async(self._connect_all(servers_config))

    def connect_all_background(self, servers_config: List[dict], on_done=None) -> None:
        """后台连接所有 MCP 服务器（不阻塞 UI 线程）"""
        def _worker():
            try:
                self._run_async(self._connect_all(servers_config))
            except Exception as e:
                logger.error(f"[MCP] 后台连接失败: {e}")
            finally:
                if on_done:
                    connected = sum(1 for c in self._connections.values() if c.session)
                    failed = [
                        s.get("name", "?") for s in servers_config
                        if s.get("enabled", True) and s.get("name") not in self._connections
                    ]
                    try:
                        on_done(connected, len(servers_config), failed)
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        threading.Thread(target=_worker, name="mcp-connect", daemon=True).start()
        logger.info("[MCP] 后台连接已启动")

    async def _connect_all(self, servers_config: List[dict]) -> None:
        if self._connected:
            await self._disconnect_all()

        # 收集需要连接的服务器列表
        enabled_servers = []
        for server_cfg in servers_config:
            name = server_cfg.get("name", "")
            if not name:
                continue
            if not server_cfg.get("enabled", True):
                logger.info(f"[MCP] 跳过已禁用的服务器: {name}")
                continue
            enabled_servers.append(server_cfg)

        # 并行启动所有服务器连接（不再串行 await）
        tasks = {
            server_cfg["name"]: asyncio.create_task(
                self._connect_single(server_cfg["name"], server_cfg),
                name=f"mcp-connect-{server_cfg['name']}",
            )
            for server_cfg in enabled_servers
        }

        # 等待所有连接完成（每个任务内部有 30 秒超时，互不阻塞）
        for name, task in tasks.items():
            try:
                await task
            except Exception as e:
                logger.error(f"[MCP] 连接服务器 '{name}' 失败: {e}")

        self._connected = True

    def connect_server_sync(self, name: str, config: dict) -> bool:
        """同步连接单个 MCP 服务器（热添加）"""
        try:
            success, err = self._run_async(self._connect_single(name, config))
            if not success and err:
                logger.error(f"[MCP] 热添加服务器 '{name}' 失败: {err}")
            return success
        except Exception as e:
            logger.error(f"[MCP] 热添加服务器 '{name}' 失败: {e}")
            return False

    def connect_server_background(self, name: str, config: dict, on_done=None) -> None:
        """后台连接单个 MCP 服务器（不阻塞 UI）

        同一 name 只允许一个进行中的连接操作，重复请求被丢弃。
        """
        # 防重：同名服务器正在连接/断开中，跳过
        with self._busy_lock:
            if name in self._busy_names:
                logger.debug(f"[MCP] '{name}' 正在连接/断开中，跳过重复请求")
                if on_done:
                    try:
                        on_done(name, False, "服务器正在操作中，请稍后重试")
                    except Exception:
                        pass
                return
            self._busy_names.add(name)

        def _worker():
            success = False
            error_msg = ""
            try:
                success, error_msg = self._run_async(self._connect_single(name, config))
            except Exception as e:
                error_msg = str(e)
                logger.error(f"[MCP] 热添加服务器 '{name}' 失败: {e}")
            finally:
                with self._busy_lock:
                    self._busy_names.discard(name)
                if on_done:
                    try:
                        on_done(name, success, error_msg)
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        threading.Thread(target=_worker, name="mcp-hot-add", daemon=True).start()

    async def _connect_single(self, name: str, config: dict) -> tuple:
        """
        连接单个服务器：启动生命周期 Task 并等待就绪
        返回: (success: bool, error_msg: str)
        """
        # 如果已存在，先断开
        if name in self._connections:
            await self._disconnect_single(name)

        conn = MCPServerConnection(name, config)
        conn._disconnect_event = asyncio.Event()
        conn._ready_event = asyncio.Event()
        conn._connect_error = None

        # 在后台事件循环中启动生命周期 Task
        conn._task = asyncio.ensure_future(self._server_lifespan(conn), loop=self._loop)

        # 等待就绪或出错（最多 30 秒）
        try:
            await asyncio.wait_for(conn._ready_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            conn._task.cancel()
            msg = f"连接服务器 '{name}' 超时（30秒）"
            logger.error(f"[MCP] {msg}")
            return False, msg

        if conn._connect_error:
            err = conn._connect_error
            # 尝试提取更友好的错误信息
            err_str = str(err)
            if "JSONRPC" in err_str and "Invalid JSON" in err_str:
                # 服务器 stdout 输出了非 JSON 内容 → 可能是 http/sse 服务器却用了 stdio 类型
                err_str += "（服务器输出了非 JSON 内容到 stdout，请检查配置类型是否正确）"
            msg = f"连接服务器 '{name}' 失败: {err_str}"
            logger.error(f"[MCP] {msg}")
            return False, msg

        self._connections[name] = conn
        self._connected = True
        logger.info(f"[MCP] 已连接服务器 '{name}'，发现 {len(conn.tools)} 个工具")
        return True, ""

    # ── 断开连接 ──────────────────────────────────────

    def disconnect_server_sync(self, name: str) -> bool:
        """同步断开单个 MCP 服务器（快，不等待 Task 清理）"""
        try:
            return self._run_async(self._disconnect_single(name))
        except Exception as e:
            logger.error(f"[MCP] 热断开服务器 '{name}' 失败: {e}")
            return False

    def disconnect_server_background(self, name: str, on_done=None) -> None:
        """后台断开单个 MCP 服务器（不阻塞 UI）

        与 connect_server_background 共享一个锁，同名请求丢弃。
        """
        with self._busy_lock:
            if name in self._busy_names:
                logger.debug(f"[MCP] '{name}' 正在连接/断开中，跳过重复请求")
                if on_done:
                    try:
                        on_done(name)
                    except Exception:
                        pass
                return
            self._busy_names.add(name)

        def _worker():
            try:
                self._run_async(self._disconnect_single(name))
            except Exception as e:
                logger.error(f"[MCP] 热断开服务器 '{name}' 失败: {e}")
            finally:
                with self._busy_lock:
                    self._busy_names.discard(name)
                if on_done:
                    try:
                        on_done(name)
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        threading.Thread(target=_worker, name="mcp-hot-disconnect", daemon=True).start()

    async def _disconnect_single(self, name: str) -> bool:
        """断开单个服务器：信号通知 + 取消 Task，不等待清理完成"""
        conn = self._connections.pop(name, None)
        if not conn:
            return False

        # 通知生命周期 Task 退出（走 async with 正常清理路径）
        if conn._disconnect_event:
            conn._disconnect_event.set()

        # 取消 Task 作为备份（CancelledError 在 async with 内触发 __aexit__）
        if conn._task and not conn._task.done():
            conn._task.cancel()

        # 立即清除引用，不等待 Task 完成
        conn.session = None
        conn.tools = []

        if not self._connections:
            self._connected = False
        return True

    def disconnect_all_sync(self) -> None:
        """同步断开所有连接（快，不等待 Task 清理）"""
        try:
            self._run_async(self._disconnect_all())
        except Exception as e:
            logger.warning(f"[MCP] 断开连接失败: {e}")

    def disconnect_all_background(self, on_done=None) -> None:
        """后台断开所有连接（不阻塞 UI）"""
        def _worker():
            try:
                self._run_async(self._disconnect_all())
            except Exception as e:
                logger.error(f"[MCP] 后台断开所有连接失败: {e}")
            finally:
                if on_done:
                    try:
                        on_done()
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        threading.Thread(target=_worker, name="mcp-disconnect-all", daemon=True).start()

    async def _disconnect_all(self) -> None:
        names = list(self._connections.keys())
        for name in names:
            try:
                await self._disconnect_single(name)
            except Exception as e:
                logger.error(f"[MCP] 断开服务器 '{name}' 失败: {e}")

        self._connections.clear()
        self._connected = False

    # ── 工具 Schema ──────────────────────────────────

    def get_tool_schemas(self) -> List[Dict]:
        """获取所有 MCP 工具的 OpenAI function calling schema"""
        schemas = []
        for server_name, conn in self._connections.items():
            if not conn.session or not conn.enabled:
                continue
            for tool in conn.tools:
                prefixed_name = f"{self.TOOL_PREFIX}{server_name}__{tool.name}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": prefixed_name,
                        "description": tool.description or f"MCP tool: {tool.name}",
                        "parameters": tool.inputSchema or {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
                schemas.append(schema)
        return schemas

    # ── 工具调用 ──────────────────────────────────────

    def call_tool_sync(self, prefixed_name: str, arguments: dict, timeout: float = 120) -> ToolResult:
        """同步调用 MCP 工具（供 ToolExecutor 调用）

        Args:
            prefixed_name: 带前缀的工具名（如 mcp__server__tool）
            arguments: 工具参数
            timeout: 超时时间（秒），默认 120 秒
        """
        try:
            return self._run_async(self._call_tool(prefixed_name, arguments), timeout=timeout)
        except TimeoutError as e:
            logger.error(f"[MCP] 调用工具 '{prefixed_name}' 超时（{timeout}s）: {e}")
            return ToolResult(False, error=f"MCP 工具调用超时（{timeout}秒），请稍后重试")
        except Exception as e:
            logger.error(f"[MCP] 调用工具 '{prefixed_name}' 失败: {e}")
            return ToolResult(False, error=f"MCP 工具调用失败: {e}")

    async def _call_tool(self, prefixed_name: str, arguments: dict) -> ToolResult:
        parsed = self._parse_tool_name(prefixed_name)
        if not parsed:
            return ToolResult(False, error=f"无效的 MCP 工具名: {prefixed_name}")

        server_name, tool_name = parsed
        conn = self._connections.get(server_name)
        if not conn or not conn.session:
            return ToolResult(False, error=f"MCP 服务器 '{server_name}' 未连接")

        try:
            result = await conn.session.call_tool(tool_name, arguments)

            text_parts = []
            for content in (result.content or []):
                if isinstance(content, mcp_types.TextContent):
                    text_parts.append(content.text)
                elif hasattr(content, "text"):
                    text_parts.append(str(content.text))

            output = "\n".join(text_parts) if text_parts else str(result)

            if result.isError:
                return ToolResult(False, error=output)

            return ToolResult(True, content=output)

        except Exception as e:
            logger.error(f"[MCP] 调用工具 '{prefixed_name}' 失败: {e}")
            return ToolResult(False, error=f"MCP 工具调用失败: {e}")

    # ── 辅助方法 ──────────────────────────────────────

    def _parse_tool_name(self, prefixed_name: str) -> Optional[tuple]:
        if not prefixed_name.startswith(self.TOOL_PREFIX):
            return None
        remainder = prefixed_name[len(self.TOOL_PREFIX):]
        if "__" not in remainder:
            return None
        server_name, tool_name = remainder.split("__", 1)
        return server_name, tool_name

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_status(self) -> List[Dict]:
        # 注意：返回的 tools 必须带 mcp__{server}__ 前缀，与 get_tool_schemas() 保持一致，
        # 避免 LLM 从 mcp_list_servers 看到裸名后误用导致调用失败。
        status = []
        with self._busy_lock:
            busy_names = set(self._busy_names)
        for name, conn in self._connections.items():
            status.append({
                "name": name,
                "type": conn.server_type,
                "enabled": conn.enabled,
                "connected": conn.session is not None,
                "busy": name in busy_names,
                "tool_count": len(conn.tools),
                "tools": [
                    f"{self.TOOL_PREFIX}{name}__{t.name}" for t in conn.tools
                ],
            })
        return status

    # ── 引用计数（多窗口生命周期）──────────────────────

    def acquire(self):
        MCPClientManager._ref_count += 1
        logger.debug(f"[MCP] acquire, ref_count={MCPClientManager._ref_count}")

    def release(self):
        MCPClientManager._ref_count = max(0, MCPClientManager._ref_count - 1)
        logger.debug(f"[MCP] release, ref_count={MCPClientManager._ref_count}")
        if MCPClientManager._ref_count == 0 and self._connected:
            self.disconnect_all_sync()

    def shutdown(self):
        """彻底关闭后台事件循环（进程退出时调用）"""
        if self._connected:
            self.disconnect_all_sync()
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)


# ═══════════════════════════════════════════════════════════
# MCP Server 自动发现
# ═══════════════════════════════════════════════════════════
# 配置路径来源参考：
# - Claude Desktop: https://modelcontextprotocol.io/docs/getting-started/installation
# - Cursor: https://www.rapidevelopers.com/mcp-tutorial/how-to-configure-mcp-in-cursor-settings
# - Windsurf: https://deepwiki.com/hidao80/mcp-tutorial-1/3.3-windsurf-setup
# - Claude Code: https://docs.code.claude.com/mcp/setup/
# - VS Code (Cline/Continue): .vscode/mcp.json（项目级）
# ═══════════════════════════════════════════════════════════


def _discover_claude_desktop_servers() -> List[dict]:
    """
    扫描 Claude Desktop 配置，发现 MCP 服务器

    配置路径：
    - Windows: %APPDATA%/Claude/claude_desktop_config.json
    - macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json
    - Linux:   ~/.config/Claude/claude_desktop_config.json

    配置格式：
    {
      "mcpServers": {
        "server-name": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
        }
      }
    }
    """
    servers = []
    config_paths = []

    if os.name == "nt" or os.environ.get("OS") == "Windows_NT":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            config_paths.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    elif os.uname().sysname == "Darwin":
        config_paths.append(os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json"))
    else:
        config_paths.append(os.path.expanduser("~/.config/Claude/claude_desktop_config.json"))

    for config_path in config_paths:
        if not os.path.exists(config_path):
            continue

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            config = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.debug(f"[MCP] 解析 Claude Desktop 配置失败 {config_path}: {e}")
            continue

        mcp_servers = config.get("mcpServers", {})
        for name, server_cfg in mcp_servers.items():
            if not isinstance(server_cfg, dict):
                continue

            command = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            if not command:
                continue

            servers.append({
                "name": name,
                "type": "stdio",
                "command": command,
                "args": args,
                "env": server_cfg.get("env"),
                "enabled": False,
                "_source": "claude_desktop",
                "_source_path": config_path,
            })
            logger.info(f"[MCP] 发现 Claude Desktop 服务器: {name}")

    return servers


def _discover_cursor_servers() -> List[dict]:
    """
    扫描 Cursor IDE 配置，发现 MCP 服务器

    配置路径（全局）：
    - Windows: %APPDATA%/Cursor/User/globalStorage/mcp-settings.json
    - macOS:   ~/Library/Application Support/Cursor/User/globalStorage/mcp-settings.json
    - Linux:   ~/.config/Cursor/User/globalStorage/mcp-settings.json

    配置格式：
    {
      "mcpServers": {
        "server-name": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem"]
        }
      }
    }

    注意：Cursor 还支持项目级 ~/.cursor/mcp.json（全局），
    但由于是用户 home 目录，与全局配置重复，这里只扫描 globalStorage。
    """
    servers = []
    config_paths = []

    if os.name == "nt" or os.environ.get("OS") == "Windows_NT":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            config_paths.append(os.path.join(appdata, "Cursor", "User", "globalStorage", "mcp-settings.json"))
    elif os.uname().sysname == "Darwin":
        config_paths.append(os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/mcp-settings.json"))
    else:
        config_paths.append(os.path.expanduser("~/.config/Cursor/User/globalStorage/mcp-settings.json"))

    for config_path in config_paths:
        if not os.path.exists(config_path):
            continue

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            config = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.debug(f"[MCP] 解析 Cursor 配置失败 {config_path}: {e}")
            continue

        mcp_servers = config.get("mcpServers", {})
        for name, server_cfg in mcp_servers.items():
            if not isinstance(server_cfg, dict):
                continue

            command = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            if not command:
                continue

            servers.append({
                "name": name,
                "type": "stdio",
                "command": command,
                "args": args,
                "env": server_cfg.get("env"),
                "enabled": False,
                "_source": "cursor",
                "_source_path": config_path,
            })
            logger.info(f"[MCP] 发现 Cursor MCP 服务器: {name}")

    return servers


def _merge_and_deduplicate(existing: List[dict], discovered: List[dict]) -> Tuple[List[dict], List[dict]]:
    """
    合并已有配置和自动发现的配置，去重

    去重规则（按 command + args 组合判断）：
    - 已有配置保留
    - 新发现的如果 command+args 与已有完全相同则跳过
    - 新发现的如果 name 相同但 command+args 不同，name 后面加后缀区分

    Returns:
        (merged_list, new_ones) — 合并后的完整列表 + 仅新发现的列表
    """
    # 已有的 command+args 组合
    existing_signatures = set()
    for s in existing:
        cmd = s.get("command", "")
        args = tuple(s.get("args", []) or [])
        if cmd:
            existing_signatures.add((cmd, args))

    new_ones = []
    for srv in discovered:
        cmd = srv.get("command", "")
        args = tuple(srv.get("args", []) or [])
        if cmd and (cmd, args) not in existing_signatures:
            new_ones.append(srv)
            existing_signatures.add((cmd, args))  # 防止多个新发现重复

    # name 冲突处理
    existing_names = {s.get("name", "") for s in existing}

    def unique_name(name: str, suffix: str) -> str:
        if name not in existing_names:
            return name
        base = name
        idx = 1
        while f"{base}{suffix}{idx}" in existing_names:
            idx += 1
        return f"{base}{suffix}{idx}"

    for srv in new_ones:
        srv["name"] = unique_name(srv["name"], "_copy")

    merged = list(existing) + new_ones
    return merged, new_ones


def discover_and_merge() -> Tuple[List[dict], List[dict]]:
    """
    自动发现所有已知来源的 MCP 服务器

    发现结果由 backend._discover_mcp_servers() 写入 user-custom 插件，
    不再直接修改 Settings.mcp_servers。

    Returns:
        (all_servers, newly_discovered) — 所有服务器（含已有的）+ 新发现的列表
    """
    from app.core.plugin_manager import PluginManager

    pm = PluginManager.get_instance()
    existing = pm.get_mcp_servers() if pm.is_initialized() else []

    all_discovered = []
    all_discovered.extend(_discover_claude_desktop_servers())
    all_discovered.extend(_discover_cursor_servers())

    merged, new_ones = _merge_and_deduplicate(existing, all_discovered)

    if new_ones:
        logger.info(f"[MCP] 自动发现 {len(new_ones)} 个新服务器")

    return merged, new_ones
