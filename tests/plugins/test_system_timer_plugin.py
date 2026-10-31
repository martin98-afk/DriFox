# -*- coding: utf-8 -*-
"""system-timer 插件测试：register 模板 / manifest 声明 / 工具 impl 直测 / 真加载（无 Qt）"""

import importlib.util
import json
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]


def _load_tool():
    p = _ROOT / "plugins" / "system-timer" / "tools" / "timer_tasks.py"
    spec = importlib.util.spec_from_file_location("timer_tool_under_test", p)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_scheduler_class():
    """直载 scheduler.py（纯逻辑不碰 Qt：QTimer 只在 start() 创建）"""
    p = _ROOT / "plugins" / "system-timer" / "ui" / "scheduler.py"
    spec = importlib.util.spec_from_file_location("timer_scheduler_under_test", p)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TimerScheduler


class _FakeScheduler:
    """调度器替身：与 TimerScheduler 任务表操作同形状（无 Qt）。"""

    def __init__(self):
        self.added = []
        self.cancelled = []
        self.tasks = []

    def add_task(self, session_id, prompt, delay_seconds=None, interval_minutes=None):
        task = {
            "id": "t0001",
            "session_id": session_id,
            "prompt": prompt,
            "type": "loop" if (interval_minutes or 0) > 0 else "once",
            "next_fire_seconds": float(delay_seconds or (interval_minutes or 0) * 60),
        }
        self.added.append(task)
        return task

    def list_tasks(self):
        return list(self.tasks)

    def cancel(self, task_id="", all_tasks=False, session_id=""):
        self.cancelled.append({"task_id": task_id, "all_tasks": all_tasks, "session_id": session_id})
        if all_tasks:
            return len(self.tasks)
        return 1 if task_id == "t0001" else 0


_CTX = {"session_id": "s1"}


def test_register_single_timer_tool():
    mod = _load_tool()
    registered = []

    class _Registry:
        def register(self, name, schema, **meta):
            registered.append((name, meta))

    mod.register(_Registry())
    assert [name for name, _meta in registered] == ["timer"]
    meta = registered[0][1]
    assert meta["danger"] == "safe"
    assert meta["icon"] == "clock"
    assert meta["cn_name"] == "定时任务"
    assert meta["group"] == "自动化"
    assert meta["description"]
    assert meta["aliases"]


