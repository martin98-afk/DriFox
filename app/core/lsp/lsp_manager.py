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
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .lsp_client import LspClient
from .lsp_config import LspServerConfig, load_lsp_configs


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
        # 幂等：仅当已初始化且配置为空时跳过（热重载通过 _on_refresh 传配置触发）
        if self._initialized and self._clients and not lsp_configs:
            logger.debug("[LspManager] 已初始化，无新配置，跳过")
            return
        self._workspace_root = workspace_root
        # 停止旧客户端再清空（热重载场景下防止孤儿进程）
        if self._clients:
            self._ensure_loop()
            if self._loop:
                asyncio.run_coroutine_threadsafe(self.stop_all(), self._loop)
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

    def sync_get_diagnostics(self, file_path: str, timeout: float = 15.0) -> Optional[str]:
        return self._submit(self.get_diagnostics_for_file(file_path), timeout)

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

    async def _quick_diagnostics(self, file_path: str) -> Optional[str]:
        """快速诊断：跳过 LSP 协议，直接 CLI + 紧凑格式化"""
        client = self.get_client_for_file(file_path)
        if not client:
            return None
        return await self._run_cli_compact(file_path, client.config.command)

    @staticmethod
    async def _run_cli_compact(file_path: str, command: str) -> Optional[str]:
        """CLI 调用 + 紧凑输出：只报 Error/Warning，15 条上限，每条截断 120 字符，总长 2000"""
        import asyncio.subprocess

        cli_cmd = "pyright" if "pyright" in command.lower() else command
        if sys.platform == "win32":
            args = ["cmd", "/c", cli_cmd, file_path, "--outputjson"]
        else:
            args = [cli_cmd, file_path, "--outputjson"]
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
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=5.0
            )
            if not stdout:
                return None

            import orjson as json
            data = json.loads(stdout)
            diags = data.get("generalDiagnostics", [])
            if not diags:
                return None

            # 只保留 Error + Warning，按行号排序
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
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    async def get_diagnostics_for_file(self, file_path: str) -> Optional[str]:
        """AI 主动获取某文件的诊断（LSP push 不可靠，用 CLI 回退）"""
        client = self.get_client_for_file(file_path)
        if not client or not await self._ensure_started(client):
            return None
        # 尝试 LSP push 诊断
        await client.did_open(file_path)
        try:
            diags = await client.wait_diagnostics(timeout=5.0)
            if diags:
                return self._format_diagnostics(file_path, diags)
        except Exception:
            pass
        finally:
            await client.did_close(file_path)  # 回收 LSP 服务器打开文件集合
        # 回退：使用 CLI 获取诊断
        return await self._cli_diagnostics(file_path, client.config.command)

    @staticmethod
    async def _cli_diagnostics(file_path: str, command: str) -> Optional[str]:
        """CLI 回退：直接调用 LSP 命令行工具获取诊断"""
        import asyncio.subprocess
        # pyright-langserver → pyright (CLI 工具)
        cli_cmd = "pyright" if "pyright" in command.lower() else command
        # Windows: .cmd 需要 cmd /c 前缀
        if sys.platform == "win32":
            args = ["cmd", "/c", cli_cmd, file_path, "--outputjson"]
        else:
            args = [cli_cmd, file_path, "--outputjson"]
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
            if stdout:
                import orjson as json
                data = json.loads(stdout)
                diags = data.get("generalDiagnostics", [])
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
            if stderr:
                return f"(LSP CLI 错误: {stderr.decode()[:500]})"
            return None
        except asyncio.TimeoutError:
            return "(LSP CLI 超时)"
        except Exception as e:
            return f"(LSP CLI 失败: {e})"

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
