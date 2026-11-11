# -*- coding: utf-8 -*-
"""门禁拦截的纯主题插件：装/卸两侧都必须刷新主题列表

用户报告（2026-11-07）：安装 cyberpunk-2077-somi（manifest 声明
min_host_version 0.6.16 > 宿主 0.6.15-beta）后主题列表不刷新；卸载却正常。

根因：_reload_new_plugin / _reload_single_plugin 的 _load_gate 早退把
themes reloader 一同跳过，theme_manager.reload() 不跑 → 收不到 on_reload
回调 → 主题卡不重建。卸载走 _cleanup_removed_plugin_components，只清理
已有组件，themes reloader 照常执行，故那边正常。

主题是纯文件数据，不执行插件代码，本就不受版本门禁约束。修复后门禁拦截
分支补一次主题源刷新，并置 result["themes"]=True 让窗口侧刷新链跑起来。
"""

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject

from app.core.services.plugin_host_service import PluginHostService


def _make_host():
    svc = PluginHostService.__new__(PluginHostService)
    QObject.__init__(svc)
    svc._agent_manager = None
    return svc


@pytest.fixture()
def gated_theme_env(monkeypatch, tmp_path):
    """被门禁拦截的纯主题插件环境（PluginManager 走 MagicMock）"""
    from app.plugins import builtin_reloaders, kernel as kernel_mod
    from app.plugins.kernel import ComponentReloaderRegistry

    reg = ComponentReloaderRegistry()
    monkeypatch.setattr(kernel_mod, "_registry", reg)
    builtin_reloaders.register_builtin_reloaders(reg)

    blocked = MagicMock()
    blocked.components = {"themes": True}
    blocked.has_component = lambda c: blocked.components.get(c, False)
    blocked.path = tmp_path
    blocked.load_blocked = True
    blocked.version_reason = "需要宿主 >= v0.6.16，当前 v0.6.15"

    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value = blocked
    fake_pm.rescan_plugin = MagicMock()
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    calls = []
    import app.utils.theme_manager as tm_mod

    fake_tm = MagicMock()
    fake_tm.reload = lambda: calls.append("reload")
    monkeypatch.setattr(tm_mod, "theme_manager", fake_tm)
    monkeypatch.setattr("app.utils.config.update_theme_options", lambda: calls.append("options"))
    return blocked, calls


def test_new_gated_theme_plugin_still_refreshes_themes(gated_theme_env):
    """装纯主题插件被门禁拦截 → 仍刷新主题源（用户报告的主场景）"""
    blocked, calls = gated_theme_env
    host = _make_host()

    result = host._reload_new_plugin("cyberpunk-2077-somi")

    assert calls == ["reload", "options"], f"主题源未刷新，calls={calls}"
    assert result["themes"] is True, "result['themes'] 必须为真，窗口侧才走刷新链"
    assert blocked.load_blocked is True, "门禁本身不得被放宽"


def test_single_reload_gated_theme_plugin_still_refreshes_themes(gated_theme_env):
    """reload_plugin_targeted 路径（市场安装/更新）同样刷新主题源"""
    blocked, calls = gated_theme_env
    host = _make_host()
    # 第一次 get_plugin 用于取 before 快照，第二次用于取重扫后的对象
    host._reload_single_plugin("cyberpunk-2077-somi", "__manifest__")

    assert calls == ["reload", "options"], f"主题源未刷新，calls={calls}"


def test_gated_non_theme_plugin_skips_theme_refresh(gated_theme_env):
    """无 themes 组件的被拦插件不白跑主题指纹扫描"""
    blocked, calls = gated_theme_env
    blocked.components = {"tools": True}
    host = _make_host()

    result = host._reload_new_plugin("some-tool-plugin")

    assert calls == [], f"无 themes 组件不应刷新主题，calls={calls}"
    assert result["themes"] is False


def test_gate_still_blocks_code_components(gated_theme_env):
    """门禁语义不变：themes 之外的结果键保持 False（不执行插件代码）"""
    blocked, _calls = gated_theme_env
    blocked.components = {"themes": True, "tools": True, "ui": True}
    host = _make_host()

    result = host._reload_new_plugin("cyberpunk-2077-somi")

    for key in ("tools", "ui", "hooks", "agents"):
        assert not result[key], f"被门禁插件的 {key} 组件不得加载，实际 {result[key]!r}"
