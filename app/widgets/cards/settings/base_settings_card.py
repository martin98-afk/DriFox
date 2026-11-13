# -*- coding: utf-8 -*-
"""
通用设置卡片基类 — 继承自 SystemCardFrame，获得统一的头部布局和固定边框
"""

from app.utils.design_tokens import Colors
from app.widgets.cards.settings.system_card_frame import SystemCardFrame


def _selected_row_qss(selected: bool) -> str:
    """可选中设置行卡片的 QSS 模板（secret_mode / update_proxy 行共用）。"""
    border = Colors.BORDER_ACCENT if selected else Colors.BORDER
    bg = Colors.HOVER_BG if selected else "transparent"
    return f"""
        QFrame {{
            background-color: {bg};
            border: 1px solid {border};
            border-radius: 6px;
        }}
        QFrame:hover {{
            border-color: {Colors.BORDER_ACCENT};
        }}
        QLabel {{
            border: none;
            background: transparent;
        }}
    """


class BaseSettingsCard(SystemCardFrame):
    """通用设置卡片基类（向后兼容）"""

    def __init__(self, title: str, icon: str = None, icon_svg: str = None, parent=None):
        super().__init__(parent)
        if icon_svg:
            self.set_icon_svg(icon_svg)
        elif icon:
            self.set_icon(icon)
        self.set_title_text(title)

    def set_title(self, title: str, icon_svg: str = None):
        """动态设置卡片标题（icon_svg 传图标名时同步切换头部 SVG 图标）"""
        if icon_svg:
            self.set_icon_svg(icon_svg)
            self.set_title_text(title)
        elif " " in title:
            parts = title.split(" ", 1)
            self.set_icon(parts[0])
            self.set_title_text(parts[1])
        else:
            self.set_title_text(title)
