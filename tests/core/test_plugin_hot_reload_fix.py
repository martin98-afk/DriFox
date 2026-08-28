# -*- coding: utf-8 -*-
"""插件热重载修复回归测试（子任务 #4）

覆盖：
- P0-1: 组件检测以物理目录为准（manifest 误声明 commands 不再触发全量命令重载）
- P1-4: __NEW__ 兜底——已注册插件降级为已知插件增量重载（不重复走全量路径）
- P1-4/B3: 插件删除路径清空 watcher 去重缓存键（删除 → 3s 内重装不被吞）
"""

import json

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject

from app.core.plugin_host_service import PluginHostService
from app.core.backend import ChatBackend
from app.plugins.managers.plugin_manager import PluginManager
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture(autouse=True)
def _cleanup_registries():
    """每个测试前后重置单例，避免测试间污染"""
    pm = PluginManager.get_instance()
    pm.reset()
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    yield
    pm.reset()
    reg.reset()


def _make_plugin_dir(tmp_path, name: str, with_commands_dir: bool, components: dict):
    """构造插件目录（.drifox-plugin 格式）

    Args:
        tmp_path: pytest tmp_path
        name: 插件名
        with_commands_dir: 物理上是否创建 commands/ 目录
        components: manifest 中声明的 components 字典
    """
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / ".drifox-plugin").mkdir()
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "components": components}),
        encoding="utf-8",
    )
    if with_commands_dir:
        (plugin_dir / "commands").mkdir()
        (plugin_dir / "commands" / "hello.md").write_text("# hello\n", encoding="utf-8")
    # UI 组件需要 ui/ 目录 + __init__.py
    (plugin_dir / "ui").mkdir()
    (plugin_dir / "ui" / "__init__.py").write_text("# empty", encoding="utf-8")
    return plugin_dir


