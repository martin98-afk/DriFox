# -*- coding: utf-8 -*-
"""InfoBadge 字号不跟随正文字号放大

角标是图标级微排版：qfluentwidgets 构造时固定 11px，qss 无 font-size。
历史问题：apply_font_size_to_widget 递归 setFont 会把它改成 14+delta，
UI.FontSize=5 时 19px、胶囊宽 44px，挂在 28px 按钮上盖住相邻图标。
"""

from PyQt5.QtWidgets import QWidget

from qfluentwidgets import FluentIcon, InfoBadge, InfoBadgePosition, TransparentToolButton

from app.utils.design_tokens import apply_font_size_to_widget, scale_font_size


def test_info_badge_keeps_11px(qapp):
    host = QWidget()
    btn = TransparentToolButton(FluentIcon.MESSAGE, host)
    btn.setFixedSize(28, 28)
    badge = InfoBadge.attension(3, parent=host, target=btn, position=InfoBadgePosition.LEFT)

    assert badge.font().pixelSize() == 11

    apply_font_size_to_widget(host, 14)

    assert badge.font().pixelSize() == 11
    # 正文控件仍按缩放后的字号走
    assert btn.font().pixelSize() == scale_font_size(14)


def test_info_badge_restored_after_being_scaled(qapp):
    host = QWidget()
    btn = TransparentToolButton(FluentIcon.MESSAGE, host)
    btn.setFixedSize(28, 28)
    badge = InfoBadge.attension(3, parent=host, target=btn, position=InfoBadgePosition.LEFT)

    # 模拟被旧逻辑放大过的现场
    f = badge.font()
    f.setPixelSize(scale_font_size(14))
    badge.setFont(f)

    apply_font_size_to_widget(host, 14)

    assert badge.font().pixelSize() == 11
