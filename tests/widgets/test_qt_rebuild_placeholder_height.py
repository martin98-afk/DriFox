"""虚拟滚动重建纯 Qt 卡片时，占位钉死必须随真实高度解除。"""

import pytest
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.widgets.message_card import MessageCard


@pytest.mark.parametrize("report_before_pin", [False, True])
def test_qt_rebuild_releases_placeholder_height(qapp, monkeypatch, report_before_pin):
    """覆盖先装占位再渲染，以及同步渲染完成后才归还占位的两种时序。"""
    monkeypatch.setattr("app.widgets.message_card._should_use_qt_renderer", lambda *a, **kw: True)
    container = QWidget()
    layout = QVBoxLayout(container)
    card = MessageCard(role="assistant", parent=container)
    layout.addWidget(card)
    container.resize(700, 1200)
    container.show()
    qapp.processEvents()
    card._streaming = False
    card._pending_content = "# 历史消息\n\n" + "这是重建后的普通正文。\n\n" * 8
    try:
        if not report_before_pin:
            card.pin_layout_height(900, content_overhead=100)
        card.ensure_rendered()
        qapp.processEvents()
        assert card._viewer_async_height is False
        if report_before_pin:
            card.pin_layout_height(900, content_overhead=100)
            qapp.processEvents()
        assert card._layout_height_pinned is False
        assert card.maximumHeight() == 16777215
    finally:
        container.deleteLater()
        qapp.processEvents()
