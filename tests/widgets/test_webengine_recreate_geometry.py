# -*- coding: utf-8 -*-
"""_on_webengine_need_recreate 重建路径：layout 不残留 / 高度 pin / 信号断开。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.widgets.card_viewers import CodeWebViewer  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

_QWIDGETSIZE_MAX = 16777215


def _make_card(old_h: int = 800, viewer_h: int = 600):
    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile()
    card = MessageCard(role="assistant")
    card.setFixedHeight(old_h)
    try:
        old = CodeWebViewer(card)
    except Exception as e:  # noqa: BLE001 - 无 WebEngine 环境直接跳过
        pytest.skip(f"CodeWebViewer 不可用: {e}")
    old.setFixedHeight(viewer_h)
    old._markdown_text = ""  # 跳过重建后的补渲
    card._viewer_layout.addWidget(old)
    card.viewer = old
    card._connect_viewer_signals()
    card._lazy_rendered = True
    return card, old


def test_recreate_removes_old_layout_item_and_pins_height():
    """重建后 layout 只有新 viewer（无 +1 残留），卡片高度不塌陷。"""
    card, old = _make_card()
    try:
        count_before = card._viewer_layout.count()
        height_before = card.height()
        assert count_before >= 1

        card._on_webengine_need_recreate()

        # 重建会清空正文区（含初始 QLabel 占位）后只放新 viewer：
        # 关键是旧 viewer 不残留，且不出现「两个 viewer」的 +1 残留。
        assert card._viewer_layout.count() == 1
        assert card._viewer_layout.itemAt(0).widget() is card.viewer
        assert card.viewer is not old
        assert count_before == 2  # QLabel 占位 + 旧 viewer
        assert abs(card.height() - height_before) < 2, (
            f"重建瞬间高度塌陷：{height_before} → {card.height()}"
        )
        assert card._layout_height_pinned is True
    finally:
        card.deleteLater()


def test_recreate_pins_then_unpins_on_real_height():
    """pin 在首个真实高度到达时解除，恢复自适应（非永久固定）。"""
    card, _old = _make_card()
    try:
        card._on_webengine_need_recreate()
        assert card._layout_height_pinned is True

        card._commit_viewer_height(900)

        assert card._layout_height_pinned is False
        assert card.minimumHeight() == 0
        assert card.maximumHeight() == _QWIDGETSIZE_MAX
    finally:
        card.deleteLater()


def test_recreate_disconnects_old_viewer_signals():
    """销毁旧 viewer 前必须先断信号，旧信号不得再打到卡片/新 viewer。"""
    card, old = _make_card()
    try:
        assert old.receivers(old.contentHeightChanged) > 0

        card._on_webengine_need_recreate()

        assert old.receivers(old.contentHeightChanged) == 0
        assert old.receivers(old.needRecreate) == 0
        assert card.viewer.receivers(card.viewer.contentHeightChanged) > 0
    finally:
        card.deleteLater()
