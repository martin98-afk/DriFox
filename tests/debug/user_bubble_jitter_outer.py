# -*- coding: utf-8 -*-
"""[DEBUG-j7x3] 用户气泡抖动·外环复现 harness

完整闭环建模（生产链路）：
  卡片高度变 → chat_container 高变 → 聊天区滚动条出现/消失（viewport 宽 ±6px）
  → 滚动条 range/value 变 → _on_scroll_changed → _scroll_sync_timer(100ms)
  → _sync_visible_cards_on_scroll → force sync_width(target=vw - max(24, vw*0.06))
  → viewer.set_width_cap(target-24) → PlainTextViewer._update_height
  → setFixedSize 高度变 → ... 循环

内环（A↔B）同 user_bubble_jitter_repro.py。
判定：采样序列中滚动条状态翻转 > 2 或卡片几何状态数 > 3 → 振荡。
用法: uv run python tests/debug/user_bubble_jitter_outer.py [text_len] [viewport_h]
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PySide6.QtCore import Qt, QTimer, QEventLoop  # noqa: E402
import PySide6.QtWebEngineWidgets  # noqa: F401,E402  # 必须先于 QApplication 创建
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

from app.widgets.message_card import PlainTextViewer  # noqa: E402

SAMPLE_MS = 20
RUN_MS = 2500


class CardSim:
    """MessageCard user 分支：sync_width + _update_height（非流式）"""

    def __init__(self, viewer: PlainTextViewer):
        self.viewer = viewer
        self.last_applied = 40
        self._last_synced_width = 0
        self.viewer.contentHeightChanged.connect(self.on_height)

    def on_height(self, h: int):
        height = max(40, int(max(40, h)))
        if height == self.last_applied:
            return
        self.last_applied = height
        self.viewer.setFixedHeight(height)

    def sync_width(self, target_width: int, force: bool = False):
        if not force and target_width == self._last_synced_width:
            return
        self._last_synced_width = target_width
        self.viewer.set_width_cap(target_width - 24)


class ChatSim(QWidget):
    """聊天区：ScrollArea + AlignBottom 容器 + 滚动同步防抖（复刻 main_widget 链）"""

    def __init__(self, viewer: PlainTextViewer, card: CardSim, debounce_ms: int = 100):
        super().__init__()
        self.card = card
        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.container = QWidget()
        lay = QVBoxLayout(self.container)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignBottom)
        lay.addWidget(viewer)
        self.area.setWidget(self.container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.area)

        # 复刻 _on_scroll_changed → _scroll_sync_timer → _sync_visible_cards_on_scroll
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._sync_visible)
        self.area.verticalScrollBar().valueChanged.connect(lambda _v: self._timer.start())
        self.area.verticalScrollBar().rangeChanged.connect(lambda _a, _b: self._timer.start())
        self.sync_calls = 0

    def _sync_visible(self):
        vw = self.area.viewport().width()
        if vw <= 0:
            return
        margin = max(24, int(vw * 0.06))
        self.card.sync_width(max(320, vw - margin), force=True)
        self.sync_calls += 1


def sample(chat: ChatSim, v: PlainTextViewer) -> str:
    sb_chat = chat.area.verticalScrollBar()
    sb_card = v.text_edit.verticalScrollBar()
    return f"w={v.width()} h={v.height()} chat_sb={int(sb_chat.maximum() > 0)} card_sb={int(sb_card.maximum() > 0)}"


def run(app, text: str, cap: int, vw: int, vh: int) -> dict:
    v = PlainTextViewer()
    card = CardSim(v)
    chat = ChatSim(v, card)
    chat.resize(vw, vh)
    v.set_width_cap(cap - 24)
    v.set_text(text)
    chat.show()

    states = []
    loop = QEventLoop()
    elapsed = {"t": 0}

    def tick():
        states.append(sample(chat, v))
        elapsed["t"] += SAMPLE_MS
        if elapsed["t"] >= RUN_MS:
            loop.quit()

    t = QTimer()
    t.setInterval(SAMPLE_MS)
    t.timeout.connect(tick)
    t.start()
    loop.exec()
    t.stop()

    def flips(states, key):
        vals = [s.split(key + "=")[1][0] for s in states]
        return sum(1 for i in range(1, len(vals)) if vals[i] != vals[i - 1])

    chat.deleteLater()
    v.deleteLater()
    app.processEvents()
    return {
        "chat_flips": flips(states, "chat_sb"),
        "card_flips": flips(states, "card_sb"),
        "geom_states": len({s.split(" chat_sb")[0] for s in states}),
        "syncs": chat.sync_calls,
        "first": states[0],
        "last": states[-1],
    }


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    text_len = int(sys.argv[1]) if len(sys.argv) > 1 else 280
    vw = int(sys.argv[2]) if len(sys.argv) > 2 else 440

    print(f"text_len={text_len} viewport={vw}xH  采样 {SAMPLE_MS}ms x {RUN_MS}ms")
    print(f"{'vh':>5} {'chatF':>5} {'cardF':>5} {'geomN':>5} {'syncs':>5}  first → last")
    osc = []
    # 扫视口高度：让容器总高（卡片 300 + margins）跨过视口高度临界
    for vh in range(230, 360, 4):
        r = run(app, "x" * text_len, vw, vw, vh)
        flag = ""
        if r["chat_flips"] > 2 or r["card_flips"] > 2:
            flag = "OSC"
            osc.append((vh, r))
        elif r["geom_states"] > 3:
            flag = "GEO"
        print(
            f"{flag:>3} {vh:>5} {r['chat_flips']:>5} {r['card_flips']:>5} "
            f"{r['geom_states']:>5} {r['syncs']:>5}  {r['first']} → {r['last']}"
        )
    print(f"\n振荡视口高度数: {len(osc)} / {len(range(230, 360, 4))}")
    return 1 if osc else 0


if __name__ == "__main__":
    sys.exit(main())
