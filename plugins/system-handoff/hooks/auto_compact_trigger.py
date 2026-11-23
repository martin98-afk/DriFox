# -*- coding: utf-8 -*-
"""PostToolUse Hook — 自动交接触发

每次工具调用后检查上下文占用比例，超过阈值时向队列登记一条「补写交接」消息
（ephemeral，不写会话不渲染）。主程序在本轮结束时发出，模型据此写交接文档。

窗口定向：hook 内 `caps_for` 无 ctx，host 会回落到「用户此刻选中的窗口」。
本 hook 的 session.send 必须显式带 `window_id`（worker 在 hook context 里
注入），否则后台窗口超阈值会把补写提示发进前台窗口——用户看到的是自己
没触发压缩的窗口突然开始写交接文档。

防重入：入队时在 `<app_data>/plugin_data/system-handoff/active/<key>.json`
写一条带 ts 的标记（key = session_id，缺失时退窗口 id）。补写期间、以及模型
没按指引调用 update_handoff 时，TTL 内不会重复入队（否则每轮工具调用都会刷）。
不用 state.kv：那是窗口级且依赖 host 解析，多窗口下会写到别的窗口去。

子智能体不参与交接：交接是会话级动作（写文档 + 切新会话），子智能体没有
自己的会话，触发了只会改坏父会话的交接文档。
"""

import json
import time
from pathlib import Path

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

def _active_dir() -> Path | None:
    """防重入标记目录；storage.data_dir 不可用时返回 None"""
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
    return Path(str(base)) / "plugin_data" / "system-handoff" / "active"

def _marker_key(context: dict) -> str:
    """防重入键：按会话隔离；会话 id 缺失时退窗口 id（同窗口多会话共享一条标记）"""
    sid = str((context or {}).get("session_id") or "").strip()
    if sid:
        key = sid
    else:
        wid = str((context or {}).get("window_id") or "").strip()
        key = f"window-{wid}" if wid else ""
    # 键要当文件名用：会话 id 理论上安全，但外部来源不做假设，非法字符一律替换
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in key)

def _is_active(base: Path | None, key: str) -> bool:
    if base is None or not key:
        return False
    path = base / f"{key}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = float((data or {}).get("ts") or 0)
    except Exception:
        return False
    if time.time() - ts < _ACTIVE_TTL_S:
        return True
    # 过期标记就地清掉，避免目录长期堆积
    try:
        path.unlink()
    except Exception:
        pass
    return False

def _mark_active(base: Path | None, key: str) -> None:
    if base is None or not key:
        return
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{key}.json").write_text(
            json.dumps({"ts": time.time(), "stage": "prompting"}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

def hook(event: str, context: dict, threshold: float = 0.60) -> str:
    """超过阈值时登记补写提示；始终返回空串或提示文本（不向工具结果追加内容）"""
    context = context or {}
    # 子智能体不参与：交接是会话级动作，触发只会污染父会话的交接文档
    if str(context.get("current_role") or "") == "subagent" or context.get("is_subagent_call"):
        return ""
    try:
        total = int(context.get("token_count") or 0)
        limit = int(context.get("token_limit") or 0)
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

    key = _marker_key(context)
    base = _active_dir()
    if _is_active(base, key):
        return ""  # 交接进行中（或补写提示已发），不重复触发
    _mark_active(base, key)

    wid = str(context.get("window_id") or "").strip()
    from app.plugins.sdk import caps_for

    send = caps_for("session.send")
    if send is not None and wid:
        # 队列里再留一条：本轮若提前结束，补写提示会作为独立消息发出。
        # 必须带 window_id：不带就落到活跃窗口（串窗根因）。
        try:
            send(_COMPACT_PROMPT, ephemeral=True, window_id=wid)
        except Exception:
            pass

    try:
        from loguru import logger

        logger.info(
            f"[handoff] 上下文 {ratio:.1%} 超阈值 {threshold:.0%}，已注入交接提示"
            f"（window={wid or '?'}）"
        )
    except Exception:
        pass

    # 关键：直接返回提示文本（add_output_to_context=true）→ 模型下一轮 API 立刻看到，
    # 不用等整轮结束。只靠入队的话，模型可能还要连续调十几个工具才轮到它。
    return _HINT.format(ratio=ratio, threshold=threshold)
