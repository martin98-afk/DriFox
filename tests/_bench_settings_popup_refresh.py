# -*- coding: utf-8 -*-
"""临时基准：测量 LLMSettingsCard.refresh_style 内部各段耗时（不入库）

跑法：.venv/Scripts/python.exe tests/_bench_settings_popup_refresh.py

关键：必须先 _ensure_page_ready 补齐全部页签，否则控件数只有首屏规模，
换色刷新耗时会比真机低 3~4 倍，测不准。
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
logging.disable(logging.CRITICAL)

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)


def bump_theme(i):
    """模拟切主题：改 CARD_BG/BORDER 并让 Colors 的幂等缓存失效"""
    from app.utils.design_tokens import Colors

    Colors.CARD_BG = f"rgba({10 + i}, {20 + i}, {30 + i}, {{alpha}})"
    Colors.BORDER = f"rgba({40 + i}, {50 + i}, {60 + i}, 1)"
    Colors.CONTENT_BG = f"#{10 + i:02x}{20 + i:02x}{30 + i:02x}"
    Colors.TEXT_PRIMARY = "#111111"
    Colors.TEXT_SECONDARY = "#222222"
    Colors.TEXT_MUTED = "#333333"
    Colors.HOVER_BG = "rgba(0,0,0,0.1)"
    Colors.TEXT_ACCENT = "#0000ff"
    Colors.ACCENT_WARM = "#ff0000"
    Colors.INPUT_PLACEHOLDER = "#444444"
    Colors.SCROLLBAR_HANDLE_BG = "#555555"
    Colors.SCROLLBAR_HANDLE_HOVER_BG = "#666666"
    Colors.CARD_BG_SOLID = f"rgb({10 + i}, {20 + i}, {30 + i})"


def timed(label, fn):
    t0 = time.perf_counter()
    fn()
    app.processEvents()
    ms = (time.perf_counter() - t0) * 1000
    print(f"  {label}: {ms:.1f}ms")
    return ms


def main():
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    host = QWidget()
    host.resize(1000, 800)
    t0 = time.perf_counter()
    popup = LLMSettingsCard(host)
    print(f"构造 LLMSettingsCard: {(time.perf_counter() - t0) * 1000:.0f}ms")

    popup.show()
    host.show()
    for _ in range(3):
        app.processEvents()

    # 补齐全部页签（真机 _prefetch_pages 会做这件事，控件数才到真实规模）
    for tab_id in ("hooks", "plugins", "tools", "agents", "skills", "provider"):
        try:
            popup._ensure_page_ready(tab_id)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] _ensure_page_ready({tab_id}): {e}")
    for _ in range(3):
        app.processEvents()
    print(f"子树控件数（全页补齐后）: {len(popup.findChildren(QWidget))}")

    # 预热
    popup.refresh_style()
    app.processEvents()

    print("同主题重复刷新：")
    for i in range(2):
        t0 = time.perf_counter()
        popup.refresh_style()
        app.processEvents()
        print(f"  第{i + 1}次: {(time.perf_counter() - t0) * 1000:.1f}ms")

    print("换色刷新（模拟真切主题）：")
    for i in range(3):
        bump_theme(i)
        t0 = time.perf_counter()
        popup.refresh_style()
        app.processEvents()
        print(f"  第{i + 1}次整体: {(time.perf_counter() - t0) * 1000:.1f}ms")

    print("换色刷新-逐段（第 4 次换色）：")
    bump_theme(10)
    timed("Colors.refresh", lambda: __import__("app.utils.design_tokens", fromlist=["Colors"]).Colors.refresh())
    timed("_apply_base_style", popup._apply_base_style)
    timed("_apply_content_surface", popup._apply_content_surface)
    timed("_update_tab_styles", lambda: popup._update_tab_styles() if hasattr(popup, "_tab_buttons") else None)
    timed("_refresh_content_children", popup._refresh_content_children)
    timed("_apply_scrollbar_style", lambda: popup._apply_scrollbar_style())
    timed("_update_nav_styles", popup._update_nav_styles)
    timed("nav_frame QSS", lambda: popup._nav_frame.setStyleSheet(popup._nav_frame_style()))

    # 逐页测量：每页 ScrollArea 内的子树 repolish 成本
    print("换色-逐页 X（对每页内容 setStyleSheet 强制 repolish）：")
    from PyQt5.QtWidgets import QWidget as _W

    for tab_id, page in popup._page_scrolls.items():
        n = len(page.findChildren(_W))
        bump_theme(20 + len(tab_id))
        t0 = time.perf_counter()
        page.styleSheet()
        page.setStyleSheet(page.styleSheet() + " ")
        app.processEvents()
        print(f"  {tab_id}: {n} 控件, {(time.perf_counter() - t0) * 1000:.1f}ms")

    # 统计
    from app.widgets.cards.settings.system_card_frame import SystemCardFrame

    frames = popup.findChildren(SystemCardFrame)
    print(f"弹窗内 SystemCardFrame 子卡数: {len(frames)}")


if __name__ == "__main__":
    main()
