# -*- coding: utf-8 -*-
"""
LSP 管理器 — 管理全部 LSP 客户端生命周期

职责：
  1. 从 PluginManager 加载 LSP 插件配置，创建 LspClient
  2. 根据文件扩展名路由到正确的 LSP 客户端
  3. 文件变更后自动通知 LSP 服务器，获取诊断
  4. 启动/停止/热重载所有 LSP 服务器
  5. 维护持久事件循环线程，确保所有 LSP 操作在同一 loop 中执行
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

from loguru import logger

from .lsp_client import LspClient
from .lsp_config import LspServerConfig


class LspManager:
    """全局 LSP 管理器（单例）

    维护一个持久 asyncio 事件循环在 daemon 线程中运行。
    所有 LSP 操作（async）通过 run_coroutine_threadsafe 提交到该 loop。
    同步调用方通过 concurrent.futures.Future.result(timeout) 等待结果。
    """

    _instance: Optional["LspManager"] = None

    @classmethod
    def get_instance(cls) -> "LspManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._clients: Dict[str, LspClient] = {}
        self._ext_map: Dict[str, str] = {}
        self._workspace_root: str = ""
        self._initialized = False

        # 持久事件循环
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()

    # ── 事件循环管理 ──────────────────────────────────────────

    def _ensure_loop(self):
        """确保持久事件循环在后台线程中运行"""
        if self._loop is not None and not self._loop.is_closed():
            return

        self._loop_ready.clear()
        self._loop = asyncio.new_event_loop()

        def _run_forever():
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            self._loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run_forever, daemon=True, name="lsp-loop"
        )
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5)
        logger.debug("[LspManager] 持久事件循环已启动")

    def _submit(self, coro, timeout: float = 30.0):
        """向持久事件循环提交协程，同步等待结果

        Args:
            coro: 协程对象
            timeout: 超时秒数

        Returns:
            协程返回值，超时或异常返回 None
        """
        self._ensure_loop()
        if self._loop is None:
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.error(f"[LspManager] 操作超时 ({timeout}s)")
            return None
        except Exception as e:
            logger.error(f"[LspManager] 操作异常: {e}")
            return None

    # ── 初始化 ──────────────────────────────────────────────────

    def initialize(self, workspace_root: str, lsp_configs: Optional[List[Dict[str, Any]]] = None) -> None:
        # 幂等：已初始化 + 已有客户端 + 未传入新配置时跳过
        # 热重载通过 _on_refresh 传入非空 lsp_configs 强制走完整重建路径
        if self._initialized and self._clients and not lsp_configs:
            logger.debug("[LspManager] 已初始化，无新配置，跳过")
            return
        self._workspace_root = workspace_root
        # 停止旧客户端再清空 — 同步等待 stop_all 完成，消除竞态条件
        if self._clients:
            self._ensure_loop()
            if self._loop:
                try:
                    future = asyncio.run_coroutine_threadsafe(self.stop_all(), self._loop)
                    future.result(timeout=10.0)  # 同步等待所有旧服务器真正停止
                except concurrent.futures.TimeoutError:
                    logger.warning("[LspManager] 停止旧服务器超时，强制清空")
                except Exception as e:
                    logger.error(f"[LspManager] 停止旧服务器异常: {e}")
        self._clients.clear()
        self._ext_map.clear()

        if not lsp_configs:
            logger.debug("[LspManager] 无 LSP 配置")
            self._initialized = True
            return

        for entry in lsp_configs:
            plugin_name = entry.get("plugin", "unknown")
            config_data = entry.get("config", {})

            for server_name, server_data in config_data.items():
                try:
                    config = LspServerConfig.from_dict(server_name, server_data, plugin_name)
                    client = LspClient(config, workspace_root)
                    self._clients[server_name] = client
                    for ext, lang in config.extension_to_language.items():
                        self._ext_map[ext.lower()] = server_name
                    logger.debug(
                        f"[LspManager] 注册 {server_name} ({plugin_name}): "
                        f"ext={list(config.extension_to_language.keys())}"
                    )
                except Exception as e:
                    logger.error(f"[LspManager] 解析 {plugin_name}/{server_name} 配置失败: {e}")

        self._initialized = True
        logger.info(f"[LspManager] 已注册 {len(self._clients)} 个 LSP 客户端: {list(self._clients.keys())}")

    # ── 生命周期 ──────────────────────────────────────────────

    async def start_all(self) -> None:
        """启动所有已注册的 LSP 服务器（必须在持久 loop 中调用）"""
        for name, client in self._clients.items():
            success = await client.start()
            if success:
                logger.info(f"[LspManager] ✅ {name} 已启动")
            else:
                logger.warning(f"[LspManager] ⚠️ {name} 启动失败")

    def start_all_background(self):
        """后台启动所有 LSP 服务器"""
        self._ensure_loop()
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.start_all(), self._loop)
        logger.debug("[LspManager] start_all 已提交到后台事件循环")

    async def stop_all(self) -> None:
        for client in self._clients.values():
            await client.stop()
        logger.info("[LspManager] 所有 LSP 服务器已停止")

    # ── 增量热重载 ───────────────────────────────────────────

    def add_plugin_servers(self, plugin_name: str, config_data: dict) -> int:
        """增量注册并启动一个插件的 LSP 服务器，不影响已有服务器

        用于新增插件的增量热重载场景：
        - 只注册该插件新增的服务器（跳过已存在的同名服务器）
        - 新服务器注册后立即后台启动（不阻塞调用线程）
        - 已有服务器不受影响

        Args:
            plugin_name: 插件名称
            config_data: .lsp.json 的配置内容（{server_name: {...}}）

        Returns:
            成功注册的服务器数量（后台启动中，不一定全部启动成功）
        """
        self._ensure_loop()
        if self._loop is None:
            logger.warning("[LspManager] 事件循环未就绪，无法增量添加服务器")
            return 0

        count = 0
        for server_name, server_data in config_data.items():
            if server_name in self._clients:
                logger.debug(f"[LspManager] 服务器 {server_name} 已存在，跳过增量注册")
                continue
            try:
                config = LspServerConfig.from_dict(server_name, server_data, plugin_name)
                client = LspClient(config, self._workspace_root)
                self._clients[server_name] = client
                for ext, lang in config.extension_to_language.items():
                    self._ext_map[ext.lower()] = server_name
                logger.debug(
                    f"[LspManager] 增量注册 {server_name} ({plugin_name}): "
                    f"ext={list(config.extension_to_language.keys())}"
                )
                # 后台启动新服务器（不阻塞调用线程）
                asyncio.run_coroutine_threadsafe(client.start(), self._loop)
                logger.info(f"[LspManager] ✅ 增量启动 {server_name} ({plugin_name})")
                count += 1
            except Exception as e:
                logger.error(f"[LspManager] 增量注册 {plugin_name}/{server_name} 失败: {e}")
        if count:
            logger.info(f"[LspManager] 增量添加 {count} 个服务器 (来自 {plugin_name})")
        return count

    def remove_plugin_servers(self, plugin_name: str) -> int:
        """移除并停止指定插件的所有 LSP 服务器

        用于插件被删除或 .lsp.json 被移除时的增量清理场景：
        - 只停止并移除属于该插件的服务器
        - 其他插件服务器不受影响

        Args:
            plugin_name: 插件名称

        Returns:
            移除的服务器数量
        """
        self._ensure_loop()
        if self._loop is None:
            return 0

        to_remove = [
            name for name, client in self._clients.items()
            if client.config.plugin_name == plugin_name
        ]
        if not to_remove:
            return 0

        for name in to_remove:
            client = self._clients.pop(name, None)
            if client:
                try:
                    # 后台停止，不阻塞调用线程
                    asyncio.run_coroutine_threadsafe(client.stop(), self._loop)
                except Exception as e:
                    logger.warning(f"[LspManager] 停止 {name} 异常: {e}")

        # 批量清理 ext_map
        remove_set = set(to_remove)
        self._ext_map = {ext: s for ext, s in self._ext_map.items() if s not in remove_set}

        logger.info(f"[LspManager] 已移除 {len(to_remove)} 个服务器 (来自 {plugin_name}): {to_remove}")
        return len(to_remove)

    async def _ensure_started(self, client: LspClient) -> bool:
        if client.is_running:
            return True
        return await client.start()

    # ── 路由 ────────────────────────────────────────────────────

    def get_client_for_file(self, file_path: str) -> Optional[LspClient]:
        ext = os.path.splitext(file_path)[1].lower()
        server_name = self._ext_map.get(ext)
        if not server_name:
            return None
        return self._clients.get(server_name)

    def get_client_by_name(self, name: str) -> Optional[LspClient]:
        return self._clients.get(name)

    # ── 同步接口（供 lsp_tools.py 调用）────────────────────────

    def sync_get_diagnostics(self, file_path: str, timeout: float = 40.0):
        """同步获取诊断（区分状态）

        Returns:
            (status, payload) 元组
            - ('ok', str | None)     : 成功。str 为诊断文本，None 表示真无诊断
            - ('no_client', str)     : 无对应 LSP 客户端（扩展名未注册）
            - ('start_failed', str)  : LSP 客户端启动失败
            - ('timeout', str)       : 操作超时
            - ('error', str)         : 协程异常
        """
        self._ensure_loop()
        if self._loop is None:
            return ("no_client", "事件循环未启动")
        client = self.get_client_for_file(file_path)
        if not client:
            return ("no_client", f"无 LSP 客户端处理 {os.path.basename(file_path)}")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.get_diagnostics_for_file(file_path), self._loop
            )
            result = future.result(timeout=timeout)
            if not client.is_running:
                return ("start_failed", "LSP 客户端启动失败（可执行文件缺失？）")
            return ("ok", result)
        except concurrent.futures.TimeoutError:
            logger.error(f"[LspManager] diagnostics 超时 ({timeout}s): {file_path}")
            return ("timeout", str(timeout))
        except Exception as e:
            logger.error(f"[LspManager] diagnostics 异常: {e}")
            return ("error", str(e))

    def sync_quick_diagnostics(self, file_path: str, timeout: float = 5.0) -> Optional[str]:
        """快速诊断：跳过 LSP 协议握手，直接走 CLI 工具获取诊断

        与 sync_get_diagnostics 的区别：
        - 不 did_open / wait_diagnostics / did_close（节省 ~5s 空等）
        - 直接调用 CLI 工具（pyright --outputjson）
        - 紧凑输出：只报 Error + Warning，15 条上限，每条消息 120 字符截断
        - 默认 5s 超时（CLI 工具通常秒级完成）

        适用场景：自动诊断（文件编辑后即时反馈）
        """
        return self._submit(self._quick_diagnostics(file_path), timeout)

    def sync_document_symbols(self, file_path: str, timeout: float = 10.0) -> Optional[list]:
        return self._submit(self.document_symbols(file_path), timeout)

    def sync_go_to_definition(self, file_path: str, line: int, column: int, timeout: float = 10.0) -> Optional[list]:
        return self._submit(self.go_to_definition(file_path, line, column), timeout)

    def sync_find_references(self, file_path: str, line: int, column: int, timeout: float = 10.0) -> Optional[list]:
        return self._submit(self.find_references(file_path, line, column), timeout)

    def sync_hover(self, file_path: str, line: int, column: int, timeout: float = 10.0) -> Optional[str]:
        return self._submit(self.hover(file_path, line, column), timeout)

    # ── 文件变更 ──────────────────────────────────────────────

    async def on_file_changed(self, file_path: str) -> Optional[str]:
        client = self.get_client_for_file(file_path)
        if not client or not await self._ensure_started(client):
            return None
        try:
            await client.did_change(file_path)
            diags = await client.wait_diagnostics(timeout=8.0)
            if diags:
                return self._format_diagnostics(file_path, diags)
        except Exception as e:
            logger.error(f"[LspManager] on_file_changed 错误: {e}")
        return None

    @staticmethod
    def _format_diagnostics(file_path: str, diagnostics: list) -> str:
        if not diagnostics:
            return ""
        fname = os.path.basename(file_path)
        sev_order = {"Error": 0, "Warning": 1, "Information": 2, "Hint": 3}
        sorted_diags = sorted(
            diagnostics,
            key=lambda d: (sev_order.get(d.get("severity", "Hint"), 99), d.get("line", 0)),
        )
        lines = [f"[LSP Diagnostic] {fname} ({len(sorted_diags)} issue(s)):"]
        for d in sorted_diags[:30]:
            sev = d.get("severity", "?")[:4].lower()
            ln = d.get("line", "?")
            col = d.get("column", "?")
            msg = d.get("message", "")
            code = d.get("code", "")
            code_str = f" ({code})" if code else ""
            lines.append(f"  {ln}:{col} [{sev}] {msg}{code_str}")
        if len(sorted_diags) > 30:
            lines.append(f"  ... ({len(sorted_diags) - 30} more issues truncated)")
        return "\n".join(lines)

    # ── 被动诊断 ──────────────────────────────────────────────

    @staticmethod
    def _has_cli_mode(config) -> bool:
        """判断 LSP 服务器是否配置了 CLI 诊断兜底（通过 .lsp.json 的 ``cliFallback``）。

        配置驱动的判定 — 不再硬编码 server command 名匹配。
        未配置 cliFallback 的 server 必须走完整 LSP 协议。
        """
        return bool(getattr(config, "cli_fallback", None))

    async def _quick_diagnostics(self, file_path: str) -> Optional[str]:
        """快速诊断

        - pyright（有 CLI 模式）：直接 CLI + 紧凑格式化（5s 超时）
        - 其他服务器（无 CLI 模式）：走完整 LSP 协议（10s 超时）
        """
        client = self.get_client_for_file(file_path)
        if not client:
            return None
        if self._has_cli_mode(client.config):
            return await self._run_cli_compact(file_path, client.config)

        # 非 pyright：必须走 LSP 协议
        if not await self._ensure_started(client):
            return None
        await client.did_open(file_path)
        try:
            diags = await client.wait_diagnostics(timeout=20.0)
            if diags:
                return self._format_diagnostics(file_path, diags)
        except Exception:
            pass
        finally:
            await client.did_close(file_path)
        return None

    @staticmethod
    async def _run_cli_with_args(
        args: list, timeout: float,
    ) -> tuple[Optional[list], str, bool, str]:
        """通用 CLI 调用 + JSON 解析（pyright 风格）。由 cli_fallback 调度。"""
        import asyncio.subprocess
        subprocess_kwargs = {}
        if sys.platform == "win32":
            subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_kwargs,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None, "", True, ""
        except Exception as e:
            return None, "", False, str(e)
        stderr_text = stderr.decode("utf-8", errors="replace")[:500] if stderr else ""
        if not stdout:
            return None, stderr_text, False, ""
        try:
            import orjson as _orjson
            data = _orjson.loads(stdout)
        except Exception as e:
            return None, stderr_text, False, f"JSON 解析失败: {e}"
        return data.get("generalDiagnostics", []), stderr_text, False, ""

    @staticmethod
    async def _raw_cli_diagnostics(file_path: str, args: list) -> Optional[str]:
        """通用 CLI 调用 + 原始 stdout 返回（不解析）。"""
        import asyncio.subprocess
        subprocess_kwargs = {}
        if sys.platform == "win32":
            subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_kwargs,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30.0
            )
        except asyncio.TimeoutError:
            return "(CLI 超时 30s)"
        except Exception as e:
            return f"(CLI 失败: {e})"
        text = stdout.decode("utf-8", errors="replace")
        if not text.strip():
            stderr_text = stderr.decode("utf-8", errors="replace")[:200]
            return f"[CLI stderr] {os.path.basename(file_path)}:\n{stderr_text}" if stderr_text else None
        return f"[CLI] {os.path.basename(file_path)}:\n{text[:2000]}"

    @staticmethod
    async def _tsc_text_diagnostics(file_path: str, args: list) -> Optional[str]:
        """解析 tsc --noEmit 文本输出::

            file.ts(2,7): error TS2322: Type 'string' is not assignable to type 'number'.
        """
        import asyncio.subprocess
        import re
        subprocess_kwargs = {}
        if sys.platform == "win32":
            subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_kwargs,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30.0
            )
        except asyncio.TimeoutError:
            return "(tsc CLI 超时 30s)"
        except Exception as e:
            return f"(tsc CLI 失败: {e})"
        text = stdout.decode("utf-8", errors="replace")
        if not text.strip():
            return None
        pattern = re.compile(
            r"^(.+?)\((\d+),(\d+)\):\s+(error|warning|info|hint)\s+(TS\d+):\s+(.+)$",
            re.MULTILINE,
        )
        diags: List[Dict[str, Any]] = []
        for m in pattern.finditer(text):
            line_n = int(m.group(2))
            col_n = int(m.group(3))
            diags.append({
                "line": line_n,
                "column": col_n,
                "range": {"start": {"line": line_n - 1, "character": col_n - 1}},
                "severity": m.group(4),
                "message": m.group(6),
                "code": m.group(5),
            })
        if not diags:
            return f"[tsc] {os.path.basename(file_path)}:\n{text[:2000]}"
        return LspManager._format_diagnostics(file_path, diags)

    @staticmethod
    async def _run_cli_compact(file_path: str, config) -> Optional[str]:
        """CLI 调用 + 紧凑输出。优先使用 config.cli_fallback 配置；否则回退到 config.command。

        由 ``cli_fallback.parser`` 决定解析方式：
        - "tsc"：解析 tsc --noEmit 文本输出
        - "pyright"：解析 pyright --outputjson JSON 输出
        - "raw"：返回原始 stdout
        """
        cf = getattr(config, "cli_fallback", None) or {}
        parser = cf.get("parser", "raw")
        cli_cmd = cf.get("command") or config.command
        if sys.platform == "win32" and not cli_cmd.lower().endswith((".cmd", ".bat", ".exe")):
            base_args = ["cmd", "/c", cli_cmd]
        else:
            base_args = [cli_cmd]
        args = list(base_args)
        for tmpl in cf.get("argsTemplate", []):
            args.append(tmpl.replace("${file}", file_path))

        if parser == "tsc":
            return await LspManager._tsc_text_diagnostics(file_path, args)

        # raw / pyright 走原 pyright JSON 路径（pyright 路径也会被 raw 覆盖，
        # 因为 _run_cli_with_args 对非 JSON 输出返回空 diags，效果等同 raw）
        diags, stderr_text, timed_out, error_msg = await LspManager._run_cli_with_args(
            args, timeout=5.0
        )
        if timed_out:
            logger.debug(f"[LspManager] CLI 诊断超时 (5s): {file_path}")
            return None
        if error_msg:
            logger.debug(f"[LspManager] CLI 诊断异常: {error_msg}")
            return None
        if stderr_text and not diags:
            # stdout 为空但 stderr 有信息（如 pyright 未安装、配置错误）— 不再静默丢弃
            logger.warning(f"[LspManager] CLI stderr: {stderr_text}")
            return None
        if not diags:
            return None

        # 只保留 Error + Warning，按 (行, 列) 排序
        filtered = [
            d for d in diags
            if d.get("severity", "") in ("error", "Error", "warning", "Warning")
        ]
        if not filtered:
            return None

        filtered.sort(key=lambda d: (
            d.get("range", {}).get("start", {}).get("line", 0),
            d.get("range", {}).get("start", {}).get("character", 0),
        ))

        fname = os.path.basename(file_path)
        err_count = sum(1 for d in filtered if d.get("severity", "") in ("error", "Error"))
        warn_count = len(filtered) - err_count
        parts = []
        if err_count:
            parts.append(f"{err_count} errors")
        if warn_count:
            parts.append(f"{warn_count} warnings")

        lines = [f"[LSP Quick] {fname} ({', '.join(parts)}):"]

        for d in filtered[:15]:
            rng = d.get("range", {}).get("start", {})
            ln = rng.get("line", 0) + 1
            col = rng.get("character", 0) + 1
            sev = d.get("severity", "err")[:4]
            msg = d.get("message", "")
            if len(msg) > 120:
                msg = msg[:117] + "..."
            lines.append(f"  {ln}:{col} [{sev}] {msg}")

        if len(filtered) > 15:
            lines.append(f"  ... ({len(filtered) - 15} more omitted)")

        result = "\n".join(lines)
        if len(result) > 2000:
            result = result[:1997] + "..."

        return result

    async def get_diagnostics_for_file(self, file_path: str) -> Optional[str]:
        """AI 主动获取某文件的诊断

        策略：
        - pyright（有 CLI 模式）：LSP push 5s，失败回退 CLI
        - 其他服务器（无 CLI 模式）：LSP push 30s，强制走纯协议
        - 被动型 server：通过 ``triggerDiagnostics`` 配置主动触发
        """
        client = self.get_client_for_file(file_path)
        if not client or not await self._ensure_started(client):
            return None
        has_cli = self._has_cli_mode(client.config)
        push_timeout = 5.0 if has_cli else 30.0

        # 尝试 LSP push 诊断
        await client.did_open(file_path)
        # 对"被动型" server 显式触发完整分析（由插件 triggerDiagnostics 配置驱动）
        await client.request_full_diagnostics(file_path)
        try:
            diags = await client.wait_diagnostics(timeout=push_timeout)
            if diags:
                return self._format_diagnostics(file_path, diags)
        except Exception:
            pass
        finally:
            await client.did_close(file_path)  # 回收 LSP 服务器打开文件集合
        # 回退：插件通过 .lsp.json 的 cliFallback 声明
        if has_cli:
            return await LspManager._cli_dispatch(file_path, client.config)
        return None  # 无 CLI 回退配置，干净返回 None

    @staticmethod
    async def _cli_dispatch(file_path: str, config) -> Optional[str]:
        """CLI 兜底的分发器。由 ``cliFallback.parser`` 决定解析器。"""
        cf = getattr(config, "cli_fallback", None) or {}
        parser = cf.get("parser", "raw")
        cli_cmd = cf.get("command") or config.command
        if sys.platform == "win32" and not cli_cmd.lower().endswith((".cmd", ".bat", ".exe")):
            base_args = ["cmd", "/c", cli_cmd]
        else:
            base_args = [cli_cmd]
        args = list(base_args)
        for tmpl in cf.get("argsTemplate", []):
            args.append(tmpl.replace("${file}", file_path))

        if parser == "tsc":
            return await LspManager._tsc_text_diagnostics(file_path, args)
        if parser == "pyright":
            diags, _, timed_out, error_msg = await LspManager._run_cli_with_args(
                args, timeout=30.0
            )
            if timed_out:
                return "(LSP CLI 超时)"
            if error_msg:
                return f"(LSP CLI 失败: {error_msg})"
            if not diags:
                return None
            fname = os.path.basename(file_path)
            lines = [f"[LSP Diagnostc] {fname} ({len(diags)} issue(s)):"]
            for d in diags[:30]:
                rng = d.get("range", {}).get("start", {})
                ln = rng.get("line", 0) + 1
                col = rng.get("character", 0) + 1
                sev = d.get("severity", "error")
                msg = d.get("message", "")
                rule = d.get("rule", "")
                code_str = f" ({rule})" if rule else ""
                lines.append(f"  {ln}:{col} [{sev}] {msg}{code_str}")
            return "\n".join(lines)
        # raw / 兜底
        return await LspManager._raw_cli_diagnostics(file_path, args)

    # ── 透明代理 LSP 操作 ─────────────────────────────────────

    async def go_to_definition(self, file_path: str, line: int, column: int) -> Optional[list]:
        client = self.get_client_for_file(file_path)
        if not client or not await self._ensure_started(client):
            return None
        await client.did_open(file_path)
        await asyncio.sleep(0.3)
        try:
            return await client.go_to_definition(file_path, line, column)
        finally:
            await client.did_close(file_path)

    async def find_references(self, file_path: str, line: int, column: int) -> Optional[list]:
        client = self.get_client_for_file(file_path)
        if not client or not await self._ensure_started(client):
            return None
        await client.did_open(file_path)
        await asyncio.sleep(0.3)
        try:
            return await client.find_references(file_path, line, column)
        finally:
            await client.did_close(file_path)

    async def hover(self, file_path: str, line: int, column: int) -> Optional[str]:
        client = self.get_client_for_file(file_path)
        if not client or not await self._ensure_started(client):
            return None
        await client.did_open(file_path)
        await asyncio.sleep(0.3)
        try:
            return await client.hover(file_path, line, column)
        finally:
            await client.did_close(file_path)

    async def document_symbols(self, file_path: str) -> Optional[list]:
        client = self.get_client_for_file(file_path)
        if not client or not await self._ensure_started(client):
            return None
        await client.did_open(file_path)
        await asyncio.sleep(0.3)
        try:
            return await client.document_symbols(file_path)
        finally:
            await client.did_close(file_path)
