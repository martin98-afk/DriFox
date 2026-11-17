# -*- coding: utf-8 -*-
"""[A2] 浮动卡弹出过渡回归：show_card 主路径接 fade_widget 弹入动画

背景：分享/命令/权限等浮动卡此前「凭空弹出」（setVisible 直显）。
修复：card_manager.show_card 主路径（覆盖层/补全弹出）显显后调
_animate_card_popup → motion.fade_widget（ENTER_MS + EASE_ENTER），
动画结束摘除 opacity effect（不留常驻离屏合成）。已可见早退不重放；
dock 停靠分支（侧栏嵌入形态）不做弹出动画。
"""

import pytest
from PyQt5.QtWidgets import QGraphicsOpacityEffect, QWidget


def _ensure_qapp():
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _CountCard(QWidget):
    """带可见性计数的桩卡（无 show_card 方法 → 走 setVisible 分支）"""

    def __init__(self):
        super().__init__()
        self.setVisible(False)
        self.show_count = 0

    def setVisible(self, on: bool):  # noqa: N802 — Qt 签名
        if on:
            self.show_count += 1
        super().setVisible(on)


@pytest.fixture()
def mgr_with_card(qtbot):
    from app.widgets.cards.card_manager import CardManager, ContainerType

    _ensure_qapp()
    mgr = CardManager.get_instance()
    card = _CountCard()
    qtbot.addWidget(card)
    mgr.register_card("w-a2", ContainerType.TOP, "a2:popup", card, system_card=False)
    qtbot.addWidget(card)
    yield mgr, card
    mgr.unregister_card("w-a2", "a2:popup")


def test_first_show_triggers_fade_effect(mgr_with_card, qtbot):
    """首次弹出：应挂 opacity effect（动画进行/收尾期）且卡可见"""
    mgr, card = mgr_with_card
    mgr.show_card("a2:popup", "w-a2")
    assert card.isVisible() or not card.isHidden(), "弹出后必须可见"
    # 动画已启动：fade effect 已挂（挂在卡上或 _fade_effect 引用）
    effect = card.graphicsEffect()
    assert effect is not None and isinstance(effect, QGraphicsOpacityEffect), "首次弹出应有淡入过渡"
    assert card.show_count == 1


def test_second_show_no_replay(mgr_with_card, qtbot):
    """已可见再调 show_card：早退不重放动画（不闪）"""
    mgr, card = mgr_with_card
    mgr.show_card("a2:popup", "w-a2")
    qtbot.wait(300)  # 等 ENTER_MS 动画收尾（effect 被摘除）
    assert card.graphicsEffect() is None, "动画收尾应摘除 effect（无常驻离屏合成）"
    # 已可见 → 早退：不重挂动画 effect（若重放会预置新 opacity=0 effect）
    mgr.show_card("a2:popup", "w-a2")
    assert card.graphicsEffect() is None, "已可见早退不得重挂动画（重放会闪）"


def test_fade_widget_curve_passthrough(qtbot):
    """fade_widget 新增 curve 参数透传 retarget（动画按指定缓动运行）"""
    from PyQt5.QtCore import QEasingCurve

    from app.utils.design_tokens import Animations
    from app.utils.motion import fade_widget

    _ensure_qapp()
    w = QWidget()
    qtbot.addWidget(w)
    effect = QGraphicsOpacityEffect(w)
    effect.setOpacity(0.0)
    w.setGraphicsEffect(effect)
    w._fade_effect = effect
    started = fade_widget(w, 1.0, duration=Animations.ENTER_MS, curve=Animations.EASE_ENTER)
    assert started is True, "差值 0→1 应启动动画"
    assert w._fade_anim.easingCurve().type() == QEasingCurve.OutCubic, "curve 应透传到动画对象"
    w.setGraphicsEffect(None)


def test_fade_widget_survives_effect_lifecycle(qtbot):
    """effect 摘除→重建后再次淡入仍能跑完（第二次弹出整卡透明的回归）

    旧实现只按属性有无判断缓存：第一次淡入收尾 setGraphicsEffect(None)
    销毁 effect，但 widget._fade_anim 仍指向绑定该 effect 的动画对象；第二次
    淡入复用这个 target 已失效的动画 → start 立即停摆、finished 不发射，
    预置的 opacity=0 effect 永久留在控件上。断言两轮都能摘除 effect。
    """
    from app.utils.motion import fade_widget

    _ensure_qapp()
    w = QWidget()
    qtbot.addWidget(w)
    w.show()

    for round_index in (1, 2):
        effect = QGraphicsOpacityEffect(w)
        effect.setOpacity(0.0)
        w.setGraphicsEffect(effect)
        w._fade_effect = effect
        fade_widget(w, 1.0, duration=80, on_finished=lambda: w.setGraphicsEffect(None))
        qtbot.wait(250)
        assert w.graphicsEffect() is None, f"第 {round_index} 轮淡入未收尾，effect 残留（整卡透明）"


def test_second_show_after_hide_fades_in(mgr_with_card, qtbot):
    """关闭后再次弹出：淡入必须完整收尾，不得留下 opacity=0 的常驻 effect"""
    mgr, card = mgr_with_card
    mgr.show_card("a2:popup", "w-a2")
    qtbot.wait(300)
    assert card.graphicsEffect() is None, "第一次弹出应正常收尾"
    mgr.hide_card("a2:popup", "w-a2")
    assert card.isHidden(), "隐藏后卡片应不可见"
    mgr.show_card("a2:popup", "w-a2")
    qtbot.wait(300)
    assert card.graphicsEffect() is None, "第二次弹出效果必须收尾（否则卡片永久透明）"
    assert not card.isHidden(), "第二次弹出卡片必须可见"
