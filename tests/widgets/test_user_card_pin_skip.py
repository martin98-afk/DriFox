# -*- coding: utf-8 -*-
"""用户卡片 T42 跳钉回归测试（长历史会话上方用户卡片高度偶现异常）。

根因：PlainTextViewer 是同步定型 viewer——set_text 当拍即 setFixedSize 到实测
尺寸，contentHeightChanged emit 时 viewer.height() 已等于上报值，卡片侧
_update_height 的 ±2px 噪声死区（target == current）恒命中 return，高度永远
到不了 _apply_viewer_height 的 unpin 出口。T42 重建时序又是「建卡（emit 已被
死区消费）→ pin_layout_height 钉死」，钉死后不再有任何自然上报 → 卡片永久
min=max=占位高度 H。H 与真实内容高的任何偏差（批次均摊、卸载到重建之间的
宽度变化、字体度量漂移）都被固化成永久高度错误。

修复：pin_layout_height 对 role == "user" 跳过钉死，高度交回布局按
PlainTextViewer.sizeHint()（实测值）自然分配。assistant 卡的 CodeWebViewer
异步上报（起步 40 → 真实高的差 > 2px）不受死区影响，钉死行为不变。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.widgets.message_card import MessageCard

_QWIDGETSIZE_MAX = 16777215


def _make_user_card(qapp, text: str, width=860):
    """建一张贴近 chat_layout 布局的 user 卡，并走完 sync_width → set_content 链。"""
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(6, 6, 6, 6)
    lay.setSpacing(8)
    lay.setAlignment(Qt.AlignBottom)
    card = MessageCard(role="user")
    card.setFixedWidth(width)
    lay.addWidget(card)
    container.resize(880, 1400)
    container.show()
    qapp.processEvents()
    # 真实链路顺序：宽度上限注入先于内容（历史加载为 set_content → sync_width，
    # 此处统一 force 先 sync，保证 viewer cap 就位后测高）
    card.sync_width(force=True)
    card.set_content(text)
    qapp.processEvents()
    return container, card


class TestUserCardPinSkip:
    """user 卡 pin 必须跳过钉死（高度由布局自然分配）。"""

    def test_pin_does_not_fix_height(self, qapp):
        text = "第一行\n第二行\n第三行\n第四行\n第五行\n第六行"
        container, card = _make_user_card(qapp, text)
        try:
            natural_h = card.height()
            viewer_h = card.viewer.height()
            assert viewer_h >= 40
            # 模拟 T42 重建：占位高度与真实内容高不同
            card.pin_layout_height(90, content_overhead=60)
            qapp.processEvents()
            assert card._layout_height_pinned is False
            # 高度未被钉在占位值：保持内容自然高度
            assert card.height() == natural_h
            assert card.minimumHeight() != 90
            assert card.maximumHeight() != 90
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_pin_taller_than_content_does_not_stretch(self, qapp):
        """占位高度大于内容高：不拉伸（P053 页脚畸变的 user 卡变体）。"""
        text = "短消息"
        container, card = _make_user_card(qapp, text)
        try:
            natural_h = card.height()
            card.pin_layout_height(600, content_overhead=60)
            qapp.processEvents()
            assert card._layout_height_pinned is False
            assert card.height() == natural_h
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_width_change_after_pin_still_follows(self, qapp):
        """跳钉后宽度变化（窗口 resize）高度正常跟随，无钉死残留。"""
        text = "这一条消息足够长，需要在宽度收窄之后发生重新折行才能观察到高度变化。"
        container, card = _make_user_card(qapp, text)
        try:
            card.pin_layout_height(120, content_overhead=60)
            qapp.processEvents()
            before = card.height()
            # 收窄宽度 cap → 重新折行 → viewer 变高 → 布局跟随
            card.viewer.set_width_cap(300)
            card.viewer.update_height()
            qapp.processEvents()
            assert card.height() >= before
            assert card._layout_height_pinned is False
            assert card.maximumHeight() == _QWIDGETSIZE_MAX
        finally:
            container.deleteLater()
            qapp.processEvents()
