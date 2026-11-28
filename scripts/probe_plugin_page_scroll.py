# -*- coding: utf-8 -*-
"""临时探针：真实 LLMSettingsCard 插件页几何（用完即删）。"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget


def main() -> None:
    app = QApplication(sys.argv)
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    win = QWidget()
    win.resize(1120, 1000)
    layout = QVBoxLayout(win)
    layout.setContentsMargins(0, 0, 0, 0)
    card = LLMSettingsCard(win)
    layout.addWidget(card)
    card.show()
    win.show()
    app.processEvents()

    card.rebuild_plugin_cards(force=True)
    card._set_active_page("plugins")
    for _ in range(30):
        app.processEvents()

    page = card.pluginSettingsPage
    outer = card._page_scrolls["plugins"]
    print("== outer page ==")
    print("outer viewport", outer.viewport().size(), "vbar max", outer.verticalScrollBar().maximum())
    print("outer widget h", outer.widget().height())
    print("== plugin page ==", page.geometry(), "visible", page.isVisible())
    print("sidebar", page._sidebar.geometry())
    print("search", page._search.geometry())
    print("list_area", page._list_area.geometry(), "vbar max", page._list_area.verticalScrollBar().maximum())
    print("detail_frame", page._detail_frame.geometry())
    print("detail view", page._detail.geometry())
    for key, sc in page._detail._pages.items():
        print(f"  detail scroll[{key}]", sc.geometry(), "vbar max", sc.verticalScrollBar().maximum())
    print("page_title", card._page_title.geometry(), "back_btn", card._back_btn.geometry())
    print("stack", card._pages_stack.geometry())


if __name__ == "__main__":
    main()