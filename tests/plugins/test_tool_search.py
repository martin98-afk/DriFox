# -*- coding: utf-8 -*-
"""test_tool_search.py — tool_search 工具（schema 懒加载）单测。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "plugins" / "system" / "tools" / "tool_search_tools.py"

spec = importlib.util.spec_from_file_location("test_tool_search_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_tool_search_mod", m)
spec.loader.exec_module(m)


class _FakeMcp:
    def __init__(self, schemas):
        self._schemas = schemas

    def get_tool_schemas(self):
        return self._schemas


def _schema(name: str, desc: str = "", params: dict | None = None) -> dict:
    fn = {"name": name, "description": desc}
    if params is not None:
        fn["parameters"] = params
    return {"type": "function", "function": fn}


def _setup_registry(monkeypatch, tools):
    """tools: [(name, cn, group, desc, aliases)] → 假 registry。"""

    class _R:
        def __init__(self, row):
            self.name, self.cn_name, self.group, self.description, self.aliases = row
            self.schema = _schema(self.name, self.description, {"type": "object", "properties": {}})

    class _Reg:
        def list(self):
            return [_R(t) for t in tools]

        def has(self, name):
            return any(t[0] == name for t in tools)

        def get(self, name):
            return next(_R(t) for t in tools if t[0] == name)

    monkeypatch.setattr(m, "ToolRegistry", type("TR", (), {"get_instance": staticmethod(lambda: _Reg())}))


_TOOLS = [
    ("write", "写入文件", "文件写入", "写文件或覆盖内容", ["写文件"]),
    ("websearch", "网络搜索", "网络", "联网搜索资料", []),
    ("bash", "终端命令", "终端与后台", "执行 shell 命令", []),
    ("read", "读文件", "文件读取", "读取文件内容", []),
]


def test_lookup_by_exact_name(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, tool_names=["write"])
    assert out.success and "## write" in out.content and "Parameters:" in out.content


def test_lookup_by_alias(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, tool_names=["写文件"])
    assert "## write" in out.content


def test_lookup_not_found(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, tool_names=["nonexistent_tool"])
    assert "没有找到" in out.content


def test_query_search_ranking(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, queries=["文件"], top_k=2)
    assert "## write" in out.content and "## read" in out.content
    assert "## websearch" not in out.content  # top_k=2 截断


def test_query_bilingual(monkeypatch):
    """双语两组词：任一命中即返回（CodeBuddy 同款建议用法）。"""
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, queries=["文件", "file"])
    assert "## write" in out.content or "## read" in out.content


def test_query_multiword_and(monkeypatch):
    """单 query 内空格分词 AND："web search" 同时命中 name 才计分。"""
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, queries=["web search"])
    assert "## websearch" in out.content


def test_mcp_tools_searchable(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS)
    mcp = _FakeMcp([_schema("mcp__time__current_time", "获取当前时间", {"type": "object"})])
    out = m._search_impl(tool_ctx={"services": {"mcp": mcp}}, queries=["时间", "time"])
    assert "## mcp__time__current_time" in out.content


def test_self_excluded(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS + [("tool_search", "搜索工具", "工具搜索", "搜索工具", [])])
    out = m._search_impl(tool_ctx={"services": {}}, queries=["搜索"])
    assert "## tool_search" not in out.content


def test_no_args_prompts_usage(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}})
    assert "queries" in out.content


def test_string_args_coerced(monkeypatch):
    """LLM 传单字符串也能用。"""
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, queries="bash", tool_names="", top_k="3")
    assert "## bash" in out.content


def test_top_k_clamped(monkeypatch):
    _setup_registry(monkeypatch, _TOOLS)
    out = m._search_impl(tool_ctx={"services": {}}, queries=["文件"], top_k=999)
    assert out.success  # 超限收敛到 20，不抛错


# ── tool_execute 中转执行 ───────────────────────────────


class _Entry:
    def __init__(self, impl):
        self.impl = impl


def _setup_registry_with_impl(monkeypatch, impls):
    """impls: {name: callable} → 假 registry（has/get/list 全支持）。"""

    class _R:
        def __init__(self, name):
            self.name = name
            self.impl = impls.get(name)
            self.cn_name = ""
            self.group = ""
            self.description = ""
            self.aliases = []
            self.schema = _schema(name)

    class _Reg:
        def list(self):
            return [_R(n) for n in impls]

        def has(self, name):
            return name in impls

        def get(self, name):
            return _R(name)

    monkeypatch.setattr(m, "ToolRegistry", type("TR", (), {"get_instance": staticmethod(lambda: _Reg())}))


def test_execute_registry_tool(monkeypatch):
    def _websearch(tool_ctx=None, **kw):
        return m.ToolResult(True, content="搜索结果：...")

    _setup_registry_with_impl(monkeypatch, {"websearch": _websearch, "tool_search": None, "tool_execute": None})
    out = m._execute_impl(tool_ctx={"services": {}}, tool_name="websearch", arguments={"query": "伊朗新闻"})
    assert out.success and "搜索结果" in out.content


def test_execute_without_ctx_param(monkeypatch):
    """impl 不接受 tool_ctx 也能调。"""

    def _plain(query):
        return m.ToolResult(True, content=f"plain {query}")

    _setup_registry_with_impl(monkeypatch, {"plain": _plain, "tool_search": None, "tool_execute": None})
    out = m._execute_impl(tool_ctx={}, tool_name="plain", arguments={"query": "x"})
    assert out.success and "plain x" in out.content


def test_execute_args_json_string(monkeypatch):
    """arguments 传 JSON 字符串容错。"""

    def _plain(query):
        return m.ToolResult(True, content=f"got {query}")

    _setup_registry_with_impl(monkeypatch, {"plain": _plain, "tool_search": None, "tool_execute": None})
    out = m._execute_impl(tool_ctx={}, tool_name="plain", arguments='{"query": "abc"}')
    assert out.success and "got abc" in out.content


def test_execute_missing_tool_hints_search(monkeypatch):
    _setup_registry_with_impl(monkeypatch, {"tool_search": None, "tool_execute": None})
    out = m._execute_impl(tool_ctx={"services": {}}, tool_name="websearch", arguments={})
    assert "tool_search" in out.content


def test_execute_self_denied(monkeypatch):
    _setup_registry_with_impl(monkeypatch, {"tool_search": None, "tool_execute": None})
    out = m._execute_impl(tool_ctx={"services": {}}, tool_name="tool_execute", arguments={})
    assert out.success and "不能中转" in out.content


def test_execute_type_error_reports_params(monkeypatch):
    def _strict(query):
        return m.ToolResult(True, content="ok")

    _setup_registry_with_impl(monkeypatch, {"strict": _strict, "tool_search": None, "tool_execute": None})
    out = m._execute_impl(tool_ctx={}, tool_name="strict", arguments={"wrong": 1})
    assert not out.success and "参数不匹配" in out.error


def test_execute_mcp_via_services(monkeypatch):
    class _Mcp:
        def call_tool_sync(self, name, args):
            return m.ToolResult(True, content=f"mcp done {name} {args}")

    _setup_registry_with_impl(monkeypatch, {"tool_search": None, "tool_execute": None})
    out = m._execute_impl(
        tool_ctx={"services": {"mcp": _Mcp()}},
        tool_name="mcp__time__now",
        arguments={"tz": "utc"},
    )
    assert out.success and "mcp done" in out.content


def test_execute_deny_policy_blocked(monkeypatch):
    """全局 deny 策略的工具中转也拒绝（安全网）。"""

    def _bash(tool_ctx=None, **kw):
        return m.ToolResult(True, content="should not run")

    _setup_registry_with_impl(monkeypatch, {"bash": _bash, "tool_search": None, "tool_execute": None})

    class _FakeSettings:
        tool_permission_policy = type("V", (), {"value": {"bash": "deny"}})()
        tool_off_behavior = type("V", (), {"value": "deny"})()

    class _FakeSettingsModule:
        @staticmethod
        def get_instance():
            return _FakeSettings()

    import sys
    import types

    fake = types.ModuleType("app.utils.config")
    fake.Settings = _FakeSettings
    monkeypatch.setitem(sys.modules, "app.utils.config", fake)
    out = m._execute_impl(tool_ctx={"services": {}}, tool_name="bash", arguments={"command": "dir"})
    assert out.success and "禁用" in out.content


def test_execute_no_name_prompts(monkeypatch):
    _setup_registry_with_impl(monkeypatch, {"tool_search": None, "tool_execute": None})
    out = m._execute_impl(tool_ctx={"services": {}}, tool_name="", arguments={})
    assert "tool_name" in out.content
