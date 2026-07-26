# -*- coding: utf-8 -*-
"""
TabPanel — Tab 管理器左侧面板

每个 Tab 项显示：Agent 图标 + 会话标题 + 关闭按钮。
支持拖拽排序、右键菜单、滚轮滚动。
"""

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon, QMouseEvent, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from loguru import logger
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon as FIF,
    PushButton,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
)

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style, scale_icon_size
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.gitee_card import GiteeAccountRow
from app.widgets.elided_label import _ElidedLabel

# ── 模块级缓存：避免 paintEvent 中反复解析 rgba 字符串 ──
import re as _re
import math as _math
from PyQt5.QtGui import QColor as _QColor


def _parse_rgba(rgba_str: str) -> _QColor:
    """将 'rgba(r,g,b,a)' 字符串解析为 QColor，缓存结果"""
    try:
        if rgba_str.startswith("rgba("):
            parts = rgba_str.strip("rgba() ").split(",")
            r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
            a_raw = parts[3].strip()
            a = int(float(a_raw) * 255) if float(a_raw) <= 1 else int(a_raw)
            return _QColor(r, g, b, a)
    except Exception:
        pass
    return _QColor(rgba_str)


# 预解析常用颜色（首次导入时计算一次）
_CACHED_SELECTED_BG = _parse_rgba(Colors.SELECTED_BG)
_CACHED_INFO = _parse_rgba(Colors.INFO)

# 彩虹动画颜色列表（模块级常量，避免每次 paint 创建 10 个 QColor）
_RAINBOW_COLORS = [
    _QColor("#60D4FF"),
    _QColor("#40C8FF"),
    _QColor("#4DA6FF"),
    _QColor("#8B7BFF"),
    _QColor("#C084FC"),
    _QColor("#F472B6"),
    _QColor("#FB7185"),
    _QColor("#F59E0B"),
    _QColor("#34D399"),
    _QColor("#22D3EE"),
]
_RAINBOW_N = len(_RAINBOW_COLORS)


