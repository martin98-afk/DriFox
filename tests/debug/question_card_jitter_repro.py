# -*- coding: utf-8 -*-
"""[DEBUG-qcard-jitter] 临时排查脚本：提问卡片出现时内部元素快速抖动复现。

症状：question 工具触发提问卡片出现时，卡片内部元素（选项/底栏）偶尔快速上下抖动。

结构（贴近真实 tab_manager_window）：
  host
  └─ chat_vsplitter (QSplitter.Vertical)
     ├─ chat_wrapper（对话区 + 输入区占位）
     └─ bottom_container (BottomCardContainer, enable_dock_mode)
        └─ question (QuestionFloatingWidget, followContent + noContainerAnimation)

核心假设 H1：heightForWidth(w) 内部子控件 sizeHint 依赖"当前实际宽度"而非参数 w，
布局中间态（容器宽度未稳）→ natural_h 错 → 容器 min/max 锁+setSizes 错高 →
布局重排宽度变化 → heightChanged → 再 _do_expand → 目标高度来回变 → 内部元素抖动。

仪表：monkeypatch 记录 _do_expand 目标序列 / setSizes / heightForWidth 轨迹；
密集采样（singleShot(0) 轮次 + 16ms 帧）容器高/底栏 y/选项 y。

场景：
  S1-S6 稳态矩阵（短/长描述/多问题/窄窗/长标题/二次提问）
  T1    ask 分轮执行（模拟真实事件循环异步）
  T2    ask 后窗口宽度变化（模拟 splitter/布局中间态稳定过程）
  T3    显示瞬间容器宽度错位（先窄后宽——真实首帧宽度未定）
运行：QT_QPA_PLATFORM=offscreen python tests/debug/question_card_jitter_repro.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton, QSplitter, QVBoxLayout, QWidget

from app.widgets.cards.card_container import BottomCardContainer, ContainerType
from app.widgets.cards.card_manager import CardManager
from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

WID_SEQ = [0]


def _flush(qapp, ms=120):
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


class Harness:
    """真实结构的提问卡片宿主 + 轨迹仪表"""

    def __init__(self, qapp, width=1200, height=800):
        WID_SEQ[0] += 1
        self.wid = f"debug-win-{WID_SEQ[0]}"
        self.qapp = qapp
        self.host = QWidget()
        self.host.resize(width, height)
        outer = QVBoxLayout(self.host)
        outer.setContentsMargins(0, 0, 0, 0)

        self.vsplitter = QSplitter(Qt.Vertical)
        self.vsplitter.setChildrenCollapsible(False)
        self.vsplitter.setHandleWidth(6)
        outer.addWidget(self.vsplitter)

        self.chat_wrapper = QWidget()
        wl = QVBoxLayout(self.chat_wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(QLabel("对话区占位"), 1)
        self.input_placeholder = QPushButton("输入区占位")
        self.input_placeholder.setFixedHeight(60)
        wl.addWidget(self.input_placeholder)

        self.container = BottomCardContainer()
        self.vsplitter.addWidget(self.chat_wrapper)
        self.vsplitter.addWidget(self.container)
        self.vsplitter.setStretchFactor(0, 1)
        self.vsplitter.setStretchFactor(1, 0)
        self.container.enable_dock_mode(self.vsplitter)

        self.card = QuestionFloatingWidget(self.host)
        self.card.setVisible(False)
        self.container.add_card("question", self.card)
        self.mgr = CardManager.get_instance()
        self.mgr.register_window(self.wid)
        self.container.bind_card_manager(self.mgr, self.wid)
        self.mgr.mark_coexist_containers(self.wid, frozenset({ContainerType.BOTTOM}))
        self.mgr.register_card(self.wid, ContainerType.BOTTOM, "question", self.card)

        self.trace = []          # 事件轨迹
        self._t0 = [0]

        # ── 仪表：_do_expand 目标轨迹 ──
        orig_expand = self.container._do_expand

        def traced_expand(*a, **kw):
            w = self.container.width()
            h = self.container.height()
            fc = self.container._visible_cards_follow_content()
            natural = self.container._follow_content_natural_h() if fc else -1
            self.trace.append(("expand.enter", f"cont={w}x{h} natural_h={natural}"))
            orig_expand(*a, **kw)
            self.trace.append(("expand.exit", f"cont_h={self.container.height()} "
                               f"min={self.container.minimumHeight()} max={self.container.maximumHeight()}"))
        self.container._do_expand = traced_expand

        # ── 仪表：setSizes 轨迹 ──
        orig_set_sizes = self.vsplitter.setSizes

        def traced_set_sizes(sizes):
            self.trace.append(("setSizes", list(sizes)))
            orig_set_sizes(sizes)
        self.vsplitter.setSizes = traced_set_sizes

        # ── 仪表：heightForWidth 轨迹 ──
        orig_hfw = self.card.heightForWidth

        def traced_hfw(w):
            h = orig_hfw(w)
            self.trace.append(("hfw", f"w={w} card_w={self.card.width()} -> h={h}"))
            return h
        self.card.heightForWidth = traced_hfw

    def ask(self, questions):
        """复刻 _on_question_asked 时序"""
        self.input_placeholder.setVisible(False)
        self.card.setUpdatesEnabled(False)
        self.card.show_question(questions)
        self.card.setUpdatesEnabled(True)
        lay = self.card.layout()
        if lay is not None:
            lay.invalidate()
        self.card.updateGeometry()
        self.mgr.show_card("question", self.wid)
        QTimer.singleShot(200, self.container._do_expand)

    def record_frame(self):
        card = self.card
        footer_y = card._footer_widget.mapTo(card, card._footer_widget.rect().topLeft()).y() \
            if card._footer_widget.isVisible() else -1
        opts_y = card._options_container.mapTo(card, card._options_container.rect().topLeft()).y() \
            if card._options_container.isVisible() else -1
        return {
            "cont_h": self.container.height(),
            "card_y": card.y(),
            "card_h": card.height(),
            "card_w": card.width(),
            "scroll_h": card._question_scroll.height(),
            "opts_y": opts_y,
            "footer_y": footer_y,
            "sp_sizes": list(self.vsplitter.sizes()),
        }

    def run_frames(self, n=90, interval=16):
        records = []

        def tick():
            self.qapp.processEvents()
            records.append(self.record_frame())
            if len(records) < n:
                QTimer.singleShot(interval, tick)

        QTimer.singleShot(interval, tick)
        _flush(self.qapp, interval * n + 300)
        return records

    def print_trace(self, limit=40):
        print(f"  ── 轨迹（共 {len(self.trace)} 条，尾 {limit} 条）──")
        for kind, detail in self.trace[-limit:]:
            print(f"    [{kind}] {detail}")


def analyze(tag, records, harness=None, show_trace=False):
    anomalies = []

    def _series_dance(name, series, thresh=1):
        diffs = [b - a for a, b in zip(series, series[1:]) if abs(b - a) > thresh]
        turns = 0
        for i in range(1, len(diffs)):
            if (diffs[i] > 0) != (diffs[i - 1] > 0):
                turns += 1
        if turns >= 2:
            anomalies.append(f"{name}: 方向交替 {turns} 次, diffs={diffs[:14]}")

    _series_dance("footer_y", [r["footer_y"] for r in records])
    _series_dance("opts_y", [r["opts_y"] for r in records])
    _series_dance("cont_h", [r["cont_h"] for r in records])

    fy = [r["footer_y"] for r in records if r["footer_y"] >= 0]
    if len(fy) > 20:
        stable = fy[20:]
        if max(stable) - min(stable) > 1:
            anomalies.append(f"footer_y 20 帧后仍未收敛: range={max(stable) - min(stable)} tail={stable[-8:]}")

    mark = "❌ 抖动" if anomalies else "OK"
    print(f"\n=== [{tag}] {mark} ===")
    for a in anomalies:
        print(f"  {a}")
    keys = [0, 1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 28, 40, 60, len(records) - 1]
    print("  帧 | cont_h card_y card_h card_w scroll_h opts_y footer_y | splitter")
    for i in keys:
        if i < len(records):
            r = records[i]
            print(f"  {i:3d} | {r['cont_h']:6d} {r['card_y']:6d} {r['card_h']:6d} {r['card_w']:6d}"
                  f" {r['scroll_h']:8d} {r['opts_y']:6d} {r['footer_y']:8d} | {r['sp_sizes']}")
    if show_trace and harness:
        harness.print_trace()
    return not anomalies


QS = {
    "short": [
        {"question": "继续吗？", "options": [{"label": "是"}, {"label": "否"}], "multiple": False},
    ],
    "desc": [
        {"question": "选择实现方案：", "options": [
            {"label": "方案A", "description": "使用状态机重构，保留现有对外接口不变，内部逻辑全部重写"},
            {"label": "方案B", "description": "最小改动，在现有回调里加判断分支，风险低但可维护性差"},
            {"label": "方案C", "description": "引入第三方库直接替换，需要评估许可证兼容性和依赖体积变化"},
        ], "multiple": False},
    ],
}


def main(qapp):
    results = []

    # ── S1 基线：短问题 ──
    h = Harness(qapp)
    h.host.show()
    _flush(qapp, 60)
    h.ask(QS["short"])
    results.append(("S1-short", analyze("S1-short", h.run_frames())))

    # ── T1 ask 分轮执行（真实事件循环异步性：hide input / 填充 / show 分轮落地）──
    h = Harness(qapp)
    h.host.show()
    _flush(qapp, 60)
    QTimer.singleShot(0, lambda: h.input_placeholder.setVisible(False))
    QTimer.singleShot(0, lambda: h.card.show_question(QS["desc"]))
    QTimer.singleShot(0, lambda: h.mgr.show_card("question", h.wid))
    QTimer.singleShot(200, h.container._do_expand)
    results.append(("T1-async-ask", analyze("T1-async-ask", h.run_frames(60), h, show_trace=True)))

    # ── T2 ask 后窗口宽度变化（模拟 splitter 中间态/输入区隐藏后布局再分配）──
    h = Harness(qapp)
    h.host.show()
    _flush(qapp, 60)
    h.ask(QS["desc"])
    QTimer.singleShot(50, lambda: h.host.resize(1000, 800))
    QTimer.singleShot(120, lambda: h.host.resize(1200, 800))
    results.append(("T2-resize-after", analyze("T2-resize-after", h.run_frames(60), h, show_trace=True)))

    # ── T3 显示瞬间宽度错位：容器先窄后宽（真实首帧 splitter 未定宽）──
    h = Harness(qapp, width=700)
    h.host.show()
    _flush(qapp, 60)
    h.ask(QS["desc"])
    # 显示后立刻放宽带宽（模拟真实 splitter 把宽度分配到位）
    QTimer.singleShot(16, lambda: h.host.resize(1200, 800))
    results.append(("T3-grow-onshow", analyze("T3-grow-onshow", h.run_frames(60), h, show_trace=True)))

    # ── T4 显示瞬间宽度错位：先宽后窄 ──
    h = Harness(qapp, width=1300)
    h.host.show()
    _flush(qapp, 60)
    h.ask(QS["desc"])
    QTimer.singleShot(16, lambda: h.host.resize(1000, 800))
    results.append(("T4-shrink-onshow", analyze("T4-shrink-onshow", h.run_frames(60), h, show_trace=True)))

    # ── S6 二次提问 ──
    h = Harness(qapp)
    h.host.show()
    _flush(qapp, 60)
    h.ask(QS["short"])
    h.run_frames(40)
    h.card.clear()
    _flush(qapp, 60)
    h.ask(QS["desc"])
    results.append(("S6-reopen", analyze("S6-reopen", h.run_frames(60), h, show_trace=True)))

    print("\n========== 汇总 ==========")
    bad = [t for t, ok in results if not ok]
    for t, ok in results:
        print(f"  {t}: {'OK' if ok else '❌ 抖动'}")
    print(f"\n结论：{'复现 ' + str(len(bad)) + ' 个场景抖动: ' + ', '.join(bad) if bad else '未复现抖动'}")


if __name__ == "__main__":
    qapp = QApplication.instance() or QApplication(sys.argv)
    main(qapp)
