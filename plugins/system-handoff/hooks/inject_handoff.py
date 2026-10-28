# -*- coding: utf-8 -*-
"""SessionStart Hook — 注入交接文档

数据源：`<app_data>/plugin_data/system-handoff/pending.json`（由 update_handoff
写入）。读到即删除，避免同一份交接重复注入；超过 TTL 视为陈旧，直接丢弃。

为什么不用 state.kv：SessionStart 触发时机常在窗口创建期，此时
`caps_for("state.kv")` 解析活跃窗口拿不到 backend.tool_executor，窗口级 KV
整键缺失 → 交接注入静默失败（实测全部失败）。文件通道不依赖 host 解析，
注入端与工具端用同一个 `storage.data_dir` 能力拼路径，天然一致。

前缀语义参考 Codex CLI 的 summary_prefix：明确告诉新模型「上一任留下了交接，
已完成的工作不必重做」，避免它把交接当成用户的新需求重新分析一遍。
"""

import json
import time
from pathlib import Path

_PREFIX = """另一个语言模型此前在处理同一个任务，留下了这份交接文档。它使用的工具与你共享，
已完成的工作不需要重做。基于下面的交接继续推进；交接里写明的用户偏好与约束同样对你有效。
从「剩余任务与下一步」的第一项开始执行，不要复述交接内容；交接里没有可执行任务时，
直接说明情况并停下等用户指示。

"""

_TTL_S = 600


def _pending_path():
    """待注入交接文件；storage.data_dir 不可用时返回 None"""
    from app.plugins.sdk import caps_for

    getter = caps_for("storage.data_dir")
    if getter is None:
        return None
    try:
        base = getter() if callable(getter) else getter
    except Exception:
        return None
    if not base:
        return None
    return Path(str(base)) / "plugin_data" / "system-handoff" / "pending.json"


def _clear_active():
    """清防重入标记（auto_compact_trigger 写入，拿不到 state.kv 就跳过）"""
    try:
        from app.plugins.sdk import caps_for

        kv = caps_for("state.kv")
        if kv:
            kv["delete"]("handoff_active")
    except Exception:
        pass


def hook(event: str, context: dict) -> str:
    """输出交接文档（前缀 + 正文）；无待注入交接时返回空字符串"""
    from loguru import logger

    path = _pending_path()
    if path is None:
        logger.debug("[handoff] SessionStart 取不到 storage.data_dir，跳过交接注入")
        return ""
    try:
        if not path.is_file():
            return ""
        pending = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"[handoff] 读取待注入交接失败，跳过: {e}")
        return ""
    if not isinstance(pending, dict) or not pending:
        return ""

    # 读后即清：同一份交接只注入一次
    try:
        path.unlink()
    except Exception:
        pass
    _clear_active()

    try:
        if time.time() - float(pending.get("ts") or 0) > _TTL_S:
            logger.info("[handoff] 待注入交接已过期，丢弃")
            return ""
    except Exception:
        return ""

    content = str(pending.get("content") or "").strip()
    if not content:
        return ""

    logger.info(f"[handoff] SessionStart 注入交接文档 ({len(content)} 字)")

    source = str(pending.get("source_session_id") or "").strip()
    tail = f"\n\n（源会话 ID：{source}，可在历史面板回查原始对话）" if source else ""
    return _PREFIX + content + tail
