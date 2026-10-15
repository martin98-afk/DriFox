# -*- coding: utf-8 -*-
"""探针（真实骨架）：用户点击卡片正文后，外层消息列表是否还能滚动。

与 probe_wheel_armed.py 的差别：本探针加载真实骨架 + 走真实渲染链路
（append_chunk / updateContent），body / #tool-content / #content-placeholder
的结构与几何全部来自线上代码，而非探针手写的裸 HTML。

场景：
  R1 未点击 + 真实骨架          → 外层应滚动（基线）
  R2 点击正文 + 真实骨架        → 复现 bug：外层不动
  R3 点击正文 + tool-content 可滚 → 内层滚（折叠框体感）
  R4 点击正文 + body 超上限       → 卡内滚到底后外层接手（链式）
"""

import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent, QWheelEvent
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from app.core.infra.webengine_profile import init_shared_web_profile
from app.widgets.card_viewers import CodeWebViewer

app = QApplication([])
init_shared_web_profile()
user32 = ctypes.windll.user32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def send_wheel(dy):
    inputs = (INPUT * 1)()
    inputs[0].type = 0
    inputs[0].mi = MOUSEINPUT(0, 0, ctypes.c_ulong(dy & 0xFFFFFFFF), 0x0800, 0, None)
    user32.SendInput(1, inputs, ctypes.sizeof(INPUT))


class DummyHost:
    def __init__(self, area):
        self.chat_scroll_area = area


class DummyCard(QWidget):
    def __init__(self, host):
        super().__init__()
        self._parent = host
        self._wheel_hits = 0

    def wheelEvent(self, event):
        self._wheel_hits += 1
        super().wheelEvent(event)


_KEEPALIVE = []


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def build():
    area = QScrollArea()
    area.resize(600, 700)
    host_w = QWidget()
    lay = QVBoxLayout(host_w)
    card = DummyCard(DummyHost(area))
    card_lay = QVBoxLayout(card)
    view = CodeWebViewer()
    view._streaming = True
    view._is_history = False
    view.setMinimumHeight(400)
    card_lay.addWidget(view)
    lay.addWidget(card)
    filler = QWidget()
    filler.setMinimumHeight(500)
    lay.addWidget(filler)
    lay.addStretch(1)
    area.setWidgetResizable(True)
    area.setWidget(host_w)
    area.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
    area.move(100, 100)
    area.show()
    area.activateWindow()
    area.raise_()
    pump(2000)
    _KEEPALIVE.append((area, host_w, card, view))
    return area, view, card


def feed(view, md, ticks=8):
    view.append_chunk(md)
    for _ in range(ticks):
        pump(400)
    view.finish_streaming(keep_dock=False, immediate=True)
    pump(600)


def click_delegate(view):
    fp = view.focusProxy()
    if fp is None:
        return False
    ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(50, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev)
    pump(80)
    return True


LONG_TEXT = "这是一段用于制造内容高度的正文内容，需要足够长才能触发内滚条。" * 300
SHORT_TEXT = "点击我之后用滚轮滚外部列表。"
CODE_MD = (
    "先给一段代码。\n\n"
    "```python\n"
    + "\n".join(f"def function_{i}(arg): return arg * {i} + 'padding text for width'" for i in range(40))
    + "\n```\n\n"
    + "代码之后还有正文。"
)
TABLE_MD = (
    "再看一个表格。\n\n"
    + "| 列A | 列B | 列C |\n|---|---|---|\n"
    + "\n".join(f"| 值{i} | 值{i} | 值{i} |" for i in range(30))
    + "\n\n表格之后还有正文。"
)
TOOL_MD = (
    "调用一个工具看看。\n\n"
    "<tool_use>\n"
    '<tool_call id="tk1" name="read_file">\n'
    '<args>{"path": "a.py"}</args>\n'
    "</tool_use>\n\n"
    "<tool_result>\n"
    + "tool output line padded for scroll range here\n" * 60
    + "</tool_result>\n\n"
    + "工具结果之后还有正文。"
)


def run_case(tag, md, armed, dys=None, cx=380, cy=420):
    print(f"=== {tag} ===")
    area, view, card = build()
    feed(view, md)
    top, mx = view._inner_scroll_range()
    print(f"渲染完成: geom_valid={view._body_geom_valid} range(top={top}, max={mx}) "
          f"inner_scrollable={view._inner_scrollable} "
          f"doc_h={view._document_height} client_h={view._body_client_height}")
    if armed:
        click_delegate(view)
        print(f"点击正文 → armed={view._wheel_inner_armed}")
    user32.SetCursorPos(ctypes.c_int(cx), ctypes.c_int(cy))
    pump(300)
    vbar = area.verticalScrollBar()
    print(f"外层range {vbar.minimum()}..{vbar.maximum()} "
          f"filter_target={'装' if view._wheel_filter_target is not None else 'None'}")
    moved = 0
    hits0 = card._wheel_hits
    fp = view.focusProxy()
    for i, dy in enumerate(dys or [-120] * 5):
        v0 = vbar.value()
        # 直接派发到 delegate（focusProxy）：真实滚轮的第一接收者，
        # 避开 SendInput 依赖桌面焦点/鼠标位置（多探针并行时不可靠）。
        ev = QWheelEvent(
            QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, dy),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
        )
        consumed = view.eventFilter(fp, ev)
        if not consumed:
            QApplication.sendEvent(fp, ev)
        pump(450)
        m = vbar.value() != v0
        moved += m
        top_now, max_now = view._inner_scroll_range()
        print(f"  tick{i} dy={dy}: vbar {v0}->{vbar.value()} 外层动={m} "
              f"filter->{consumed} armed={view._wheel_inner_armed} "
              f"card.wheelEvent={card._wheel_hits - hits0} "
              f"body(top={top_now},max={max_now})")
    print(f"结果: 外层滚动 {moved}/{len(dys or [-120] * 5)}")
    area.hide()
    pump(200)
    return moved


