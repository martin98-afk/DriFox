# -*- coding: utf-8 -*-
"""A4：插件危险 import 审计面（T8.1 基准表 行 4）。

验证加载路径的 AST 审计为「仅日志不改行为」：
- tools / runtime 组件模块级 socket/subprocess/requests/urllib/ctypes
  → 载入成功（工具照常注册）+ 结构化 warning（插件名+行号+符号名）
- 函数内延迟 import 不告警；干净插件无告警
网络与写盘零触达：仅静态 AST 审计，恶意 fixture 全部 tmp_path 语义。
"""
import pytest
from loguru import logger

from app.plugins.loaders._ast_guard import audit_dangerous_imports
from app.plugins.loaders.plugin_tool_loader import (
    load_plugin_tools,
    unload_plugin_tools,
)
from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
from app.tools.registry import ToolRegistry


@pytest.fixture()
def log_capture():
    """loguru WARNING+ 捕获为文本列表。"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def _make_tool_plugin(root, plugin_name: str, body: str, tool_name: str):
    tools_dir = root / plugin_name / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / f"{tool_name}.py").write_text(body, encoding="utf-8")
    return root / plugin_name


def _load_with_settings_enabled(plugin_name: str, plugin_root, log_capture):
    """按 P0-1 口径把测试插件临时加入 enabled_plugins（结束还原）。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = list(cfg.enabled_plugins.value or [])
    cfg.enabled_plugins.value = saved + [plugin_name]
    try:
        reg = ToolRegistry.get_instance()
        loaded = load_plugin_tools(registry=reg, plugin_roots=[plugin_root.parent], root_tracker={})
        return reg, loaded
    finally:
        cfg.enabled_plugins.value = saved


def _assert_no_audit_noise(log_capture):
    assert not any("[AST审计]" in r for r in log_capture)


def test_module_level_dangerous_import_logged_and_still_loaded(tmp_path, log_capture):
    """模块级 import socket：工具照常注册（行为不变），审计清单可见。"""
    body = (
        "import socket\n"
        "def register(registry):\n"
        "    registry.register('a4_socket_tool',\n"
        "        {'description': 't', 'parameters': {'type': 'object', 'properties': {}}},\n"
        "        impl=lambda: 'ok', danger='safe')\n"
    )
    _make_tool_plugin(tmp_path, "a4-socket-plug", body, "socket_tool")
    reg, loaded = _load_with_settings_enabled("a4-socket-plug", tmp_path / "a4-socket-plug", log_capture)
    try:
        assert loaded.get("a4-socket-plug") == {"a4_socket_tool"}
        hits = [r for r in log_capture if "[AST审计]" in r]
        assert len(hits) == 1
        assert "a4-socket-plug" in hits[0] and "socket" in hits[0]
    finally:
        unload_plugin_tools("a4-socket-plug", loaded.get("a4-socket-plug", set()), reg)


def test_function_level_import_not_audited(tmp_path, log_capture):
    """函数内延迟 import 不属于模块级副作用，不告警。"""
    body = (
        "def register(registry):\n"
        "    import subprocess\n"
        "    registry.register('a4_fn_tool',\n"
        "        {'description': 't', 'parameters': {'type': 'object', 'properties': {}}},\n"
        "        impl=lambda: 'ok', danger='safe')\n"
    )
    _make_tool_plugin(tmp_path, "a4-fn-plug", body, "fn_tool")
    reg, loaded = _load_with_settings_enabled("a4-fn-plug", tmp_path / "a4-fn-plug", log_capture)
    try:
        assert loaded.get("a4-fn-plug") == {"a4_fn_tool"}
        _assert_no_audit_noise(log_capture)
    finally:
        unload_plugin_tools("a4-fn-plug", loaded.get("a4-fn-plug", set()), reg)


