# -*- coding: utf-8 -*-
"""chat_area 模块 — 对话滚动区装配（源 main_widget.setup_ui L3076-3105）

属性契约（host.setattr）：
- chat_scroll_area  SingleDirectionScrollArea
- chat_container    QWidget（承载消息的容器）
- chat_layout       QVBoxLayout（chat_container 内部布局）
- _on_chat_scrolled 滚动条回调（connect scroll_bar.valueChanged）
"""

from app.plugins.contracts.ui_module import UIModule


class ChatAreaModule(UIModule):
    """对话区模块：top/bottom 卡容器间嵌入 chat scroll 区域"""

    module_id = "chat_area"

    def build(self, host) -> None:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
        from qfluentwidgets import SingleDirectionScrollArea

        # CHAT_SCROLL_STYLE 在 main_widget 模块级定义（避免循环引用）
        from app.main_widget import CHAT_SCROLL_STYLE

        layout = host.layout()

        # ── 对话滚动区 ──
        host.chat_scroll_area = SingleDirectionScrollArea(host)
        host.chat_scroll_area.setMinimumHeight(0)
        host.chat_scroll_area.setMinimumWidth(320)
        host.chat_scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        host.chat_scroll_area.setStyleSheet(CHAT_SCROLL_STYLE)
        host.chat_scroll_area.setWidgetResizable(True)
        host.chat_scroll_area.setViewportMargins(2, 2, 10, 2)
        host.chat_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        host.chat_container = QWidget()
        host.chat_container.setStyleSheet("background: transparent;")
        host.chat_container.setAcceptDrops(True)
        try:
            host.chat_container.installEventFilter(host)
        except Exception:
            pass
        host.chat_layout = QVBoxLayout(host.chat_container)
        host.chat_layout.setContentsMargins(6, 6, 6, 6)
        host.chat_layout.setSpacing(8)
        host.chat_layout.setAlignment(Qt.AlignBottom)
        host.chat_scroll_area.setWidget(host.chat_container)

        # 连接滚动事件（虚拟滚动回收）
        try:
            scroll_bar = host.chat_scroll_area.verticalScrollBar()
            scroll_bar.valueChanged.connect(host._on_chat_scrolled)
        except Exception:
            pass

        # ── 装配三段式：top 卡容器 → chat 滚动区 → bottom 卡容器 ──
        # _top_card_container / _bottom_card_container 已在 setup_ui 头部创建
        if getattr(host, "_top_card_container", None) is not None:
            layout.addWidget(host._top_card_container)
        layout.addWidget(host.chat_scroll_area, 1)
        if getattr(host, "_bottom_card_container", None) is not None:
            layout.addWidget(host._bottom_card_container)
