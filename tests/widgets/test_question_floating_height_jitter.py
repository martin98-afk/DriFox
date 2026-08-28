# -*- coding: utf-8 -*-
"""QuestionFloatingWidget 高度抖动回归测试

== 问题描述 ==
用户偶尔看到提问卡片（QuestionFloatingWidget）高度抖动一下，
即使不开启自定义输入也会出现。

== 根因诊断 ==
有两个独立的抖动来源：

【来源A】容器层：CardContainer 动画与卡片的 Resize 事件形成自激循环
  1. _do_expand 开始动画 maxHeight: 0 → 200（200ms）
  2. 容器高度增长 → 内部卡片被撑大 → 卡片 Resize 事件触发
  3. eventFilter 捕获 Resize → _schedule_expand → _do_expand
  4. _do_expand 取消进行中的动画，重读 natural_h，重新开启动画
  5. 回到步骤 2，直到布局稳定
  每帧动画都被打断 → 肉眼可见的"抽一下"效果

【来源B】卡片层：_restore_answer 路径多发 heightChanged 信号
  - set_active(True) → QTimer(10, _emit_height_update) → heightChanged
  - _on_back/_on_next 末尾 → QTimer(0, heightChanged.emit)
  两条路径 10ms 内各发一次 → 容器动画被迫重启
  （已移除 20ms 冗余 timer，还剩 set_active 的 10ms 兜底）

== 修复 ==
1. 容器层：eventFilter 跳过动画运行时的卡片 Resize 事件
2. 卡片层：移除 set_active 中冗余的 QTimer(10, _emit_height_update)
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except Exception:
    pass

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import QEvent, QEventLoop, QPoint, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication


def _make_left_click():
    return QMouseEvent(
        QEvent.MouseButtonPress,
        QPoint(0, 0),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )


class _HeightChangeCounter:
    """统计指定时间窗内的 heightChanged 触发次数"""

    def __init__(self, target_widget, window_ms=80):
        self.target = target_widget
        self.window_ms = window_ms
        self.events = []  # [(t_ms, height)]
        self.t_start = None

    def __enter__(self):
        self.t_start = time.monotonic() * 1000
        self.target.heightChanged.connect(self._on_emit)
        return self

    def __exit__(self, *args):
        try:
            self.target.heightChanged.disconnect(self._on_emit)
        except Exception:
            pass

    def _on_emit(self):
        self.events.append((time.monotonic() * 1000 - self.t_start, self.target.height()))

    def rapid_emit_count(self) -> int:
        """80ms 窗内的发射次数"""
        return sum(1 for e in self.events if e[0] <= self.window_ms)


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _setup():
    app = QApplication.instance() or QApplication(sys.argv)
    return app


# ═══════════════════════════════════════════════════════════
# 场景 A：导航切换题目（无自定义输入，纯选项切换）
# ═══════════════════════════════════════════════════════════

def test_navigation_without_custom_input():
    """前进/返回切换题目（无自定义输入）→ heightChanged ≤ 1 次"""
    app = _setup()
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    widget = QuestionFloatingWidget()
    questions = [
        {"question": "Q1（2选项）", "options": [{"label": "是"}, {"label": "否"}], "multiple": False},
        {"question": "Q2（5选项）", "options": [
            {"label": "A"}, {"label": "B"}, {"label": "C"}, {"label": "D"}, {"label": "E"}
        ], "multiple": False},
    ]
    widget.show_question(questions, show_custom_input=False)
    _pump(150)

    # 前进
    counter = _HeightChangeCounter(widget)
    counter.__enter__()
    try:
        widget._on_next()
        _pump(400)
    finally:
        counter.__exit__()
    print(f"[前进·无自定义] heightChanged: {counter.rapid_emit_count()} 次")
    assert counter.rapid_emit_count() <= 1, (
        f"前进（无自定义）触发 {counter.rapid_emit_count()} 次 heightChanged"
    )

    # 返回
    counter2 = _HeightChangeCounter(widget)
    counter2.__enter__()
    try:
        widget._on_back()
        _pump(400)
    finally:
        counter2.__exit__()
    print(f"[返回·无自定义] heightChanged: {counter2.rapid_emit_count()} 次")
    assert counter2.rapid_emit_count() <= 1, (
        f"返回（无自定义）触发 {counter2.rapid_emit_count()} 次 heightChanged"
    )


# ═══════════════════════════════════════════════════════════
# 场景 B：打开问题卡片（首次 show）→ 无多余信号
# ═══════════════════════════════════════════════════════════

def test_initial_show_without_custom_input():
    """首次显示问题卡片（show_question）→ heightChanged ≤ 1 次"""
    app = _setup()
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    widget = QuestionFloatingWidget()

    counter = _HeightChangeCounter(widget)
    counter.__enter__()
    try:
        widget.show_question(
            [{"question": "测试问题？", "options": [
                {"label": "A"}, {"label": "B"}, {"label": "C"}
            ], "multiple": False}],
            show_custom_input=False,
        )
        _pump(300)
    finally:
        counter.__exit__()
    print(f"[首次显示] heightChanged: {counter.rapid_emit_count()} 次")
    assert counter.rapid_emit_count() <= 1, (
        f"首次显示触发 {counter.rapid_emit_count()} 次 heightChanged"
    )


# ═══════════════════════════════════════════════════════════
# 场景 C：多题型混切（单选→多选→回退）
# ═══════════════════════════════════════════════════════════

def test_mixed_type_navigation():
    """单选→多选切换（布局变化最大场景）→ heightChanged ≤ 1 次"""
    app = _setup()
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    widget = QuestionFloatingWidget()
    questions = [
        {"question": "单选", "options": [{"label": "A"}, {"label": "B"}], "multiple": False},
        {"question": "多选", "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}], "multiple": True},
    ]
    widget.show_question(questions, show_custom_input=False)
    _pump(150)

    counter = _HeightChangeCounter(widget)
    counter.__enter__()
    try:
        widget._on_next()
        _pump(400)
    finally:
        counter.__exit__()
    print(f"[单选→多选] heightChanged: {counter.rapid_emit_count()} 次")
    assert counter.rapid_emit_count() <= 1, (
        f"单选→多选切换触发 {counter.rapid_emit_count()} 次 heightChanged"
    )


# ═══════════════════════════════════════════════════════════
# 场景 D：返回带自定义输入答案的题（升级版）
# ═══════════════════════════════════════════════════════════

def test_back_with_saved_custom_input():
    """返回带自定义输入答案的题 → heightChanged ≤ 1 次"""
    app = _setup()
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    widget = QuestionFloatingWidget()
    questions = [
        {"question": "Q1", "options": [{"label": "是"}, {"label": "否"}], "multiple": False},
        {"question": "Q2", "options": [{"label": "A"}, {"label": "B"}], "multiple": False},
    ]
    widget.show_question(questions, show_custom_input=True)
    _pump(150)

    custom = widget._custom_input_widget
    custom.mousePressEvent(_make_left_click())
    _pump(50)
    custom._text_edit.setPlainText("第一题的自定义答案")
    _pump(50)
    widget._on_next()  # 到 Q2
    _pump(150)

    counter = _HeightChangeCounter(widget)
    counter.__enter__()
    try:
        widget._on_back()  # 返回 Q1，恢复自定义输入答案
        _pump(400)
    finally:
        counter.__exit__()
    print(f"[返回·有自定义] heightChanged: {counter.rapid_emit_count()} 次")
    for t, h in counter.events:
        print(f"  t={t:6.1f}ms, height={h}px")
    assert counter.rapid_emit_count() <= 1, (
        f"返回（有自定义）触发 {counter.rapid_emit_count()} 次 heightChanged"
    )


if __name__ == "__main__":
    print("=" * 70)
    print("QuestionFloatingWidget 高度抖动回归测试 v2")
    print("=" * 70)

    results = []
    for name, fn in [
        ("A: 前进/返回无自定义", test_navigation_without_custom_input),
        ("B: 首次显示", test_initial_show_without_custom_input),
        ("C: 单选→多选切换", test_mixed_type_navigation),
        ("D: 返回·有自定义输入", test_back_with_saved_custom_input),
    ]:
        print(f"\n{'#'*70}\n# {name}\n{'#'*70}")
        try:
            fn()
            results.append((name, "✅"))
        except AssertionError as e:
            print(f"❌ {e}")
            results.append((name, "❌"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"❌ 异常: {e}")
            results.append((name, "⚠️"))

    print("\n" + "=" * 70)
    print("结果汇总")
    print("=" * 70)
    for name, res in results:
        print(f"  {res} {name}")