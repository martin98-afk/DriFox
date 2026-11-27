# -*- coding: utf-8 -*-
"""右侧工作台分隔线手动拖动：整卡画面取证（正文消失 / 几何抖动）。

采样通道用 ``card.grab(子矩形)``：实测与真实桌面合成像素一致（同帧正文计数相同），
但不受桌面遮挡与焦点干扰，窗口可留在屏幕外，不打扰用户桌面。

采样区取卡片左侧 340×380（左对齐卡，正文不随限宽居中漂移），同时记录：
- 正文像素数与包围盒 → 区分「正文消失」与「整体位移」；
- 平均色 → 整片底色突变（占位色/页面底色闪现）；
- 卡片宽高、聊天视口宽、居中留白、滚动条 → 几何/滚动条抖动；
- 生产侧调用点时序与耗时（只包测试侧实例方法，不改生产代码）。

场景（QTest 真实鼠标事件，按下后保持按键）：
- fast：125Hz 定时器驱动连续往返拖动（单步 3px、行程 ±180px）；
- slow_pause：慢拖 + 中途停顿 260ms（> 侧栏 120ms 静默阈值），看是否被误判松手；
- release：拖动后松手，采样 ~1.2s 静默期（异步 LayoutRequest 是否又把正文切占位）；
- preview_cycle：先走一次 hover 悬浮预览（frame 摘出 splitter 再回挂，Qt 重建 handle），
  随后拖动，验证新 handle 上的拖动保护还在；
- many：视口附近 9 张大卡片，拖拽期每拍都要同步多张卡宽度；
- capped：窗口拉宽到对话区限宽档位（standard=1000）生效，拖把手只改居中留白。

另有一条独立复现（`test_post_release_layout_resize_does_not_blank_cards`）：
松手 ~100ms 后 `_guard_workbench_relayout` 的保护被 `_sync_all_cards_width` 清掉，
此后对话区任意一次布局 resize 都会重新进 preview 分支，把正文换成占位。

运行：pytest tests/ui/test_workbench_drag_pixels.py -m ui -s
"""

import time

import numpy as np
import pytest
from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, QTimer, Qt
from PyQt5.QtGui import QImage, QMouseEvent
from PyQt5.QtWidgets import QApplication

_BODY_TEXT = "\n\n".join(["<span style='color:#f00883'>██████████████████████████████</span>"] * 10)


def _analyze(pix, width, height):
    """→ (正文像素数, 平均色 RGB, 正文包围盒 (row0,col0,row1,col1) 或 None)。"""
    img = pix.toImage().convertToFormat(QImage.Format_RGB32) if pix is not None else None
    if img is None or img.isNull():
        return 0, (0, 0, 0), None
    arr = np.frombuffer(img.constBits().asarray(img.byteCount()), dtype=np.uint8)
    arr = arr.reshape(height, width, 4).astype(np.int16)
    blue, green, red = arr[..., 0], arr[..., 1], arr[..., 2]
    mask = (red > 180) & (green < 70) & (blue > 75) & (blue < 180)
    count = int(mask.sum())
    rows, cols = np.nonzero(mask)
    bbox = (int(rows.min()), int(cols.min()), int(rows.max()), int(cols.max())) if count else None
    return count, (int(red.mean()), int(green.mean()), int(blue.mean())), bbox


def _prepare(ui_app, qtbot, width=1400):
    """建工作台展开态 + 一张带亮粉正文的助手卡，返回 (tm, mw, card, region)。"""
    from app.widgets.message_card import MessageCard

    tm, mw = ui_app["tm"], ui_app["chat_window"]
    tm.resize(width, 760)
    tm.set_workbench_visible(True, animate=False)
    qtbot.wait(600)
    card = MessageCard(role="assistant")
    mw.chat_layout.insertWidget(0, card)
    card.set_content(_BODY_TEXT)
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible(), timeout=15000)
    mw.chat_scroll_area.verticalScrollBar().setValue(0)
    mw._do_debounced_resize()
    qtbot.wait(500)
    off = card.viewer.mapTo(card, QPoint(0, 0))
    region = QRect(off, card.viewer.size())
    region.setWidth(min(region.width(), 340))
    region.setHeight(min(region.height(), 380))
    return tm, mw, card, region


