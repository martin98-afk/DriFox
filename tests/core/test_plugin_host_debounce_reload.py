# -*- coding: utf-8 -*-
"""去抖窗口精准请求保真回归测试

背景（2026-11-01）：300ms 去抖窗口内的精准重载请求 (plugin, component) 被
_schedule_debounced_reload 丢弃，到期一律全量 reload_plugin_subsystems。
其 rescan 门禁（added/removed/changed）对系统插件内部文件变更恒空
（rescan 只追踪目录增删与用户覆盖系统场景），兜底 _detect_unloaded_components
又显式排除 agents/hooks/commands → 修改被静默跳过、永不生效
（实测：改 plugins/system-agents/agents/leader.md 热重载无效）。

修复：_schedule_debounced_reload 携带目标攒入 _pending_reload_targets，
_do_debounced_reload 到期逐个精准重载；仅空名目标保留全量语义。
本文件不依赖真实 PluginManager / Qt 事件循环（singleShot 被 monkeypatch）。
"""

import pytest

from app.core.services.plugin_host_service import PluginHostService

def _make_host() -> PluginHostService:
    """绕过 __init__（QObject/信号初始化）取裸实例，预置实例字典属性

    注意：PyQt5 未初始化 wrapper 上 getattr 缺失属性抛 RuntimeError
    （super-class __init__ was never called），getattr 默认值防御无效，
    被测方法触碰的实例属性必须预置进 __dict__。
    """
    host = PluginHostService.__new__(PluginHostService)
    host.__dict__["_reload_pending"] = False
    host.__dict__["_last_reload_at"] = 0.0
    host.__dict__["_pending_reload_targets"] = set()
    return host

def _patch_silent(monkeypatch):
    """静默掉 _do_debounced_reload 的副作用出口（广播/索引重建）"""
    monkeypatch.setattr(PluginHostService, "emit_plugin_changed", lambda self, result: None)
    monkeypatch.setattr(PluginHostService, "_rebuild_watcher_prefixes", lambda self: None)

class TestScheduleCollectsTargets:
    def test_precise_target_collected(self, monkeypatch):
        host = _make_host()
        fired = []
        monkeypatch.setattr(
            "app.core.services.plugin_host_service.QTimer.singleShot",
            lambda ms, fn: fired.append(ms),
        )
        host._schedule_debounced_reload("system-agents", "agents")
        assert host._pending_reload_targets == {("system-agents", "agents")}
        assert fired == [300]

    def test_duplicate_targets_dedup_and_single_shot(self, monkeypatch):
        host = _make_host()
        fired = []
        monkeypatch.setattr(
            "app.core.services.plugin_host_service.QTimer.singleShot",
            lambda ms, fn: fired.append(ms),
        )
        host._schedule_debounced_reload("p1", "agents")
        host._schedule_debounced_reload("p1", "agents")
        host._schedule_debounced_reload("p2", "commands")
        assert host._pending_reload_targets == {("p1", "agents"), ("p2", "commands")}
        assert fired == [300]  # 风暴合并语义保留：只计划一次

class TestDebouncedReloadDispatch:
    def test_precise_target_never_goes_full(self, monkeypatch):
        """核心回归：精准目标到期走 _do_single_reload，绝不降级全量 rescan 门禁"""
        host = _make_host()
        host._pending_reload_targets = {("system-agents", "agents")}
        calls = []

        def _forbid_full(self, force_full=False):
            raise AssertionError("精准目标存在时不应走全量 reload_plugin_subsystems")

        monkeypatch.setattr(PluginHostService, "reload_plugin_subsystems", _forbid_full)
        monkeypatch.setattr(
            PluginHostService,
            "_do_single_reload",
            lambda self, name, comp: calls.append((name, comp)) or {"agents": 1},
        )
        _patch_silent(monkeypatch)

        host._do_debounced_reload()
        assert calls == [("system-agents", "agents")]
        assert host._pending_reload_targets == set()  # 消费后清空
        assert host._reload_pending is False

    def test_empty_name_target_falls_back_to_full(self, monkeypatch):
        """空名目标（抑制窗口兜底/跨插件合并语义）保留全量路径"""
        host = _make_host()
        host._pending_reload_targets = {("", "")}
        full_calls = []
        monkeypatch.setattr(
            PluginHostService,
            "reload_plugin_subsystems",
            lambda self, force_full=False: full_calls.append(1) or {"agents": 0},
        )
        _patch_silent(monkeypatch)

        host._do_debounced_reload()
        assert full_calls == [1]

    def test_multiple_targets_executed_and_merged(self, monkeypatch):
        host = _make_host()
        host._pending_reload_targets = {("p1", "agents"), ("p2", "commands")}
        results = {("p1", "agents"): {"agents": 2}, ("p2", "commands"): {"commands": True}}
        monkeypatch.setattr(
            PluginHostService,
            "_do_single_reload",
            lambda self, n, c: results[(n, c)],
        )
        captured = []
        monkeypatch.setattr(
            PluginHostService, "emit_plugin_changed", lambda self, r: captured.append(r)
        )
        monkeypatch.setattr(PluginHostService, "_rebuild_watcher_prefixes", lambda self: None)

        host._do_debounced_reload()
        assert captured and captured[0].get("agents") == 2
        assert captured[0].get("commands") is True

    def test_empty_targets_falls_back_to_full(self, monkeypatch):
        """无目标（理论不该发生）兜底全量，不空转"""
        host = _make_host()
        full_calls = []
        monkeypatch.setattr(
            PluginHostService,
            "reload_plugin_subsystems",
            lambda self, force_full=False: full_calls.append(1) or {"agents": 0},
        )
        _patch_silent(monkeypatch)

        host._do_debounced_reload()
        assert full_calls == [1]
