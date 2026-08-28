# -*- coding: utf-8 -*-
"""验证修复效果。

覆盖场景：
A. splitter 缓存分配 > natural_h（最常见的「下一步下方空白」诱因）
B. 连续多次 _do_expand
C. 问题切换（natural_h 变化：变长 → 变回）
"""

import os
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ.setdefault("QT_QPA_PLATFORM", "minimal")
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
from PySide6.QtWebEngineWidgets import QWebEngineView
# noqa: F401

class FakeSplitter:
    def __init__(self, initial_sizes):
        self._sizes = list(initial_sizes)
        self._count = len(initial_sizes)
        self._log = []

    def indexOf(self, w):
        return 0

    def sizes(self):
        return list(self._sizes)

    def setSizes(self, lst):
        self._sizes = list(lst)
        self._log.append(list(lst))

    def height(self):
        return 1000

    def handleWidth(self):
        return 1

    def width(self):
        return 1000

    def orientation(self):
        return Qt.Horizontal

    def count(self):
        return self._count

    def widget(self, i):
        return None


def make_container_and_question(initial_sizes, questions, container_w=800):
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    container = BottomCardContainer()
    fake_sp = FakeSplitter(initial_sizes)
    container.enable_dock_mode(fake_sp)

    q = QuestionFloatingWidget()
    q.show_question(questions)
    container.add_card("question", q)
    # 模拟真实布局：给容器分配宽度，让卡片 heightForWidth 用真实宽度计算
    container.resize(container_w, 300)
    container.layout().activate()
    q.resize(container_w - 8, 300)
    q.layout().activate()
    return container, q, fake_sp


def report(label, container, fake_sp, q):
    # followContent 卡片：内容高度 = 卡片 heightForWidth(当前宽度)（修复后真实高度）
    natural_h = q.heightForWidth(q.width()) if q.hasHeightForWidth() else q.layout().sizeHint().height()
    print(f"\n--- {label} ---")
    print(f"  heightForWidth:       {natural_h}px")
    print(
        f"  container:             max={container.maximumHeight()} min={container.minimumHeight()} h={container.height()}"
    )
    print(f"  splitter.sizes:        {fake_sp.sizes()}")

    sp_alloc = fake_sp.sizes()[0]
    container_h = container.height()
    diff_outside = container_h - natural_h
    diff_splitter = sp_alloc - natural_h
    diff_max = container.maximumHeight() - natural_h

    fail = []
    if diff_outside > 1:
        fail.append(f"容器实际高 {container_h} > 内容 {natural_h} → 槽位内 {diff_outside}px 空白")
    if diff_splitter > 1:
        fail.append(f"splitter 给了 {sp_alloc}px 但只需 {natural_h}px → 槽位外 {diff_splitter}px 空白")
    if diff_max > 1:
        fail.append(f"max {container.maximumHeight()} >> natural_h {natural_h}（未锁紧）")

    if fail:
        for msg in fail:
            print(f"  ❌ {msg}")
    else:
        print(f"  ✓ 三项对齐：容器高度 = splitter 分配 = natural_h = {natural_h}px")