def test_manifest_declares_session_send_and_components():
    """session.send 漏声明 → 触发时 pool 里没有该键，到期触发静默失败"""
    manifest = json.loads(
        (_ROOT / "plugins" / "system-timer" / ".drifox-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["api_version"] == 2
    assert "session.send" in manifest["capabilities"]
    assert manifest["components"] == {"tools": True, "ui": True}
    assert manifest["type"] == "system"


def test_create_once_records_session_id(monkeypatch):
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(_CTX, action="create", prompt="提醒我喝水", delay_seconds=90)
    assert res.success is True
    assert len(fake.added) == 1
    assert fake.added[0]["session_id"] == "s1"
    assert fake.added[0]["type"] == "once"
    assert "t0001" in str(res.content)


def test_create_loop_with_interval(monkeypatch):
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(_CTX, action="create", prompt="整点汇报", interval_minutes=30)
    assert res.success is True
    assert fake.added[0]["type"] == "loop"


def test_create_rejects_bad_args(monkeypatch):
    """时机缺失 / 双给 / 非正数 / prompt 空 → 全部拒绝"""
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    assert mod._timer_impl(_CTX, action="create", prompt="x").success is False
    assert mod._timer_impl(_CTX, action="create", prompt="  ").success is False
    assert mod._timer_impl(_CTX, action="create", prompt="x", delay_seconds=10, interval_minutes=5).success is False
    assert mod._timer_impl(_CTX, action="create", prompt="x", delay_seconds=0).success is False


def test_create_requires_session_id(monkeypatch):
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    res = mod._timer_impl({}, action="create", prompt="x", delay_seconds=10)
    assert res.success is False
    assert "session_id" in (res.error or "")


def test_scheduler_not_ready(monkeypatch):
    """ui 组件未加载（调度器单例不存在）必须显式报错，不能静默假成功"""
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: None)
    res = mod._timer_impl(_CTX, action="create", prompt="x", delay_seconds=10)
    assert res.success is False


def test_list_scopes_to_current_session(monkeypatch):
    mod = _load_tool()
    fake = _FakeScheduler()
    fake.tasks = [
        {"id": "a", "session_id": "s1", "prompt": "mine", "type": "once", "next_fire_seconds": 5.0},
        {"id": "b", "session_id": "s2", "prompt": "other", "type": "loop", "next_fire_seconds": 60.0},
    ]
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(_CTX, action="list")
    assert res.success is True
    assert "mine" in str(res.content) and "other" not in str(res.content)


def test_cancel_by_id_and_all(monkeypatch):
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    assert mod._timer_impl(_CTX, action="cancel", task_id="t0001").success is True
    assert mod._timer_impl(_CTX, action="cancel", all=True).success is True
    assert fake.cancelled[0] == {"task_id": "t0001", "all_tasks": False, "session_id": "s1"}
    assert fake.cancelled[1]["all_tasks"] is True
    assert fake.cancelled[1]["session_id"] == "s1"
    # task_id 与 all 都不给 → 报错
    assert mod._timer_impl(_CTX, action="cancel").success is False


def test_unknown_action_rejected(monkeypatch):
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    res = mod._timer_impl(_CTX, action="pause")
    assert res.success is False


def test_manifest_validate_clean():
    """validate_manifest 宽容校验零告警（字段名/类型全部合规）"""
    from app.plugins.contracts.manifest_schema import validate_manifest

    raw = json.loads(
        (_ROOT / "plugins" / "system-timer" / ".drifox-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    _out, warnings = validate_manifest(raw, source="system-timer", fallback_name="system-timer")
    assert warnings == []


def test_list_snapshot_no_internal_fields():
    """真调度器快照只含白名单字段，不泄内部状态（fire_at/firing/interval_seconds/锁）"""
    sched = _load_scheduler_class()()
    sched.add_task("s1", "提醒", delay_seconds=30)
    sched.add_task("s1", "循环", interval_minutes=2)
    snaps = sched.list_tasks()
    assert len(snaps) == 2
    allowed = {"id", "session_id", "prompt", "type", "next_fire_seconds"}
    for snap in snaps:
        assert set(snap.keys()) == allowed, f"快照字段泄漏：{set(snap.keys())}"
    loop = next(s for s in snaps if s["type"] == "loop")
    assert 119.0 <= loop["next_fire_seconds"] <= 120.0


def test_cancel_missing_reports_error(monkeypatch):
    """取消不存在的任务：工具层报错不假成功（fake 对非 t0001 返回 0）"""
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    res = mod._timer_impl(_CTX, action="cancel", task_id="ghost")
    assert res.success is False
    assert "ghost" in (res.error or "")


def test_real_load_registers_timer_tool(plugin_enabled):
    """真加载链：plugin_enabled 白名单 + load_plugin_tools 扫真实 plugins/ 根

    验证 manifest components/门禁/loader/register 全链路，"timer" 真实进注册表。
    """
    from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
    from app.tools.registry import ToolRegistry

    ToolRegistry.reset_instance()
    restore = plugin_enabled("system-timer")
    try:
        reg = ToolRegistry.get_instance()
        loaded = load_plugin_tools(registry=reg, plugin_roots=[_ROOT / "plugins"])
        assert "timer" in loaded.get("system-timer", set()), f"system-timer 未加载：{loaded.get('system-timer')}"
        entry = reg.get("timer")
        assert entry is not None
        assert entry.source == "plugin:system-timer"
        assert entry.danger == "safe"
        assert entry.cn_name == "定时任务"
    finally:
        restore()
        ToolRegistry.reset_instance()
