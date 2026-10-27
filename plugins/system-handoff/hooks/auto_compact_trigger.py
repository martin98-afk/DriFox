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

# 直接注入当前轮的工具结果上下文：模型下一轮 API 就能看到，不必等整轮结束
_HINT = (
    "\n\n【上下文交接】当前上下文已用 {ratio:.1%}，超过阈值 {threshold:.0%}。\n"
    "请立即调用 `update_handoff` 写入交接文档（默认为 switch=true，写完自动开新会话继续），"
    "不要再发起其他工具调用。交接写完后本会话会立刻切走。"
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
    ratio = total / limit
    if ratio < threshold:
        # 接近阈值时留一条痕迹，否则 hook 静默返回、无法判断它是否在工作
        if ratio >= threshold * 0.8:
            try:
                from loguru import logger

                logger.debug(f"[handoff] 上下文 {ratio:.1%}，未到阈值 {threshold:.0%}")
            except Exception:
                pass
        return ""

    from app.plugins.sdk import caps_for

    kv = caps_for("state.kv")
    if kv is None:
        try:
            from loguru import logger

            logger.warning("[handoff] state.kv 不可用，本轮无法登记交接")
        except Exception:
            pass
        return ""
    try:
        active = kv["get"]("handoff_active") or {}
        if time.time() - float(active.get("ts") or 0) < _ACTIVE_TTL_S:
            return ""  # 交接进行中（或补写提示已发），不重复触发
        kv["set"]("handoff_active", {"ts": time.time(), "stage": "prompting"})
    except Exception:
        pass

    send = caps_for("session.send")
    if send is not None:
        # 队列里再留一条：本轮若提前结束，补写提示会作为独立消息发出
        try:
            send(_COMPACT_PROMPT, ephemeral=True)
        except Exception:
            pass

    try:
        from loguru import logger

        logger.info(f"[handoff] 上下文 {ratio:.1%} 超阈值 {threshold:.0%}，已注入交接提示")
    except Exception:
        pass

    # 关键：直接返回提示文本（add_output_to_context=true）→ 模型下一轮 API 立刻看到，
    # 不用等整轮结束。只靠入队的话，模型可能还要连续调十几个工具才轮到它。
    return _HINT.format(ratio=ratio, threshold=threshold)