def main():
    from PySide6.QtCore import QTimer

    app = QApplication(sys.argv)

    SHORT_Q = [
        {
            "question": "你更喜欢哪种编程方式？",
            "options": [
                {"label": "函数式", "description": "纯函数"},
                {"label": "面向对象", "description": "类 + 继承"},
                {"label": "声明式", "description": "DSL"},
            ],
        }
    ]
    LONG_Q = [
        {
            "question": "选一个最贴近你日常习惯的方案：",
            "options": [
                {"label": "函数式"},
                {"label": "面向对象", "description": "用类建模"},
                {"label": "声明式", "description": "DSL 风格"},
                {"label": "响应式", "description": "事件流"},
            ],
        }
    ]

    def _flush(ms=300):
        """让 Qt 事件循环跑够时间，确保动画 on_finished 回调已触发"""
        from PySide6.QtCore import QEventLoop

        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    print("\n========== 场景 A：splitter 缓存分配 500px（远大于 natural_h ~397）==========")
    container, q, sp = make_container_and_question([500, 500], SHORT_Q)
    q.setVisible(True)
    container._do_expand()
    _flush()
    report("A.1 首次 _do_expand", container, sp, q)
    container._do_expand()
    _flush()
    report("A.2 再次 _do_expand", container, sp, q)

    print("\n========== 场景 B：连续三次 _do_expand（diff<2px 边界）==========")
    container2, q2, sp2 = make_container_and_question([400, 600], SHORT_Q)
    q2.setVisible(True)
    container2._do_expand()
    _flush()
    report("B.1 首次", container2, sp2, q2)
    container2._do_expand()
    _flush()
    report("B.2 连续二次", container2, sp2, q2)
    container2._do_expand()
    _flush()
    report("B.3 连续三次", container2, sp2, q2)

    print("\n========== 场景 C：问题内容变化（短 → 长 → 短）==========")
    container3, q3, sp3 = make_container_and_question([300, 700], SHORT_Q)
    q3.setVisible(True)
    container3._do_expand()
    _flush()
    report("C.1 短问题（首展）", container3, sp3, q3)
    q3.show_question(LONG_Q)
    container3._do_expand()
    _flush()
    report("C.2 切到长问题", container3, sp3, q3)
    q3.show_question(SHORT_Q)
    container3._do_expand()
    _flush()
    report("C.3 切回短问题", container3, sp3, q3)

    print("\n========== 场景 D：wordWrap 描述高估 → 卡片内空白 ==========")
    # 选项带长 description：wordWrap QLabel 的 C++ sizeHint() 用较窄固定宽度
    # 估算换行高度，在卡片实际较宽时严重高估（单行按 2-3 行算），逐级放大到
    # 卡片 sizeHint → 容器锁高 → 「下一步」按钮下方大段空白。
    # 修复：CardContainer follow_content 分支改用卡片 heightForWidth(容器宽)。
    # FakeSplitter 无真实布局、容器宽度失真，此场景用真实 QSplitter 验证。
    DESC_Q = [
        {
            "question": "请选择你偏好的开发工作流方案：",
            "options": [
                {
                    "label": "方案一：快速原型",
                    "description": "优先使用脚本语言快速验证想法，强调迭代速度，适合探索性任务。这是较长的描述文字，用于在窄窗口下触发多行换行。",
                },
                {
                    "label": "方案二：工程化",
                    "description": "严格遵循设计文档与测试驱动，强调可维护性与长期演进，适合生产级任务。同样加长确保换行。",
                },
                {
                    "label": "方案三：混合模式",
                    "description": "前期快速原型验证可行性，中期切换工程化流程，兼顾速度与质量。继续加长文本长度。",
                },
            ],
        }
    ]

    def _real_env(win_w):
        from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

        from app.widgets.cards.card_container import BottomCardContainer
        from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

        win = QWidget()
        win.resize(win_w, 700)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        win.setLayout(lay)
        vdock = QSplitter(Qt.Vertical, win)
        vdock.addWidget(QWidget(win))
        bottom = BottomCardContainer()
        vdock.addWidget(bottom)
        vdock.setStretchFactor(0, 1)
        vdock.setStretchFactor(1, 0)
        vdock.setHandleWidth(6)
        vdock.setChildrenCollapsible(False)
        lay.addWidget(vdock)
        q = QuestionFloatingWidget(bottom)
        bottom.enable_dock_mode(vdock)
        bottom.add_card("question", q)
        win.show()
        q.show_question(DESC_Q)
        q.setVisible(True)
        bottom._do_expand()
        _flush()
        return win, vdock, bottom, q

    def _inner_gap(q):
        # 卡片高 vs 子区域实际高度之和：差值 = 布局 spacing + margins（正常）
        parts = [
            q._header_widget.height(),
            q._question_scroll.height(),
            q._hint_label.height(),
            q._options_container.height(),
            q._footer_widget.height(),
        ]
        return q.height() - sum(parts)

    def _check(tag, q, bottom):
        gap = _inner_gap(q)
        # 布局 spacing(2px × 4) + margins(2+2) ≈ 12px 属正常；超 20px 才是异常空白
        h4w = q.heightForWidth(q.width())
        fit = abs(q.height() - h4w) <= 2
        print(f"\n--- {tag} ---")
        print(f"  卡片高 {q.height()} h4w={h4w} 子区域合计 {q.height() - gap} → 内部未分配 {gap}px")
        if gap > 20 or not fit:
            print("  ❌ 卡片内部存在空白（按钮行下方）")
        else:
            print("  ✓ 卡片高度 = heightForWidth，无异常空白")

    win5, vdock5, bottom5, q5 = _real_env(760)
    _check("D.1 带 desc 选项（窄窗口 760）", q5, bottom5)
    before_h = bottom5.height()

    # 拉宽 → desc 换行减少 → 容器应跟随收缩
    win5.resize(1400, 700)
    _flush(600)
    _check("D.2 拉宽到 1400（desc 换行减少）", q5, bottom5)
    if bottom5.height() < before_h:
        print(f"  ✓ 容器跟随收缩 {before_h} → {bottom5.height()}px")
    else:
        print(f"  ⚠ 容器未收缩（仍 {bottom5.height()}px）——desc 可能未触发换行变化")


if __name__ == "__main__":
    main()
