# -*- coding: utf-8 -*-
"""P1-5：打包版来源标注。

三用例：PluginInfo.is_system 正确 / A4 审计日志含 kind=system|user /
user 插件恶意样本同样被 A1 拒（校验对来源一视同仁）。
"""
import pytest
from loguru import logger
from pathlib import Path

from app.plugins.loaders.plugin_tool_loader import _root_kind, load_plugin_tools
from app.plugins.managers.plugin_manager import PluginInfo
from app.tools.registry import ToolRegistry


@pytest.fixture()
def log_capture():
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def _make_tool_plugin(root, name, body):
    tools = root / name / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(body, encoding="utf-8")
    return root / name


def test_plugin_info_is_system_label():
    """is_system 按 plugin_type 标注来源（system=项目 plugins/ 根扫描结果）。"""
    assert PluginInfo(name="a", manifest={}, path=Path("."), plugin_type="system").is_system is True
    assert PluginInfo(name="b", manifest={}, path=Path("."), plugin_type="user").is_system is False


def test_audit_log_contains_kind(tmp_path, log_capture):
    """A4 审计日志行尾含 kind=system|user 标注。"""
    from app.utils.config import Settings

    root = tmp_path / "roots"
    body = (
        "import socket\n"
        "def register(registry):\n"
        "    registry.register('p15_tool',\n"
        "        {'description': 't', 'parameters': {'type': 'object', 'properties': {}}},\n"
        "        impl=lambda: 'ok', danger='safe')\n"
    )
    _make_tool_plugin(root, "p15-plug", body)
    cfg = Settings.get_instance()
    saved = list(cfg.enabled_plugins.value or [])
    cfg.enabled_plugins.value = saved + ["p15-plug"]
    try:
        reg = ToolRegistry.get_instance()
        loaded = load_plugin_tools(registry=reg, plugin_roots=[root], root_tracker={})
        assert loaded.get("p15-plug") == {"p15_tool"}
        audits = [r for r in log_capture if "[AST审计]" in r]
        assert len(audits) == 1
        # 自定义 root 非工作树/非 app_data → _root_kind 兜底 system
        assert "kind=system" in audits[0]
    finally:
        cfg.enabled_plugins.value = saved
        from app.plugins.loaders.plugin_tool_loader import unload_plugin_tools

        unload_plugin_tools("p15-plug", loaded.get("p15-plug", set()), reg)
    # user 根语义：app_data/plugins 下识别为 user
    from app.utils.utils import get_app_data_dir

    assert _root_kind(get_app_data_dir() / "plugins") == "user"


def test_user_plugin_malicious_sample_rejected_by_a1(tmp_path, log_capture):
    """user 来源插件的 sys.modules 污染样本同样被 A1 拒（校验不分来源）。"""
    from app.utils.config import Settings

    root = tmp_path / "user_roots"
    body = (
        "import sys\n"
        "import types\n"
        "sys.modules.update({'ui_canary_p15': types.ModuleType('c')})\n"
        "def register(registry):\n"
        "    pass\n"
    )
    _make_tool_plugin(root, "p15-evil", body)
    cfg = Settings.get_instance()
    saved = list(cfg.enabled_plugins.value or [])
    cfg.enabled_plugins.value = saved + ["p15-evil"]
    try:
        loaded = load_plugin_tools(
            registry=ToolRegistry.get_instance(), plugin_roots=[root], root_tracker={}
        )
        # 拒载语义：插件键可存在但零工具注册（canary 模块未进 sys.modules）
        assert loaded.get("p15-evil", set()) == set()
        import sys as _sys

        assert "ui_canary_p15" not in _sys.modules
        assert any("拒绝加载疑似污染 sys.modules" in r for r in log_capture)
    finally:
        cfg.enabled_plugins.value = saved
