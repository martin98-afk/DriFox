# -*- coding: utf-8 -*-
"""插件热重载清理面回归测试

背景：插件热重载偶发「清不干净」——旧注册、旧模块、旧实例残留到进程重启。
本次修复覆盖的几条确定性缺口，逐条锁定行为：

1. watcher 去重键写入「当前时刻」而非「未来时刻」（写未来会让抑制窗口翻倍成 20s，
   市场装/更新后的真实修改事件被吞，改文件永不生效）
2. 插件已被删除时不再写去重键（否则删除段刚清的键被重新写回，
   「删除 → 10s 内重装」的残留事件被判重复）
3. 删除段组件快照为空时回退全组件清理（插件从未注册成功但 tools/providers
   已由独立扫描器注册的情形）
4. 删除段统一回收宿主级资源（窗口服务槽为新增项）
5. ui/__init__.py 不存在时 load_plugin 先卸载旧注册（删掉 ui 目录 → 旧 UI 永久残留）
6. 去抖合并时精准目标不被全量目标丢弃

运行: python -m pytest tests/plugins/test_plugin_hot_reload_cleanup.py -v
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.services.plugin_host_service import PluginHostService
from app.plugins.kernel import KNOWN_COMPONENTS

# ── 1/2. 去重键时间语义 ──────────────────────────────────────────

class TestWatcherDedupStamp:
    @staticmethod
    def _service() -> PluginHostService:
        svc = PluginHostService.__new__(PluginHostService)
        svc._watcher_dedup_cache = {}
        return svc

    def test_stamp_writes_now_not_future(self, monkeypatch):
        """写未来时间戳会让 now-last<10s 恒命中，抑制窗口翻倍。"""
        svc = self._service()
        pm = SimpleNamespace(get_plugin=lambda name: object())
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.get_instance",
            classmethod(lambda cls: pm),
        )
        before = time.time()
        svc._stamp_watcher_dedup_for("demo_plugin", {"ui": True})
        stamped = svc._watcher_dedup_cache[("demo_plugin", "ui")]
        assert stamped <= time.time() + 0.01, "去重键写入了未来时间戳"
        assert stamped >= before - 0.01

    def test_stamp_skipped_when_plugin_removed(self, monkeypatch):
        """插件已不在索引时不写键：删除段刚清的键不能被 stamp 写回。"""
        svc = self._service()
        pm = SimpleNamespace(is_initialized=lambda: True, get_plugin=lambda name: None)
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.get_instance",
            classmethod(lambda cls: pm),
        )
        svc._stamp_watcher_dedup_for("demo_plugin", {"ui": True})
        assert svc._watcher_dedup_cache == {}

# ── 3/4. 删除段全组件清理 + 宿主资源回收 ─────────────────────────

class TestRemovedPluginCleanup:
    @staticmethod
    def _fake_registry(calls: list):
        class _Registry:
            def reload(self, ctx):
                calls.append(ctx.component)
                return True

        return _Registry()

    def test_empty_component_snapshot_falls_back_to_all(self, monkeypatch):
        """快照为空（插件从未注册成功）时不能零清理。"""
        svc = PluginHostService.__new__(PluginHostService)
        calls: list = []
        monkeypatch.setattr(
            "app.plugins.kernel.get_reloader_registry", lambda: self._fake_registry(calls)
        )
        monkeypatch.setattr(svc, "_release_plugin_host_resources", lambda name: None)
        svc._cleanup_removed_plugin_components(
            "demo_unregistered_plugin", {}, {k: False for k in KNOWN_COMPONENTS}, tuple(KNOWN_COMPONENTS)
        )
        assert set(calls) == set(KNOWN_COMPONENTS), "空快照未回退到全组件清理"

    def test_declared_only_purge_does_not_touch_stdlib(self, monkeypatch):
        """删除路径只清显式声明的前缀，不能用裸插件名匹配标准库模块。"""
        svc = PluginHostService.__new__(PluginHostService)
        monkeypatch.setattr(
            "app.plugins.kernel.get_reloader_registry", lambda: self._fake_registry([])
        )
        monkeypatch.setattr(svc, "_release_plugin_host_resources", lambda name: None)

        import sys

        before = sys.modules.get("json")
        svc._cleanup_removed_plugin_components(
            "json", {"ui": True}, {k: False for k in KNOWN_COMPONENTS}, tuple(KNOWN_COMPONENTS), manifest={}
        )
        assert sys.modules.get("json") is before, "裸插件名匹配误清了标准库模块"

    def test_release_host_resources_clears_window_services(self):
        """窗口级服务槽必须由宿主兜底摘除（此前只有插件自己清）。"""
        from app.plugins.registries.window_services import get_window_services

        reg = get_window_services()
        reg.register("demo.service", object(), plugin_name="demo_plugin", window_id="w-test")
        PluginHostService._release_plugin_host_resources("demo_plugin")
        assert reg.get("demo.service", "w-test") is None

# ── 5. ui 目录消失时先卸载 ──────────────────────────────────────

class TestUILoadWithoutUIDir:
    def test_missing_ui_init_triggers_unload(self, monkeypatch, tmp_path):
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        called: list = []
        monkeypatch.setattr(
            registry, "unload_plugin", lambda name: called.append(name) or True
        )
        assert registry.load_plugin("demo_plugin_no_ui", Path(tmp_path)) is False
        assert called == ["demo_plugin_no_ui"], "ui 目录消失时未卸载旧注册"

# ── 6. 去抖合并保留精准目标 ─────────────────────────────────────

class TestDebouncedReloadKeepsPreciseTargets:
    def test_precise_targets_survive_global_full(self, monkeypatch):
        svc = PluginHostService.__new__(PluginHostService)
        svc._pending_reload_targets = {("demo_plugin", "ui"), ("", "")}
        svc._last_reload_at = 0.0
        precise_calls: list = []
        full_calls: list = []
        monkeypatch.setattr(
            svc,
            "_do_single_reload",
            lambda plugin_name, component: precise_calls.append((plugin_name, component)) or {"ui": True},
        )
        monkeypatch.setattr(
            svc, "reload_plugin_subsystems", lambda *a, **k: full_calls.append(1) or {"commands": True}
        )
        monkeypatch.setattr(svc, "emit_plugin_changed", lambda *a, **k: None)
        monkeypatch.setattr(svc, "_rebuild_watcher_prefixes", lambda: None)

        svc._do_debounced_reload()

        assert ("demo_plugin", "ui") in precise_calls, "全量目标把精准目标整批丢弃了"
        assert full_calls, "全量目标本身仍需执行"
