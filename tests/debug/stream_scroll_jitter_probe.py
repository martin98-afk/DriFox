# -*- coding: utf-8 -*-
"""滚动区级探针：量化「置底抖动」与「新卡插入高度跳变」。

为什么必须带滚动区
------------------
单卡探针测不到抖动：抖动的成因是「卡片高度变化」与「外层滚动条补偿」
两条链以不同节拍推进（卡片 40ms 追踪拍 vs Qt 布局传播/滚动条上界更新），
只有把卡片放进 QScrollArea 才能观察到 gap = maximum - value 的浮动。

场景
----
1. 先放两张已完成的历史卡（撑出可滚动高度）
2. 新建 assistant 卡 → 加入布局 → start_streaming_anim → 逐批 append_text
3. 宿主编取 main_widget._on_message_card_height_changed 的 delta 补偿逻辑
   （保持与生产一致的「增量补偿」语义，便于修复后 A/B 对比）

指标
----
* **gap 抖动**：gap = sb.maximum() - sb.value()。置底时理想恒为 0；
  输出 mean / p90 / max / 非零拍占比 / 方向翻转次数（value 一上一下即抖动）。
* **起步高度**：新卡插入后前 800ms 的 (viewer_h, card_h)，输出峰值与终值。

运行：
  QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --no-sandbox --disable-dev-shm-usage" \
    python tests/debug/stream_scroll_jitter_probe.py [tag]
"""

import os
import sys

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

try:
    from app.utils.config import Settings

    Settings.get_instance().ui_compact_tool_area.value = True
except Exception:  # noqa: BLE001
    pass

BODY = (
    "这是流式正文输出，用于观察卡片高度与外层滚动条之间的跟随关系。"
    "每批追加一小段中文，句号触发软边界渲染，换行触发段落边界。"
)


class Host(QWidget):
    """最小聊天区：QScrollArea + 纵向容器 + delta 补偿（复刻 main_widget）。"""

    def __init__(self):
        super().__init__()
        self.resize(900, 700)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.sa = QScrollArea()
        self.sa.setWidgetResizable(True)
        outer.addWidget(self.sa)
        self.container = QWidget()
        self.lay = QVBoxLayout(self.container)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(12)
        self.sa.setWidget(self.container)
        self.samples = []
        self.follow = True

    # ── 复刻 main_widget._on_message_card_height_changed 的补偿段 ──
    def on_height_changed(self, _h):
        card = self.sender()
        try:
            delta = int(getattr(card, "_last_height_delta", 0) or 0)
            card._last_height_delta = 0
            if not delta:
                return
            sb = self.sa.verticalScrollBar()
            if delta > 0:
                sb.setMaximum(max(0, sb.maximum() + delta))
            value = sb.value()
            top = card.mapTo(self.container, card.rect().topLeft()).y()
            bottom = top + card.height()
            if self.follow:
                # 修复后：跟随态贴底（取当前上界），误差不累积
                sb.setValue(sb.maximum())
            elif bottom <= value:
                sb.setValue(max(0, value + delta))
        except RuntimeError:
            pass

    def scroll_bottom(self):
        sb = self.sa.verticalScrollBar()
        sb.setValue(sb.maximum())


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "run"
    host = Host()
    host.show()
    QApplication.processEvents()

    # 历史卡：撑出可滚动高度
    texts = [
        "历史消息一：用于撑高滚动区，使新卡插入时视口处于底部。\n\n" + BODY * 3,
        "历史消息二：同上，继续撑高，确保滚动条有足够量程。\n\n" + BODY * 3,
    ]
    for t in texts:
        c = MessageCard("assistant")
        c.set_content(t)
        c.ensure_rendered()
        host.lay.addWidget(c)
        c.heightChanged.connect(host.on_height_changed)
    QApplication.processEvents()
    for _ in range(40):
        QTest.qWait(100)
        if host.sa.verticalScrollBar().maximum() > 400:
            break
    host.scroll_bottom()
    QTest.qWait(300)

    # ── 新卡 ──
    card = MessageCard("assistant")
    card.ensure_rendered()
    host.lay.addWidget(card)
    card.heightChanged.connect(host.on_height_changed)
    start_samples = []

    st = QTimer()
    st.setInterval(16)
    st.timeout.connect(
        lambda: start_samples.append((getattr(card, "viewer", None) and card.viewer.height() or -1, card.height()))
    )
    st.start()
    card.start_streaming_anim()
    QTest.qWait(800)
    st.stop()

    if start_samples:
        vs = [s[0] for s in start_samples]
        cs = [s[1] for s in start_samples]
        print(
            f"[{tag}] 起步段(800ms): viewer min={min(vs)} max={max(vs)} final={vs[-1]} | "
            f"card min={min(cs)} max={max(cs)} final={cs[-1]}",
            flush=True,
        )
        # 打印起步段变化点
        prev = None
        for i, s in enumerate(start_samples):
            if prev is None or s != prev:
                print(f"    t={i * 16}ms viewer={s[0]} card={s[1]}", flush=True)
            prev = s

    # ── 流式 ──
    samples = []
    timer = QTimer()
    timer.setInterval(16)
    timer.timeout.connect(
        lambda: samples.append(
            (
                host.sa.verticalScrollBar().value(),
                host.sa.verticalScrollBar().maximum(),
                card.viewer.height() if getattr(card, "viewer", None) else -1,
            )
        )
    )
    timer.start()
    for _i in range(26):
        card.append_text(BODY)
        QTest.qWait(110)
    QTest.qWait(400)
    card.finish_streaming()
    QTest.qWait(1500)
    timer.stop()

    gaps = [m - v for v, m, _ in samples]
    nz = [g for g in gaps if g != 0]
    flips = 0
    prev_d = 0
    for i in range(1, len(samples)):
        d = samples[i][0] - samples[i - 1][0]
        if d and prev_d and d * prev_d < 0:
            flips += 1
        if d:
            prev_d = d
    n = len(gaps)
    sg = sorted(gaps)
    print(
        f"[{tag}] 置底抖动: 采样 {n} 拍 | gap mean={sum(gaps) / n:.1f} "
        f"p50={sg[n // 2]} p90={sg[int(n * 0.9)]} max={max(gaps)} | "
        f"非零拍 {len(nz)}/{n} = {100.0 * len(nz) / n:.0f}% | value 方向翻转 {flips} 次",
        flush=True,
    )


main()
