# -*- coding: utf-8 -*-
"""历史会话首屏逐帧回归（真实 Qt/WebEngine 窗口，合成消息）。

覆盖两条真实链路：
- 短会话（6 轮）：从历史入口打开后，首屏应一次显示，不出现成排空白卡片；
- 长会话（24 轮 + 长回复）：首屏定位最新消息，屏外卡片后续落高不得造成可见跳动。

数据全部为合成消息，写入隔离的临时 DRIFOX_DATA_DIR（conftest 已隔离）。
采样口径：
- 逐帧记录聊天区滚动条、布局中每张消息卡的内容坐标/可见区坐标/渲染状态；
- 逐帧 grab 聊天视口作为人眼证据（PNG/GIF 存临时目录，不做像素断言）；
- 全程不使用 runJavaScript（遵循 tests/ui 的 S2 约束）。

运行： ``python -m pytest tests/ui/test_history_first_paint.py -v``
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from PyQt5.QtCore import QPoint, QRect

pytestmark = pytest.mark.ui

# 证据产物目录：默认系统临时目录，可用 DRIFOX_FIRST_PAINT_ARTIFACTS 覆盖
ARTIFACT_DIR = Path(
    os.environ.get("DRIFOX_FIRST_PAINT_ARTIFACTS") or (Path(tempfile.gettempdir()) / "drifox_first_paint")
)

LONG_TEXT = "这是历史会话的填充正文，用于撑起卡片高度并触发懒渲染与滚动补偿。" * 12
SHORT_TEXT = "短回复，用于构造首屏能一屏容纳的历史。"


def _make_messages(rounds: int, long_text: bool) -> list[dict]:
    """构造合成历史消息（user/assistant 交替，无真实用户数据）。"""
    answer = LONG_TEXT if long_text else SHORT_TEXT
    messages: list[dict] = []
    for i in range(rounds):
        messages.append({"role": "user", "content": f"第 {i + 1} 个问题：请说明当前实现。"})
        messages.append(
            {
                "role": "assistant",
                "content": f"第 {i + 1} 个回答：\n\n{answer}",
                "model_name": "ui-test-model",
            }
        )
    return messages


def _seed_history(mw, messages: list[dict], title: str) -> str:
    """把合成会话写入隔离的历史存储，并确认内存读链可命中。"""
    sid = f"ui-first-paint-{title}"
    mw.history_manager.save_session(
        messages=messages,
        title=title,
        session_id=sid,
        project="UI测试项目",
    )
    mw.history_manager.flush()
    assert mw.history_manager.get_session_by_session_id(sid), "合成历史会话未写入"
    return sid


def _reset_to_empty_session(mw) -> None:
    """回到空会话起始态，保证每次采样从同一基线出发。"""
    sm = mw.session_manager
    empty = sm.create_new_session()
    if hasattr(sm, "set_current_session"):
        sm.set_current_session(empty)
    mw._display_current_session()
    mw.chat_scroll_area.verticalScrollBar().setValue(0)


def _card_state(mw, viewport) -> list[dict]:
    """采样布局内全部消息卡（内容坐标 + 视口坐标 + 渲染状态）。"""
    from app.widgets.message_card import MessageCard

    content = mw.chat_scroll_area.widget()
    vp_rect = viewport.rect()
    cards: list[dict] = []
    layout = mw.chat_layout
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget() if item else None
        if not isinstance(widget, MessageCard):
            continue
        try:
            card_pos = widget.mapTo(viewport, QPoint(0, 0))
            intersection = QRect(card_pos, widget.size()).intersected(vp_rect)
            hit = viewport.childAt(intersection.center()) if not intersection.isEmpty() else None
            in_view = widget.isVisible() and hit is not None and (hit is widget or widget.isAncestorOf(hit))
            content_y = widget.mapTo(content, QPoint(0, 0)).y() if content is not None else widget.y()
        except RuntimeError:
            continue
        viewer = getattr(widget, "viewer", None)
        rendered = bool(getattr(widget, "_lazy_rendered", False)) and viewer is not None
        js_ready = bool(getattr(viewer, "_is_js_ready", True)) if viewer is not None else False
        cards.append(
            {
                "idx": getattr(widget, "_message_index", -1),
                "role": getattr(widget, "role", "?"),
                "content_y": content_y,
                "viewport_y": card_pos.y(),
                "height": widget.height(),
                "visible": widget.isVisible(),
                "in_view": in_view,
                "rendered": rendered,
                "js_ready": js_ready,
                # 未渲染的问答卡在视口内 = 用户看到的空白/骨架
                "skeleton": (
                    getattr(widget, "role", "") in ("assistant", "tool", "welcome")
                    and (not rendered or not js_ready)
                ),
            }
        )
    return cards


def _frame(mw, viewport, t_ms: float) -> dict:
    """单帧快照：滚动状态 + 全部卡片 + 视口内卡片摘要。"""
    bar = mw.chat_scroll_area.verticalScrollBar()
    cards = _card_state(mw, viewport)
    in_view = [c for c in cards if c["in_view"]]
    return {
        "t": round(t_ms, 1),
        "scroll": bar.value(),
        "maximum": bar.maximum(),
        "pending_lazy": len(mw._pending_lazy_cards),
        "loading": bool(mw._loading_session),
        "cards": cards,
        "view_ids": [c["idx"] for c in in_view],
        "view_skeletons": [c["idx"] for c in in_view if c["skeleton"]],
    }


def _grab_viewport(viewport) -> bytes | None:
    """视口截图（PNG bytes）；仅作人眼证据，失败不阻断。"""
    from PyQt5.QtCore import QBuffer, QIODevice

    try:
        pix = viewport.grab()
    except Exception:  # noqa: BLE001 — grab 在窗口析构竞态下可能失败
        return None
    if pix.isNull():
        return None
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pix.save(buf, "PNG")
    return bytes(buf.data())


def _save_evidence(name: str, frames: list[dict], images: list[tuple[str, bytes]]) -> None:
    """把逐帧指标与关键帧截图落盘，供人工对比首屏观感。"""
    import json

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / f"{name}.json").write_text(
        json.dumps(frames, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for tag, data in images:
        (ARTIFACT_DIR / f"{name}-{tag}.png").write_bytes(data)


def _skeleton_streak(frames: list[dict]) -> int:
    """首屏阶段「视口内同时出现 ≥2 张空白卡」连续帧数（铺满骨架的直接证据）。"""
    streak = longest = 0
    for f in frames:
        if len(f["view_skeletons"]) >= 2:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return longest


def _visible_jump(frames: list[dict], tag: str) -> tuple[int, float]:
    """已渲染卡片在视口内的最大内容坐标位移（排除首次出现/消失帧）。"""
    prev: dict[int, float] = {}
    worst = 0.0
    count = 0
    for f in frames:
        cur = {
            c["idx"]: c["viewport_y"]
            for c in f["cards"]
            if c["in_view"] and c["rendered"] and not c["skeleton"] and isinstance(c["idx"], int) and c["idx"] >= 0
        }
        for idx, y in cur.items():
            if idx in prev:
                delta = abs(y - prev[idx])
                if delta > worst:
                    worst = delta
                if delta > 4:
                    count += 1
        prev = cur
    return count, worst


def _open_history_and_sample(mw, qtbot, sid: str, max_frames: int = 240, *, welcome=False) -> tuple[list[dict], list]:
    """从历史入口打开会话，逐帧采样到画面静止（贴底 + 懒队列空 + 高度稳定）。"""
    import time

    viewport = mw.chat_scroll_area.viewport()
    record = mw.history_manager.get_session_by_session_id(sid)
    assert record and record.get("messages"), "历史会话读链未命中（内存/DB 均为空）"

    frames: list[dict] = []
    images: list[tuple[str, bytes]] = []
    # 打开前一帧：起始态基线
    frames.append(_frame(mw, viewport, 0.0))

    t0 = time.monotonic()
    if welcome:
        mw.handle_recommended_question(sid, "session")
    else:
        mw._load_session_from_record(record)

    still = 0
    for i in range(max_frames):
        qtbot.wait(16)
        t_ms = (time.monotonic() - t0) * 1000
        snap = _frame(mw, viewport, t_ms)
        frames.append(snap)
        # 关键帧截图：首帧、首屏撑开、静止
        if i in (0, 1, 2, 4, 8, 16, 32, 64) or i == max_frames - 1:
            data = _grab_viewport(viewport)
            if data:
                images.append((f"f{i:03d}", data))
        at_bottom = snap["maximum"] - snap["scroll"] <= 2
        idle = (
            snap["pending_lazy"] == 0
            and not snap["loading"]
            and not snap["view_skeletons"]
            and any(c["in_view"] and self_role(c) and c["rendered"] for c in snap["cards"])
            and at_bottom
        )
        still = still + 1 if idle else 0
        if still >= 12:
            break
    return frames, images


def _assert_first_paint(frames: list[dict], name: str, *, max_skeletons: int = 1) -> None:
    """首屏约定：不成排空白、不来回抖、落定贴底且视口有正文。"""
    assert len(frames) > 5, f"{name}: 采样帧不足 {len(frames)}"
    first_body = next(
        (i for i, f in enumerate(frames) if any(
            c["in_view"] and c["rendered"] and c["js_ready"] and self_role(c) for c in f["cards"]
        )),
        None,
    )
    streak = _skeleton_streak(frames)
    assert streak <= max_skeletons, (
        f"{name}: 首屏连续 {streak} 帧出现 ≥2 张空白卡片（铺骨架）；"
        f"帧摘要={[(f['t'], f['view_skeletons']) for f in frames[:40]]}"
    )
    jump_count, worst = _visible_jump(frames, name)
    assert jump_count == 0, f"{name}: 已显示卡片相对视口跳动 {jump_count} 次，最大 {worst:.1f}px"
    last = frames[-1]
    assert last["maximum"] - last["scroll"] <= 2, (
        f"{name}: 落定未贴底 scroll={last['scroll']} max={last['maximum']}"
    )
    assert last["view_ids"], f"{name}: 落定后视口内没有任何消息卡片"
    assert first_body is not None, f"{name}: 全程未出现已渲染正文"


def self_role(card: dict) -> bool:
    return card["role"] in ("assistant", "tool")


def _run_case(ui_app, qtbot, *, rounds: int, long_text: bool, name: str, welcome=False) -> None:
    tm, mw = ui_app["tm"], ui_app["chat_window"]
    tm.showNormal()
    tm.resize(1120, 760)
    qtbot.wait(300)

    _reset_to_empty_session(mw)
    qtbot.wait(300)
    sid = _seed_history(mw, _make_messages(rounds, long_text), name)
    frames, images = _open_history_and_sample(mw, qtbot, sid, welcome=welcome)
    _save_evidence(name, frames, images)
    _assert_first_paint(frames, name)


def test_first_paint_short_session(ui_app, qtbot):
    """短会话：6 轮问答应一次显示在最新消息处。"""
    _run_case(ui_app, qtbot, rounds=6, long_text=False, name="short")


def test_first_paint_long_session(ui_app, qtbot):
    """长会话：24 轮长回复，屏外卡片落高不得带动可见区跳动。"""
    _run_case(ui_app, qtbot, rounds=24, long_text=True, name="long")


def test_first_paint_welcome_recent_session(ui_app, qtbot):
    """欢迎卡片的最近会话经 session 动作按 ID 打开，不走历史列表入口。"""
    _run_case(ui_app, qtbot, rounds=6, long_text=False, name="welcome_recent", welcome=True)
