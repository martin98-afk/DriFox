# -*- coding: utf-8 -*-
"""SessionStart Hook — 注入交接文档

数据源：窗口级 `state.kv["handoff_pending"]`（由 update_handoff 工具写入）。
读到即清除，避免同一份交接重复注入；超过 TTL 视为陈旧，直接丢弃。

前缀语义参考 Codex CLI 的 summary_prefix：明确告诉新模型「上一任留下了交接，
已完成的工作不必重做」，避免它把交接当成用户的新需求重新分析一遍。
"""

import time

_PREFIX = """另一个语言模型此前在处理同一个任务，留下了这份交接文档。它使用的工具与你共享，
已完成的工作不需要重做。基于下面的交接继续推进；交接里写明的用户偏好与约束同样对你有效。
从「剩余任务与下一步」的第一项开始执行，不要复述交接内容；交接里没有可执行任务时，
直接说明情况并停下等用户指示。

"""

_TTL_S = 600


def hook(event: str, context: dict) -> str:
    """输出交接文档（前缀 + 正文）；无待注入交接时返回空字符串"""
    from app.plugins.sdk import caps_for

    kv = caps_for("state.kv")
    if not kv:
        return ""
    try:
        pending = kv["get"]("handoff_pending") or {}
    except Exception:
        return ""
    if not pending:
        return ""

    # 读后即清：同一份交接只注入一次
    try:
        kv["delete"]("handoff_pending")
        kv["delete"]("handoff_active")
    except Exception:
        pass

    try:
        if time.time() - float(pending.get("ts") or 0) > _TTL_S:
            return ""
    except Exception:
        return ""

    content = str(pending.get("content") or "").strip()
    if not content:
        return ""

    source = str(pending.get("source_session_id") or "").strip()
    tail = f"\n\n（源会话 ID：{source}，可在历史面板回查原始对话）" if source else ""
    return _PREFIX + content + tail
