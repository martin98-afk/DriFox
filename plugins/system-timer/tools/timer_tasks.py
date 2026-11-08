# -*- coding: utf-8 -*-
"""system-timer 定时任务工具：create / list / cancel。

调度器单例由 ui 组件（register_ui）在主线程创建；本模块只经
sys.modules["ui_plugin_system_timer"] 解析单例引用（tools 与 ui 是两个
独立加载的模块，唯一共享面）。impl 跑在 worker 线程，只允许加锁写任务表，
严禁触碰任何 Qt 对象（QTimer 属 scheduler.start/stop，仅主线程调用）。
"""

import math
from datetime import datetime, timedelta
from typing import Any, Optional

from app.plugins.sdk import ToolResult

# ui 组件在宿主里的注册模块名（ui_plugin_registry.load_plugin 命名规则）
_UI_MODULE = "ui_plugin_system_timer"

# 可订阅事件：与 ui/scheduler.py 的 EVENT_NAMES 保持一致（backend 真实转发集）
_EVENT_NAMES = (
    "stream_finished",
    "stream_started",
    "messages_updated",
    "error_occurred",
    "session_changed",
)

_TIMER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "timer",
        "description": (
            "定时/事件任务管理：到期或命中事件后自动向当前会话发触发消息(开启新一轮对话)。"
            "时间触发用 delay_seconds / fire_at / interval_minutes；"
            "事件触发用 trigger=event + event + watch(可监听指定窗口/会话/团队成员的回答结束)。"
            "任务仅存内存，重启失效。仅当前会话生效，切换会话即自动取消。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "cancel"],
                    "description": "create=创建任务；list=列出当前会话的任务；cancel=取消任务",
                },
                "prompt": {"type": "string", "description": "create 必填：到期后发给会话的触发消息"},
                "delay_seconds": {
                    "type": "integer",
                    "description": "create(一次性)：延迟秒数，触发一次后终结",
                },
                "fire_at": {
                    "type": "string",
                    "description": (
                        "create(一次性)：触发时刻，'YYYY-MM-DD HH:MM[:SS]'(已过去报错)"
                        "或纯 'HH:MM[:SS]'(今天已过转明天)"
                    ),
                },
                "interval_minutes": {"type": "integer", "description": "create（循环）：触发间隔分钟数"},
                "mode": {
                    "type": "string",
                    "enum": ["queue", "skip", "hook"],
                    "description": (
                        "create(可选)：触发方式。queue=流式中排队(默认)；"
                        "skip=流式中忽略；hook=流式中静默注入上下文(不打断)"
                    ),
                },
                "task_id": {"type": "string", "description": "cancel：任务 ID（创建时返回）"},
                "all": {"type": "boolean", "description": "cancel：true 时取消当前会话的全部任务"},
                "trigger": {
                    "type": "string",
                    "enum": ["time", "event"],
                    "description": "create(可选)：time=时间触发(默认)；event=事件触发",
                },
                "event": {
                    "type": "string",
                    "enum": [
                        "stream_finished",
                        "stream_started",
                        "messages_updated",
                        "error_occurred",
                        "session_changed",
                    ],
                    "description": (
                        "create(trigger=event 必填)：stream_finished=一轮回答结束；"
                        "stream_started=开始回答；messages_updated=消息落盘；"
                        "error_occurred=本轮出错；session_changed=窗口切换会话"
                    ),
                },
                "watch": {
                    "type": "string",
                    "description": (
                        "create(trigger=event 可选)：监听对象。留空=当前会话；"
                        "session:<id> / window:<id> / agent:<角色名>[@window_id] / *（所有窗口）"
                    ),
                },
                "repeat": {
                    "type": "boolean",
                    "description": "create(trigger=event 可选)：true=每次命中都触发；默认只触发一次",
                },
                "cooldown_seconds": {
                    "type": "integer",
                    "description": "create(trigger=event 可选)：repeat=true 时的最小触发间隔，默认 5 秒",
                },
                "max_fires": {
                    "type": "integer",
                    "description": "create(trigger=event 可选)：repeat=true 时的最大触发次数，默认 100，上限 1000",
                },
            },
            "required": ["action"],
        },
    },
}


def register(registry):
    registry.register(
        "timer",
        _TIMER_SCHEMA,
        impl=_timer_impl,
        danger="safe",
        icon="clock",
        cn_name="定时任务",
        group="自动化",
        description="定时任务管理：创建一次性/循环定时任务，到期后自动向当前会话发送触发消息",
        aliases=["Timer", "ScheduleTask", "schedule", "timer_task"],
    )


