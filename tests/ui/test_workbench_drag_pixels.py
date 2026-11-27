# -*- coding: utf-8 -*-
"""右侧工作台分隔线手动拖动：整卡画面取证（正文消失 vs 几何抖动）。

采样通道用 ``card.grab(子矩形)``：实测与真实桌面合成像素一致（同帧 text 计数相同），
但不受桌面遮挡/焦点干扰，窗口可以留在屏幕外（place_offscreen），不打扰用户桌面。

采样区取卡片左侧 340×380（左对齐卡，正文不随限宽居中漂移），同时记录：
- 正文像素数（亮粉测试正文）与像素包围盒 → 区分「正文消失」与「整体位移」；
- 平均色 → 整片底色突变（页面底色/占位色闪现）；
- 卡片宽高、聊天视口宽、滚动条可见性 → 几何/滚动条抖动；
- 生产侧调用点时序（只包测试侧实例方法，不改生产代码）。

场景（QTest 真实鼠标事件，按下后保持按键）：
- fast：125Hz 定时器驱动连续往返拖动（单步 3px、行程 ±180px）；
- slow_pause：慢拖 + 中途停顿 260ms（> 侧栏 120ms 静默阈值），看是否被误判松手；
- release：拖动后松手，采样 400ms 静默期（异步 LayoutRequest 是否又把正文切回占位）；
- preview_cycle：先走一次 hover 悬浮预览（frame 摘出 splitter 再回挂，Qt 重建 handle），
  随后拖动，验证新 handle 上的拖动保护是否还在。

运行：pytest tests/ui/test_workbench_drag_pixels.py -m ui -s
"""
import time

import numpy as np
import pytest
from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, QSize, QTimer, Qt
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
    mean = (int(red.mean()), int(green.mean()), int(blue.mean()))
    return count, mean, bbox


