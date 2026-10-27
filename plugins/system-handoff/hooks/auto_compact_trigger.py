# -*- coding: utf-8 -*-
"""PostToolUse Hook — 自动交接触发

每次工具调用后检查上下文占用比例，超过阈值时向队列登记一条「补写交接」消息
（ephemeral，不写会话不渲染）。主程序在本轮结束时发出，模型据此写交接文档。

防重入：入队时写 `state.kv["handoff_active"]`（带 ts）。补写期间、以及模型
没按指引调用 update_handoff 时，TTL 内不会重复入队（否则每轮工具调用都会刷）。
"""

import time

_ACTIVE_TTL_S = 600

_COMPACT_PROMPT = (
    "请更新当前会话的交接文档，然后切到新会话继续。"
    "只调用一次 update_handoff 工具并传 switch: true，不要执行其他工具，"
    "也不要把交接内容当普通回复贴出来。\n\n"
    "文档六节：## 任务目标 / ## 已完成 / ## 关键上下文 / ## 未决问题 / "
    "## 剩余任务与下一步 / ## 用户偏好与约束。\n"
    "「已完成」每条带文件路径或函数名；「剩余任务与下一步」写成下一任可直接执行的步骤。"
)


def hook(event: str, context: dict, threshold: float = 0.60) -> str:
    """超过阈值时登记补写提示；始终返回空串（不向工具结果追加内容）"""
    try:
        total = int((context or {}).get("token_count") or 0)
        limit = int((context or {}).get("token_limit") or 0)
    except Exception:
        return ""
    if total <= 0 or limit <= 0:
        return ""
    if total / limit < threshold:
        return ""

    from app.plugins.sdk import caps_for

    kv = caps_for("state.kv")
    if kv:
        try:
            active = kv["get"]("handoff_active") or {}
            if time.time() - float(active.get("ts") or 0) < _ACTIVE_TTL_S:
                return ""  # 交接进行中（或补写提示已发），不重复触发
            kv["set"]("handoff_active", {"ts": time.time(), "stage": "prompting"})
        except Exception:
            pass

    send = caps_for("session.send")
    if send is None:
        return ""
    try:
        send(_COMPACT_PROMPT, ephemeral=True)
    except Exception:
        pass
    return ""
