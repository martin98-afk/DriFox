# -*- coding: utf-8 -*-
"""
LSP 工具 — 统一入口，通过 operation 参数分派

延迟导入 ToolResult 以避免与 app/tools/__init__.py 的循环依赖。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _make_result(success: bool, content=None, error: Optional[str] = None):
    from app.tools.result import ToolResult
    return ToolResult(success, content=content, error=error)


class LspToolsIntegration:
    """LSP 工具集成 — 桥接 LspManager 与 BuiltinTools/ToolExecutor"""

    def __init__(self, lsp_manager=None, owner=None):
        from app.core.lsp.lsp_manager import LspManager
        self._manager = lsp_manager or LspManager.get_instance()
        self._owner = owner  # BuiltinTools 引用，用于动态读取 workdir

    @property
    def workdir(self):
        """动态从 owner 读取工作目录（多项目切换安全）"""
        if self._owner:
            return self._owner.workdir
        return None

    # ═══════════════════════════════════════════════════════════
    # 统一入口
    # ═══════════════════════════════════════════════════════════

    def lsp(self, path: str, operation: str,
            line: int = 0, column: int = 0, language: Optional[str] = None):
        file_path = self._resolve(path)

        if operation == "listServers":
            return self._do_list_servers()

        if not file_path:
            return _make_result(False, error=f"文件不存在: {path}")

        op = operation
        fp = str(file_path)

        # 检查是否有对应的 LSP 服务器
        client = self._manager.get_client_for_file(fp)
        if not client:
            ext = os.path.splitext(fp)[1].lower()
            return _make_result(False, error=(
                f"无 LSP 服务器支持 {ext} 文件。"
                f"已注册 {len(self._manager._clients)} 个服务器。"
            ))

        try:
            if op == "diagnostics":
                return self._do_diagnostics(fp)
            elif op == "documentSymbols":
                return self._do_symbols(fp)
            elif op == "goToDefinition":
                return self._do_definition(fp, line, column)
            elif op == "findReferences":
                return self._do_references(fp, line, column)
            elif op == "hover":
                return self._do_hover(fp, line, column)
            else:
                return _make_result(False, error=(
                    f"未知操作: {operation}。支持: diagnostics, documentSymbols, "
                    f"goToDefinition, findReferences, hover, listServers"
                ))
        except Exception as e:
            from loguru import logger
            logger.error(f"[LspTools] {operation} 失败: {e}")
            return _make_result(False, error=f"LSP {operation} 失败: {e}")

    # ═══════════════════════════════════════════════════════════
    # 各操作实现（全部通过 sync_* 提交到持久事件循环）
    # ═══════════════════════════════════════════════════════════

    def _do_diagnostics(self, fp: str):
        status, payload = self._manager.sync_get_diagnostics(fp, timeout=40.0)
        if status == "ok":
            if payload:
                return _make_result(True, content=payload)
            return _make_result(True, content=f"(无诊断问题: {os.path.basename(fp)})")
        # 状态异常：把"为什么失败"显式告诉 LLM，避免静默误判为"无问题"
        from loguru import logger
        logger.warning(f"[LspTools] diagnostics 状态={status}: {payload}")
        return _make_result(False, error=f"[LSP] 诊断失败 ({status}): {payload}")

    def _do_symbols(self, fp: str):
        symbols = self._manager.sync_document_symbols(fp, timeout=10.0)
        if symbols:
            lines = [f"符号列表 ({len(symbols)} 个):"]
            for s in symbols:
                lines.append(
                    f"  [{s['kind']}] {s['name']} @ L{s['line']}:C{s['column']}"
                )
            return _make_result(True, content="\n".join(lines))
        return _make_result(True, content=f"(无符号: {os.path.basename(fp)})")

    def _do_definition(self, fp: str, line: int, column: int):
        locs = self._manager.sync_go_to_definition(fp, line, column, timeout=10.0)
        if locs:
            lines = [f"定义位置 ({len(locs)} 处):"]
            for loc in locs:
                uri = loc.get("uri", "")
                fname = uri.split("/")[-1] if "/" in uri else uri
                lines.append(f"  → {fname} L{loc['line']}:C{loc['column']}")
            return _make_result(True, content="\n".join(lines))
        return _make_result(True, content="(未找到定义)")

    def _do_references(self, fp: str, line: int, column: int):
        refs = self._manager.sync_find_references(fp, line, column, timeout=10.0)
        if refs:
            lines = [f"引用列表 ({len(refs)} 处):"]
            for ref in refs:
                uri = ref.get("uri", "")
                fname = uri.split("/")[-1] if "/" in uri else uri
                lines.append(f"  → {fname} L{ref['line']}:C{ref['column']}")
            return _make_result(True, content="\n".join(lines))
        return _make_result(True, content="(未找到引用)")

    def _do_hover(self, fp: str, line: int, column: int):
        info = self._manager.sync_hover(fp, line, column, timeout=10.0)
        if info:
            return _make_result(True, content=info)
        return _make_result(True, content="(无文档信息)")

    def _do_list_servers(self):
        clients = self._manager._clients
        if not clients:
            return _make_result(True, content="(无 LSP 服务器注册)")
        lines = [f"已注册 LSP 服务器 ({len(clients)} 个):"]
        for name, client in clients.items():
            status = "RUNNING" if client.is_running else "STOPPED"
            exts = list(client.config.extension_to_language.keys())
            lines.append(f"  [{status}] {name}: {', '.join(exts)}")
        return _make_result(True, content="\n".join(lines))

    # ── 内部 ─────────────────────────────────────────────────────

    def _resolve(self, path: str) -> Optional[Path]:
        p = Path(path)
        if not p.is_absolute():
            p = (self.workdir or Path.cwd()) / p
        return p if p.exists() else None
