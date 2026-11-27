# -*- coding: utf-8 -*-
"""临时探针（本会话自建，验证完删除）：历史首屏一次揭示的逐帧实测 + 切换/短会话。"""

from __future__ import annotations

import time

import pytest
from PyQt5.QtCore import QPoint, QRect

pytestmark = pytest.mark.ui

LONG_TEXT = "这是历史会话的填充正文，用于撑起卡片高度并触发懒渲染与滚动补偿。" * 12
SHORT_TEXT = "短回复，用于构造首屏能一屏容纳的历史。"


def _messages(rounds: int, long_text: bool = True) -> list[dict]:
    answer = LONG_TEXT if long_text else SHORT_TEXT
    msgs = []
    for i in range(rounds):
        msgs.append({"role": "user", "content": f"第 {i + 1} 个问题：请说明当前实现。"})
        msgs.append({"role": "assistant", "content": f"第 {i + 1} 个回答：\n\n{answer}", "model_name": "probe-model"})
    return msgs


def _cards(mw):
    from app.widgets.message_card import MessageCard

    viewport = mw.chat_scroll_area.viewport()
    vp_rect = viewport.rect()
    out = {}
    for i in range(mw.chat_layout.count()):
        item = mw.chat_layout.itemAt(i)
        widget = item.widget() if item else None
        if not isinstance(widget, MessageCard):
            continue
        try:
            pos = widget.mapTo(viewport, QPoint(0, 0))
            inter = QRect(pos, widget.size()).intersected(vp_rect)
            hit = viewport.childAt(inter.center()) if not inter.isEmpty() else None
            in_view = widget.isVisible() and hit is not None and (hit is widget or widget.isAncestorOf(hit))
            settled = bool(widget.is_layout_height_settled())
        except RuntimeError:
            continue
        out[getattr(widget, "_message_index", -1)] = {
            "idx": getattr(widget, "_message_index", -1),
            "role": getattr(widget, "role", "?"),
            "vp_y": pos.y(),
            "h": widget.height(),
            "in_view": in_view,
            "rendered": bool(getattr(widget, "_lazy_rendered", False)),
            "settled": settled,
        }
    return out


def _reset_empty(mw, qtbot):
    sm = mw.session_manager
    empty = sm.create_new_session()
    if hasattr(sm, "set_current_session"):
        sm.set_current_session(empty)
    mw._display_current_session()
    qtbot.wait(250)


def _seed(mw, sid, messages):
    mw.history_manager.save_session(messages=messages, title=sid, session_id=sid, project="探针")
    mw.history_manager.flush()
    rec = mw.history_manager.get_session_by_session_id(sid)
    assert rec and rec.get("messages")
    return rec


def test_probe_long_session(ui_app, qtbot):
    """长会话：揭示时长 / 揭示时是否贴底 / 揭示后视口内卡片是否跳动 / 骨架帧数。"""
    mw, tm = ui_app["chat_window"], ui_app["tm"]
    tm.showNormal()
    tm.resize(1120, 760)
    qtbot.wait(300)
    _reset_empty(mw, qtbot)
    record = _seed(mw, "probe-long", _messages(24))

    bar = mw.chat_scroll_area.verticalScrollBar()
    t0 = time.monotonic()
    mw._load_session_from_record(record)
    gate = mw.__dict__.get("_history_first_paint_gate")

    prev_ys, reveal_t, skeleton_streak, worst = {}, None, 0, 0.0
    for i in range(400):
        qtbot.wait(16)
        t_ms = (time.monotonic() - t0) * 1000
        waiting = bool(getattr(gate, "is_waiting", False))
        cards = _cards(mw)
        in_view = [c for c in cards.values() if c["in_view"]]
        skeletons = [c["idx"] for c in in_view if c["role"] in ("assistant", "tool") and not c["rendered"]]
        if len(skeletons) >= 2:
            skeleton_streak += 1
        if reveal_t is None and not waiting:
            reveal_t = t_ms
        cur = {c["idx"]: c["vp_y"] for c in in_view if c["rendered"] and c["settled"]}
        if reveal_t is not None:
            for idx, y in cur.items():
                if idx in prev_ys:
                    worst = max(worst, abs(y - prev_ys[idx]))
            prev_ys = cur
        idle = (
            not waiting
            and len(mw._pending_lazy_cards) == 0
            and not mw._loading_session
            and bar.maximum() - bar.value() <= 2
            and any(c["rendered"] and c["settled"] for c in in_view)
        )
        if idle and i > 3:
            break
    print(
        f"[probe-long] reveal_t={reveal_t:.0f}ms frames={i + 1} "
        f"skeleton_streak={skeleton_streak} worst_jump={worst:.0f}px "
        f"bottom_gap={bar.maximum() - bar.value()}"
    )
    assert reveal_t is not None
    assert skeleton_streak == 0, "遮罩期间不得被采样到成排骨架"
    assert worst <= 4, f"揭示后可见卡片跳动 {worst}px"


