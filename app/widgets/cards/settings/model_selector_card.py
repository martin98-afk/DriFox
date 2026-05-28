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
from app.widgets.model_selector_popup import ProviderHeader, ModelItem, _calculate_scroll_height


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
            header = ProviderHeader(provider_name, self)
            self.content_layout.addWidget(header)

            # 模型列表
            for model_name in filtered_models:
                is_active = (
                    provider_name == current_provider and model_name == current_model
                )
                item = ModelItem(provider_name, model_name, is_active, self)
                if is_active:
                    self._active_model_item = item
                item.clicked.connect(self._on_model_clicked)
                self.content_layout.addWidget(item)
                self._model_widgets.append(item)
                self._all_model_items.append((item, provider_name, model_name))

        # 如果没有匹配的模型
        if not self._all_model_items and search_text:
            no_result = QLabel(f"未找到匹配 \"{search_text}\" 的模型", self)
            no_result.setAlignment(Qt.AlignCenter)
            no_result.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(12)}; padding: 20px;"
            )
            self.content_layout.addWidget(no_result)

        # 底部弹性空间
        self.content_layout.addStretch(1)

        # 滚动到当前选中模型
        if self._active_model_item is not None:
            QApplication.processEvents()
            self._scroll_to_item_center(self._active_model_item)

    def refresh_style(self):
        """刷新主题样式"""
        Colors.refresh()
        self._apply_search_style()
        self.content_widget.setStyleSheet("background: transparent;")

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
