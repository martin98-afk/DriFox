# -*- coding: utf-8 -*-
"""[DEBUG-j7x3] 用户气泡滚动条抖动自动复现 harness

反馈环建模（与生产代码一致）：
  A: PlainTextViewer._update_height → setFixedSize + contentHeightChanged.emit
  B: MessageCard._update_height → viewer.setFixedHeight（复刻）
  A→B: contentHeightChanged 信号
  B→A: setFixedHeight 触发 resizeEvent → 50ms 防抖 → A 重算

判定：跑事件循环 T 秒，滚动条 visible 状态翻转次数 > 2 即振荡复现。
用法: python tests/debug/user_bubble_jitter_repro.py [cap] [trials]
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt5.QtCore import QTimer, QEventLoop  # noqa: E402
import PyQt5.QtWebEngineWidgets  # noqa: F401,E402  # 必须先于 QApplication 创建
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.widgets.message_card import PlainTextViewer  # noqa: E402

SAMPLE_MS = 20
RUN_MS = 1200


class CardSim:
    """复刻 MessageCard._update_height 的非流式路径（user 卡不走流式防抖/动画）"""

    def __init__(self, viewer: PlainTextViewer):
        self.viewer = viewer
        self.viewer.contentHeightChanged.connect(self.on_height)
        self.last_applied = 40

    def on_height(self, h: int):
        target = max(40, h)
        current = self.viewer.height() or self.viewer.minimumHeight() or 40
        if abs(target - current) < 10:
            self.apply(target)
        else:
            self.apply(target)

    def apply(self, value: int):
        height = max(40, int(value))
        if height == self.last_applied:
            return
        self.last_applied = height
        self.viewer.setFixedHeight(height)  # ← B 路径：外部写入 viewer 高度


def sample_state(v: PlainTextViewer) -> str:
    sb = v.text_edit.verticalScrollBar()
    doc = v.text_edit.document()
    # offscreen 未 show 时 isVisible() 恒 False，用 maximum()>0 判溢出
    return f"w={v.width()} h={v.height()} sb={int(sb.maximum() > 0)} dtw={int(doc.textWidth())} dh={int(doc.size().height())}"


def run_trial(app, text: str, cap: int) -> dict:
    v = PlainTextViewer()
    sim = CardSim(v)
    v.set_width_cap(cap)
    v.set_text(text)
    v.show()  # offscreen 下标记可见，确保布局生效

    states = []
    loop = QEventLoop()
    elapsed = {"t": 0}

    def tick():
        states.append(sample_state(v))
        elapsed["t"] += SAMPLE_MS
        if elapsed["t"] >= RUN_MS:
            loop.quit()

    t = QTimer()
    t.setInterval(SAMPLE_MS)
    t.timeout.connect(tick)
    t.start()
    loop.exec_()
    t.stop()

    sb_flips = sum(
        1
        for i in range(1, len(states))
        if states[i].split(" sb=")[1][0] != states[i - 1].split(" sb=")[1][0]
    )
    geom_changes = len({s.rsplit(" dtw=", 1)[0] for s in states})
    v.cleanup()
    v.deleteLater()
    return {"sb_flips": sb_flips, "geom_states": geom_changes, "first": states[0], "last": states[-1], "n": len(states)}


def probe_state(app, text: str, cap: int) -> str:
    """不跑事件循环，快速收敛后采一次状态（粗定位临界长度）"""
    v = PlainTextViewer()
    v.set_width_cap(cap)
    v.set_text(text)
    v.update_height()  # 同步收敛一次
    s = sample_state(v)
    print(f"    probe {len(text)} chars → {s}", flush=True)
    v.deleteLater()
    app.processEvents()
    return s


def main():
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    app = QApplication.instance() or QApplication(sys.argv[:1])

    print(f"cap={cap}  MAX_HEIGHT={PlainTextViewer.MAX_HEIGHT}  采样 {SAMPLE_MS}ms x {RUN_MS}ms")
    # 第一步：粗探测，找 doc 高度落在临界区 [MAX_HEIGHT-40, MAX_HEIGHT+40] 的文本长度
    crit = []
    for n in range(200, 4000, 60):
        s = probe_state(app, "x" * n, cap)
        dh = int(s.split("dh=")[1])
        if abs(dh - PlainTextViewer.MAX_HEIGHT) <= 60:
            crit.append(n)
    print(f"临界长度样本: {crit[:12]}")
    if not crit:
        print("此 cap 下无临界长度（MAX_HEIGHT 未被触发），换 cap 重试")
        return 0

    # 第二步：临界区精扫 + 完整事件循环，检测振荡
    print(f"{'len':>5} {'flips':>5} {'geomN':>5}  first → last")
    oscillating = []
    for base in crit[:6]:
        for n in range(base - 60, base + 60, 7):
            r = run_trial(app, "x" * n, cap)
            if r["sb_flips"] > 2:
                oscillating.append((n, r))
                print(f"{'OSC':>3} {n:>5} {r['sb_flips']:>5} {r['geom_states']:>5}  {r['first']} → {r['last']}")
            elif r["geom_states"] > 3:
                print(f"{'GEO':>3} {n:>5} {r['sb_flips']:>5} {r['geom_states']:>5}  {r['first']} → {r['last']}")
    print(f"\n振荡复现数: {len(oscillating)}")
    if oscillating:
        n, r = oscillating[0]
        print(f"首个振荡样本: len={n}")
        print(f"  first: {r['first']}")
        print(f"  last:  {r['last']}")
        return 1
    print("未复现振荡（此 cap 下 A↔B 环收敛）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