class TestComponentDetectionPhysicalOverride:
    """P0-1: 组件检测以物理目录为准"""

    def test_manifest_declares_commands_but_no_physical_dir(self, tmp_path):
        """manifest 声明 commands:true 但无 commands/ 目录 → 组件不含 commands"""
        plugin_dir = _make_plugin_dir(
            tmp_path,
            "browser-like",
            with_commands_dir=False,
            components={"commands": True, "ui": True},
        )
        pm = PluginManager.get_instance()
        info = pm._scan_one_plugin_dir(plugin_dir, "user")
        assert info is not None
        # 修复前：update() 保留 manifest 声明的 commands → has_component("commands") True
        # 修复后：以物理目录为准 → commands 组件被剔除，UI 组件（物理存在）保留
        assert info.has_component("commands") is False
        assert info.has_component("ui") is True

    def test_physical_commands_dir_still_detected(self, tmp_path):
        """物理存在 commands/ 目录 → commands 组件仍被检测"""
        plugin_dir = _make_plugin_dir(
            tmp_path,
            "real-cmds",
            with_commands_dir=True,
            components={"commands": True, "ui": True},
        )
        pm = PluginManager.get_instance()
        info = pm._scan_one_plugin_dir(plugin_dir, "user")
        assert info is not None
        assert info.has_component("commands") is True
        assert info.has_component("ui") is True

    def test_full_scan_uses_physical_components(self, tmp_path):
        """_scan_plugins 全量扫描同样以物理目录为准"""
        base = tmp_path / "plugins"
        base.mkdir()
        _make_plugin_dir(
            base,
            "ghost-cmds",
            with_commands_dir=False,
            components={"commands": True},
        )
        pm = PluginManager.get_instance()
        plugins = pm._scan_plugins(base, "user")
        assert len(plugins) == 1
        assert plugins[0].has_component("commands") is False

    def test_team_templates_physical_detected_in_full_scan(self, tmp_path):
        """_scan_plugins 全量扫描也识别物理存在的 team_templates 组件"""
        base = tmp_path / "plugins"
        base.mkdir()
        plugin_dir = base / "tmpl"
        plugin_dir.mkdir()
        (plugin_dir / ".drifox-plugin").mkdir()
        (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(
            json.dumps({"name": "tmpl", "version": "1.0.0", "components": {}}),
            encoding="utf-8",
        )
        tmpl_dir = plugin_dir / "team_templates"
        tmpl_dir.mkdir()
        (tmpl_dir / "team.yaml").write_text("name: team\n", encoding="utf-8")

        pm = PluginManager.get_instance()
        plugins = pm._scan_plugins(base, "user")
        assert len(plugins) == 1
        assert plugins[0].has_component("team_templates") is True


class TestHotReloadNewPluginFallback:
    """P1-4: __NEW__ 兜底——已注册插件降级为增量重载"""

    def _make_backend_with_registered_plugin(self, tmp_path, name: str):
        """构造 PluginHostService + 已注册插件（模拟索引未更新场景）"""
        pm = PluginManager.get_instance()
        # 注册一个插件（模拟 pm 中已存在，但 watcher 路径索引尚未包含）
        plugin_dir = _make_plugin_dir(tmp_path, name, with_commands_dir=False, components={"ui": True})
        info = pm._scan_one_plugin_dir(plugin_dir, "user")
        assert info is not None
        pm._plugins[name] = info
        pm._initialized = True

        svc = PluginHostService.__new__(PluginHostService)
        QObject.__init__(svc)  # 手动初始化 Qt 基类，绕过单例拦截
        # __new__ 实例无 Qt 信号，mock 掉 emit（断言只关心重载分发路径）
        svc.plugin_changed = MagicMock()
        return svc, pm

    def test_new_plugin_sentinel_for_unregistered_plugin(self, tmp_path, monkeypatch):
        """未注册插件 → 仍走 _reload_new_plugin 全量增量路径"""
        svc, pm = self._make_backend_with_registered_plugin(tmp_path, "existing")
        called = {}

        def fake_new_plugin(plugin_name):
            called["new"] = plugin_name
            return {}

        def fake_single_plugin(plugin_name, component=""):
            called["single"] = (plugin_name, component)
            return {}

        monkeypatch.setattr(svc, "_reload_new_plugin", fake_new_plugin)
        monkeypatch.setattr(svc, "_reload_single_plugin", fake_single_plugin)

        # "ghost" 插件未在 pm 注册 → 走 _reload_new_plugin
        svc._on_hot_reload_requested(PluginHostService._NEW_PLUGIN_SENTINEL, "ghost")
        assert called.get("new") == "ghost"
        assert "single" not in called

    def test_new_plugin_sentinel_for_registered_plugin_falls_back(self, tmp_path, monkeypatch):
        """已注册插件被误判为 __NEW__ → 仍走 _reload_new_plugin（幂等，组件错重载）

        fc024a43 起 __NEW__ 分支不再 has_plugin 降级为 _reload_single_plugin：
        watch 线程可能在 emit 前对全新安装的插件做过 rescan 注册（组件尚未加载），
        降级会全 False 跳过导致组件永不生效；_reload_new_plugin 对已注册插件幂等。
        """
        svc, pm = self._make_backend_with_registered_plugin(tmp_path, "existing")
        called = {}

        def fake_new_plugin(plugin_name):
            called["new"] = plugin_name
            return {}

        def fake_single_plugin(plugin_name, component=""):
            called["single"] = (plugin_name, component)
            return {}

        monkeypatch.setattr(svc, "_reload_new_plugin", fake_new_plugin)
        monkeypatch.setattr(svc, "_reload_single_plugin", fake_single_plugin)

        # "existing" 已注册但被误判为 __NEW__ → 仍走 _reload_new_plugin（幂等，不降级）
        svc._on_hot_reload_requested(PluginHostService._NEW_PLUGIN_SENTINEL, "existing")
        assert called.get("new") == "existing"
        assert "single" not in called

    def test_rebuild_prefixes_runs_in_finally(self, tmp_path, monkeypatch):
        """_rebuild_watcher_prefixes 在重载异常时也必然执行（try/finally）"""
        svc, pm = self._make_backend_with_registered_plugin(tmp_path, "boom")
        rebuilt = []

        def fake_rebuild():
            rebuilt.append(True)

        def boom_new_plugin(plugin_name):
            raise RuntimeError("simulated reload failure")

        monkeypatch.setattr(svc, "_reload_new_plugin", boom_new_plugin)
        monkeypatch.setattr(svc, "_rebuild_watcher_prefixes", fake_rebuild)

        # 未注册插件 + 重载抛异常 → 索引仍应重建（finally）
        svc._on_hot_reload_requested(PluginHostService._NEW_PLUGIN_SENTINEL, "ghost")
        assert rebuilt == [True], "异常路径也应重建 watcher 路径索引"


class TestDedupCacheCleanupOnRemove:
    """P1-4/B3: 插件删除路径清空 watcher 去重缓存键"""

    def _make_backend_with_dedup(self):
        svc = PluginHostService.__new__(PluginHostService)
        QObject.__init__(svc)  # 手动初始化 Qt 基类，绕过单例拦截
        # 模拟 watcher 闭包挂载的去重缓存（_start_plugin_watcher 中设置）
        svc._watcher_dedup_cache = {
            ("victim", ""): 100.0,
            ("victim", "mcp"): 200.0,
            ("other", ""): 300.0,
        }
        return svc

    def test_single_reload_removed_plugin_clears_dedup_keys(self, tmp_path, monkeypatch):
        """插件被删除时，清空该插件全部去重键（其他插件保留）"""
        pm = PluginManager.get_instance()
        pm._initialized = True
        # 注册一个插件，但随后删除其目录（触发 removed 分支）
        plugin_dir = _make_plugin_dir(tmp_path, "victim", with_commands_dir=False, components={"ui": True})
        info = pm._scan_one_plugin_dir(plugin_dir, "user")
        assert info is not None
        pm._plugins["victim"] = info
        import shutil

        shutil.rmtree(str(plugin_dir))  # 删除物理目录

        svc = self._make_backend_with_dedup()
        monkeypatch.setattr(svc, "_rebuild_watcher_prefixes", lambda: None)
        svc._reload_single_plugin("victim", "")

        dedup = svc._watcher_dedup_cache
        assert ("victim", "") not in dedup
        assert ("victim", "mcp") not in dedup
        # 其他插件键不受影响
        assert ("other", "") in dedup

    def test_full_reload_removed_plugins_clear_dedup_keys(self, monkeypatch):
        """全量重载（reload_plugin_subsystems）移除插件时清空对应去重键"""
        pm = PluginManager.get_instance()
        pm._initialized = True
        pm._plugins.clear()

        svc = PluginHostService.__new__(PluginHostService)
        QObject.__init__(svc)  # 手动初始化 Qt 基类，绕过单例拦截
        svc._watcher_dedup_cache = {
            ("gone-a", ""): 100.0,
            ("gone-b", "ui"): 200.0,
            ("stay", ""): 300.0,
        }
        monkeypatch.setattr(svc, "_rebuild_watcher_prefixes", lambda: None)
        fake_removed = [
            type("P", (), {"name": "gone-a", "components": {"ui": True}})(),
            type("P", (), {"name": "gone-b", "components": {"ui": True}})(),
        ]
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.rescan",
            lambda self: {"added": [], "removed": fake_removed, "changed": []},
        )
        # reload_plugin_subsystems 前置条件：agent_manager 等为 None 时分支跳过
        svc._agent_manager = None
        svc.reload_plugin_subsystems()

        dedup = svc._watcher_dedup_cache
        assert ("gone-a", "") not in dedup
        assert ("gone-b", "ui") not in dedup
        assert ("stay", "") in dedup