def test_probe_short_session(ui_app, qtbot):
    """短会话：应很快揭示且不出现骨架/跳动。"""
    mw, tm = ui_app["chat_window"], ui_app["tm"]
    tm.showNormal()
    tm.resize(1120, 760)
    qtbot.wait(300)
    _reset_empty(mw, qtbot)
    record = _seed(mw, "probe-short", _messages(6, long_text=False))

    bar = mw.chat_scroll_area.verticalScrollBar()
    t0 = time.monotonic()

    import traceback

    _orig_begin = mw._begin_history_first_paint
    _orig_cancel = mw._cancel_history_first_paint

    def _begin(sid):
        print(f"[trace] begin {sid} @ {[(f.name, f.lineno) for f in traceback.extract_stack()[-4:-1]]}")
        return _orig_begin(sid)

    def _cancel():
        print(f"[trace] cancel @ {[(f.name, f.lineno) for f in traceback.extract_stack()[-4:-1]]}")
        return _orig_cancel()

    mw._begin_history_first_paint = _begin
    mw._cancel_history_first_paint = _cancel
    mw._load_session_from_record(record)
    gate = mw.__dict__.get("_history_first_paint_gate")

    reveal_t, skeleton_streak, prev_ys, worst = None, 0, {}, 0.0
    for i in range(200):
        qtbot.wait(16)
        t_ms = (time.monotonic() - t0) * 1000
        waiting = bool(getattr(gate, "is_waiting", False))
        print(
            f"[f{i:03d}] t={t_ms:.0f} wait={int(waiting)} cov={int(gate.cover_visible())} "
            f"lazy={len(mw._pending_lazy_cards)} load={int(mw._loading_session)} "
            f"state={mw._history_first_paint_state()}"
        )
        cards = _cards(mw)
        in_view = [c for c in cards.values() if c["in_view"]]
        if len([c for c in in_view if c["role"] in ("assistant", "tool") and not c["rendered"]]) >= 2:
            skeleton_streak += 1
        if reveal_t is None and not waiting:
            reveal_t = t_ms
        cur = {c["idx"]: c["vp_y"] for c in in_view if c["rendered"] and c["settled"]}
        if reveal_t is not None:
            for idx, y in cur.items():
                if idx in prev_ys:
                    worst = max(worst, abs(y - prev_ys[idx]))
            prev_ys = cur
        idle = (
            not waiting
            and len(mw._pending_lazy_cards) == 0
            and not mw._loading_session
            and bar.maximum() - bar.value() <= 2
            and any(c["rendered"] and c["settled"] for c in in_view)
        )
        if idle and i > 3:
            break
    print(
        f"[probe-short] reveal_t={reveal_t:.0f}ms frames={i + 1} "
        f"skeleton_streak={skeleton_streak} worst_jump={worst:.0f}px "
        f"bottom_gap={bar.maximum() - bar.value()}"
    )
    assert reveal_t is not None
    assert skeleton_streak == 0
    assert worst <= 4


def test_probe_switch_cancels(ui_app, qtbot):
    """等待期连续切会话：遮罩必须立刻消失，且旧等待不会揭示新会话。"""
    mw, tm = ui_app["chat_window"], ui_app["tm"]
    tm.showNormal()
    tm.resize(1120, 760)
    qtbot.wait(300)
    _reset_empty(mw, qtbot)
    long_rec = _seed(mw, "probe-switch", _messages(24))
    short_rec = _seed(mw, "probe-switch-2", _messages(4, long_text=False))

    mw._load_session_from_record(long_rec)
    gate = mw.__dict__.get("_history_first_paint_gate")
    qtbot.wait(80)
    assert gate is not None and gate.is_waiting, "长会话加载中应在等待（遮罩期）"
    assert gate.cover_visible()

    mw._load_session_from_record(short_rec)  # 等待期内切会话
    qtbot.wait(60)
    assert gate.session_id == "probe-switch-2", "旧等待应作废，新会话重新上罩"
    print("[probe-switch] after switch waiting=", gate.is_waiting, "session=", gate.session_id)

    # 等最终揭示
    bar = mw.chat_scroll_area.verticalScrollBar()
    for _ in range(200):
        qtbot.wait(16)
        if not gate.is_waiting and bar.maximum() - bar.value() <= 2:
            break
    assert not gate.cover_visible(), "切换后最终必须撤罩，不能永久停在提示页"
    cards = _cards(mw)
    visible = [c for c in cards.values() if c["in_view"]]
    print(f"[probe-switch] final covered={gate.cover_visible()} visible={[(c['idx'], c['role']) for c in visible]}")
    assert visible, "撤罩后视口内必须有卡片"
