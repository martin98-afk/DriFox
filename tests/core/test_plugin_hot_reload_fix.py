# -*- coding: utf-8 -*-
"""插件热重载修复回归测试（子任务 #4）

覆盖：
- P0-1: 组件检测以物理目录为准（manifest 误声明 commands 不再触发全量命令重载）
- P1-4: __NEW__ 兜底——已注册插件降级为已知插件增量重载（不重复走全量路径）
- P1-4/B3: 插件删除路径清空 watcher 去重缓存键（删除 → 3s 内重装不被吞）
"""

import json

import pytest

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
        """构造 ChatBackend + 已注册插件（模拟索引未更新场景）"""
        pm = PluginManager.get_instance()
        # 注册一个插件（模拟 pm 中已存在，但 watcher 路径索引尚未包含）
        plugin_dir = _make_plugin_dir(tmp_path, name, with_commands_dir=False, components={"ui": True})
        info = pm._scan_one_plugin_dir(plugin_dir, "user")
        assert info is not None
        pm._plugins[name] = info
        pm._initialized = True

        backend = ChatBackend()
        return backend, pm

    def test_new_plugin_sentinel_for_unregistered_plugin(self, tmp_path, monkeypatch):
        """未注册插件 → 仍走 _reload_new_plugin 全量增量路径"""
        backend, pm = self._make_backend_with_registered_plugin(tmp_path, "existing")
        called = {}

        def fake_new_plugin(plugin_name):
            called["new"] = plugin_name
            return {}

        def fake_single_plugin(plugin_name, component=""):
            called["single"] = (plugin_name, component)
            return {}

        monkeypatch.setattr(backend, "_reload_new_plugin", fake_new_plugin)
        monkeypatch.setattr(backend, "_reload_single_plugin", fake_single_plugin)

        # "ghost" 插件未在 pm 注册 → 走 _reload_new_plugin
        backend._on_hot_reload_requested(ChatBackend._NEW_PLUGIN_SENTINEL, "ghost")
        assert called.get("new") == "ghost"
        assert "single" not in called

    def test_new_plugin_sentinel_for_registered_plugin_falls_back(self, tmp_path, monkeypatch):
        """已注册插件被误判为 __NEW__ → 仍走 _reload_new_plugin（幂等，组件错重载）

        fc024a43 起 __NEW__ 分支不再 has_plugin 降级为 _reload_single_plugin：
        watch 线程可能在 emit 前对全新安装的插件做过 rescan 注册（组件尚未加载），
        降级会全 False 跳过导致组件永不生效；_reload_new_plugin 对已注册插件幂等。
        """
        backend, pm = self._make_backend_with_registered_plugin(tmp_path, "existing")
        called = {}

        def fake_new_plugin(plugin_name):
            called["new"] = plugin_name
            return {}

        def fake_single_plugin(plugin_name, component=""):
            called["single"] = (plugin_name, component)
            return {}

        monkeypatch.setattr(backend, "_reload_new_plugin", fake_new_plugin)
        monkeypatch.setattr(backend, "_reload_single_plugin", fake_single_plugin)

        # "existing" 已注册但被误判为 __NEW__ → 仍走 _reload_new_plugin（幂等，不降级）
        backend._on_hot_reload_requested(ChatBackend._NEW_PLUGIN_SENTINEL, "existing")
        assert called.get("new") == "existing"
        assert "single" not in called

    def test_rebuild_prefixes_runs_in_finally(self, tmp_path, monkeypatch):
        """_rebuild_watcher_prefixes 在重载异常时也必然执行（try/finally）"""
        backend, pm = self._make_backend_with_registered_plugin(tmp_path, "boom")
        rebuilt = []

        def fake_rebuild():
            rebuilt.append(True)

        def boom_new_plugin(plugin_name):
            raise RuntimeError("simulated reload failure")

        monkeypatch.setattr(backend, "_reload_new_plugin", boom_new_plugin)
        monkeypatch.setattr(backend, "_rebuild_watcher_prefixes", fake_rebuild)

        # 未注册插件 + 重载抛异常 → 索引仍应重建（finally）
        backend._on_hot_reload_requested(ChatBackend._NEW_PLUGIN_SENTINEL, "ghost")
        assert rebuilt == [True], "异常路径也应重建 watcher 路径索引"