@pytest.mark.ui
@pytest.mark.parametrize("mode", ["fast", "slow_pause", "release", "preview_cycle", "screen"])
def test_right_handle_drag_keeps_card_painted(ui_app, qtbot, mode):
    from app.widgets.message_card import MessageCard

    tm, mw = ui_app["tm"], ui_app["chat_window"]
    tm.resize(1400, 760)
    screen = QApplication.primaryScreen()
    geom = screen.availableGeometry()
    if mode == "screen":
        # 桌面合成通道：需要窗口真的在屏幕上，才能与窗口内渲染逐个比对
        tm.resize(min(1200, geom.width() - 40), 700)
        tm.move(geom.topLeft())
        tm.raise_()
        tm.activateWindow()
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

    if mode == "preview_cycle":
        ctrl = tm._wb_preview_ctrl
        ctrl.on_button_hover(True)
        qtbot.wait(700)
        ctrl.on_button_hover(False)
        qtbot.wait(900)
        idx = tm._splitter.indexOf(tm._workbench_frame)
        assert tm._splitter_right_handle is tm._splitter.handle(idx), "预览回挂后 handle 未重新绑定"

    viewer = card.viewer
    off = viewer.mapTo(card, QPoint(0, 0))
    region = QRect(off, viewer.size())
    region.setWidth(min(region.width(), 340))
    region.setHeight(min(region.height(), 380))
    grab_w, grab_h = region.width(), region.height()

    # ── 调用点时序记录（只包测试侧实例方法，不改生产代码）──
    t0 = time.monotonic()
    events = []

    def _wrap(obj, name, tag):
        orig = getattr(obj, name)

        def wrapper(*args, **kwargs):
            vals = tuple(a if isinstance(a, (bool, int, float)) else type(a).__name__ for a in args)
            events.append((round((time.monotonic() - t0) * 1000), tag, vals))
            return orig(*args, **kwargs)

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
        (card, "set_resize_preview_mode", "card_preview"),
    ):
        _wrap(obj, name, tag)

    samples = []
    dumps = []
    import tempfile
    from pathlib import Path

    dump_dir = Path(tempfile.gettempdir()) / "drifox_drag_pix"
    dump_dir.mkdir(exist_ok=True)
    scrollbar = mw.chat_scroll_area.verticalScrollBar()

    def sample(tag):
        st = {
            "tag": tag,
            "live": bool(getattr(mw, "_sidebar_drag_live", False)),
            "preview": bool(getattr(mw, "_resize_preview_active", False)),
            "card_preview": bool(getattr(card, "_resize_preview_mode", False)),
            "viewer": card.viewer.isVisible(),
            "placeholder": card.resize_placeholder.isVisible(),
            "card_w": card.width(),
            "card_h": card.height(),
            "vp_w": mw.chat_scroll_area.viewport().width(),
            "sb": scrollbar.isVisible(),
            "sbv": scrollbar.value(),
            "t": round((time.monotonic() - t0) * 1000),
        }
        st["text"], st["mean"], st["bbox"] = _analyze(card.grab(region), grab_w, grab_h)
        if mode == "screen":
            srect = QRect(viewer.mapToGlobal(QPoint(0, 0)), QSize(grab_w, grab_h)).intersected(geom)
            pix = screen.grabWindow(0, srect.x(), srect.y(), srect.width(), srect.height())
            st["screen_text"], st["screen_mean"], _ = _analyze(pix, srect.width(), srect.height())
            if st["screen_text"] == 0 and st["text"] > 0 and len(dumps) < 6:
                path = dump_dir / f"screen_mismatch_{len(dumps)}.png"
                pix.save(str(path))
                dumps.append((st["t"], st["text"], st["screen_mean"], str(path)))
        samples.append(st)
        return st

    base = sample("base")
    assert base["text"] > 20, f"未取得可辨识正文像素：{base}"
    base_w = base["card_w"]

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
    width_seen = [base_w]
    try:
        if mode == "fast" or mode == "screen":
            st = {"dx": 0, "dir": -1, "legs": 0, "done": False}

            def tick():
                st["dx"] += st["dir"] * 3
                if abs(st["dx"]) >= 180:
                    st["dx"] = st["dir"] * 180
                    st["dir"] *= -1
                    st["legs"] += 1
                    if st["legs"] >= (2 if mode == "screen" else 8):
                        st["done"] = True
                drag_to(st["dx"])

            timer = QTimer()
            timer.setInterval(8)
            timer.timeout.connect(tick)
            timer.start()
            while not st["done"]:
                qtbot.wait(4)
                width_seen.append(sample("fast")["card_w"])
            timer.stop()
        elif mode == "slow_pause":
            for rnd, dx in enumerate((-30, -60, -60, -30, 0)):
                drag_to(dx)
                qtbot.wait(260)
                st = sample(f"slow{rnd}")
                width_seen.append(st["card_w"])
                assert st["live"], f"按住停顿被误判松手：{st}"
                assert not st["card_preview"] and not st["placeholder"], f"停顿期间切占位：{st}"
        else:  # release / preview_cycle
            for k in range(1, 5):
                drag_to(-40 * k)
                qtbot.wait(10)
                width_seen.append(sample(f"pre{k}")["card_w"])
    finally:
        qtbot.mouseRelease(handle, Qt.LeftButton)

    for i in range(20):
        qtbot.wait(20)
        sample(f"post{i}")
    qtbot.wait(300)
    sample("final")

    # ── 判定 ──
    qt_bad = [s for s in samples if s["card_preview"] or s["placeholder"] or not s["viewer"]]
    text_zero = [s for s in samples[1:] if s["text"] == 0]
    mean_jump = [
        s
        for s in samples[1:]
        if max(abs(s["mean"][c] - base["mean"][c]) for c in range(3)) > 8 and s["text"] < base["text"] * 0.3
    ]
    # 几何位移：宽度未变的两帧之间正文包围盒整体平移 > 3px
    geom_shift = []
    for a, b in zip(samples, samples[1:]):
        if a["bbox"] is None or b["bbox"] is None or a["card_w"] != b["card_w"]:
            continue
        d = max(abs(a["bbox"][0] - b["bbox"][0]), abs(a["bbox"][1] - b["bbox"][1]))
        if d > 3:
            geom_shift.append((a["tag"], b["tag"], d))
    sb_flips = sum(1 for a, b in zip(samples, samples[1:]) if a["sb"] != b["sb"])
    widths = [s["card_w"] for s in samples]
    width_flips = sum(1 for a, b, c in zip(widths, widths[1:], widths[2:]) if a == c != b)

    print(f"\n[{mode}] base={base}")
    print(f"[{mode}] 卡片宽度={width_seen}")
    print(
        f"[{mode}] 采样数={len(samples)} text范围={min(s['text'] for s in samples)}..{max(s['text'] for s in samples)} "
        f"滚动条翻转={sb_flips} 宽度回摆={width_flips} 包围盒位移={geom_shift[:10]}"
    )
    timeline = [
        (s["t"], s["tag"], s["text"], s["card_w"], s["card_h"], s["vp_w"], s["sb"], s["preview"], s["card_preview"])
        for s in samples
    ]
    print(f"[{mode}] 采样={timeline}")
    print(f"[{mode}] 调用点={events}")
    if mode == "screen":
        screen_zero = [s for s in samples if s.get("screen_text") == 0]
        print(f"[{mode}] 桌面采样归零帧={len(screen_zero)}/{len(samples)} 截图={dumps}")

    assert not qt_bad, f"Qt 侧出现占位/隐藏：{qt_bad}"
    assert not text_zero, f"正文像素归零帧：{text_zero}"
    assert not mean_jump, f"整片底色突变帧：{mean_jump}"
    assert not geom_shift, f"同一宽度下正文位移：{geom_shift[:10]}"
