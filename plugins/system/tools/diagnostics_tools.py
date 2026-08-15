# -*- coding: utf-8 -*-
"""
系统工具插件 — 诊断与代码智能（平台服务）

get_diagnostics / lsp / codegraph_explore：LSP 与 CodeGraph 引擎是平台能力，
impl 通过 tool_ctx["services"] 调用，工具层逻辑（参数/结果）在插件内。
"""
from app.tools.result import ToolResult

GROUP_DIAG = "诊断与代码智能"

_GET_DIAGNOSTICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_diagnostics",
        "description": "获取文件语法检查结果(错误/警告/提示)。支持 Python(pyright/mypy/flake8)、JS/TS(tsc/eslint)、Shell(shellcheck)。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "language": {"type": "string", "description": "语言: python/javascript/typescript/shellscript"},
            },
            "required": ["path"],
        },
    },
}


def _get_diagnostics_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("diagnostics")
    if service is None:
        return ToolResult(False, error="诊断服务不可用")
    return service.get_diagnostics(kwargs.get("path", ""), kwargs.get("language"))


_LSP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lsp",
        "description": (
            "通过LSP执行代码智能操作。\n"
            "\n"
            "操作类型：\n"
            "- diagnostics: 文件诊断(错误/警告/提示)\n"
            "- documentSymbols: 符号列表(类/函数/变量)\n"
            "- goToDefinition: 跳定义\n"
            "- findReferences: 找引用\n"
            "- hover: 光标位置文档/类型\n"
            "- listServers: 列出LSP服务器状态\n"
            "\n"
            "line/column从1开始。diagnostics/listServers无需line/column。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（listServers 操作不需要但可忽略）"},
                "operation": {
                    "type": "string",
                    "enum": ["diagnostics", "documentSymbols", "goToDefinition", "findReferences", "hover", "listServers"],
                    "description": "操作类型",
                },
                "line": {"type": "integer", "description": "行号（diagnostics/listServers 不需要）"},
                "column": {"type": "integer", "description": "列号（diagnostics/listServers/documentSymbols 不需要）"},
            },
            "required": ["path", "operation"],
        },
    },
}


def _lsp_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("lsp")
    if service is None:
        return ToolResult(False, error="LSP 服务不可用")
    return service.lsp(
        path=kwargs.get("path", ""),
        operation=kwargs.get("operation", "diagnostics"),
        line=int(kwargs.get("line") or 0),
        column=int(kwargs.get("column") or 0),
        language=kwargs.get("language"),
    )


_CODEGRAPH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "codegraph_explore",
        "description": (
            "统一代码探索工具。通过 mode 切换不同能力：\n"
            "  - status: 查看索引状态（文件/符号/边/待同步变更）\n"
            "  - search: 搜索符号（函数/类/方法/变量），支持按 kind 过滤\n"
            "  - callers: 查找谁调用了指定符号\n"
            "  - callees: 查找指定符号调用了谁\n"
            "  - explore: （默认）综合搜索+调用上下文，一次输出\n"
            "  - impact: 变更影响分析，评估改动波及范围\n"
            "  - sync: 同步索引与文件系统变更\n"
            "  - files: 列出已索引文件\n"
            "\n"
            "新参数（v1.4.0）:\n"
            "  substring=true  — 子串匹配，搜 Manager 也能找到 SessionManager\n"
            "  visibility=private — 只搜 _ 开头的私有符号\n"
            "  case_sensitive=true — 大小写敏感\n"
            "\n"
            "使用示例：\n"
            "  codegraph_explore(mode='status') — 看索引状态\n"
            "  codegraph_explore('ChatBackend') — 探索 ChatBackend\n"
            "  codegraph_explore('Manager', mode='search', substring=true, kind='class') — 搜所有 Manager 类\n"
            "  codegraph_explore('send_message', mode='callers') — 找调用者\n"
            "  codegraph_explore('on_click', mode='impact') — 影响分析"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索的符号名或关键词（status/sync/files 模式不需要）", "default": ""},
                "mode": {
                    "type": "string",
                    "enum": ["status", "search", "callers", "callees", "explore", "impact", "sync", "files"],
                    "description": "操作模式（默认 explore）",
                    "default": "explore",
                },
                "depth": {"type": "integer", "description": "callers/callees/impact 的遍历深度（默认 2）", "default": 2},
                "kind": {"type": "string", "description": "search 模式按类型过滤：function/class/method/variable/field/enum 等"},
                "max_files": {"type": "integer", "description": "explore 模式最大文件数（默认 50）", "default": 50},
                "directory": {"type": "string", "description": "files 模式按目录筛选（如 app/tools）"},
                "limit": {"type": "integer", "description": "search 模式最大返回数（默认 50）", "default": 50},
                "exact": {"type": "boolean", "description": "search 模式是否精确匹配（默认模糊）", "default": False},
                "substring": {"type": "boolean", "description": "search 模式使用子串匹配（搜 Manager 也可命中 SessionManager）", "default": False},
                "visibility": {"type": "string", "enum": ["public", "private"], "description": "search 模式按可见性过滤：public（无 _ 前缀）/private（有 _ 前缀）"},
                "case_sensitive": {"type": "boolean", "description": "search 模式是否大小写敏感（默认不敏感）", "default": False},
            },
            "required": [],
        },
    },
}


def _codegraph_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("codegraph")
    if service is None:
        return ToolResult(False, error="代码图服务不可用")
    return service.codegraph_explore(
        query=kwargs.get("query", ""),
        mode=kwargs.get("mode", "explore"),
        depth=int(kwargs.get("depth") or 2),
        max_files=int(kwargs.get("max_files") or 50),
        kind=kwargs.get("kind"),
        directory=kwargs.get("directory"),
        limit=int(kwargs.get("limit") or 50),
        exact=kwargs.get("exact", False),
    )


def register(registry):
    registry.register(
        "get_diagnostics", _GET_DIAGNOSTICS_SCHEMA, impl=_get_diagnostics_impl,
        danger="safe", icon="工具", cn_name="诊断",
        group=GROUP_DIAG, description="获取代码诊断信息",
    )
    registry.register(
        "lsp", _LSP_SCHEMA, impl=_lsp_impl,
        danger="safe", icon="工具", cn_name="LSP",
        group=GROUP_DIAG, description="LSP代码智能操作",
    )
    registry.register(
        "codegraph_explore", _CODEGRAPH_SCHEMA, impl=_codegraph_impl,
        danger="safe", icon="Search", cn_name="代码探索",
        group=GROUP_DIAG, description="语义级代码探索",
        aliases=["CodeGraphExplore", "cg_explore", "codegraph"],
    )
