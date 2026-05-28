# -*- coding: utf-8 -*-
"""
扁平式模型选择上拉框 - 类似 OpenCode 风格
展示所有已配置服务商的模型，服务商作为小标题，下面是模型列表
"""
from typing import List, Tuple, Dict, Any, Optional

from PyQt5.QtCore import Qt, pyqtSignal, QPoint
from PyQt5.QtWidgets import (
    QWidget,
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QLineEdit,
    QApplication,
    QDialog,
    QPushButton,
)
from qfluentwidgets import FluentIcon, TransparentToolButton
from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget

from app.utils.utils import get_font_family_css, get_icon
from app.utils.design_tokens import Colors, font_size_css


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


class ModelSelectorPopup(QWidget):
    """扁平式模型选择弹窗"""
    modelSelected = pyqtSignal(str, str)  # provider_name, model_name
    addProviderClicked = pyqtSignal()  # 添加服务商
    configureProviderClicked = pyqtSignal()  # 配置服务商

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.parent_widget = parent

        # 数据
        self._provider_models: List[Tuple[str, List[str]]] = []  # [(provider, [models])]
        self._current_provider: str = ""
        self._current_model: str = ""
        self._model_widgets: List[ModelItem] = []
        self._reference_widget: Optional[QWidget] = None
        self._all_model_items: List[Tuple[ModelItem, str, str]] = []  # (widget, provider, model)
        self._active_model_item: Optional[ModelItem] = None  # 当前选中模型的 item 引用

        # 注意：事件过滤器不在 __init__ 安装，而是在 show_at() 中安装。
        # 因为 close() 移除了过滤器后，若在 __init__ 安装则无法恢复。
        # 改为在 show_at() 中动态安装，确保每次显示时都有事件过滤器。

        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("popupFrame")
        self.main_frame.setStyleSheet(f"""
            QFrame#popupFrame {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.main_frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        # 搜索框 + 操作按钮区域
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
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
        separator.setStyleSheet(f"background-color: {Colors.BORDER}; max-height: 1px; margin: 6px 0;")
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
        # 初始化时设置最小高度，后续 set_providers_data 会动态调整
        self.scroll_area.setMinimumHeight(_MIN_SCROLL_HEIGHT)
        self.scroll_area.setMaximumHeight(_MAX_SCROLL_HEIGHT)
        layout.addWidget(self.scroll_area, 1)

        # 整体窗口布局
        window_layout = QVBoxLayout(self)
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.addWidget(self.main_frame)

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
            no_result.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(12)}; padding: 20px;")
            self.content_layout.addWidget(no_result)

        # 底部弹性空间
        self.content_layout.addStretch(1)

        # 计算实际 item 总数，根据数量动态调整滚动区域高度
        total_items = len(self._all_model_items)
        target_height = _calculate_scroll_height(total_items)
        self.scroll_area.setMinimumHeight(target_height)

        # 隐藏时 adjustSize 不生效，改用显式 resize
        self.main_frame.layout().activate()
        QApplication.processEvents()
        content_size = self.main_frame.sizeHint()
        self.resize(content_size.width(), content_size.height())

        # 如果弹窗已显示且有参考控件，重新计算位置确保下边缘对齐按钮向上扩展
        if self.isVisible() and self._reference_widget is not None:
            reference_widget = self._reference_widget
            screen = reference_widget.screen() or QApplication.primaryScreen()
            screen_geom = screen.availableGeometry() if screen else None

            btn_rect = reference_widget.rect()
            btn_global_pos = reference_widget.mapToGlobal(btn_rect.topLeft())

            popup_width = min(self.width(), self.maximumWidth())
            popup_height = min(self.height(), self.maximumHeight())

            x = btn_global_pos.x()
            y = btn_global_pos.y() - popup_height

            if screen_geom:
                if x < screen_geom.left():
                    x = screen_geom.left() + 10
                if x + popup_width > screen_geom.right():
                    x = screen_geom.right() - popup_width - 10
                if y < screen_geom.top():
                    y = btn_global_pos.y() + btn_rect.height()

            self.move(x, y)

            # 搜索后重建列表时，重新滚动到当前选中模型
            if self._active_model_item is not None:
                QApplication.processEvents()
                self._scroll_to_item_center(self._active_model_item)

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

    def refresh_style(self):
        """刷新弹窗主题样式"""
        Colors.refresh()
        self.main_frame.setStyleSheet(f"""
            QFrame#popupFrame {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)
        self._apply_search_style()
        self.content_widget.setStyleSheet("background: transparent;")

    def _on_search_changed(self, text: str):
        """搜索文本变化时刷新列表"""
        # 重新构建 provider_models 数据（从保存的原始数据重建）
        # 需要重新获取 is_current_provider 信息
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
        self.close()

    def eventFilter(self, obj, event):
        """检测外部点击，关闭弹窗"""
        if event.type() == event.MouseButtonPress:
            global_pos = event.globalPos()
            popup_geo = self.geometry()
            if not popup_geo.contains(global_pos):
                # 检查是否点击在输入框上（输入法需要）
                focus_widget = QApplication.focusWidget()
                if focus_widget and isinstance(focus_widget, QLineEdit):
                    edit_geo = focus_widget.rect().translated(focus_widget.mapToGlobal(QPoint(0, 0)))
                    if edit_geo.contains(global_pos):
                        return False
                self.close()
        return super().eventFilter(obj, event)

    def close(self):
        """关闭弹窗（不移除事件过滤器，由 show_at 统一管理）"""
        super().close()

    def show_at(self, reference_widget: QWidget):
        """在参考控件上方显示弹窗（向上展开）"""
        self._reference_widget = reference_widget
        # 安装事件过滤器（每次显示时确保存在，防止 close 后丢失）
        QApplication.instance().installEventFilter(self)
        # 先显示以激活布局计算
        self.show()
        QApplication.processEvents()

        # 获取内容实际需要的尺寸
        content_size = self.content_widget.sizeHint()
        content_width = max(content_size.width(), 350)  # 最小宽度确保能显示完整名称
        scroll_area_height = content_size.height() + 20  # 搜索框+边距

        # 设置合理的最大尺寸
        screen = reference_widget.screen() or QApplication.primaryScreen()
        if screen:
            screen_geom = screen.availableGeometry()
            max_width = max(min(450, screen_geom.width() - 40), content_width)
            max_height = min(500, screen_geom.height() - 120)
        else:
            max_width = max(450, content_width)
            max_height = 500

        self.setMaximumSize(max_width, max_height)

        # 使用内容尺寸 resize（宽高各缩小1/3）
        new_width = max_width * 2 // 3
        new_height = min(scroll_area_height, 500) * 2 // 3
        self.resize(new_width, new_height)

        btn_rect = reference_widget.rect()
        btn_global_pos = reference_widget.mapToGlobal(btn_rect.topLeft())

        popup_width = min(self.width(), self.maximumWidth())
        popup_height = min(self.height(), self.maximumHeight())

        # 水平位置：左对齐到按钮左边缘
        x = btn_global_pos.x()

        # 垂直位置：在按钮上方展开
        y = btn_global_pos.y() - popup_height

        if screen:
            # 水平边界检查
            if x < screen_geom.left():
                x = screen_geom.left() + 10
            if x + popup_width > screen_geom.right():
                x = screen_geom.right() - popup_width - 10
            # 如果上方空间不够，显示在下方
            if y < screen_geom.top():
                y = btn_global_pos.y() + btn_rect.height()

        self.move(x, y)
        self.show()
        self.raise_()

        # 滚动到当前选中模型，使其居中显示
        if self._active_model_item is not None:
            QApplication.processEvents()
            self._scroll_to_item_center(self._active_model_item)

        self.search_edit.setFocus()
        self.search_edit.selectAll()


class ProviderConfigListDialog(QDialog):
    """配置服务商弹窗 - 展示已配置的服务商列表"""

    def __init__(self, providers: Dict[str, Dict[str, Any]], current_provider: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("配置服务商")
        self.setMinimumSize(420, 350)
        self.setMaximumSize(520, 600)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
        self.setModal(True)
        self._providers = providers
        self._current_provider = current_provider
        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {Colors.CONTENT_BG};
            }}
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                background: transparent;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 标题
        title = QLabel("已配置的服务商")
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {font_size_css(16)}; font-weight: bold; {get_font_family_css()}")
        layout.addWidget(title)

        # 提示文字
        hint = QLabel("点击编辑按钮可修改服务商配置，点击删除按钮可移除服务商。")
        hint.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(12)}; {get_font_family_css()}")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 分隔线
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background-color: {Colors.BORDER}; max-height: 1px;")
        layout.addWidget(sep)

        # 滚动区域
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
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
        """)

        content_widget = QWidget()
        content_widget.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)

        if not self._providers:
            empty_label = QLabel("暂无已配置的服务商，请点击「添加」按钮添加。")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(13)}; padding: 30px;")
            content_layout.addWidget(empty_label)
        else:
            for provider_name, config in self._providers.items():
                provider_widget = self._create_provider_item(provider_name, config)
                content_layout.addWidget(provider_widget)

        content_layout.addStretch(1)
        scroll.setWidget(content_widget)
        layout.addWidget(scroll, 1)

        # 关闭按钮
        close_btn_layout = QHBoxLayout()
        close_btn_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setFixedSize(100, 36)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_SECONDARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                {get_font_family_css()} {font_size_css(13)};
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                color: {Colors.TEXT_PRIMARY};
            }}
        """)
        close_btn.clicked.connect(self.accept)
        close_btn_layout.addWidget(close_btn)
        layout.addLayout(close_btn_layout)

    def _create_provider_item(self, provider_name: str, config: Dict[str, Any]) -> QWidget:
        """创建一个服务商配置项"""
        widget = QWidget()
        widget.setFixedHeight(48)
        is_current = provider_name == self._current_provider
        widget.setStyleSheet(f"""
            QWidget#provider_item_{provider_name} {{
                background-color: {Colors.SELECTED_BG if is_current else Colors.HOVER_BG};
                border-radius: 6px;
                border: {'1px solid ' + Colors.BORDER_ACCENT if is_current else '1px solid transparent'};
            }}
            QWidget#provider_item_{provider_name}:hover {{
                background-color: {Colors.HOVER_BG};
            }}
        """)
        widget.setObjectName(f"provider_item_{provider_name}")

        hlayout = QHBoxLayout(widget)
        hlayout.setContentsMargins(12, 4, 8, 4)
        hlayout.setSpacing(8)

        # 服务商图标
        icon_widget = ProviderIconWidget(provider_name, 24)
        hlayout.addWidget(icon_widget)

        # 服务商名称 + 模型名称
        info_layout = QVBoxLayout()
        info_layout.setSpacing(1)

        name_label = QLabel(provider_name)
        name_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(13)}; font-weight: bold; background: transparent;")
        info_layout.addWidget(name_label)

        model_name = config.get("模型名称", "")
        model_label = QLabel(f"模型: {model_name}" if model_name else "未设置模型")
        model_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}; background: transparent;")
        info_layout.addWidget(model_label)

        hlayout.addLayout(info_layout, 1)

        if is_current:
            current_tag = QLabel("当前")
            current_tag.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.BORDER_ACCENT};
                    {get_font_family_css()} {font_size_css(11)};
                    font-weight: bold;
                    background: {Colors.HOVER_BG};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 4px;
                    padding: 2px 6px;
                }}
            """)
            hlayout.addWidget(current_tag)

        return widget
