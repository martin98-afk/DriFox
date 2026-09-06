# -*- coding: utf-8 -*-
"""同名覆盖显性化：overridden_by 标记 + 双路径 warning + allow_user_override 开关。

三用例：同名→标记+日志 / 设置关→用户版跳过系统版生效 / claude→user 双层覆盖链照常
（junction 形态语义：用户目录插件覆盖一切来源）。
"""
import pytest
from loguru import logger

from app.plugins.managers.plugin_manager import PluginInfo, PluginManager


@pytest.fixture()
def log_capture():
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


def _info(tmp_path, name, ptype):
    return PluginInfo(name=name, manifest={"name": name}, path=tmp_path / f"{name}-{ptype}", plugin_type=ptype)


def _make_pm(tmp_path, system_info=None, claude_info=None, user_info=None, app_data_dir=None):
    pm = PluginManager.__new__(PluginManager)
    pm._initialized = True
    pm._app_data_dir = app_data_dir
    pm._plugins = {}
    if system_info:
        pm._plugins[system_info.name] = system_info
    # monkeypatch 扫描：按 plugin_type 返回预置插件
    def _fake_scan(base_dir, plugin_type):
        if plugin_type == "system" and system_info:
            return [system_info]
        if plugin_type == "claude" and claude_info:
            return [claude_info]
        if plugin_type == "user" and user_info:
            return [user_info]
        return []

    pm._scan_plugins = _fake_scan
    pm._load_plugin_ui = lambda name: None
    pm._restore_enabled_from_settings = lambda: None
    return pm


def test_same_name_override_marked_and_logged(tmp_path, log_capture):
    """用户插件同名覆盖系统版 → overridden_by 标记 + warning 含两来源路径 + changed。"""
    sys_info = _info(tmp_path, "dup-plug", "system")
    user_info = _info(tmp_path, "dup-plug", "user")
    pm = _make_pm(tmp_path, system_info=sys_info, user_info=user_info, app_data_dir=tmp_path)
    result = pm.rescan()
    # 覆盖写回：运行时查询立即可见新实例（overridden_by 可见）
    assert pm._plugins["dup-plug"] is user_info
    assert user_info.overridden_by == "system"
    assert sys_info.overridden_by == ""  # 被覆盖方不打标记，标记在生效方
    assert any("dup-plug" in r and "被用户插件覆盖" in r for r in log_capture)
    assert any(str(sys_info.path) in r and str(user_info.path) in r for r in log_capture)
    assert user_info in result["changed"]


def test_override_disabled_user_skipped(tmp_path, log_capture, monkeypatch):
    """allow_user_override=false → 用户版跳过、系统版生效。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = cfg.allow_user_override.value
    cfg.allow_user_override.value = False
    try:
        sys_info = _info(tmp_path, "dup-plug", "system")
        user_info = _info(tmp_path, "dup-plug", "user")
        pm = _make_pm(tmp_path, system_info=sys_info, user_info=user_info, app_data_dir=tmp_path)
        result = pm.rescan()
        assert pm._plugins["dup-plug"] is sys_info  # 系统版生效
        assert user_info.overridden_by == ""
        assert user_info not in result["changed"]
        assert any("allow_user_override=false" in r and "dup-plug" in r for r in log_capture)
    finally:
        cfg.allow_user_override.value = saved


def test_claude_then_user_chain_override(tmp_path, log_capture):
    """junction 形态语义：claude 覆盖 system，user 再覆盖 claude，链路照常。"""
    sys_info = _info(tmp_path, "chain-plug", "system")
    claude_info = _info(tmp_path, "chain-plug", "claude")
    user_info = _info(tmp_path, "chain-plug", "user")
    pm = _make_pm(
        tmp_path, system_info=sys_info, claude_info=claude_info, user_info=user_info,
        app_data_dir=tmp_path,
    )
    pm.rescan()
    assert pm._plugins["chain-plug"] is user_info
    assert user_info.overridden_by == "claude"
    assert claude_info.overridden_by == "system"
    assert any("'chain-plug' 被 Claude 插件覆盖" in r for r in log_capture)
    assert any("'chain-plug' 被用户插件覆盖" in r for r in log_capture)
