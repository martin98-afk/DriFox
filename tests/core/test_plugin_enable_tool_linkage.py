# -*- coding: utf-8 -*-
"""
插件启用/禁用 → 工具注册/注销联动测试

覆盖（T2 计划 P5 + T14 补充 5）：
P5：
- enable_plugin → 工具注册（watcher 重扫生效）
- disable_plugin → 工具注销
- 重复 enable/disable 幂等（不重复注册/重复注销）
补充 5（D8 双写持久化）：
- disable → enabled 移除 + disabled 加入（写盘）→ PluginManager reset+initialize
  （模拟重启）→ is_enabled False → 工具未注册
- enable 对称恢复：enable 后 disabled 移除 + enabled 加入 → 重启后 is_enabled True

运行: python -m pytest tests/core/test_plugin_enable_tool_linkage.py -v
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.plugin_manager import PluginManager
from app.tools.plugin_tool_loader import PluginToolWatcher
from app.tools.registry import ToolRegistry

# 临时插件名：需在 enabled_plugins 白名单，否则被加载过滤跳过
_TEST_PLUGIN = "enable-link-test"


@pytest.fixture(autouse=True)
def _cleanup_state():
    """重置 registry / PluginManager / Settings 启停状态"""
    ToolRegistry.reset_instance()
    pm = PluginManager.get_instance()
    pm.reset()
    cfg = __import__("app.utils.config", fromlist=["Settings"]).Settings.get_instance()
    saved_enabled = list(cfg.enabled_plugins.value or [])
    saved_disabled = list(cfg.disabled_plugins.value or [])
    cfg.enabled_plugins.value = saved_enabled + [_TEST_PLUGIN]
    cfg.disabled_plugins.value = [d for d in saved_disabled if d != _TEST_PLUGIN]
    yield
    ToolRegistry.reset_instance()
    pm.reset()
    cfg.enabled_plugins.value = saved_enabled
    cfg.disabled_plugins.value = saved_disabled


def _make_plugin_dir(tmp_path: Path, name: str) -> Path:
    """构造带 tools/ 目录 + manifest 的插件目录"""
    plugin_dir = tmp_path / name
    tools_dir = plugin_dir / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "link_tool.py").write_text(
        "from app.tools.registry import DANGER_SAFE\n"
        "def register(registry):\n"
        "    registry.register('link_tool', {'type': 'function', 'function': {'name': 'link_tool'}},\n"
        "                impl=lambda **kw: 'ok', danger=DANGER_SAFE)\n",
        encoding="utf-8",
    )
    (plugin_dir / ".drifox-plugin").mkdir()
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(
        f'{{"name": "{name}", "version": "1.0.0", "components": {{"tools": true}}}}',
        encoding="utf-8",
    )
    return plugin_dir


def _make_watcher(root: Path, registry=None):
    """构造指向临时根的 watcher（不启动后台线程）"""
    return PluginToolWatcher(registry=registry or ToolRegistry.get_instance(), roots=[root])


def _register_plugin_in_pm(pm: PluginManager, plugin_dir: Path, name: str):
    """把临时插件目录注册进 PluginManager（模拟 rescan 发现）"""
    info = pm._scan_one_plugin_dir(plugin_dir, "user")
    assert info is not None
    pm._plugins[name] = info
    return info


class TestEnableDisableToolLinkage:
    """P5：启停 → 工具注册/注销联动"""

    def test_enable_plugin_registers_tools(self, tmp_path, monkeypatch):
        """enable_plugin → watcher 重扫 → 工具注册"""
        plugin_dir = _make_plugin_dir(tmp_path, _TEST_PLUGIN)
        pm = PluginManager.get_instance()
        _register_plugin_in_pm(pm, plugin_dir, _TEST_PLUGIN)

        watcher = _make_watcher(tmp_path)
        monkeypatch.setattr(
            "app.tools.plugin_tool_loader.ensure_plugin_tool_watcher", lambda: watcher
        )

        # 禁用状态 → 工具未注册
        pm.disable_plugin(_TEST_PLUGIN)
        assert "link_tool" not in ToolRegistry.get_instance().names()

        # 启用 → 工具注册
        pm.enable_plugin(_TEST_PLUGIN)
        reg = ToolRegistry.get_instance().get("link_tool")
        assert reg is not None
        assert reg.source == f"plugin:{_TEST_PLUGIN}"

    def test_disable_plugin_unregisters_tools(self, tmp_path, monkeypatch):
        """disable_plugin → watcher 重扫 → 工具注销"""
        plugin_dir = _make_plugin_dir(tmp_path, _TEST_PLUGIN)
        pm = PluginManager.get_instance()
        _register_plugin_in_pm(pm, plugin_dir, _TEST_PLUGIN)

        watcher = _make_watcher(tmp_path)
        monkeypatch.setattr(
            "app.tools.plugin_tool_loader.ensure_plugin_tool_watcher", lambda: watcher
        )

        # 先启用 → 工具注册
        pm.enable_plugin(_TEST_PLUGIN)
        assert "link_tool" in ToolRegistry.get_instance().names()

        # 禁用 → 工具注销
        pm.disable_plugin(_TEST_PLUGIN)
        assert "link_tool" not in ToolRegistry.get_instance().names()

    def test_repeated_enable_idempotent(self, tmp_path, monkeypatch):
        """重复 enable 幂等：不重复注册（registry 无重复条目）"""
        plugin_dir = _make_plugin_dir(tmp_path, _TEST_PLUGIN)
        pm = PluginManager.get_instance()
        _register_plugin_in_pm(pm, plugin_dir, _TEST_PLUGIN)

        watcher = _make_watcher(tmp_path)
        monkeypatch.setattr(
            "app.tools.plugin_tool_loader.ensure_plugin_tool_watcher", lambda: watcher
        )

        pm.enable_plugin(_TEST_PLUGIN)
        pm.enable_plugin(_TEST_PLUGIN)
        pm.enable_plugin(_TEST_PLUGIN)
        reg = ToolRegistry.get_instance()
        # scan_now 幂等：link_tool 只注册一次
        assert reg.names().count("link_tool") == 1

    def test_repeated_disable_idempotent(self, tmp_path, monkeypatch):
        """重复 disable 幂等：注销后再次 disable 不报错、不残留"""
        plugin_dir = _make_plugin_dir(tmp_path, _TEST_PLUGIN)
        pm = PluginManager.get_instance()
        _register_plugin_in_pm(pm, plugin_dir, _TEST_PLUGIN)

        watcher = _make_watcher(tmp_path)
        monkeypatch.setattr(
            "app.tools.plugin_tool_loader.ensure_plugin_tool_watcher", lambda: watcher
        )

        pm.enable_plugin(_TEST_PLUGIN)
        pm.disable_plugin(_TEST_PLUGIN)
        pm.disable_plugin(_TEST_PLUGIN)
        assert "link_tool" not in ToolRegistry.get_instance().names()

    def test_unknown_plugin_no_crash(self):
        """启停不存在的插件 → 不崩溃、registry 不变"""
        pm = PluginManager.get_instance()
        pm.enable_plugin("ghost-plugin")
        pm.disable_plugin("ghost-plugin")
        assert True  # 无异常即通过


class TestPersistAcrossRestart:
    """补充 5（D8 双写持久化）：disable/enable 写盘 + 重启恢复"""

    def _restart_pm(self, pm: PluginManager, app_data: Path, plugin_dir: Path):
        """模拟重启：reset + initialize（复用同一 app_data 目录）"""
        pm.reset()
        pm.initialize(app_data)
        # 手动注册临时插件（initialize 只扫系统/用户目录；临时插件在 tmp_path）
        info = pm._scan_one_plugin_dir(plugin_dir, "user")
        pm._plugins[info.name] = info
        return pm

    def test_disabled_persists_across_restart(self, tmp_path, monkeypatch):
        """disable → enabled 移除 + disabled 加入 → 重启 → is_enabled False → 工具未注册"""
        plugin_dir = _make_plugin_dir(tmp_path, _TEST_PLUGIN)
        pm = PluginManager.get_instance()
        _register_plugin_in_pm(pm, plugin_dir, _TEST_PLUGIN)

        # 构造独立 app_data（模拟 .drifox 用户插件目录，避免污染真实环境）
        app_data = tmp_path / "_app_data"
        app_data.mkdir()
        pm.initialize(app_data)

        watcher = _make_watcher(tmp_path)
        monkeypatch.setattr(
            "app.tools.plugin_tool_loader.ensure_plugin_tool_watcher", lambda: watcher
        )

        # 先确保启用（工具注册），再禁用
        pm.enable_plugin(_TEST_PLUGIN)
        assert "link_tool" in ToolRegistry.get_instance().names()
        pm.disable_plugin(_TEST_PLUGIN)
        assert "link_tool" not in ToolRegistry.get_instance().names()

        # D8 双写断言：enabled 移除 + disabled 加入
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        assert _TEST_PLUGIN not in (cfg.enabled_plugins.value or [])
        assert _TEST_PLUGIN in (cfg.disabled_plugins.value or [])

        # 模拟重启：reset + initialize → 恢复的启用集合不含该插件
        pm2 = self._restart_pm(pm, app_data, plugin_dir)
        assert pm2.is_enabled(_TEST_PLUGIN) is False

        # 重启后 watcher 重扫 → 工具不注册
        ToolRegistry.reset_instance()
        watcher2 = _make_watcher(tmp_path)
        watcher2.scan_now()
        assert "link_tool" not in ToolRegistry.get_instance().names()

    def test_enable_persists_across_restart(self, tmp_path, monkeypatch):
        """enable 对称恢复：enabled 加入 + disabled 移除 → 重启 → is_enabled True"""
        plugin_dir = _make_plugin_dir(tmp_path, _TEST_PLUGIN)
        pm = PluginManager.get_instance()
        _register_plugin_in_pm(pm, plugin_dir, _TEST_PLUGIN)

        app_data = tmp_path / "_app_data2"
        app_data.mkdir()
        pm.initialize(app_data)

        watcher = _make_watcher(tmp_path)
        monkeypatch.setattr(
            "app.tools.plugin_tool_loader.ensure_plugin_tool_watcher", lambda: watcher
        )

        # disable → enable 恢复
        pm.disable_plugin(_TEST_PLUGIN)
        pm.enable_plugin(_TEST_PLUGIN)

        from app.utils.config import Settings

        cfg = Settings.get_instance()
        assert _TEST_PLUGIN in (cfg.enabled_plugins.value or [])
        assert _TEST_PLUGIN not in (cfg.disabled_plugins.value or [])

        # 重启 → 启用状态恢复 → 工具注册
        pm2 = self._restart_pm(pm, app_data, plugin_dir)
        assert pm2.is_enabled(_TEST_PLUGIN) is True

        ToolRegistry.reset_instance()
        watcher2 = _make_watcher(tmp_path)
        watcher2.scan_now()
        assert "link_tool" in ToolRegistry.get_instance().names()
