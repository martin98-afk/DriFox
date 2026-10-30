# -*- coding: utf-8 -*-
"""临时基准：测量 LLMSettingsCard.refresh_style 内部各段耗时（不入库）

跑法：.venv/Scripts/python.exe tests/_bench_settings_popup_refresh.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)


def main():
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    host = QWidget()
    host.resize(1000, 800)
    t0 = time.perf_counter()
    popup = LLMSettingsCard(host)
    print(f"构造 LLMSettingsCard: {(time.perf_counter() - t0) * 1000:.0f}ms")
    print(f"子树控件数: {len(popup.findChildren(QWidget))}")

    popup.show()
    host.show()
    for _ in range(3):
        app.processEvents()

    # 预热一次（首次含惰性构建）
    t0 = time.perf_counter()
    popup.refresh_style()
    app.processEvents()
    print(f"refresh_style 首次: {(time.perf_counter() - t0) * 1000:.0f}ms")

    for i in range(3):
        t0 = time.perf_counter()
        popup.refresh_style()
        app.processEvents()
        print(f"refresh_style 第{i + 2}次: {(time.perf_counter() - t0) * 1000:.0f}ms")

    # 拆到方法级
    from app.utils.design_tokens import CardStyles

    t0 = time.perf_counter()
    CardStyles.floating("SystemCardFrame", alpha=230)
    print(f"CardStyles.floating: {(time.perf_counter() - t0) * 1000:.2f}ms")

    ss = CardStyles.floating("SystemCardFrame", alpha=230)
    same = ss == popup.styleSheet()
    print(f"串相同? {same}")

    t0 = time.perf_counter()
    popup._apply_base_style()
    print(f"_apply_base_style: {(time.perf_counter() - t0) * 1000:.2f}ms")

    t0 = time.perf_counter()
    popup._apply_content_surface()
    print(f"_apply_content_surface: {(time.perf_counter() - t0) * 1000:.2f}ms")

    t0 = time.perf_counter()
    popup._refresh_content_children()
    print(f"_refresh_content_children: {(time.perf_counter() - t0) * 1000:.2f}ms")

    # 换主题色后重测（模拟真实切主题）
    from app.utils.design_tokens import Colors

    Colors.refresh()
    old = Colors.CARD_BG
    try:
        Colors.CARD_BG = "rgba(10, 20, 30, {alpha})"
        t0 = time.perf_counter()
        popup.refresh_style()
        app.processEvents()
        print(f"换色后 refresh_style: {(time.perf_counter() - t0) * 1000:.0f}ms")
    finally:
        Colors.CARD_BG = old


if __name__ == "__main__":
    main()