def _resolve_scheduler():
    """从 ui 组件模块取调度器单例（ui 未加载返回 None，由调用方报错）。"""
    import sys

    getter = getattr(sys.modules.get(_UI_MODULE), "get_scheduler", None)
    return getter() if callable(getter) else None


def _session_id_of(tool_ctx: Optional[dict]) -> str:
    return str((tool_ctx or {}).get("session_id") or "")


def _as_positive_int(value: Any) -> Optional[int]:
    """归一为正整数；无法解析或非正数返回 None（视同未提供）。"""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _parse_fire_at(value: Any) -> tuple[Optional[int], Optional[datetime], str]:
    """解析 fire_at 为「距现在的延迟秒数」与目标时刻。

    双格式（本地时间）：完整时间戳 YYYY-MM-DD HH:MM[:SS]（空格或 T 分隔），
    已过去即报错；纯 HH:MM[:SS] 取今天该时刻，已过自动顺延明天。
    返回 (delay_seconds, target, error)，未提供时三值均空，成功时 error 为空。
    """
    text = str(value or "").strip()
    if not text:
        return None, None, ""
    now = datetime.now()
    target: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            target = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if target is None:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                hhmm = datetime.strptime(text, fmt)
            except ValueError:
                continue
            target = now.replace(hour=hhmm.hour, minute=hhmm.minute, second=hhmm.second, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            break
    if target is None:
        return None, None, f"无法解析 fire_at：{text}，支持 'YYYY-MM-DD HH:MM[:SS]' 或 'HH:MM[:SS]'"
    if target <= now:
        return None, None, f"fire_at 指定的时刻 {target:%Y-%m-%d %H:%M:%S} 已过去"
    return max(1, math.ceil((target - now).total_seconds())), target, ""


def _timer_impl(tool_ctx, **kwargs) -> ToolResult:
    action = str(kwargs.get("action") or "").strip().lower()
    if action not in ("create", "list", "cancel"):
        return ToolResult(False, error=f"未知 action：{action or '(空)'}，可用值 create/list/cancel")

    scheduler = _resolve_scheduler()
    if scheduler is None:
        return ToolResult(False, error="定时调度器未就绪（ui 组件未加载或已停用）")

    if action == "create":
        return _create(scheduler, tool_ctx, kwargs)
    if action == "list":
        return _list(scheduler, tool_ctx)
    return _cancel(scheduler, tool_ctx, kwargs)


def _create(scheduler, tool_ctx, kwargs: dict) -> ToolResult:
    prompt = str(kwargs.get("prompt") or "").strip()
    if not prompt:
        return ToolResult(False, error="创建定时任务必须提供 prompt（到期后发送的触发消息）")
    session_id = _session_id_of(tool_ctx)
    if not session_id:
        return ToolResult(False, error="拿不到当前会话 session_id，无法创建定时任务")

    mode_val = str(kwargs.get("mode") or "queue").strip().lower()
    if mode_val not in ("queue", "skip", "hook"):
        return ToolResult(False, error=f"非法 mode：{mode_val}，可用值 queue/skip/hook")
    trigger = str(kwargs.get("trigger") or "time").strip().lower()
    if trigger not in ("time", "event"):
        return ToolResult(False, error=f"非法 trigger：{trigger}，可用值 time/event")
    if trigger == "event":
        return _create_event(scheduler, session_id, prompt, kwargs, mode_val)

    delay_val = _as_positive_int(kwargs.get("delay_seconds"))
    interval_val = _as_positive_int(kwargs.get("interval_minutes"))
    fire_delay, fire_target, fire_err = _parse_fire_at(kwargs.get("fire_at"))
    if fire_err:
        return ToolResult(False, error=fire_err)
    provided = [
        name
        for name, val in (("delay_seconds", delay_val), ("interval_minutes", interval_val), ("fire_at", fire_delay))
        if val is not None
    ]
    if len(provided) > 1:
        return ToolResult(False, error="delay_seconds / interval_minutes / fire_at 只能三选一")
    if not provided:
        return ToolResult(
            False,
            error="必须提供触发时机：一次性任务用 delay_seconds 或 fire_at，循环任务用 interval_minutes",
        )

    task = scheduler.add_task(
        session_id=session_id,
        prompt=prompt,
        delay_seconds=delay_val if delay_val is not None else fire_delay,
        interval_minutes=interval_val,
        mode=mode_val,
    )
    kind = "循环" if task["type"] == "loop" else "一次性"
    mode_note = {
        "skip": "，会话正忙时将忽略本次触发",
        "hook": "，会话正忙时静默注入当前对话上下文",
    }.get(mode_val, "")
    if task["type"] == "loop":
        when = f"{task['next_fire_seconds'] / 60:.1f} 分钟后首次触发"
    elif fire_target is not None:
        when = f"将于 {fire_target:%Y-%m-%d %H:%M:%S} 触发"
    else:
        when = f"{task['next_fire_seconds']:.0f} 秒后触发"
    return ToolResult(
        True,
        content=(
            f"定时任务已创建（ID: {task['id']}）：{kind}任务，{when}{mode_note}。"
            f"到期后将向本会话自动发送：{prompt[:80]}"
        ),
        data={"task": task},
    )


def _create_event(scheduler, session_id: str, prompt: str, kwargs: dict, mode_val: str) -> ToolResult:
    """事件任务分支：校验事件名与 watch，交给调度器登记。"""
    event = str(kwargs.get("event") or "").strip()
    if event not in _EVENT_NAMES:
        return ToolResult(
            False,
            error=f"trigger=event 需要 event 参数，可用值：{'/'.join(_EVENT_NAMES)}",
        )
    for name in ("delay_seconds", "interval_minutes", "fire_at"):
        if kwargs.get(name) not in (None, ""):
            return ToolResult(False, error=f"事件任务不能用 {name}，请去掉时间触发参数")

    resolve = getattr(scheduler, "resolve_watch", None)
    if not callable(resolve):
        return ToolResult(False, error="调度器不支持事件任务（组件版本不匹配，请重载插件）")
    watch_kind, watch_value, watch_label, watch_err = resolve(kwargs.get("watch"), session_id)
    if watch_err:
        return ToolResult(False, error=watch_err)

    repeat = bool(kwargs.get("repeat"))
    cooldown = _as_positive_int(kwargs.get("cooldown_seconds")) or 5
    max_fires = min(_as_positive_int(kwargs.get("max_fires")) or 100, 1000)
    task = scheduler.add_event_task(
        session_id=session_id,
        prompt=prompt,
        event=event,
        watch_kind=watch_kind,
        watch_value=watch_value,
        watch_label=watch_label,
        mode=mode_val,
        repeat=repeat,
        cooldown_seconds=cooldown,
        max_fires=max_fires,
    )
    life = f"每次命中触发，间隔≥{cooldown}秒，最多{max_fires}次" if repeat else "命中一次后自动结束"
    return ToolResult(
        True,
        content=(
            f"事件任务已创建（ID: {task['id']}）：监听 {watch_label} 的 {event}，{life}。"
            f"命中后将向本会话发送：{prompt[:80]}"
        ),
        data={"task": task},
    )


def _list(scheduler, tool_ctx) -> ToolResult:
    session_id = _session_id_of(tool_ctx)
    tasks = [t for t in scheduler.list_tasks() if not session_id or t["session_id"] == session_id]
    if not tasks:
        return ToolResult(True, content="当前会话没有定时任务")
    lines = [f"共 {len(tasks)} 个定时任务："]
    for t in tasks:
        if t.get("trigger") == "event":
            life = f"已触发 {t.get('fires', 0)} 次" if t.get("repeat") else "命中一次后结束"
            lines.append(
                f"- [{t['id']}] 事件 {t.get('event')}（监听 {t.get('watch') or '当前会话'}），{life}：{t['prompt'][:60]}"
            )
            continue
        kind = "循环" if t["type"] == "loop" else "一次性"
        when = (
            f"下次 {t['next_fire_seconds'] / 60:.1f} 分钟后"
            if t["type"] == "loop"
            else f"还有 {t['next_fire_seconds']:.0f} 秒"
        )
        lines.append(f"- [{t['id']}] {kind}，{when}：{t['prompt'][:60]}")
    return ToolResult(True, content="\n".join(lines), data={"tasks": tasks})


def _cancel(scheduler, tool_ctx, kwargs: dict) -> ToolResult:
    session_id = _session_id_of(tool_ctx)
    task_id = str(kwargs.get("task_id") or "").strip()
    cancel_all = bool(kwargs.get("all"))
    if not task_id and not cancel_all:
        return ToolResult(False, error="取消任务需要提供 task_id，或 all=true 取消当前会话全部任务")
    removed = scheduler.cancel(task_id=task_id, all_tasks=cancel_all, session_id=session_id)
    if cancel_all:
        return ToolResult(True, content=f"已取消当前会话的 {removed} 个定时任务")
    if removed:
        return ToolResult(True, content=f"定时任务 {task_id} 已取消")
    return ToolResult(False, error=f"未找到任务 {task_id}（可能已触发、已取消或不属于当前会话）")
