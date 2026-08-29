# -*- coding: utf-8 -*-
"""QuestionFloatingWidget 卡内文字抖动回归测试

== 问题描述 ==
提问卡片（QuestionFloatingWidget）偶尔在出现时，卡内文字（问题标题、提示行、
选项、底栏按钮）整体上下抖动，同时主线程明显卡顿一下。长问题（标题区触发
内部滚动条）时尤其容易出现。

== 根因：布局自激循环 ==
_AutoHeightScrollArea 用 **viewport().width()** 作为测量基准，而该宽度会随
垂直滚动条的出现/消失跳变，形成闭合回路：

    _do_expand → 容器锁高 → 滚动区高度变化 → 滚动条出现 → viewport 变窄
    → 标题重排变高 → sizeHint 变化 → QLabel LayoutRequest → _sync_question_area
    → heightChanged → _do_expand →（回到起点）

测量基准与"布局最终给到内容的宽度"不一致 → 不存在不动点 → 永不收敛。
实测超长问题场景 1.5s 内 CardContainer._do_expand 被调用 1.7 万次，主线程被
布局占满（1500ms 只采到 8 帧），容器高度在 376↔348 间反复横跳，卡内文字
（QLabel / 按钮垂直居中）随之上下位移——这就是用户看到的"文字抖动"。

== 修复 ==
1. _AutoHeightScrollArea 实现 hasHeightForWidth / heightForWidth，并抽出
   _height_for_width(outer_w)：以**控件外宽**为唯一输入，内部自行扣除 frame
   与（需要滚动时的）滚动条宽度 → 同一宽度恒定输出同一高度 → 存在不动点。
   父布局 QVBoxLayout.heightForWidth 因此也用真实宽度测量，不再读 viewport
   的瞬时宽度。
2. QuestionFloatingWidget 高度通知合并（_emit_height_changed）：同一轮事件
   循环内多个通知源（问题区重排 / 选项区 / resize / 折叠）只发一次，避免
   连续逼迫容器重排。

== 回归断言 ==
- 展开次数不爆炸（修复前 1.7 万次，现 ≤ 30）
- 出现过程结束后几何完全静止，卡内文字 y 最多变化 1 次（首帧布局）
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt

Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PyQt5.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget  # noqa: E402

# 长问题：标题区内容超过最大高度 → 触发内部滚动条（抖动必现场景）
LONG_QUESTION = [
    {
        "question": "超长问题：这是一段很长的描述文字用于撑满标题区的最大高度限制，"
        "继续补充更多的内容以便超过窗口高度百分之三十的上限从而触发内部滚动条的出现，"
        "再继续写一些内容确保一定超过限制，再多写一点，再多写一点，再多写一点，"
        "最后再来一句收尾的话让内容足够长。",
        "options": [{"label": "选项一"}, {"label": "选项二"}],
        "multiple": False,
    }
]

SAMPLE_MS = 5
TOTAL_MS = 700
STABLE_MS = 400  # 400ms 后必须完全静止


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _make_scene(win_w=900, win_h=800):
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    win = QWidget()
    outer = QVBoxLayout(win)
    chat = QWidget()
    chat.setMinimumHeight(400)
    splitter = QSplitter()
    splitter.setOrientation(1)
    splitter.addWidget(chat)
    splitter.setStretchFactor(0, 1)
    container = BottomCardContainer()
    splitter.addWidget(container)
    splitter.setStretchFactor(1, 0)
    outer.addWidget(splitter)
    # 必须显式固定窗口尺寸：offscreen 下若只 resize，窗口会被内容 sizeHint
    # 反向撑动 → 容器"宽度随高度变化"的假象，污染测量基准。
    win.setFixedSize(win_w, win_h)
    win.show()
    splitter.setSizes([win_h - 300, 300])
    _pump(80)

    container.enable_dock_mode(splitter)
    card = QuestionFloatingWidget(container)
    container.add_card("question", card)
    _pump(50)
    return win, container, card


def _count_do_expand():
    """统计 CardContainer._do_expand 调用次数"""
    from app.widgets.cards.card_container import CardContainer

    stats = {"n": 0}
    orig = CardContainer._do_expand

    def patched(self):
        stats["n"] += 1
        return orig(self)

    CardContainer._do_expand = patched
    return stats, orig


def _sample(card):
    """采样卡片内文字控件相对卡片顶部的 y 坐标"""
    return (
        card._question_label.mapTo(card, card._question_label.rect().topLeft()).y(),
        card._hint_label.mapTo(card, card._hint_label.rect().topLeft()).y(),
        card._option_widgets[0].mapTo(card, card._option_widgets[0].rect().topLeft()).y()
        if card._option_widgets
        else -1,
        card._next_btn.mapTo(card, card._next_btn.rect().topLeft()).y(),
    )


def test_long_question_no_layout_livelock():
    """长问题（触发滚动条）：不出现布局自激循环，展开次数收敛"""
    app = QApplication.instance() or QApplication(sys.argv)
    win, container, card = _make_scene()

    stats, orig = _count_do_expand()
    try:
        card.setVisible(True)
        card.show_question(LONG_QUESTION, show_custom_input=False)
        _pump(TOTAL_MS)
    finally:
        from app.widgets.cards.card_container import CardContainer

        CardContainer._do_expand = orig

    print(f"[长问题] _do_expand={stats['n']} 次")
    # 修复前 1.7 万次；修复后个位数。阈值 30 留足环境噪声余量
    assert stats["n"] <= 30, (
        f"出现 {stats['n']} 次 _do_expand，疑似布局自激循环（阈值 30）"
    )
    win.close()


def test_text_position_stable_after_show():
    """卡片出现后卡内文字位置静止（变化 ≤ 1 次，仅首帧布局）"""
    app = QApplication.instance() or QApplication(sys.argv)
    win, container, card = _make_scene()

    rows = []
    t0 = time.monotonic() * 1000
    timer = QTimer()
    timer.setInterval(SAMPLE_MS)
    timer.timeout.connect(lambda: rows.append((time.monotonic() * 1000 - t0, _sample(card))))

    timer.start()
    card.setVisible(True)
    card.show_question(LONG_QUESTION, show_custom_input=False)
    _pump(TOTAL_MS)
    timer.stop()

    assert len(rows) > 50, f"主线程被阻塞，{TOTAL_MS}ms 只采到 {len(rows)} 帧"

    changes = 0
    prev = None
    for t, pos in rows:
        if prev is None:
            prev = pos
            continue
        if pos != prev:
            changes += 1
            prev = pos
    print(f"[长问题] 文字位置变化 {changes} 次 / {len(rows)} 帧")
    assert changes <= 1, f"卡内文字位置变化 {changes} 次（>1 即为抖动）"

    # 稳定窗口内必须完全静止
    tail = [pos for t, pos in rows if t >= STABLE_MS]
    assert len(set(tail)) == 1, f"{STABLE_MS}ms 之后几何仍在变化: {sorted(set(tail))}"
    win.close()


def test_short_question_stable():
    """短问题（无滚动条）：同样不抖动"""
    app = QApplication.instance() or QApplication(sys.argv)
    win, container, card = _make_scene()

    timer = QTimer()
    rows = []
    t0 = time.monotonic() * 1000
    timer.setInterval(SAMPLE_MS)
    timer.timeout.connect(lambda: rows.append((time.monotonic() * 1000 - t0, _sample(card))))

    timer.start()
    card.setVisible(True)
    card.show_question(
        [{"question": "使用哪个框架？", "options": [{"label": "A"}, {"label": "B"}], "multiple": False}],
        show_custom_input=False,
    )
    _pump(TOTAL_MS)
    timer.stop()

    tail = [pos for t, pos in rows if t >= STABLE_MS]
    assert len(set(tail)) == 1, f"短问题场景 {STABLE_MS}ms 后仍在变化: {sorted(set(tail))}"
    win.close()


def test_scroll_area_measurement_is_deterministic():
    """_AutoHeightScrollArea 测量必须是纯函数：同宽度恒定输出，与滚动条状态无关"""
    app = QApplication.instance() or QApplication(sys.argv)
    from app.widgets.cards.floating.question_floating_widget import _AutoHeightScrollArea

    sc = _AutoHeightScrollArea()
    from PyQt5.QtWidgets import QLabel

    label = QLabel("测量确定性测试：" + "长文本" * 40)
    label.setWordWrap(True)
    sc.setWidget(label)
    sc.setWidgetResizable(True)
    sc.setMaximumHeight(240)
    sc.resize(400, 300)
    sc.show()
    _pump(80)

    assert sc.hasHeightForWidth(), "_AutoHeightScrollArea 必须声明 heightForWidth"
    # 同一宽度重复测量 → 结果必须完全一致（修复前随 viewport 宽度漂移）
    vals = {sc.heightForWidth(400) for _ in range(5)}
    assert len(vals) == 1, f"同一宽度的 heightForWidth 结果不稳定: {vals}"
    # 宽度越大 → 高度越小（单调性），且不超过 maximumHeight
    h_narrow = sc.heightForWidth(300)
    h_wide = sc.heightForWidth(600)
    assert h_narrow >= h_wide, f"高度未随宽度单调变化: 300px→{h_narrow}, 600px→{h_wide}"
    assert h_wide <= sc.maximumHeight(), f"高度 {h_wide} 超过 maximumHeight {sc.maximumHeight()}"
    sc.close()


if __name__ == "__main__":
    print("=" * 70)
    print("QuestionFloatingWidget 卡内文字抖动回归测试")
    print("=" * 70)
    ok = True
    for fn in (
        test_long_question_no_layout_livelock,
        test_text_position_stable_after_show,
        test_short_question_stable,
        test_scroll_area_measurement_is_deterministic,
    ):
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except Exception:
            import traceback

            traceback.print_exc()
            ok = False
            print(f"❌ {fn.__name__}")
    sys.exit(0 if ok else 1)
