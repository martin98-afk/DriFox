# -*- coding: utf-8 -*-
"""system-timer 插件测试：register 模板 / manifest 声明 / 工具 impl 直测 / 真加载（无 Qt）"""

import importlib.util
import json
import os
import time
from datetime import datetime, timedelta
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

    def add_task(self, session_id, prompt, delay_seconds=None, interval_minutes=None, mode="queue"):
        task = {
            "id": "t0001",
            "session_id": session_id,
            "prompt": prompt,
            "type": "loop" if (interval_minutes or 0) > 0 else "once",
            "mode": mode,
            "next_fire_seconds": float(delay_seconds or (interval_minutes or 0) * 60),
        }
        self.added.append(task)
        return task

    def list_tasks(self):
        return list(self.tasks)

    def resolve_watch(self, watch, session_id):
        text = str(watch or "").strip()
        if not text:
            return "session", session_id, "当前会话", ""
        if text == "*":
            return "all", "", "所有窗口", ""
        if text.startswith("session:"):
            value = text[len("session:") :]
            return "session", value, f"会话 {value[:8]}", ""
        if text.startswith("window:"):
            value = text[len("window:") :]
            return "window", value, f"窗口 {value}", ""
        if text.startswith("agent:"):
            ident = text[len("agent:") :]
            if ident == "ghost":
                return "", "", "", f"未找到团队成员 {ident}（团队未组建或成员已退出）"
            return "window", "w-agent", f"成员 {ident.split('@', 1)[0]}", ""
        return "", "", "", f"无法解析 watch：{text}"

    def add_event_task(
        self,
        session_id,
        prompt,
        event,
        watch_kind,
        watch_value,
        watch_label="",
        mode="queue",
        repeat=False,
        cooldown_seconds=5,
        max_fires=100,
    ):
        task = {
            "id": "e0001",
            "session_id": session_id,
            "prompt": prompt,
            "type": "event",
            "trigger": "event",
            "event": event,
            "watch": watch_label,
            "repeat": repeat,
            "fires": 0,
            "cooldown_seconds": cooldown_seconds,
            "max_fires": max_fires,
        }
        self.added.append(task)
        return task

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


def test_create_with_fire_at_hhmm(monkeypatch):
    """纯 HH:MM：今天已过自动明天，恒能创建，回显墙钟时刻"""
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(_CTX, action="create", prompt="定点提醒", fire_at="15:00")
    assert res.success is True
    assert fake.added[0]["type"] == "once"
    assert 1 <= fake.added[0]["next_fire_seconds"] <= 86400
    assert "将于" in str(res.content)


def test_create_with_fire_at_full_timestamp(monkeypatch):
    """完整时间戳：延迟≈2小时，返回消息含目标时刻"""
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    target = datetime.now() + timedelta(hours=2)
    res = mod._timer_impl(_CTX, action="create", prompt="定点提醒", fire_at=target.strftime("%Y-%m-%d %H:%M"))
    assert res.success is True
    assert 7100 <= fake.added[0]["next_fire_seconds"] <= 7200
    assert target.strftime("%H:%M") in str(res.content)


def test_create_fire_at_conflicts_with_other_triggers(monkeypatch):
    """fire_at 与 delay_seconds / interval_minutes 三选一"""
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    r1 = mod._timer_impl(_CTX, action="create", prompt="x", fire_at="00:00", delay_seconds=10)
    r2 = mod._timer_impl(_CTX, action="create", prompt="x", fire_at="00:00", interval_minutes=5)
    assert r1.success is False and r2.success is False
    assert "三选一" in (r1.error or "")


def test_create_fire_at_past_full_timestamp_rejected(monkeypatch):
    """完整时间戳已过去 → 报错不静默"""
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    res = mod._timer_impl(_CTX, action="create", prompt="x", fire_at="2020-01-01 00:00")
    assert res.success is False
    assert "已过去" in (res.error or "")


