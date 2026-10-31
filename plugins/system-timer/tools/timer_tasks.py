# -*- coding: utf-8 -*-
"""system-timer 定时任务工具：create / list / cancel。

调度器单例由 ui 组件（register_ui）在主线程创建；本模块只经
sys.modules["ui_plugin_system_timer"] 解析单例引用（tools 与 ui 是两个
独立加载的模块，唯一共享面）。impl 跑在 worker 线程，只允许加锁写任务表，
严禁触碰任何 Qt 对象（QTimer 属 scheduler.start/stop，仅主线程调用）。
"""

from typing import Any, Optional

from app.plugins.sdk import ToolResult

# ui 组件在宿主里的注册模块名（ui_plugin_registry.load_plugin 命名规则）
_UI_MODULE = "ui_plugin_system_timer"

_TIMER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "timer",
        "description": (
            "定时任务管理：创建到期后自动向当前会话发送触发消息的定时任务"
            "（触发消息会开启一轮新对话）。一次性任务传 delay_seconds，"
            "循环任务传 interval_minutes；任务仅存内存，应用重启后失效。"
            "到期触发要求会话窗口存活：会话切走后一次性任务按错过终结，循环任务跳过该轮继续。"
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
                    "description": "create（一次性）：延迟秒数，触发一次后任务终结",
                },
                "interval_minutes": {"type": "integer", "description": "create（循环）：触发间隔分钟数"},
                "task_id": {"type": "string", "description": "cancel：任务 ID（创建时返回）"},
                "all": {"type": "boolean", "description": "cancel：true 时取消当前会话的全部任务"},
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
    except TypeError, ValueError:
        return None
    return num if num > 0 else None


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

    delay_val = _as_positive_int(kwargs.get("delay_seconds"))
    interval_val = _as_positive_int(kwargs.get("interval_minutes"))
    if delay_val is not None and interval_val is not None:
        return ToolResult(False, error="delay_seconds 与 interval_minutes 只能二选一")
    if delay_val is None and interval_val is None:
        return ToolResult(
            False,
            error="必须提供触发时机：一次性任务用 delay_seconds，循环任务用 interval_minutes",
        )

    task = scheduler.add_task(
        session_id=session_id,
        prompt=prompt,
        delay_seconds=delay_val,
        interval_minutes=interval_val,
    )
    kind = "循环" if task["type"] == "loop" else "一次性"
    when = (
        f"{task['next_fire_seconds'] / 60:.1f} 分钟后首次触发"
        if task["type"] == "loop"
        else f"{task['next_fire_seconds']:.0f} 秒后触发"
    )
    return ToolResult(
        True,
        content=(f"定时任务已创建（ID: {task['id']}）：{kind}任务，{when}。到期后将向本会话自动发送：{prompt[:80]}"),
        data={"task": task},
    )


def _list(scheduler, tool_ctx) -> ToolResult:
    session_id = _session_id_of(tool_ctx)
    tasks = [t for t in scheduler.list_tasks() if not session_id or t["session_id"] == session_id]
    if not tasks:
        return ToolResult(True, content="当前会话没有定时任务")
    lines = [f"共 {len(tasks)} 个定时任务："]
    for t in tasks:
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
