# -*- coding: utf-8 -*-
"""
模型选择卡片内容 - 底部卡片形式展示所有服务商的模型列表
"""
from typing import List, Tuple, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QScrollArea, QSizePolicy, QFrame, QApplication,
)
from qfluentwidgets import FluentIcon, TransparentToolButton

from app.utils.utils import get_font_family_css, get_icon
from app.utils.design_tokens import Colors, font_size_css
from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget


# item 高度常量
_ITEM_HEIGHT = 34  # ModelItem 高度
_HEADER_HEIGHT = 36  # ProviderHeader 高度
_MIN_ITEMS = 3  # 最少显示 item 数
_MAX_ITEMS = 10  # 最多显示 item 数

# 滚动区域高度计算
_MIN_SCROLL_HEIGHT = _MIN_ITEMS * _ITEM_HEIGHT  # 最小高度：约 102px
_MAX_SCROLL_HEIGHT = _MAX_ITEMS * _ITEM_HEIGHT + _HEADER_HEIGHT  # 最大高度：约 274px


def _calculate_scroll_height(total_items: int) -> int:
    """根据 item 总数计算滚动区域高度"""
    if total_items <= _MIN_ITEMS:
        return _MIN_SCROLL_HEIGHT
    elif total_items >= _MAX_ITEMS:
        return _MAX_SCROLL_HEIGHT
    else:
        ratio = (total_items - _MIN_ITEMS) / (_MAX_ITEMS - _MIN_ITEMS)
        return int(_MIN_SCROLL_HEIGHT + ratio * (_MAX_SCROLL_HEIGHT - _MIN_SCROLL_HEIGHT))


class ProviderHeader(QWidget):
    """服务商标题行"""

    def __init__(self, provider_name: str, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        self.setFixedHeight(36)
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 8, 0)
        layout.setSpacing(8)

        # 服务商图标
        self.icon_widget = ProviderIconWidget(provider_name, 20)
        layout.addWidget(self.icon_widget)

        # 服务商名称
        self.name_label = QLabel(provider_name, self)
        self._apply_name_style()
        layout.addWidget(self.name_label)

        layout.addStretch(1)

    def _apply_name_style(self):
        Colors.refresh()
        self.name_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: bold;")


class ModelItem(QWidget):
    """单个模型项 - 可点击"""
    clicked = pyqtSignal(str, str)  # provider_name, model_name

    def __init__(self, provider_name: str, model_name: str, is_active: bool = False, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        self.model_name = model_name
        self.is_active = is_active
        self.setFixedHeight(34)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(30, 0, 12, 0)
        layout.setSpacing(8)

        # 选中状态指示点
        self.dot = QLabel("●", self)
        self.dot.setStyleSheet(
            f"color: {Colors.BORDER_ACCENT}; {get_font_family_css()} {font_size_css(10)};" if self.is_active else f"color: transparent; {get_font_family_css()} {font_size_css(10)};"
        )
        self.dot.setFixedWidth(14)
        layout.addWidget(self.dot)

        # 模型名
        self.name_label = QLabel(self.model_name, self)
        self._apply_name_style()
        layout.addWidget(self.name_label, 1)

    def _apply_name_style(self):
        Colors.refresh()
        if self.is_active:
            self.name_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-weight: bold; {get_font_family_css()} {font_size_css(13)};")
        else:
            self.name_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(13)};")

    def set_active(self, active: bool):
        self.is_active = active
        self.dot.setStyleSheet(
            f"color: {Colors.BORDER_ACCENT}; {get_font_family_css()} {font_size_css(10)};" if active else f"color: transparent; {get_font_family_css()} {font_size_css(10)};"
        )
        self._apply_name_style()

    def mousePressEvent(self, event):
        self.clicked.emit(self.provider_name, self.model_name)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        if not self.is_active:
            self.name_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(13)};")
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_name_style()
        super().leaveEvent(event)


