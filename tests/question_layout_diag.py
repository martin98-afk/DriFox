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

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401


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


def make_container_and_question(initial_sizes, questions):
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    container = BottomCardContainer()
    fake_sp = FakeSplitter(initial_sizes)
    container.enable_dock_mode(fake_sp)

    q = QuestionFloatingWidget()
    q.show_question(questions)
    container.add_card("question", q)
    return container, q, fake_sp


def report(label, container, fake_sp, q):
    natural_h = q.layout().sizeHint().height()
    print(f"\n--- {label} ---")
    print(f"  layout.sizeHint:       {natural_h}px")
    print(f"  container:             max={container.maximumHeight()} min={container.minimumHeight()} h={container.height()}")
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

    print("\n========== 场景 A：splitter 缓存分配 500px（远大于 natural_h ~397）==========")
    container, q, sp = make_container_and_question([500, 500], SHORT_Q)
    q.setVisible(True)
    container._do_expand()
    report("A.1 首次 _do_expand", container, sp, q)
    container._do_expand()
    report("A.2 再次 _do_expand", container, sp, q)

    print("\n========== 场景 B：连续三次 _do_expand（diff<2px 边界）==========")
    container2, q2, sp2 = make_container_and_question([400, 600], SHORT_Q)
    q2.setVisible(True)
    container2._do_expand()
    report("B.1 首次", container2, sp2, q2)
    container2._do_expand()
    report("B.2 连续二次", container2, sp2, q2)
    container2._do_expand()
    report("B.3 连续三次", container2, sp2, q2)

    print("\n========== 场景 C：问题内容变化（短 → 长 → 短）==========")
    container3, q3, sp3 = make_container_and_question([300, 700], SHORT_Q)
    q3.setVisible(True)
    container3._do_expand()
    report("C.1 短问题（首展）", container3, sp3, q3)
    q3.show_question(LONG_Q)
    container3._do_expand()
    report("C.2 切到长问题", container3, sp3, q3)
    q3.show_question(SHORT_Q)
    container3._do_expand()
    report("C.3 切回短问题", container3, sp3, q3)


if __name__ == "__main__":
    main()