@pytest.mark.ui
@pytest.mark.parametrize("mode", ["fast", "slow_pause", "release", "preview_cycle", "many", "capped"])
def test_right_handle_drag_keeps_card_painted(ui_app, qtbot, mode):
    gap = QApplication.primaryScreen().availableGeometry().width() - 40
    tm, mw, card, region = _prepare(ui_app, qtbot, min(2000, gap) if mode == "capped" else 1400)
    grab_w, grab_h = region.width(), region.height()

    if mode == "many":
        # 真实会话规模：视口上下各摆几张大卡片，拖拽期每拍都要同步多张卡宽度
        from app.widgets.message_card import MessageCard

        for _ in range(8):
            extra = MessageCard(role="assistant")
            mw.chat_layout.addWidget(extra)
            extra.set_content(_BODY_TEXT)
            extra.ensure_rendered()
        qtbot.waitUntil(
            lambda: all(
                getattr(w, "viewer", None) is not None and w.viewer._is_js_ready for w in mw.findChildren(MessageCard)
            ),
            timeout=60000,
        )
        mw.chat_scroll_area.verticalScrollBar().setValue(0)
        mw._do_debounced_resize()
        qtbot.wait(800)

    if mode == "preview_cycle":
        ctrl = tm._wb_preview_ctrl
        ctrl.on_button_hover(True)
        qtbot.wait(700)
        ctrl.on_button_hover(False)
        qtbot.wait(900)
        idx = tm._splitter.indexOf(tm._workbench_frame)
        assert tm._splitter_right_handle is tm._splitter.handle(idx), "预览回挂后 handle 未重新绑定"

    # ── 调用点时序记录（只包测试侧实例方法，不改生产代码）──
    t0 = time.monotonic()
    events = []

    def _wrap(obj, name, tag):
        orig = getattr(obj, name)

        def wrapper(*args, **kwargs):
            vals = tuple(a if isinstance(a, (bool, int, float)) else type(a).__name__ for a in args)
            t_start = time.monotonic()
            result = orig(*args, **kwargs)
            duration = round((time.monotonic() - t_start) * 1000, 1)
            events.append((round((t_start - t0) * 1000), tag, duration, vals))
            return result

        setattr(obj, name, wrapper)

    for obj, name, tag in (
        (mw, "_set_sidebar_drag_live", "drag_live"),
        (mw, "_set_cards_resize_preview_mode", "mw_preview"),
        (mw, "_do_debounced_resize", "debounced_resize"),
        (mw, "_sync_all_cards_width", "sync_all_width"),
        (mw, "_guard_workbench_relayout", "guard_relayout"),
        (tm, "_sync_chat_wrapper_width", "wrapper_width"),
        (tm, "_on_splitter_manually_moved", "splitter_moved"),
        (tm, "_on_splitter_idle", "splitter_idle"),
        (tm, "_start_wb_anim", "start_wb_anim"),
        (tm, "_on_sidebar_toggled", "sidebar_toggled"),
        (card, "set_resize_preview_mode", "card_preview"),
    ):
        _wrap(obj, name, tag)

    samples = []
    scrollbar = mw.chat_scroll_area.verticalScrollBar()

    def sample(tag):
        st = {
            "tag": tag,
            "t": round((time.monotonic() - t0) * 1000),
            "live": bool(getattr(mw, "_sidebar_drag_live", False)),
            "preview": bool(getattr(mw, "_resize_preview_active", False)),
            "card_preview": bool(getattr(card, "_resize_preview_mode", False)),
            "viewer": card.viewer.isVisible(),
            "placeholder": card.resize_placeholder.isVisible(),
            "card_w": card.width(),
            "card_h": card.height(),
            "vp_w": mw.chat_scroll_area.viewport().width(),
            "margin": tm._chat_wrapper_layout.contentsMargins().left(),
            "sb": scrollbar.isVisible(),
            "sbv": scrollbar.value(),
        }
        st["text"], st["mean"], st["bbox"] = _analyze(card.grab(region), grab_w, grab_h)
        samples.append(st)
        return st

    base = sample("base")
    assert base["text"] > 20, f"未取得可辨识正文像素：{base}"

    splitter = tm._splitter
    index = splitter.indexOf(tm._workbench_frame)
    handle = splitter.handle(index)
    assert handle is tm._splitter_right_handle
    origin = handle.mapToGlobal(handle.rect().center())

    def drag_to(dx):
        dest = origin + QPoint(dx, 0)
        move = QMouseEvent(
            QEvent.MouseMove,
            QPointF(handle.mapFromGlobal(dest)),
            QPointF(dest),
            Qt.NoButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        QApplication.sendEvent(handle, move)

    qtbot.mousePress(handle, Qt.LeftButton)
    try:
        if mode in ("fast", "many", "capped"):
            # 125Hz 定时器驱动往返：每 8ms 位移 3px，行程 ±180px
            legs_needed = 4 if mode in ("many", "capped") else 8
            st = {"dx": 0, "dir": -1, "legs": 0, "done": False}

            def tick():
                st["dx"] += st["dir"] * 3
                if abs(st["dx"]) >= 180:
                    st["dx"] = st["dir"] * 180
                    st["dir"] *= -1
                    st["legs"] += 1
                    if st["legs"] >= legs_needed:
                        st["done"] = True
                drag_to(st["dx"])

            timer = QTimer()
            timer.setInterval(8)
            timer.timeout.connect(tick)
            timer.start()
            while not st["done"]:
                qtbot.wait(4)
                sample(mode)
            timer.stop()
        elif mode == "slow_pause":
            for rnd, dx in enumerate((-30, -60, -60, -30, 0)):
                drag_to(dx)
                qtbot.wait(260)
                st = sample(f"slow{rnd}")
                assert st["live"], f"按住停顿被误判松手：{st}"
                assert not st["card_preview"] and not st["placeholder"], f"停顿期间切占位：{st}"
        else:  # release / preview_cycle
            for k in range(1, 5):
                drag_to(-40 * k)
                qtbot.wait(10)
                sample(f"pre{k}")
    finally:
        qtbot.mouseRelease(handle, Qt.LeftButton)

    for i in range(40):
        qtbot.wait(20)
        sample(f"post{i}")
    qtbot.wait(500)
    sample("final")

    # ── 判定 ──
    qt_bad = [s for s in samples if s["card_preview"] or s["placeholder"] or not s["viewer"]]
    text_zero = [s for s in samples[1:] if s["text"] == 0]
    mean_jump = [
        s
        for s in samples[1:]
        if max(abs(s["mean"][c] - base["mean"][c]) for c in range(3)) > 8 and s["text"] < base["text"] * 0.3
    ]
    sb_flips = sum(1 for a, b in zip(samples, samples[1:]) if a["sb"] != b["sb"])
    widths = [s["card_w"] for s in samples]
    width_flips = sum(1 for a, b, c in zip(widths, widths[1:], widths[2:]) if a == c != b)

    gaps = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
    durations = {}
    for _t, tag, dur, _vals in events:
        durations.setdefault(tag, []).append(dur)
    print(
        f"\n[{mode}] base={base}\n"
        f"[{mode}] 采样数={len(samples)} text={min(s['text'] for s in samples)}..{max(s['text'] for s in samples)} "
        f"卡片宽={min(widths)}..{max(widths)} 滚动条翻转={sb_flips} 宽度回摆={width_flips}\n"
        f"[{mode}] 采样间隔 max={max(gaps)}ms p50={sorted(gaps)[len(gaps) // 2]}ms\n"
        f"[{mode}] 调用耗时={{ {', '.join(f'{k}:({len(v)},{max(v)})' for k, v in durations.items())} }}\n"
        f"[{mode}] 调用点={events}"
    )

    assert not qt_bad, f"Qt 侧出现占位/隐藏：{qt_bad[:6]}"
    assert not text_zero, f"正文像素归零帧：{text_zero[:6]}"
    assert not mean_jump, f"整片底色突变帧：{mean_jump[:6]}"


@pytest.mark.ui
@pytest.mark.xfail(
    strict=False,
    reason="已复现：松手 ~100ms 后对话区任何一次布局 resize 都会重新切占位（宿主窗口尺寸没变也算）；修复后去掉 xfail",
)
def test_post_release_layout_resize_does_not_blank_cards(ui_app, qtbot):
    """松手后由布局排入的对话区 resize 不得把正文重新切成占位。

    真实链路：拖拽结束 → `_guard_workbench_relayout()` 只保护到
    `_sync_all_cards_width`（100ms）并把 `_workbench_relayout_host_size` 清空；
    之后对话区任何一次 resize（异步 LayoutRequest / 居中留白变化 / 滚动条级联）
    都会重新走 `resizeEvent` 的 preview 分支。本用例用一次直接的对话区宽度变化
    模拟这次异步布局（宿主窗口尺寸不变），检查卡片是否被切成占位。
    """
    tm, mw, card, region = _prepare(ui_app, qtbot)
    grab_w, grab_h = region.width(), region.height()

    preview_calls = []
    orig_preview = mw._set_cards_resize_preview_mode

    def spy_preview(enabled):
        preview_calls.append(bool(enabled))
        return orig_preview(enabled)

    mw._set_cards_resize_preview_mode = spy_preview

    def state(tag):
        text, _mean, _bbox = _analyze(card.grab(region), grab_w, grab_h)
        return {
            "tag": tag,
            "text": text,
            "w": card.width(),
            "preview": bool(mw._resize_preview_active),
            "card_preview": bool(card._resize_preview_mode),
            "placeholder": card.resize_placeholder.isVisible(),
            "viewer": card.viewer.isVisible(),
        }

    splitter = tm._splitter
    handle = splitter.handle(splitter.indexOf(tm._workbench_frame))
    origin = handle.mapToGlobal(handle.rect().center())
    press = state("before")

    qtbot.mousePress(handle, Qt.LeftButton)
    try:
        for k in range(1, 6):
            dest = origin + QPoint(-10 * k, 0)
            QApplication.sendEvent(
                handle,
                QMouseEvent(
                    QEvent.MouseMove,
                    QPointF(handle.mapFromGlobal(dest)),
                    QPointF(dest),
                    Qt.NoButton,
                    Qt.LeftButton,
                    Qt.NoModifier,
                ),
            )
            qtbot.wait(10)
    finally:
        qtbot.mouseRelease(handle, Qt.LeftButton)

    # 等到 _sync_all_cards_width 跑完（100ms）并把 workbench 保护清掉
    qtbot.wait(400)
    settled = state("settled")
    w, h = mw.size().width(), mw.size().height()
    mw.resize(w - 30, h)  # 模拟松手后排入的异步布局
    qtbot.wait(40)
    after = state("after_layout_resize")
    mw.resize(w, h)
    qtbot.wait(60)
    restored = state("restored")

    print(f"\n[post_release] 拖动前={press} 松手落定={settled}")
    print(f"[post_release] 布局 resize 后={after} 复原后={restored} preview调用={preview_calls}")

    assert not settled["preview"] and settled["viewer"], f"松手落定后本身就不正常：{settled}"
    blank = after["card_preview"] or after["placeholder"] or not after["viewer"] or after["text"] == 0
    assert not blank, f"松手后的布局 resize 把正文切成占位：{after}"