class ModelSelectorCardContent(QWidget):
    """模型选择卡片内容"""

    modelSelected = pyqtSignal(str, str)  # provider_name, model_name
    addProviderClicked = pyqtSignal()
    configureProviderClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._provider_models: List[Tuple[str, List[str]]] = []
        self._current_provider: str = ""
        self._current_model: str = ""
        self._model_widgets: List[ModelItem] = []
        self._all_model_items: List[Tuple[ModelItem, str, str]] = []
        self._active_model_item: Optional[ModelItem] = None
        self._provider_headers: List[Tuple[QWidget, str]] = []  # (header_widget, provider_name)
        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 搜索框 + 操作按钮区域
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(4, 0, 4, 0)
        search_layout.setSpacing(4)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("搜索模型...")
        self.search_edit.setClearButtonEnabled(True)
        self._apply_search_style()
        self.search_edit.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self.search_edit, 1)

        # 添加服务商按钮
        self.add_provider_btn = TransparentToolButton(FluentIcon.ADD, self)
        self.add_provider_btn.setFixedSize(28, 28)
        self.add_provider_btn.setToolTip("添加服务商")
        self.add_provider_btn.clicked.connect(lambda: self.addProviderClicked.emit())
        search_layout.addWidget(self.add_provider_btn)

        # 配置服务商按钮
        self.config_provider_btn = TransparentToolButton(get_icon("配置管理"), self)
        self.config_provider_btn.setFixedSize(28, 28)
        self.config_provider_btn.setToolTip("配置服务商")
        self.config_provider_btn.clicked.connect(lambda: self.configureProviderClicked.emit())
        search_layout.addWidget(self.config_provider_btn)

        layout.addLayout(search_layout)

        # 分隔线
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet(f"background-color: {Colors.BORDER}; max-height: 1px; margin: 4px 0;")
        layout.addWidget(separator)

        # 吸顶服务商标签（初始隐藏，滚动时在顶部显示当前服务商）
        self._sticky_header = QFrame(self)
        self._sticky_header.setFixedHeight(36)
        self._sticky_header.setVisible(False)
        self._sticky_header.setStyleSheet(f"""
            QFrame {{
                background: {Colors.CONTENT_BG};
                border: none;
                border-bottom: 1px solid {Colors.BORDER};
            }}
        """)
        sticky_layout = QHBoxLayout(self._sticky_header)
        sticky_layout.setContentsMargins(10, 0, 8, 0)
        sticky_layout.setSpacing(8)
        self._sticky_icon = QLabel("", self._sticky_header)
        self._sticky_icon.setFixedSize(20, 20)
        self._sticky_icon.setStyleSheet("background: transparent; border: none;")
        self._sticky_icon.setAlignment(Qt.AlignCenter)
        sticky_layout.addWidget(self._sticky_icon)
        self._sticky_label = QLabel("", self._sticky_header)
        self._sticky_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: bold; background: transparent;"
        )
        sticky_layout.addWidget(self._sticky_label)
        sticky_layout.addStretch(1)
        layout.addWidget(self._sticky_header)

        # 滚动区域
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                border: none;
                background: transparent;
                width: 12px;
                margin: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.BORDER};
                border-radius: 6px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {Colors.TEXT_MUTED};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)

        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        # 底部弹性空间，让内容靠上
        self.content_layout.addStretch(1)

        self.scroll_area.setWidget(self.content_widget)

        # 连接滚动事件，更新吸顶服务商
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

        layout.addWidget(self.scroll_area, 1)

    # ── 公有方法 ──────────────────────────────────────

    def set_providers_data(
        self,
        provider_models: List[Tuple[str, List[str], bool]],  # (provider, [models], is_current_provider)
        current_provider: str,
        current_model: str,
    ):
        """设置服务商和模型数据"""
        self._current_provider = current_provider
        self._current_model = current_model
        self._provider_models = [(p, m) for p, m, _ in provider_models]
        self._model_widgets.clear()
        self._all_model_items.clear()
        self._provider_headers.clear()
        self._active_model_item = None

        # 清空内容区域（保留最后的 stretch）
        while self.content_layout.count() > 0:
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

        search_text = self.search_edit.text().strip().lower()

        for provider_name, models, is_current_provider in provider_models:
            # 过滤
            if search_text:
                filtered_models = [m for m in models if search_text in m.lower()]
                if not filtered_models:
                    continue
            else:
                filtered_models = models

            # 服务商标题
            header = ProviderHeader(provider_name, self.content_widget)
            self.content_layout.addWidget(header)
            self._provider_headers.append((header, provider_name))

            # 模型列表
            for model_name in filtered_models:
                is_active = (
                    provider_name == current_provider and model_name == current_model
                )
                item = ModelItem(provider_name, model_name, is_active, self.content_widget)
                if is_active:
                    self._active_model_item = item
                item.clicked.connect(self._on_model_clicked)
                self.content_layout.addWidget(item)
                self._model_widgets.append(item)
                self._all_model_items.append((item, provider_name, model_name))

        # 如果没有匹配的模型
        if not self._all_model_items and search_text:
            no_result = QLabel(f"未找到匹配 \"{search_text}\" 的模型", self.content_widget)
            no_result.setAlignment(Qt.AlignCenter)
            no_result.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(12)}; padding: 20px;"
            )
            self.content_layout.addWidget(no_result)

        # 底部弹性空间
        self.content_layout.addStretch(1)

        # 更新吸顶服务商显示
        self._update_sticky_header()

        # 滚动到当前选中模型
        if self._active_model_item is not None:
            QApplication.processEvents()
            self._scroll_to_item_center(self._active_model_item)

    def refresh_style(self):
        """刷新主题样式"""
        Colors.refresh()
        self._apply_search_style()
        self.content_widget.setStyleSheet("background: transparent;")
        self._sticky_header.setStyleSheet(f"""
            QFrame {{
                background: {Colors.CONTENT_BG};
                border: none;
                border-bottom: 1px solid {Colors.BORDER};
            }}
        """)
        self._sticky_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: bold; background: transparent;"
        )

    # ── 内部方法 ──────────────────────────────────────

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _scroll_to_item_center(self, item_widget: QWidget):
        """滚动滚动区域，使指定 item 居中显示"""
        scrollbar = self.scroll_area.verticalScrollBar()
        item_y = item_widget.pos().y()
        item_half = item_widget.height() // 2
        view_half = self.scroll_area.viewport().height() // 2
        target_scroll = item_y + item_half - view_half
        target_scroll = max(0, min(target_scroll, scrollbar.maximum()))
        scrollbar.setValue(target_scroll)

    def _on_scroll(self, value):
        """滚动条变化时更新吸顶服务商"""
        self._update_sticky_header()

    def _update_sticky_header(self):
        """根据当前滚动位置，更新吸顶服务商显示"""
        if not self._provider_headers:
            self._sticky_header.setVisible(False)
            return

        scroll_pos = self.scroll_area.verticalScrollBar().value()

        # 找到最后一个被滚过顶部的服务商
        sticky_name = None
        for header_widget, provider_name in self._provider_headers:
            if header_widget.y() - scroll_pos <= 0:
                sticky_name = provider_name
            else:
                break

        if sticky_name:
            self._sticky_header.setVisible(True)
            self._sticky_label.setText(sticky_name)
            # 更新图标 - 从这里直接获取 ProviderIconWidget 方式显示
            from app.constants import PROVIDER_ICONS
            icon_name = PROVIDER_ICONS.get(sticky_name, "大模型")
            from app.utils.utils import get_icon
            icon = get_icon(icon_name)
            if icon:
                pixmap = icon.pixmap(20, 20)
                self._sticky_icon.setPixmap(pixmap)
            else:
                # 取首字作为文字图标
                text = sticky_name[0] if sticky_name else "?"
                self._sticky_icon.setText(text)
                Colors.refresh()
                self._sticky_icon.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; background: transparent; border: none; font-weight: bold; {font_size_css(14)};")
            self._sticky_icon.setToolTip(sticky_name)
        else:
            self._sticky_header.setVisible(False)

    def _apply_search_style(self):
        """应用搜索框样式（动态从 Colors 读取）"""
        Colors.refresh()
        self.search_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 6px 10px;
                selection-background-color: {Colors.BORDER_ACCENT};
                selection-color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(13)};
            }}
            QLineEdit:focus {{
                border-color: {Colors.BORDER_ACCENT};
                background-color: {Colors.CONTENT_BG};
            }}
            QLineEdit::placeholder {{
                color: {Colors.TEXT_MUTED};
            }}
            QLineEdit::text {{
                background-color: transparent;
            }}
            QLineEdit QToolButton {{
                background-color: transparent;
                border: none;
                padding: 2px;
            }}
            QLineEdit QToolButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-radius: 3px;
            }}
        """)

    def _on_search_changed(self, text: str):
        """搜索文本变化时刷新列表"""
        provider_models_with_flag = []
        for prov, models in self._provider_models:
            is_cur = prov == self._current_provider
            provider_models_with_flag.append((prov, models, is_cur))

        self.set_providers_data(
            provider_models_with_flag,
            self._current_provider,
            self._current_model,
        )

    def _on_model_clicked(self, provider_name: str, model_name: str):
        """模型被点击"""
        self.modelSelected.emit(provider_name, model_name)
