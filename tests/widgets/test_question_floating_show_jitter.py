# -*- coding: utf-8 -*-
"""提问卡片出现时内部元素抖动回归测试：容器隐藏时 skip_anim 误判

== 问题描述 ==
提问卡片出现时偶尔内部元素快速抖动。

== 根因 ==
`CardContainer._should_skip_animation()` 用 `isVisible()` 判断可见卡片，
而同函数调用方 `_do_expand` 中 `has_visible` / `_visible_cards_follow_content`
用 `isHidden()`（意图语义）。容器处于折叠态（hide）时显示 followContent 卡片：

  card.setVisible(True)        # 自身置可见，但父容器 hide → isVisible()=False
  → _schedule_expand → _do_expand
     skip_anim = _should_skip_animation()  # ❌ False（父链断导致误判）
  → 启动 200ms 展开动画
  → 紧随其后的 singleShot 链（show_question / showEvent 的 heightChanged）
    再次触发 _do_expand → stop 动画 + snap
  → 出现瞬间高度多轮跳变 = 内部元素快速抖动（时序竞态 → 偶发）

== 修复 ==
`_should_skip_animation()` 改用 `isHidden()`，与 has_visible /
follow_content 判定语义对齐："不参与动画"是卡片意图属性，不受父链可见性影响。

本文件固化该行为，防止后续回归。
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
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget

from app.widgets.cards.card_container import BottomCardContainer, CardContainer, ContainerType
from app.widgets.cards.card_manager import CardManager
from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

_QUESTIONS = [
    {
        "question": "选择实现方案：",
        "options": [
            {"label": "方案A", "description": "使用状态机重构，保留现有对外接口不变"},
            {"label": "方案B", "description": "最小改动，在现有回调里加判断分支"},
        ],
        "multiple": False,
    }
]

_WID_SEQ = [0]


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _make_harness():
    """真实结构：纵向 splitter + 折叠态 bottom 容器 + question 卡片"""
    app = QApplication.instance() or QApplication(sys.argv)

    host = QWidget()
    host.resize(1200, 800)
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    vsplitter = QSplitter(Qt.Vertical)
    vsplitter.setChildrenCollapsible(False)
    outer.addWidget(vsplitter)
    chat = QWidget()
    vsplitter.addWidget(chat)
    container = BottomCardContainer()
    vsplitter.addWidget(container)
    container.enable_dock_mode(vsplitter)

    _WID_SEQ[0] += 1
    wid = f"jit-{_WID_SEQ[0]}"
    card = QuestionFloatingWidget(host)
    card.setVisible(False)
    container.add_card("question", card)
    mgr = CardManager.get_instance()
    mgr.register_window(wid)
    container.bind_card_manager(mgr, wid)
    mgr.register_card(wid, ContainerType.BOTTOM, "question", card)

    host.show()
    _pump(50)
    assert container.isHidden(), "前置失败：容器应处于折叠隐藏态"
    return app, host, container, card


def test_skip_animation_uses_hidden_semantics():
    """容器隐藏时显示声明 NO_ANIMATION 的卡片，_should_skip_animation 必须返回 True

    isVisible() 受父链可见性影响（容器 hide → False），
    isHidden() 只看卡片自身意图（False=意图可见）。
    二者不一致时，首次展开会误启动 200ms 动画，随后被 heightChanged
    链取消转 snap，造成出现瞬间高度多轮跳变。
    """
    _, host, container, card = _make_harness()

    card.show_question(_QUESTIONS)
    _pump(30)

    # 真实时序：容器仍隐藏时卡片 setVisible(True)
    assert container.isHidden(), "前置失败：容器应仍处于隐藏态"
    card.setVisible(True)

    assert card.isHidden() is False, "卡片意图应为可见"
    assert card.isVisible() is False, "前置确认：父容器隐藏时 isVisible 应为 False（时序成立条件）"

    # 修复点：skip 判定必须用意图语义（isHidden），不受父链断影响
    assert container._should_skip_animation() is True, (
        "容器隐藏时 _should_skip_animation 误判 False："
        "followContent 卡片首次展开将误启动 200ms 动画，"
        "随后被 singleShot 链取消转 snap，出现瞬间高度多轮跳变（抖动根因）"
    )


def test_no_animation_started_on_show_while_container_hidden():
    """容器隐藏时 show 卡片并触发 _do_expand，不得启动展开动画"""
    _, host, container, card = _make_harness()

    card.show_question(_QUESTIONS)
    _pump(30)

    anim_log = []
    orig_anim = container._animate_height

    def traced_anim(start_h, end_h, on_finished=None):
        anim_log.append((start_h, end_h))
        orig_anim(start_h, end_h, on_finished)

    container._animate_height = traced_anim
    try:
        # 复刻 _on_question_asked：容器隐藏时 show_card → _on_card_shown → _do_expand
        card.setVisible(True)
        container._do_expand()

        assert not anim_log, (
            f"出现瞬间启动了展开动画 {anim_log}：动画将被后续 heightChanged 链取消转 snap，"
            "高度多轮跳变即用户看到的内部元素快速抖动"
        )
        # snap 路径生效：容器高度应立即到位
        assert container.height() > 0, "snap 后容器高度应为内容高度"
    finally:
        container._animate_height = orig_anim


if __name__ == "__main__":
    print("=" * 70)
    print("提问卡片出现抖动回归测试（skip_anim 语义修复）")
    print("=" * 70)
    test_skip_animation_uses_hidden_semantics()
    print("✅ 1: skip 判定用 isHidden 语义")
    test_no_animation_started_on_show_while_container_hidden()
    print("✅ 2: 容器隐藏时 show 不启动动画")