class TestDedupCacheCleanupOnRemove:
    """P1-4/B3: 插件删除路径清空 watcher 去重缓存键"""

    def _make_backend_with_dedup(self):
        backend = ChatBackend()
        # 模拟 watcher 闭包挂载的去重缓存（_start_plugin_watcher 中设置）
        backend._watcher_dedup_cache = {
            ("victim", ""): 100.0,
            ("victim", "mcp"): 200.0,
            ("other", ""): 300.0,
        }
        return backend

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

        backend = self._make_backend_with_dedup()
        monkeypatch.setattr(backend, "_rebuild_watcher_prefixes", lambda: None)
        backend._reload_single_plugin("victim", "")

        dedup = backend._watcher_dedup_cache
        assert ("victim", "") not in dedup
        assert ("victim", "mcp") not in dedup
        # 其他插件键不受影响
        assert ("other", "") in dedup

    def test_full_reload_removed_plugins_clear_dedup_keys(self, monkeypatch):
        """全量重载（reload_plugin_subsystems）移除插件时清空对应去重键"""
        pm = PluginManager.get_instance()
        pm._initialized = True
        pm._plugins.clear()

        backend = ChatBackend()
        backend._watcher_dedup_cache = {
            ("gone-a", ""): 100.0,
            ("gone-b", "ui"): 200.0,
            ("stay", ""): 300.0,
        }
        monkeypatch.setattr(backend, "_rebuild_watcher_prefixes", lambda: None)
        fake_removed = [
            type("P", (), {"name": "gone-a"})(),
            type("P", (), {"name": "gone-b"})(),
        ]
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.rescan",
            lambda self: {"added": [], "removed": fake_removed, "changed": []},
        )
        # reload_plugin_subsystems 前置条件：agent_manager 等为 None 时分支跳过
        backend._agent_manager = None
        backend._hook_manager = None
        backend.reload_plugin_subsystems()

        dedup = backend._watcher_dedup_cache
        assert ("gone-a", "") not in dedup
        assert ("gone-b", "ui") not in dedup
        assert ("stay", "") in dedup


class TestPluginChangedBroadcast:
    """T3: 插件热更新 plugin_changed 必须广播到全部活跃 backend。

    根因：watcher 线程是类级单例，只有首个启动 watcher 的 backend 连接了
    _hot_reload_requested → _on_hot_reload_requested 只 emit 该 backend 的
    plugin_changed。宿主窗口关闭断开信号后，watcher 线程仍存活（其他窗口
    refcount>0）、数据照常重载，但 emit 无接收者 → 所有窗口 UI 静默不刷新。
    修复：广播到 ChatBackend._active_instances 中全部活跃实例。
    """

    def _make_backend_with_spy(self):
        """构造 backend 并连接 plugin_changed 记录器"""
        backend = ChatBackend()
        received = []
        backend.plugin_changed.connect(received.append)
        return backend, received

    def test_broadcast_reaches_all_active_backends(self, monkeypatch):
        """宿主 backend 重载后，所有活跃 backend 的 plugin_changed 都收到；
        未注册（已清理）的 backend 收不到。"""
        a, recv_a = self._make_backend_with_spy()
        b, recv_b = self._make_backend_with_spy()
        c, recv_c = self._make_backend_with_spy()
        try:
            # 模拟 c 未注册/已关闭（不在 _active_instances 中）
            ChatBackend._active_instances.discard(c)
            result = {"ui": True, "agents": 1}
            monkeypatch.setattr(a, "_reload_single_plugin", lambda name, comp: result)
            monkeypatch.setattr(a, "_rebuild_watcher_prefixes", lambda: None)

            a._on_hot_reload_requested("some-plugin", "ui")

            assert recv_a == [result], "宿主 backend 必须收到 plugin_changed"
            assert recv_b == [result], "其他活跃 backend 必须收到广播"
            assert recv_c == [], "未注册 backend 不得收到广播"
        finally:
            a.cleanup()
            b.cleanup()
            c.cleanup()

    def test_cleanup_removes_from_active_instances(self):
        """backend.cleanup() 后从 _active_instances 移除（防泄漏/防幽灵广播）"""
        a, _ = self._make_backend_with_spy()
        b, _ = self._make_backend_with_spy()
        b.cleanup()
        try:
            assert a in ChatBackend._active_instances
            assert b not in ChatBackend._active_instances, "cleanup 后不得留在活跃集合"
        finally:
            a.cleanup()

    def test_cleaned_backend_no_longer_receives_broadcast(self, monkeypatch):
        """cleanup 后的 backend 不再接收 plugin_changed 广播"""
        a, recv_a = self._make_backend_with_spy()
        b, recv_b = self._make_backend_with_spy()
        b.cleanup()  # b 窗口已关闭
        try:
            result = {"ui": True}
            monkeypatch.setattr(a, "_reload_single_plugin", lambda name, comp: result)
            monkeypatch.setattr(a, "_rebuild_watcher_prefixes", lambda: None)

            a._on_hot_reload_requested("p", "")

            assert recv_a == [result]
            assert recv_b == [], "已清理 backend 不得收到广播"
        finally:
            a.cleanup()
