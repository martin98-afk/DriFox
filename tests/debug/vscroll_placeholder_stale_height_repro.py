# -*- coding: utf-8 -*-
"""探针：占位高度失真 → 视口内容位移（「滚轮乱跳」的直接量化）。

跑法（项目根）：
    python tests/debug/vscroll_placeholder_stale_height_repro.py

## 现象

消息卡片滚出视野被回收、再滚回来时，滚轮位置乱跳：明明只滚了两格，画面整屏
挪走，往回滚又要滚好几次才回到原处。

## 根因链

`_lazy_rendered` 在 viewer **创建完成**时就置 True，而 viewer 的真实高度要等
JS 渲染完异步上报（CodeWebViewer 数百 ms）。这段窗口里 `card.height()` 仍是
起步值 40px。回收器若按它装等高占位：

    1. `_recycle_out_of_view_batches` 第 1 步给视口 ±8 批全部 ensure_rendered
       （`_lazy_rendered` 立刻 True，高度仍 40）
    2. 渲染计数超配额 → 下一帧 `_recycle_lru_batches` 淘汰距视口 >2 的批次
       —— 正好是刚建好的 ±3~±8
    3. `_batch_fully_rendered` 旧判据（只看 `_lazy_rendered`）通过
       → 占位高度取 40/张
    4. 用户滚回该区 → 重建 pin 到 40 → viewer 落高 800/张
       → 视口内容整体位移数千 px

## 实测（未修复，6 批 800px 卡）

    user 阅读位置:      value=6464  anchor.y()=6464  屏内偏移=0
    前6张收缩到 40 后:  value=6464  anchor.y()=1904  屏内偏移=-4560
    回收侧量到批次高 = 280（真实 4840，低估 4560px）
    重建 pin:           value=6264  anchor.y()=1904
    落高到 4840 后:     value=6264  anchor.y()=6464  屏内偏移=200
    ★ 视口内容位移 = 200 px（value 一动没动）

## 判据

修复后（`is_layout_height_settled` + `_batch_fully_rendered` 双判据）：
未落高的批次**根本不会被回收**，占位高度永远等于真实高度，
视口内容位移 ≤ AT_BOTTOM_TOLERANCE。
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

REAL_H = 800
START_H = 40
GAP = 8
N = 16
VIEW_H = 600
STALE_COUNT = 6


def pump(n=8):
    for _ in range(n):
        app.processEvents()


class Card(QWidget):
    def __init__(self, tag):
        super().__init__()
        self.tag = tag


def build():
    area = QScrollArea()
    area.setWidgetResizable(True)
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(GAP)
    cards = [Card(i) for i in range(N)]
    for c in cards:
        c.setFixedHeight(REAL_H)
        lay.addWidget(c)
    area.setWidget(container)
    area.resize(520, VIEW_H)
    area.show()
    pump(10)
    return area, container, lay, cards


def main():
    area, container, lay, cards = build()
    sb = area.verticalScrollBar()

    # 视口顶落在第 8 张卡（用户在中间阅读）
    sb.setValue(sum(cards[i].height() + GAP for i in range(8)))
    pump(8)
    anchor = cards[8]
    base_offset = anchor.y() - sb.value()
    print(f"阅读位置: value={sb.value()}  anchor.y()={anchor.y()}  屏内偏移={base_offset}", flush=True)

    # 前 6 张姿态改为「刚懒渲染完、高度还在起步值」
    for c in cards[:STALE_COUNT]:
        c.setFixedHeight(START_H)
    pump(8)
    measured = sum(c.height() for c in cards[:STALE_COUNT]) + GAP * (STALE_COUNT - 1)
    real_total = REAL_H * STALE_COUNT + GAP * (STALE_COUNT - 1)
    print(
        f"前 {STALE_COUNT} 张收缩到 {START_H}px：回收侧会量到 {measured}（真实 {real_total}，低估 {real_total - measured}px）",
        flush=True,
    )

    # 正确行为：这个姿态下 `is_layout_height_settled()` 必须判 False，
    # 回收器因此整批保留，不会装失真占位。
    from app.widgets.message_card import MessageCard  # noqa: E402

    probe = MessageCard(role="assistant")
    probe._lazy_rendered = True
    probe._viewer_async_height = True
    probe.viewer = QWidget(probe)
    probe.viewer.setFixedHeight(START_H)
    probe._last_applied_viewer_height = START_H
    settled = probe.is_layout_height_settled()

    probe_ok = MessageCard(role="assistant")
    probe_ok._lazy_rendered = True
    probe_ok._viewer_async_height = True
    probe_ok.viewer = QWidget(probe_ok)
    probe_ok.viewer.setFixedHeight(REAL_H)
    probe_ok._last_applied_viewer_height = REAL_H
    settled_ok = probe_ok.is_layout_height_settled()

    print(f"落定判据：起步 {START_H}px → {settled}（必须 False）；落高 {REAL_H}px → {settled_ok}（必须 True）", flush=True)

    ok = (not settled) and settled_ok
    print(f"{'PASS' if ok else 'FAIL'} 未落高批次不得进入等高占位路径", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