def test_create_mode_passthrough(monkeypatch):
    """mode 透传到调度器；非法值拒绝；缺省回落 queue"""
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(_CTX, action="create", prompt="催收", delay_seconds=60, mode="hook")
    assert res.success is True
    assert fake.added[0]["mode"] == "hook"
    assert "静默注入" in str(res.content)
    bad = mod._timer_impl(_CTX, action="create", prompt="x", delay_seconds=60, mode="bogus")
    assert bad.success is False
    assert "非法 mode" in (bad.error or "")
    assert mod._timer_impl(_CTX, action="create", prompt="x", delay_seconds=60).success is True
    assert fake.added[-1]["mode"] == "queue"


def test_parse_fire_at_hhmm_rolls_to_next_day():
    """00:00 今天必已过 → 恒落到明天，延迟不超过 24h"""
    mod = _load_tool()
    delay, target, err = mod._parse_fire_at("00:00")
    assert err == "" and target is not None and delay is not None
    assert target.date() == (datetime.now() + timedelta(days=1)).date()
    assert 1 <= delay <= 86400


def test_parse_fire_at_unparseable():
    mod = _load_tool()
    delay, target, err = mod._parse_fire_at("25:00")
    assert delay is None and target is None
    assert "无法解析" in err


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
    allowed = {"id", "session_id", "prompt", "type", "mode", "next_fire_seconds", "trigger"}
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


# ═══════════════════════════════════════════════════════════════
# 事件任务：参数校验 / watch 解析 / list 展示
# ═══════════════════════════════════════════════════════════════


def test_create_event_requires_event_name(monkeypatch):
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    res = mod._timer_impl(_CTX, action="create", prompt="x", trigger="event")
    assert res.success is False
    assert "event" in (res.error or "")


def test_create_event_rejects_time_args(monkeypatch):
    """事件任务与时间参数互斥，混用直接拒绝（防语义含糊）"""
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    for name, kwargs in (
        ("delay_seconds", {"delay_seconds": 10}),
        ("interval_minutes", {"interval_minutes": 5}),
        ("fire_at", {"fire_at": "15:00"}),
    ):
        res = mod._timer_impl(
            _CTX, action="create", prompt="x", trigger="event", event="stream_finished", **kwargs
        )
        assert res.success is False
        assert name in (res.error or "")


def test_create_event_agent_watch(monkeypatch):
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(
        _CTX,
        action="create",
        prompt="成员答完提醒我",
        trigger="event",
        event="stream_finished",
        watch="agent:build",
    )
    assert res.success is True
    assert fake.added[0]["event"] == "stream_finished"
    assert fake.added[0]["watch"] == "成员 build"
    assert "事件任务已创建" in res.content


def test_create_event_watch_errors(monkeypatch):
    mod = _load_tool()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: _FakeScheduler())
    bad = mod._timer_impl(
        _CTX, action="create", prompt="x", trigger="event", event="stream_finished", watch="whatever"
    )
    assert bad.success is False
    assert "无法解析 watch" in (bad.error or "")
    ghost = mod._timer_impl(
        _CTX, action="create", prompt="x", trigger="event", event="stream_finished", watch="agent:ghost"
    )
    assert ghost.success is False
    assert "未找到团队成员" in (ghost.error or "")


def test_create_event_repeat_params(monkeypatch):
    mod = _load_tool()
    fake = _FakeScheduler()
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(
        _CTX,
        action="create",
        prompt="每次落盘都记一笔",
        trigger="event",
        event="messages_updated",
        repeat=True,
        cooldown_seconds=30,
        max_fires=5,
    )
    assert res.success is True
    assert fake.added[0]["repeat"] is True
    assert fake.added[0]["cooldown_seconds"] == 30
    assert fake.added[0]["max_fires"] == 5


def test_list_event_task_renders_event_fields(monkeypatch):
    mod = _load_tool()
    fake = _FakeScheduler()
    fake.tasks = [
        {
            "id": "e1",
            "session_id": "s1",
            "prompt": "成员答完提醒",
            "type": "event",
            "trigger": "event",
            "event": "stream_finished",
            "watch": "成员 build",
            "repeat": False,
            "fires": 0,
        }
    ]
    monkeypatch.setattr(mod, "_resolve_scheduler", lambda: fake)
    res = mod._timer_impl(_CTX, action="list")
    assert res.success is True
    assert "事件 stream_finished" in res.content
    assert "成员 build" in res.content