class TabItem(QFrame):
    """单个 Tab 项的 UI 组件"""

    closeRequested = pyqtSignal()

    def __init__(self, title: str, icon=None, parent=None, panel=None):
        super().__init__(parent)
        self._title = title
        self._icon_pixmap = icon
        self._selected = False
        self._streaming = False
        self._stream_error = False
        self._question = False  # AI 提问等待用户回答（橙黄脉动）
        self._panel = panel  # TabPanel 引用，用于读取 _anim_phase
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)

        # 图标尺寸：跟随系统字体缩放
        self._icon_size = scale_icon_size(20)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(6)

        # 图标
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(self._icon_size, self._icon_size)
        if self._icon_pixmap:
            from PyQt5.QtGui import QPixmap

            if isinstance(self._icon_pixmap, QPixmap):
                self._icon_label.setPixmap(
                    self._icon_pixmap.scaled(
                        self._icon_size, self._icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                )
            else:
                # QIcon 等类型：转为 QPixmap
                try:
                    pixmap = self._icon_pixmap.pixmap(self._icon_size, self._icon_size)
                    if pixmap:
                        self._icon_label.setPixmap(pixmap)
                except Exception:
                    pass
        layout.addWidget(self._icon_label)

        # ── 团队角色胶囊（默认隐藏）──
        self._capsule_label = QLabel(self)
        self._capsule_label.setVisible(False)
        self._capsule_label.setFixedHeight(20)
        self._capsule_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        layout.addWidget(self._capsule_label)

        # 标题（使用 _ElidedLabel 自动省略，保证关闭按钮始终可见）
        self._title_label = _ElidedLabel(self._title, self)
        self._apply_title_style()
        layout.addWidget(self._title_label, 1)

        # 关闭按钮（与主标题栏一致的 FluentIcon.CLOSE）
        self._close_btn = TransparentToolButton(self)
        self._close_btn.setIcon(FIF.CLOSE)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setVisible(False)
        self._close_btn.clicked.connect(self.closeRequested.emit)
        layout.addWidget(self._close_btn)

    def _apply_title_style(self):
        """应用标题样式：使用系统字体 + 缩放字号 + 当前主题色

        注意：单纯用 setStyleSheet 设置 font-family 在某些 Qt 场景下会被父级
        stylesheet 覆盖。这里同时调用 setFont，setFont 优先级最高，确保生效。

        每次主题或字体设置变更后都需调用，确保颜色和字体一致。
        """
        from app.utils.utils import get_unified_font

        # 直接通过 setFont 强制应用字体（避开 stylesheet 继承坑）
        self._title_label.setFont(get_unified_font(13))
        # 颜色随主题刷新（字体同一行写也能 setFont，但颜色走 stylesheet 更稳）
        self._title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"background: transparent; "
            f"{get_font_family_css()} "
            f"{font_size_css(13)}"
        )

    def refresh_style(self):
        """主题 / 字体变更后刷新样式，重新调整图标尺寸与文字字号/颜色"""
        # 重新读取缩放后的图标尺寸
        new_size = scale_icon_size(20)
        if new_size != self._icon_size:
            self._icon_size = new_size
            self._icon_label.setFixedSize(self._icon_size, self._icon_size)
            if self._icon_pixmap:
                from PyQt5.QtGui import QPixmap

                if isinstance(self._icon_pixmap, QPixmap):
                    self._icon_label.setPixmap(
                        self._icon_pixmap.scaled(
                            self._icon_size, self._icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                        )
                    )
                else:
                    try:
                        pixmap = self._icon_pixmap.pixmap(self._icon_size, self._icon_size)
                        if pixmap:
                            self._icon_label.setPixmap(pixmap)
                    except Exception:
                        pass
        self._apply_title_style()
        self.update()

    def set_selected(self, selected: bool):
        self._selected = selected
        self.update()

    def set_streaming(self, streaming: bool, error: bool = False):
        self._streaming = streaming
        self._stream_error = error
        self.update()

    def set_question(self, question: bool):
        """设置"提问等待回答"态：仅点亮橙色脉动指示条"""
        self._question = question
        self.update()

    def set_title(self, title: str):
        self._title = title
        self._title_label.setText(title)

    def set_icon(self, icon):
        self._icon_pixmap = icon
        if icon:
            from PyQt5.QtGui import QPixmap

            if isinstance(icon, QPixmap):
                self._icon_label.setPixmap(
                    icon.scaled(
                        self._icon_size, self._icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                )
            else:
                try:
                    pixmap = icon.pixmap(self._icon_size, self._icon_size)
                    if pixmap:
                        self._icon_label.setPixmap(pixmap)
                except Exception:
                    pass

    def set_capsule(self, text: str, color: str = ""):
        """显示团队角色胶囊"""
        if not color:
            # 从 agent 名 hash 生成稳定色
            h = abs(hash(text)) % 360
            color = f"hsl({h}, 65%, 50%)"
        self._capsule_label.setText(text)
        self._capsule_label.setStyleSheet(f"""
            QLabel {{
                background: {color};
                color: white;
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 11px;
                font-weight: bold;
            }}
        """)
        self._capsule_label.setVisible(True)

    def clear_capsule(self):
        """隐藏团队角色胶囊"""
        self._capsule_label.setVisible(False)
        self._capsule_label.setText("")

    def enterEvent(self, event):
        self._close_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._selected:
            self._close_btn.setVisible(False)
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._selected:
            painter.fillRect(self.rect(), _CACHED_SELECTED_BG)

        # ── 流式/错误状态指示条 ──
        if self._streaming or self._stream_error:
            h = self.height()
            y0, y1 = 4, h - 8
            if self._stream_error:
                painter.fillRect(0, y0, 3, y1, _QColor(220, 50, 50))
            else:
                # 彩虹渐变动画
                phase = self._panel._anim_phase if self._panel else 0
                idx = int((phase / 360) * _RAINBOW_N) % _RAINBOW_N
                painter.fillRect(0, y0, 3, y1, _RAINBOW_COLORS[idx])
        elif self._question:
            # AI 提问等待回答：橙黄 #F59E0B 慢呼吸脉动（1.2s 一周期）
            h = self.height()
            y0, y1 = 4, h - 8
            phase = self._panel._question_phase if self._panel else 0
            # 50ms 帧速 +6°/帧 ≈ 1.2s 一周期；亮度在 ~80~220 间脉动
            alpha = int(150 + _math.sin(_math.radians(phase)) * 70)
            painter.fillRect(0, y0, 3, y1, _QColor(245, 158, 11, max(0, min(255, alpha))))
        elif self._selected:
            # 左侧选中指示条
            painter.fillRect(0, 4, 3, self.height() - 8, _CACHED_INFO)

        super().paintEvent(event)


class UIPluginRow(QFrame):
    """TabPanel 中的 UI 插件行，固定图标和文本的相对位置。"""

    clicked = pyqtSignal()

    def __init__(self, title: str, icon: Optional[QIcon] = None, parent=None, plugin_name: str = ""):
        super().__init__(parent)
        self._plugin_name = plugin_name  # 存储插件名，主题刷新时重新获取图标
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(scale_icon_size(16), scale_icon_size(16))
        self._title_label = QLabel(title, self)
        self._title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)
        layout.addWidget(self._icon_label)
        layout.addWidget(self._title_label, 1)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("uiPluginRow")
        self.set_icon(icon)

    def set_icon(self, icon: Optional[QIcon]):
        size = scale_icon_size(16)
        self._icon_label.setFixedSize(size, size)
        if icon is not None:
            self._icon_label.setPixmap(icon.pixmap(size, size))
        else:
            self._icon_label.clear()

    def refresh_style(self):
        """刷新主题样式：字体 + 颜色 + 主题相关图标"""
        from app.utils.utils import get_unified_font

        # 应用系统字体（避免 stylesheet 继承问题）
        self._title_label.setFont(get_unified_font(12))
        self._title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; "
            f"{get_font_family_css()} "
            f"{font_size_css(12)}"
        )
        self._icon_label.setStyleSheet("background: transparent;")
        self.setStyleSheet(f"""
            #uiPluginRow {{
                background: transparent;
                border-radius: 5px;
            }}
            #uiPluginRow:hover {{
                background: {Colors.HOVER_BG};
            }}
        """)

        # 重新获取主题相关图标（浅/深色主题图标不同）
        if self._plugin_name:
            try:
                from app.core.plugin_manager import PluginManager

                pm = PluginManager.get_instance()
                plugin = pm.get_plugin(self._plugin_name)
                icon_config = getattr(plugin, "icon_config", None) if plugin else None
                icon_path = icon_config.get("dark" if isDarkTheme() else "light") if icon_config else None
                if icon_path:
                    self.set_icon(QIcon(str(icon_path)))
            except Exception:
                pass

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class TabPanel(QWidget):
    """左侧 Tab 列表面板"""

    tabSelected = pyqtSignal(int)  # 选中 Tab 索引
    tabCloseRequested = pyqtSignal(int)  # 关闭 Tab 索引
    newTabRequested = pyqtSignal()  # 新建 Tab
    tabsReordered = pyqtSignal(list)  # 拖拽排序后新顺序（索引列表）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[TabItem] = []
        self._active_index: int = -1
        self._plugin_section: Optional[QWidget] = None
        self._plugin_layout: Optional[QVBoxLayout] = None
        self._plugin_title: Optional[CaptionLabel] = None
        self._plugin_infos: list[tuple[str, str, str]] = []
        self._plugin_buttons: list[UIPluginRow] = []
        self._gitee_account_row: Optional[GiteeAccountRow] = None
        self._anim_phase: float = 0.0  # 彩虹动画相位
        self._question_phase: float = 0.0  # question 脉动相位（独立，避免与彩虹冲突）
        self._anim_timer: Optional[QTimer] = None  # 有 tab 流式/question 时启动
        self._streaming_count: int = 0  # 当前流式 tab 计数
        self._question_count: int = 0  # 当前 question 状态 tab 计数
        self._setup_ui()
        # 注册主题刷新回调：主题/字体变更后刷新所有 Tab 项样式
        theme_manager.register_refresh_target(self)

    _SEPARATOR_STYLE = f"""
        QFrame {{
            background: {Colors.BORDER};
            max-height: 1px;
        }}
    """

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 顶部：UI 插件列表（带滚动） ──
        self._plugin_scroll = QScrollArea(self)
        self._plugin_scroll.setWidgetResizable(True)
        self._plugin_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._plugin_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._plugin_scroll.setFrameShape(QFrame.NoFrame)
        self._plugin_scroll.setMaximumHeight(180)  # 最多显示 ~4 个插件项，超出滚动
        self._plugin_scroll.setStyleSheet(
            f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(4)}
            """
        )
        self._plugin_scroll.viewport().setStyleSheet("background: transparent;")

        self._plugin_section = QWidget(self._plugin_scroll)
        plugin_layout = QVBoxLayout(self._plugin_section)
        plugin_layout.setContentsMargins(6, 6, 6, 4)
        plugin_layout.setSpacing(2)
        self._plugin_layout = plugin_layout
        plugin_title = CaptionLabel("UI 插件", self._plugin_section)
        self._plugin_title = plugin_title
        plugin_title.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(11)}")
        plugin_layout.addWidget(plugin_title)
        self._plugin_section.setStyleSheet("background: transparent;")
        self._plugin_section.setVisible(False)
        self._plugin_scroll.setVisible(False)  # 无插件时隐藏整个滚动区域
        self._plugin_scroll.setWidget(self._plugin_section)
        layout.addWidget(self._plugin_scroll)

        # ── 顶部：新建按钮 ──
        top_bar = QWidget(self)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(6, 6, 6, 4)
        self._new_btn = TransparentPushButton(FIF.ADD, "新建标签页", top_bar)
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._new_btn.clicked.connect(self.newTabRequested.emit)
        top_layout.addWidget(self._new_btn)
        layout.addWidget(top_bar)

        # ── 中间：Tab 列表 ──
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setFrameShape(QFrame.NoFrame)
        # QScrollArea viewport 在 Windows 上默认可能为白色，需显式透明；
        # 滚动条样式与项目其他列表保持一致（get_unified_scrollbar_style）
        self._scroll_area.setStyleSheet(
            f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(6)}
            """
        )
        self._scroll_area.viewport().setStyleSheet("background: transparent;")

        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(6, 0, 6, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch()
        self._scroll_area.setWidget(self._list_widget)
        layout.addWidget(self._scroll_area, 1)

        # ── 分隔线 ──
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(separator)

        # ── 底部：设置按钮 ──
        bottom_bar = QWidget(self)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(6, 4, 6, 6)

        self._settings_btn = TransparentPushButton(FIF.SETTING, "设置", bottom_bar)
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._settings_btn.clicked.connect(self._on_settings_clicked)
        bottom_layout.addWidget(self._settings_btn)

        layout.addWidget(bottom_bar)

        account_separator = QFrame(self)
        account_separator.setFrameShape(QFrame.HLine)
        account_separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(account_separator)

        self._gitee_account_row = GiteeAccountRow(self)
        layout.addWidget(self._gitee_account_row)

    def _on_settings_clicked(self):
        """打开设置卡片"""
        from PyQt5.QtWidgets import QWidget

        # 沿父链向上找 OpenAIChatToolWindow
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "_toggle_settings_card"):
                parent._toggle_settings_card()
                return
            parent = parent.parent()
        # 兜底：通过 TabManagerWindow 切换回独立模式
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm:
            current = tm.get_current_window()
            if current and hasattr(current, "_toggle_settings_card"):
                current._toggle_settings_card()

    def refresh_ui_plugins(self):
        """刷新 Tab 模式顶部的 UI 插件按钮列表"""
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            cards = UIPluginRegistry.get_instance().get_floating_cards()
        except Exception:
            cards = {}

        infos = []
        for card_id, info in cards.items():
            try:
                title = (info.title or "").strip() or card_id
                infos.append((card_id, title, info.plugin_name))
            except Exception:
                continue
        infos.sort(key=lambda item: item[1].lower())
        self._plugin_infos = infos

        if self._plugin_layout is None or self._plugin_section is None:
            return
        while self._plugin_layout.count() > 1:  # 保留索引 0 的标题
            item = self._plugin_layout.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._plugin_buttons = []

        from app.core.plugin_manager import PluginManager

        plugin_manager = PluginManager.get_instance()
        for card_id, title, plugin_name in infos:
            row = UIPluginRow(
                title,
                self._get_plugin_icon(plugin_manager, plugin_name),
                self._plugin_section,
                plugin_name=plugin_name,  # 传入插件名，主题刷新时重新获取图标
            )
            row.clicked.connect(lambda cid=card_id: self._on_ui_plugin_clicked(cid))
            self._plugin_layout.addWidget(row)
            self._plugin_buttons.append(row)

        self._plugin_section.setVisible(bool(infos))
        self._plugin_scroll.setVisible(bool(infos))
        self._refresh_plugin_style()

    @staticmethod
    def _get_plugin_icon(plugin_manager, plugin_name):
        """读取插件当前主题图标，读取失败时不影响列表显示"""
        try:
            plugin = plugin_manager.get_plugin(plugin_name)
            icon_config = getattr(plugin, "icon_config", None) if plugin else None
            icon_path = icon_config.get("dark" if isDarkTheme() else "light") if icon_config else None
            return QIcon(str(icon_path)) if icon_path else None
        except Exception:
            return None

    def _on_ui_plugin_clicked(self, card_id: str):
        """在当前活动 Tab 中打开 UI 插件卡片"""
        parent = self.parent()
        while parent is not None and not hasattr(parent, "get_current_window"):
            parent = parent.parent()
        if parent is None:
            logger.warning(f"[TabPanel] UI 插件 {card_id} 点击：无法找到 TabManagerWindow")
            return
        current_window = parent.get_current_window()
        if current_window is None:
            logger.warning(f"[TabPanel] UI 插件 {card_id} 点击：当前窗口为空")
            return
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().toggle_floating_card(card_id, main_widget=current_window)
        except Exception as e:
            logger.error(f"[TabPanel] UI 插件 {card_id} 打开失败：{e}")

    def _refresh_plugin_style(self):
        """刷新插件区域的主题和字号样式"""
        if self._plugin_section is None:
            return
        if self._plugin_title is not None:
            self._plugin_title.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(11)}")
        for row in self._plugin_buttons:
            row.refresh_style()

    def add_tab(self, title: str, icon=None) -> int:
        """添加 Tab 项，返回其索引"""
        idx = len(self._items)
        item = TabItem(title, icon, self._list_widget, panel=self)

        # 连接信号 — ★ 使用动态索引查找，防止删除前序 tab 后索引漂移
        # 不能用 lambda 捕获 idx，否则删除 tab 0 后 tab 1 的 lambda 中 idx 仍为 1
        item.closeRequested.connect(
            lambda _item=item: self.tabCloseRequested.emit(self._items.index(_item) if _item in self._items else -1)
        )

        # 连接点击事件 — ★ 同样动态查找当前索引
        def _on_tab_click(ev, _item=item):
            if ev.button() == Qt.LeftButton:
                if _item in self._items:
                    self.set_active_index(self._items.index(_item))

        item.mousePressEvent = _on_tab_click

        # 在 stretch 之前插入
        self._list_layout.insertWidget(idx, item)
        self._items.append(item)

        # 如果这是第一个 Tab，自动选中
        if len(self._items) == 1:
            self.set_active_index(0)

        return idx

    def remove_tab(self, index: int):
        """移除指定索引的 Tab"""
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            # 如果该 tab 正在流式，递减计数
            if item._streaming:
                self._streaming_count = max(0, self._streaming_count - 1)
                if self._streaming_count == 0:
                    self._stop_anim_timer()
            self._list_layout.removeWidget(item)
            item.deleteLater()

            # 更新选中态
            if self._active_index == index:
                # 切换到相邻 Tab
                new_idx = min(index, len(self._items) - 1) if self._items else -1
                self.set_active_index(new_idx)
            elif self._active_index > index:
                self._active_index -= 1

    def set_active_index(self, index: int):
        """设置选中 Tab"""
        # 取消旧的选中态
        if 0 <= self._active_index < len(self._items):
            self._items[self._active_index].set_selected(False)
            self._items[self._active_index]._close_btn.setVisible(False)

        self._active_index = index

        # 设置新的选中态
        if 0 <= index < len(self._items):
            self._items[index].set_selected(True)
            self.tabSelected.emit(index)

    def update_tab_title(self, index: int, title: str):
        """更新 Tab 标题"""
        if 0 <= index < len(self._items):
            self._items[index].set_title(title)

    def update_tab_icon(self, index: int, icon):
        """更新 Tab 图标"""
        if 0 <= index < len(self._items):
            self._items[index].set_icon(icon)

    def update_tab_capsule(self, index: int, text: str):
        """显示团队角色胶囊"""
        if 0 <= index < len(self._items):
            self._items[index].set_capsule(text)

    def clear_tab_capsule(self, index: int):
        """隐藏团队角色胶囊"""
        if 0 <= index < len(self._items):
            self._items[index].clear_capsule()

    def update_tab_streaming(self, index: int, streaming: bool, error: bool = False):
        """更新 Tab 的流式/错误状态"""
        if 0 <= index < len(self._items):
            old = self._items[index]._streaming
            self._items[index].set_streaming(streaming, error)
            if streaming and not old:
                self._streaming_count += 1
                self._ensure_anim_timer()
            elif not streaming and old:
                self._streaming_count = max(0, self._streaming_count - 1)
                if self._streaming_count + self._question_count == 0:
                    self._stop_anim_timer()

    def update_tab_question(self, index: int, question: bool):
        """更新 Tab 的 question 状态（AI 提问等待用户回答）"""
        if not (0 <= index < len(self._items)):
            return
        old = self._items[index]._question
        if old == question:
            return
        self._items[index].set_question(question)
        if question:
            self._question_count += 1
            self._ensure_anim_timer()
        else:
            self._question_count = max(0, self._question_count - 1)
            if self._streaming_count + self._question_count == 0:
                self._stop_anim_timer()

    def _ensure_anim_timer(self):
        """确保彩虹动画定时器已启动"""
        if self._anim_timer is None:
            from PyQt5.QtCore import QTimer

            self._anim_timer = QTimer(self)
            self._anim_timer.setInterval(50)  # 50ms ≈ 20fps
            self._anim_timer.timeout.connect(self._on_anim_tick)
        if not self._anim_timer.isActive():
            self._anim_timer.start()

    def _stop_anim_timer(self):
        if self._anim_timer and self._anim_timer.isActive():
            self._anim_timer.stop()
        self._anim_phase = 0.0

    def _on_anim_tick(self):
        """动画帧：推进相位 + 刷新所有流式 / question tab"""
        self._anim_phase = (self._anim_phase + 12) % 360
        self._question_phase = (self._question_phase + 6) % 360  # 1.2s 一周期（慢呼吸）
        for item in self._items:
            if item._streaming or item._question:
                item.update()

    def refresh_style(self):
        """ThemeManager 统一刷新入口：主题/字体变更后调用"""
        from app.utils.design_tokens import Colors as _Colors

        _Colors.refresh()
        for item in self._items:
            item.refresh_style()
            # 强制重绘（解决 stylesheet 重应用后 widget 未及时更新的问题）
            item.repaint()
        self._refresh_plugin_style()
        if self._gitee_account_row is not None:
            self._gitee_account_row.refresh_style()

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def active_index(self) -> int:
        return self._active_index

    def contextMenuEvent(self, event):
        """显示右键菜单"""
        if self._active_index < 0:
            return

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {Colors.CARD_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
        """)
        close_action = menu.addAction("关闭标签页")
        menu.addSeparator()
        duplicate_action = menu.addAction("复制窗口")
        branch_action = menu.addAction("分支窗口")
        menu.addSeparator()
        rename_action = menu.addAction("重命名会话")

        action = menu.exec_(event.globalPos())
        if action == close_action:
            self.tabCloseRequested.emit(self._active_index)
