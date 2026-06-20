# -*- coding: utf-8 -*-
"""
LSP 客户端 — 基于 pygls 2.x JsonRPCClient 的单语言服务器连接

职责：
  1. 管理 LSP 服务器进程生命周期（启动/停止/重启）
  2. 提供核心 LSP 操作（goToDefinition/hover/findReferences/documentSymbol/diagnostics）
  3. 处理 didOpen/didChange 文件同步通知
  4. 自动接收 publishDiagnostics 推送

注意：
  - pygls 2.x send_request_async 返回原生 dict，非 lsprotocol 类型，需手动访问嵌套
  - pygls 2.x notify 不返回 Future，不能 await
  - Windows: asyncio 子进程需要完整路径或通过 cmd
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

try:
    from pygls.client import JsonRPCClient
    from lsprotocol import types as lsp
except ImportError as e:
    JsonRPCClient = None  # type: ignore[assignment]
    lsp = None  # type: ignore[assignment]


# ─── Helper: 安全访问 dict/object 嵌套属性 ─────────────────────────
def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    elif hasattr(obj, key):
        return getattr(obj, key)
    return default


class LspClient:
    """单个 LSP 服务器客户端"""

    def __init__(self, config, workspace_root: str):
        """
        Args:
            config: LspServerConfig
            workspace_root: 项目根目录
        """
        from .lsp_config import LspServerConfig as _Config

        self.config: _Config = config
        self.workspace_root = workspace_root
        self._client: Optional[Any] = None  # JsonRPCClient
        self._started = False
        self._diag_callback: Optional[Callable[[str, list], None]] = None
        self._diag_future: Optional[asyncio.Future] = None
        self._diagnostics: List[Dict[str, Any]] = []
        self._restart_count = 0

    # ── 生命周期 ──────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._started and self._client is not None

    async def start(self) -> bool:
        """启动 LSP 服务器进程并完成 initialize 握手"""
        if self.is_running:
            return True

        if JsonRPCClient is None:
            logger.error(f"[LspClient:{self.config.name}] pygls 未安装")
            return False

        # 查找可执行文件
        exe = self._resolve_command()
        if not exe:
            logger.warning(f"[LspClient:{self.config.name}] 未找到可执行文件: {self.config.command}")
            return False

        try:
            self._client = JsonRPCClient()

            # 注册诊断回调
            if lsp:
                @self._client.feature(lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
                def _on_diag(client_ls, params):
                    diags = []
                    items = _get(params, "diagnostics", [])
                    file_uri = _get(params, "uri", "")
                    for item in items:
                        rng = _get(item, "range", {})
                        st = _get(rng, "start", {})
                        diags.append({
                            "line": _get(st, "line", 0) + 1,
                            "column": _get(st, "character", 0) + 1,
                            "severity": str(_get(item, "severity", "?")).split(".")[-1],
                            "message": _get(item, "message", ""),
                            "code": _get(item, "code", ""),
                            "uri": file_uri,
                        })
                    self._diagnostics = diags
                    if self._diag_future and not self._diag_future.done():
                        self._diag_future.set_result(diags)
                    logger.debug(
                        f"[LspClient:{self.config.name}] publishDiagnostics: "
                        f"{os.path.basename(file_uri) if file_uri else '?'} "
                        f"→ {len(diags)} issues"
                    )
                    if self._diag_callback:
                        self._diag_callback(file_uri, diags)

                @self._client.feature("window/logMessage")
                def _on_log(client_ls, params):
                    msg = _get(params, "message", str(params))
                    logger.debug(f"[LspClient:{self.config.name}] pyright: {msg[:200]}")

            # 启动子进程
            args = [exe, *self.config.args]
            logger.debug(f"[LspClient:{self.config.name}] 启动: {' '.join(args)}")
            await self._client.start_io(*args)

            # initialize 握手
            root = Path(self.workspace_root)
            result = await self._client.protocol.send_request_async(
                lsp.INITIALIZE,
                lsp.InitializeParams(
                    capabilities=lsp.ClientCapabilities(),
                    root_uri=root.as_uri(),
                    root_path=str(root),
                    workspace_folders=None,
                ),
            )
            caps = _get(result, "capabilities", {})
            si = _get(caps, "server_info", None)
            server_name = _get(si, "name", self.config.name) if si else self.config.name
            server_version = _get(si, "version", "?") if si else "?"

            # 发送 initialized
            self._client.protocol.notify(lsp.INITIALIZED, lsp.InitializedParams())

            self._started = True
            self._restart_count = 0
            logger.info(f"[LspClient:{self.config.name}] ✅ 已连接 {server_name} v{server_version}")
            return True

        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] 启动失败: {e}")
            await self.stop()
            return False

    async def stop(self) -> None:
        """停止 LSP 服务器进程"""
        if not self._client or not self._started:
            return

        try:
            if lsp:
                await self._client.protocol.send_request_async(lsp.SHUTDOWN, None)
                self._client.protocol.notify(lsp.EXIT, None)
        except Exception:
            pass
        finally:
            self._client._stop_event.set()
            self._started = False
            self._client = None
            await asyncio.sleep(0.2)
            logger.debug(f"[LspClient:{self.config.name}] 已停止")

    async def restart(self) -> bool:
        """重启 LSP 服务器（用于崩溃恢复或热加载）"""
        if self._restart_count >= self.config.max_restarts:
            logger.warning(f"[LspClient:{self.config.name}] 达到最大重启次数 {self.config.max_restarts}，不再重启")
            return False

        self._restart_count += 1
        logger.info(f"[LspClient:{self.config.name}] 第 {self._restart_count} 次重启...")
        await self.stop()
        return await self.start()

    # ── 核心 LSP 操作 ──────────────────────────────────────────────

    async def go_to_definition(self, file_path: str, line: int, column: int) -> Optional[list]:
        """跳转到定义位置"""
        if not self.is_running or not lsp:
            return None
        try:
            uri = Path(file_path).resolve().as_uri()
            result = await self._client.protocol.send_request_async(
                lsp.TEXT_DOCUMENT_DEFINITION,
                lsp.DefinitionParams(
                    text_document=lsp.TextDocumentIdentifier(uri=uri),
                    position=lsp.Position(line=line - 1, character=column - 1),
                ),
            )
            return self._normalize_locations(result)
        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] goToDefinition 失败: {e}")
            return None

    async def find_references(self, file_path: str, line: int, column: int) -> Optional[list]:
        """查找所有引用"""
        if not self.is_running or not lsp:
            return None
        try:
            uri = Path(file_path).resolve().as_uri()
            result = await self._client.protocol.send_request_async(
                lsp.TEXT_DOCUMENT_REFERENCES,
                lsp.ReferenceParams(
                    text_document=lsp.TextDocumentIdentifier(uri=uri),
                    position=lsp.Position(line=line - 1, character=column - 1),
                    context=lsp.ReferenceContext(include_declaration=True),
                ),
            )
            return self._normalize_locations(result)
        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] findReferences 失败: {e}")
            return None

    async def hover(self, file_path: str, line: int, column: int) -> Optional[str]:
        """获取悬停文档信息"""
        if not self.is_running or not lsp:
            return None
        try:
            uri = Path(file_path).resolve().as_uri()
            result = await self._client.protocol.send_request_async(
                lsp.TEXT_DOCUMENT_HOVER,
                lsp.HoverParams(
                    text_document=lsp.TextDocumentIdentifier(uri=uri),
                    position=lsp.Position(line=line - 1, character=column - 1),
                ),
            )
            contents = _get(result, "contents")
            if contents:
                if isinstance(contents, dict):
                    return _get(contents, "value", str(contents))
                elif isinstance(contents, list):
                    parts = []
                    for c in contents:
                        parts.append(_get(c, "value", str(c)))
                    return " ".join(parts)
                else:
                    return str(contents)
            return None
        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] hover 失败: {e}")
            return None

    async def document_symbols(self, file_path: str) -> Optional[list]:
        """获取文件符号列表"""
        if not self.is_running or not lsp:
            return None
        try:
            uri = Path(file_path).resolve().as_uri()
            result = await self._client.protocol.send_request_async(
                lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL,
                lsp.DocumentSymbolParams(text_document=lsp.TextDocumentIdentifier(uri=uri)),
            )
            if result and isinstance(result, list):
                symbols = []
                for s in result:
                    # LSP 服务端可能返回 DocumentSymbol（有 range）或 SymbolInformation（有 location），
                    # 这里兼容两种形态（pyright 默认返回 SymbolInformation）
                    rng = _get(s, "range") or _get(_get(s, "location"), "range", {})
                    st = _get(rng, "start", {})
                    kind_num = _get(s, "kind", 0)
                    # 符号类型映射
                    kind_names = {
                        1: "File", 2: "Module", 3: "Namespace", 4: "Package",
                        5: "Class", 6: "Method", 7: "Property", 8: "Field",
                        9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
                        13: "Variable", 14: "Constant", 15: "String", 16: "Number",
                        17: "Boolean", 18: "Array",
                    }
                    symbols.append({
                        "name": _get(s, "name", "?"),
                        "kind": kind_names.get(kind_num, f"Kind{kind_num}"),
                        "line": _get(st, "line", 0) + 1,
                        "column": _get(st, "character", 0) + 1,
                    })
                return symbols
            return None
        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] documentSymbol 失败: {e}")
            return None

    async def get_diagnostics(self, file_path: str) -> list:
        """获取已缓存的诊断（不发起新请求）"""
        return [d for d in self._diagnostics if file_path in _get(d, "uri", "")]
        # 如果没有匹配，可能是 uri 格式不同，返回全部
        # return list(self._diagnostics)

    # ── 文件同步 ─────────────────────────────────────────────────

    async def did_open(self, file_path: str, language: Optional[str] = None) -> None:
        """通知 LSP 服务器文件已打开"""
        if not self.is_running or not lsp:
            return

        try:
            uri = Path(file_path).resolve().as_uri()
            content = Path(file_path).read_text(encoding="utf-8")
            lang = language or self._guess_lang(file_path)

            self._diagnostics.clear()
            self._diag_future = None

            self._client.protocol.notify(
                lsp.TEXT_DOCUMENT_DID_OPEN,
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": lang,
                        "version": 1,
                        "text": content,
                    }
                },
            )
            logger.debug(f"[LspClient:{self.config.name}] didOpen: {file_path} ({lang})")
        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] didOpen 失败: {e}")

    async def did_change(self, file_path: str) -> None:
        """通知 LSP 服务器文件已变更"""
        if not self.is_running or not lsp:
            return

        try:
            uri = Path(file_path).resolve().as_uri()
            content = Path(file_path).read_text(encoding="utf-8")
            self._diagnostics.clear()
            self._diag_future = None

            self._client.protocol.notify(
                lsp.TEXT_DOCUMENT_DID_CHANGE,
                {
                    "textDocument": {"uri": uri, "version": 1},
                    "contentChanges": [{"text": content}],
                },
            )
            logger.debug(
                f"[LspClient:{self.config.name}] didChange: {os.path.basename(file_path)}"
            )
        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] didChange 失败: {e}")

    async def did_close(self, file_path: str) -> None:
        """通知 LSP 服务器文件已关闭"""
        if not self.is_running or not lsp:
            return
        try:
            uri = Path(file_path).resolve().as_uri()
            self._client.protocol.notify(
                lsp.TEXT_DOCUMENT_DID_CLOSE,
                {"textDocument": {"uri": uri}},
            )
            logger.debug(
                f"[LspClient:{self.config.name}] didClose: {os.path.basename(file_path)}"
            )
        except Exception as e:
            logger.error(f"[LspClient:{self.config.name}] didClose 失败: {e}")

    async def wait_diagnostics(self, timeout: float = 10.0) -> list:
        """等待诊断结果（使用 loop-bound Future 确保多线程安全）"""
        loop = asyncio.get_running_loop()
        self._diag_future = loop.create_future()
        try:
            return await asyncio.wait_for(self._diag_future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug(f"[LspClient:{self.config.name}] wait_diagnostics 超时 ({timeout}s)")
            return list(self._diagnostics)
        except Exception:
            return list(self._diagnostics)

    def set_diagnostics_callback(self, cb: Callable[[str, list], None]) -> None:
        """设置诊断推送回调"""
        self._diag_callback = cb

    # ── 内部 ─────────────────────────────────────────────────────

    def _resolve_command(self) -> Optional[str]:
        """解析 LSP 服务器可执行文件路径"""
        cmd = self.config.command
        # 如果是绝对路径，直接使用
        if Path(cmd).is_absolute():
            return cmd
        # 在 PATH 中查找
        found = shutil.which(cmd)
        if found:
            return found
        return None

    def _guess_lang(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        for ext_key, lang in self.config.extension_to_language.items():
            if ext == ext_key.lower():
                return lang
        return "plaintext"

    @staticmethod
    def _normalize_locations(result: Any) -> Optional[list]:
        """统一位置格式"""
        if not result:
            return None
        items = result if isinstance(result, list) else [result]
        locs = []
        for item in items:
            rng = _get(item, "range", {})
            st = _get(rng, "start", {})
            locs.append({
                "uri": _get(item, "uri", ""),
                "line": _get(st, "line", 0) + 1,
                "column": _get(st, "character", 0) + 1,
            })
        return locs
