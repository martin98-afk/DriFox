# -*- coding: utf-8 -*-
"""设置表单通用控件：无滚轮下拉框 + 标签-控件行布局。

从 mcp_setting_card.py 抽出的公共位（#34 链桥解耦）：两符号曾住在 mcp_setting_card，
hook_setting_card 顶层 import 它会连带拉起 mcp SDK 全家桶（app.tools.mcp_tools，
cum ~600ms）——通用控件住在功能卡里是结构异味，收敛到此轻量模块
（仅依赖 qfluentwidgets/QtWidgets，零主程序内部包），双方各自 import 本模块。
"""

from typing import Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox


class NoWheelComboBox(ComboBox):
    """禁用滚轮切换的普通下拉框（防止误触）"""

    def wheelEvent(self, event):
        event.ignore()


def _make_row(label_text: str, widget: QWidget, label_width: int = 70) -> Tuple[QHBoxLayout, "BodyLabel"]:
    """构造一行：右对齐标签 + 输入控件"""
    row = QHBoxLayout()
    row.setSpacing(8)
    label = BodyLabel(label_text)
    label.setFixedWidth(label_width)
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(label)
    row.addWidget(widget, 1)
    return row, label
