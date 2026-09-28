# -*- coding: utf-8 -*-
"""T42 占位高度守恒的钉死/解除回归测试。

畸变场景（真机截图）：虚拟滚动批次重建时 `_apply_placeholder_height` 用
``setFixedHeight(H)`` 把卡片钉死（min=max=H）保证容器总高守恒。此后 viewer
高度上报只改 viewer 自身，卡片钉死值不跟随内容；H 与内容实际高度的差被卡片
布局压给仅有的两个可伸缩项（气泡容器 + 页脚栏）——页脚模型胶囊带边框，拉伸
后成竖长条，页脚行本身也被撑到数百像素高。修复：viewer 真实高度到达时解除
钉死，此刻内容自然高度 ≈ H，同一次布局收敛、无可见跳动。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.widgets.height_commit_batch import HeightCommitBatch
from app.widgets.message_card import MessageCard

_QWIDGETSIZE_MAX = 16777215


def _make_card(qapp, model_name="glm-5.3-flash", width=860):
    """建一张带 AlignBottom 容器的 assistant 卡（贴近 chat_layout 真实布局）。"""
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(6, 6, 6, 6)
    lay.setSpacing(8)
    lay.setAlignment(Qt.AlignBottom)
    card = MessageCard(role="assistant", model_name=model_name)
    card.setFixedWidth(width)
    lay.addWidget(card)
    container.resize(880, 1400)
    container.show()
    qapp.processEvents()
    return container, card


def _footer_h(card: MessageCard) -> int:
    return card._footer_bar.geometry().height()


class TestPinStretchRepro:
    """根因固化：钉死高度 ≠ 内容高度 → 页脚被拉伸。"""

    def test_pinned_card_stretches_footer(self, qapp):
        container, card = _make_card(qapp)
        try:
            base_h = _footer_h(card)
            card.pin_layout_height(900)  # 模拟 T42 钉死（回收瞬间实高）
            qapp.processEvents()
            # 未解除时：多余空间被气泡+页脚平分，页脚远超自然高度（~19px）
            assert _footer_h(card) > base_h * 3
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_unpin_restores_footer(self, qapp):
        container, card = _make_card(qapp)
        try:
            base_h = _footer_h(card)
            card.pin_layout_height(900)
            qapp.processEvents()
            card._unpin_layout_height()
            qapp.processEvents()
            assert _footer_h(card) == base_h
            assert card.minimumHeight() == 0
            assert card.maximumHeight() == _QWIDGETSIZE_MAX
        finally:
            container.deleteLater()
            qapp.processEvents()


class TestUnpinTriggers:
    """viewer 真实高度到达的每条链路都必须解除钉死。"""

    def test_commit_viewer_height_unpins(self, qapp):
        container, card = _make_card(qapp)
        try:
            card.pin_layout_height(900)
            card.viewer = _StubViewer()
            card._streaming = False
            card._commit_viewer_height(500)
            assert card._layout_height_pinned is False
            assert card.maximumHeight() == _QWIDGETSIZE_MAX
            assert card.viewer.height() == 500
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_batch_flush_unpins(self, qapp):
        from PyQt5.QtWidgets import QScrollArea

        container, card = _make_card(qapp)
        try:
            card.pin_layout_height(900)
            card.viewer = QWidget()
            card.viewer.setFixedHeight(40)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(container)
            scroll.resize(880, 400)
            scroll.show()
            qapp.processEvents()

            batch = HeightCommitBatch(scroll, container, lambda: False)
            batch.begin()
            batch.submit(card, 600)
            batch.flush()

            assert card._layout_height_pinned is False
            assert card.maximumHeight() == _QWIDGETSIZE_MAX
            assert card.viewer.height() == 600
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_unpin_idempotent(self, qapp):
        container, card = _make_card(qapp)
        try:
            card._unpin_layout_height()
            card._unpin_layout_height()  # 重复调用零副作用
            assert card.minimumHeight() == 0
            assert card.maximumHeight() == _QWIDGETSIZE_MAX
        finally:
            container.deleteLater()
            qapp.processEvents()


class TestPinHelper:
    """main_widget._apply_placeholder_height 走 pin_layout_height 打标。"""

    def test_pin_marks_and_fixes(self, qapp):
        container, card = _make_card(qapp)
        try:
            card.pin_layout_height(700)
            assert card._layout_height_pinned is True
            assert card.minimumHeight() == 700
            assert card.maximumHeight() == 700
            qapp.processEvents()
            assert card.height() == 700
            # 解除后恢复内容自适应（钉死标记同时清除）
            card._unpin_layout_height()
            qapp.processEvents()
            assert card._layout_height_pinned is False
            assert card.height() == card.sizeHint().height()
        finally:
            container.deleteLater()
            qapp.processEvents()


class _StubViewer:
    """最小 viewer 桩：_commit_viewer_height 直连路径够用。"""

    def __init__(self, height=40):
        self._height = height
        self.setFixedHeight_calls = []

    @property
    def height(self):
        return lambda: self._height

    @property
    def minimumHeight(self):
        return lambda: self._height

    def setFixedHeight(self, h):
        self.setFixedHeight_calls.append(h)
        self._height = h