def test_audit_line_and_symbol_accurate(tmp_path, log_capture):
    """结构化清单含精确行号与符号名（含 from-import 的模块名）。"""
    body = (
        "import requests\n"
        "from urllib.request import urlopen\n"
        "def register(registry):\n"
        "    registry.register('a4_line_tool',\n"
        "        {'description': 't', 'parameters': {'type': 'object', 'properties': {}}},\n"
        "        impl=lambda: 'ok', danger='safe')\n"
    )
    _make_tool_plugin(tmp_path, "a4-line-plug", body, "line_tool")
    reg, loaded = _load_with_settings_enabled("a4-line-plug", tmp_path / "a4-line-plug", log_capture)
    try:
        assert loaded.get("a4-line-plug") == {"a4_line_tool"}
        hits = [r for r in log_capture if "[AST审计]" in r]
        assert len(hits) == 1
        assert "line 1: requests" in hits[0]
        assert "line 2: urllib.request" in hits[0]
    finally:
        unload_plugin_tools("a4-line-plug", loaded.get("a4-line-plug", set()), reg)


def test_clean_tool_no_audit_warning(tmp_path, log_capture):
    """干净工具零告警，注册行为不受审计影响。"""
    body = (
        "def register(registry):\n"
        "    registry.register('a4_clean_tool',\n"
        "        {'description': 't', 'parameters': {'type': 'object', 'properties': {}}},\n"
        "        impl=lambda: 'ok', danger='safe')\n"
    )
    _make_tool_plugin(tmp_path, "a4-clean-plug", body, "clean_tool")
    reg, loaded = _load_with_settings_enabled("a4-clean-plug", tmp_path / "a4-clean-plug", log_capture)
    try:
        assert loaded.get("a4-clean-plug") == {"a4_clean_tool"}
        _assert_no_audit_noise(log_capture)
    finally:
        unload_plugin_tools("a4-clean-plug", loaded.get("a4-clean-plug", set()), reg)


def test_audit_covers_all_five_dangerous_roots():
    """五类高危根全覆盖：import 与 from-import 都命中；函数内不命中；语法错误静默。"""
    src = (
        "import socket\n"
        "import subprocess\n"
        "import requests\n"
        "import urllib.parse\n"
        "import ctypes\n"
        "from subprocess import run\n"
        "import json\n"
        "def helper():\n"
        "    import socket\n"
    )
    hits = audit_dangerous_imports(src)
    assert ("line 1", "socket") in [(f"line {ln}", s) for ln, s in hits]
    symbols = [s for _, s in hits]
    assert symbols.count("socket") == 1  # 函数内那次不命中
    assert "subprocess" in symbols
    assert "requests" in symbols
    assert "urllib.parse" in symbols
    assert "ctypes" in symbols
    assert "json" not in symbols
    assert audit_dangerous_imports("def broken(:\n") == []


class _FakeRuntimeRegistry:
    """仅满足 _RegistryProxy.register(item, source=...) 的最小桩。"""

    def register(self, item, source=None, **kwargs):
        pass

    def unregister_source(self, *args, **kwargs):
        pass


def test_runtime_component_loader_audits_dangerous_import(tmp_path, log_capture):
    """runtime 组件加载路径同样审计：载入成功 + 告警含组件与符号。"""
    body = (
        "import ctypes\n"
        "def register(registry):\n"
        "    pass\n"
    )
    comp_dir = tmp_path / "rt-plug" / "model_adapters"
    comp_dir.mkdir(parents=True)
    (comp_dir / "risky.py").write_text(body, encoding="utf-8")
    loader = RuntimeComponentLoader("model_adapters", _FakeRuntimeRegistry())
    ok = loader._load_module(comp_dir / "risky.py", "rt-plug", "user")
    assert ok is True  # 仅告警不拒载
    hits = [r for r in log_capture if "[AST审计]" in r]
    assert len(hits) == 1
    assert "rt-plug" in hits[0] and "ctypes" in hits[0]