class TestPluginChangedBroadcast:
    """插件热更新 plugin_changed 由服务直接发出（UI 各窗口直连服务信号）。

    历史根因（T3，已随服务化消除）：watcher 线程原寄生在首个 backend 上，
    需广播到全部活跃 backend；现 watcher 与信号同属服务单例，单一 emit 即达。
    """

    def _make_service_with_spy(self):
        """构造服务实例并连接 plugin_changed 记录器"""
        svc = PluginHostService.__new__(PluginHostService)
        QObject.__init__(svc)  # 手动初始化 Qt 基类，绕过单例拦截
        received = []

        class _FakeSignal:
            def connect(self, fn):
                pass

            def emit(self, *args):
                received.append(args[0] if args else None)

        svc.plugin_changed = _FakeSignal()
        return svc, received

    def test_signal_emits_annotated_result(self, monkeypatch):
        """重载完成后 plugin_changed 携带事件标识（_event_seq/_plugin_name）"""
        svc, received = self._make_service_with_spy()
        result = {"ui": True, "agents": 1}
        monkeypatch.setattr(svc, "_reload_single_plugin", lambda name, comp: result)
        monkeypatch.setattr(svc, "_rebuild_watcher_prefixes", lambda: None)

        svc._on_hot_reload_requested("some-plugin", "ui")

        expected = dict(result)
        expected["_event_seq"] = received[0].get("_event_seq", 1)
        expected["_plugin_name"] = "some-plugin"
        assert received == [expected], "服务必须发出 plugin_changed（含事件标识）"

    def test_cleanup_removes_from_active_instances(self):
        """backend.cleanup() 后从 _active_instances 移除（防泄漏/防幽灵 hook 触发）"""
        a = ChatBackend()
        b = ChatBackend()
        b.cleanup()
        try:
            assert a in ChatBackend._active_instances
            assert b not in ChatBackend._active_instances, "cleanup 后不得留在活跃集合"
        finally:
            a.cleanup()