def run_case_geom_loss(tag, md):
    """R7：几何回报链断（模拟历史 bare 路径）→ 轮询兜底恢复卡内滚动。

    步骤：armed → 手动清几何（模拟 console 链不达）→ 滚 1 拍应兜底转外层
    → 等 700ms（300ms 轮询拉回几何）→ 再滚 2 拍应放行卡内滚（body 动、外层不动）。
    """
    print(f"=== {tag} ===")
    area, view, card = build()
    # 禁 sentinel 补渲：补渲会重建 DOM 使 scrollHeight 收缩且 console 链对新
    # DOM 断报（真实场景流式卡 console 链活跃会立即覆盖，此处干扰轮询验证）
    view._content_sentinel_check = lambda: None
    feed(view, md)
    top, mx = view._inner_scroll_range()
    print(f"渲染完成: geom_valid={view._body_geom_valid} range(top={top}, max={mx})")
    click_delegate(view)
    print(f"点击正文 → armed={view._wheel_inner_armed}")
    # 等待可能的 content-sentinel 强制补渲彻底完成（补渲重建 DOM 期间 scrollHeight
    # 瞬时收缩，轮询若在该窗口拉值会定格过期边界态——真实场景仅流式卡有补渲且
    # console 链活跃会立即覆盖，历史 bare 卡 DOM 静止无此窗口）
    pump(2500)
    top0, mx0 = view._inner_scroll_range()
    print(f"补渲等待后: range=({top0}, {mx0}) geom_valid={view._body_geom_valid}")
    # 模拟 console 链断裂：清几何缓存（等价 viewer 复用复位路径，一并重启轮询）
    view._document_height = 0
    view._body_scroll_top = 0
    view._body_client_height = 0
    view._body_geom_valid = False
    view._geom_poll_count = 0
    view._geom_poll_timer.start()
    vbar = area.verticalScrollBar()
    fp = view.focusProxy()

    def one_tick(tag_i):
        v0 = vbar.value()
        b0 = view._inner_scroll_range()
        ev = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, -120),
                         Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        consumed = view.eventFilter(fp, ev)
        if not consumed:
            QApplication.sendEvent(fp, ev)
        pump(450)
        b1 = view._inner_scroll_range()
        print(f"  {tag_i}: vbar {v0}->{vbar.value()} 外层动={vbar.value() != v0} "
              f"filter->{consumed} geom_valid={view._body_geom_valid} "
              f"body(top {b0[0]}->{b1[0]}, max={b1[1]})")
        return vbar.value() != v0, b1[0] != b0[0]

    outer1, body1 = one_tick("失效拍")
    print(f"  等待轮询(700ms)… geom_valid={view._body_geom_valid} "
          f"poll_count={view._geom_poll_count}")
    pump(700)
    print(f"  轮询后: geom_valid={view._body_geom_valid} "
          f"range={view._inner_scroll_range()} poll_count={view._geom_poll_count}")
    # 独立读数对照：轮询拉回值必须与 JS 直读一致（保真性）。
    # 注：探针环境长内容 DOM 可能自发收缩（console 链与直读可同时变），故不
    # 断言具体值，只验证轮询与真实 DOM 同步；恢复拍行为随之由真实几何决定。
    box = []
    view.page().runJavaScript(
        "(function(){var b=document.body;return b?(b.scrollHeight|0)+'|'+(b.scrollTop|0)"
        "+'|'+(b.clientHeight|0):'NOBODY';})()",
        lambda r: box.append(r),
    )
    pump(300)
    print(f"  独立读数: {box}")
    top_now, max_now = view._inner_scroll_range()
    poll_faithful = False
    if box and box[0]:
        try:
            sh, st, ch = (int(float(x)) for x in str(box[0]).split("|")[:3])
            poll_faithful = (top_now == st and max_now == max(0, sh - ch) and ch > 0)
        except Exception:  # noqa: BLE001
            pass
    outer2 = body2 = 0
    o, b = one_tick("恢复拍1")
    outer2, body2 = outer2 + o, body2 + b
    o, b = one_tick("恢复拍2")
    outer2, body2 = outer2 + o, body2 + b
    ok = poll_faithful and (body2 >= 1 or max_now <= 0 or top_now >= max_now)
    print(f"结果: 失效拍(外层={outer1},卡内={body1}) → 轮询保真={poll_faithful} → "
          f"恢复拍(外层={outer2},卡内={body2}) {'PASS' if ok else 'FAIL'}")
    area.hide()
    pump(200)
    return 1 if ok else 0


def main():
    r1 = run_case("R1 未点击 + 真实骨架（基线）", SHORT_TEXT, armed=False)
    r2 = run_case("R2 点击正文 + 真实骨架（复现）", SHORT_TEXT, armed=True)
    r3 = run_case("R3 点击正文 + tool-content 可滚", TOOL_MD, armed=True)
    r5 = run_case("R5 点击正文 + 长代码块（疑误判 inner_scrollable）", CODE_MD, armed=True)
    r6 = run_case("R6 点击正文 + 长表格（疑误判 inner_scrollable）", TABLE_MD, armed=True)
    r4 = run_case("R4 点击正文 + body 超上限（链式）", LONG_TEXT, armed=True, dys=[-120] * 30)
    r7 = run_case_geom_loss("R7 几何链断 → 轮询恢复卡内滚动", LONG_TEXT)
    print(f"SUMMARY R1={r1} R2={r2} R3={r3} R5={r5} R6={r6} R4={r4} R7={r7}")
    print("DONE-REAL")
    sys.exit(0)


if __name__ == "__main__":
    main()
