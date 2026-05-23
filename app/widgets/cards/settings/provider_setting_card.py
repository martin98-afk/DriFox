# -*- coding: utf-8 -*-
import requests
from PyQt5.QtCore import pyqtSignal, QSize, Qt, QRect
from PyQt5.QtGui import QIcon, QPainter, QColor, QFont
from PyQt5.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QPushButton,
)
from qfluentwidgets import (
    ToolButton,
    FluentIcon,
    PushButton,
    qconfig,
    ExpandSettingCard,
    ConfigItem,
    Dialog,
    IconWidget,
)

from app.constants import (
    PROVIDER_ICONS,
)
from app.utils.design_tokens import Colors, Sizes, ButtonStyles
from app.utils.design_tokens import font_size_css
from app.utils.utils import get_icon, get_unified_font, get_font_family_css


def _is_text_chat_model(model_id: str) -> bool:
    """判断模型是否为文本聊天模型，过滤掉图片、音频、词嵌入等非文本模型"""
    if not model_id:
        return False
    
    model_lower = model_id.lower()
    
    # 非文本模型关键词黑名单
    non_text_keywords = [
        # 图片生成/视觉模型
        'dall-e', 'dalle', 'stable-diffusion', 'sd-', 'imagen', 'flux',
        'image', 'diffusion', 'kandinsky', 'midjourney', 'wan', 'vision',
        'vl', 'llava', 'seance', 'cogview', 'cogvideo', 'pixart', 'visual',
        # 音频模型
        'whisper', 'tts', 'speech', 'audio', 'piper', 'voice',
        # 词嵌入模型
        'embedding', 'embed', 'text-embedding', 'bge',
        # 其他非聊天模型
        'moderation', 'rerank', 'search', 'retrieval',
    ]
    
    # 检查是否包含非文本模型关键词
    for keyword in non_text_keywords:
        if keyword in model_lower:
            return False
    
    return True


def fetch_provider_models(
    api_url: str, api_key: str, provider_name: str, auth_type: str = "bearer"
) -> list:
    """Fetch model list from provider API. Returns text chat models only."""
    headers = {"Authorization": f"Bearer {api_key}"} if auth_type == "bearer" else {}

    urls_to_try = []
    if provider_name == "DeepSeek":
        urls_to_try = [f"{api_url.rstrip('/')}/models"]
    else:
        urls_to_try = [
            f"{api_url.rstrip('/')}/models",
            f"{api_url.rstrip('/')}/v1/models",
        ]

    last_error = ""
    for url in urls_to_try:
        try:
            print(f"[ProviderEditDialog] Trying {url}")
            response = requests.get(url, headers=headers, timeout=10)
            print(f"[ProviderEditDialog] Response status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()

                if isinstance(data, dict):
                    if "data" in data:
                        all_models = [
                            m.get("id") or m.get("name", "") or m.get("model", "")
                            for m in data["data"]
                            if isinstance(m, dict)
                        ]
                        # 过滤只保留文本聊天模型
                        return [m for m in all_models if _is_text_chat_model(m)]
                    elif "models" in data:
                        all_models = [
                            m.get("id") or m.get("name", "")
                            for m in data["models"]
                            if isinstance(m, dict)
                        ]
                        return [m for m in all_models if _is_text_chat_model(m)]
                    elif "object" in data and isinstance(data["object"], list):
                        all_models = [
                            m.get("id", "")
                            for m in data["object"]
                            if isinstance(m, dict)
                        ]
                        return [m for m in all_models if _is_text_chat_model(m)]
                    for key in ["items", "result"]:
                        if key in data and isinstance(data[key], list):
                            all_models = [
                                m.get("id")
                                or m.get("name", "")
                                or (m if isinstance(m, str) else "")
                                for m in data[key]
                            ]
                            return [m for m in all_models if _is_text_chat_model(m)]
                elif isinstance(data, list):
                    all_models = [
                        m.get("id", "") if isinstance(m, dict) else str(m) for m in data
                    ]
                    return [m for m in all_models if _is_text_chat_model(m)]
            else:
                last_error = f"HTTP {response.status_code}"

        except requests.exceptions.Timeout:
            last_error = "请求超时"
        except requests.exceptions.ConnectionError:
            last_error = "连接失败"
        except Exception as e:
            last_error = str(e)

    if last_error:
        print(f"[ProviderEditDialog] All attempts failed. Last error: {last_error}")
    return []


class ProviderIconWidget(IconWidget):
    def __init__(self, provider_name: str, size: int = 32, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        self.setFixedSize(size, size)
        self._init_icon()

    def _init_icon(self):
        icon_name = PROVIDER_ICONS.get(self.provider_name, "")
        if icon_name:
            icon = get_icon(icon_name)
            if icon:
                self.setIcon(icon)
                return
        letters = ""
        for part in self.provider_name.split():
            if part and part not in ["(", ")", "（", "）"]:
                letters += part[0]
        if len(letters) > 2:
            letters = letters[:2]
        self._text = letters

    def paintEvent(self, event):
        if not hasattr(self, "_text") or not self._text:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self._get_color()
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 6, 6)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont(get_unified_font().family(), self.width() // 3, QFont.Bold))
        painter.drawText(
            QRect(0, 0, self.width(), self.height()), Qt.AlignCenter, self._text
        )

    def _get_color(self):
        colors = [
            "#0078d4",
            "#e74c3c",
            "#2ecc71",
            "#9b59b6",
            "#f39c12",
            "#1abc9c",
            "#34495e",
        ]
        hash_val = sum(ord(c) for c in self.provider_name)
        return colors[hash_val % len(colors)]


class ProviderItem(QWidget):
    removed = pyqtSignal(QWidget)
    selected = pyqtSignal(QWidget)
    editRequested = pyqtSignal(str, dict)

    def __init__(
        self, provider_name: str, provider_info: dict, is_default: bool, parent=None
    ):
        super().__init__(parent=parent)
        self.provider_name = provider_name
        self.provider_info = provider_info
        self.is_default = is_default
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setStyleSheet(f"""
            ProviderItem {{
                background-color: transparent;
                border-radius: 8px;
            }}
            ProviderItem:hover {{
                background-color: {Colors.HOVER_BG};
            }}
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 8)
        main_layout.setSpacing(12)

        self.radioButton = QPushButton()
        self.radioButton.setFixedSize(20, 20)
        self.radioButton.setCheckable(True)
        self.radioButton.setChecked(self.is_default)
        self.radioButton.setCursor(Qt.PointingHandCursor)
        self.radioButton.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: 2px solid #555555;
                border-radius: 10px;
            }
            QPushButton:checked {
                border: 2px solid #0078d4;
                background-color: #0078d4;
            }
            QPushButton:hover {
                border-color: #0078d4;
            }
        """)

        self.iconWidget = ProviderIconWidget(self.provider_name, 32)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        self.nameLabel = QLabel(self.provider_name)
        self.nameLabel.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(14)} font-weight: 500; {get_font_family_css()}"
        )
        self.modelLabel = QLabel(self.provider_info.get("模型名称", ""))
        self.modelLabel.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(12)}; {get_font_family_css()}")

        info_layout.addWidget(self.nameLabel)
        info_layout.addWidget(self.modelLabel)

        main_layout.addWidget(self.radioButton, 0, Qt.AlignLeft | Qt.AlignVCenter)
        main_layout.addWidget(self.iconWidget, 0, Qt.AlignLeft | Qt.AlignVCenter)
        main_layout.addLayout(info_layout)
        main_layout.addStretch(1)

        btn_widget = QWidget()
        btn_widget.setStyleSheet("background-color: transparent;")
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(4)
        self.editButton = ToolButton(FluentIcon.EDIT)
        self.removeButton = ToolButton(FluentIcon.CLOSE)
        self.editButton.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        self.removeButton.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        self.editButton.setIconSize(Sizes.TOOL_ICON_SZ)
        self.removeButton.setIconSize(Sizes.TOOL_ICON_SZ)
        self.editButton.setStyleSheet(ButtonStyles.tool_button())
        self.removeButton.setStyleSheet(ButtonStyles.tool_button())
        btn_layout.addWidget(self.editButton)
        btn_layout.addWidget(self.removeButton)
        main_layout.addWidget(btn_widget, 0, Qt.AlignRight | Qt.AlignVCenter)

    def _connect_signals(self):
        self.removeButton.clicked.connect(lambda: self.removed.emit(self))
        self.radioButton.clicked.connect(lambda: self.selected.emit(self))
        self.editButton.clicked.connect(self._on_edit)

    def _on_edit(self):
        self.editRequested.emit(self.provider_name, self.provider_info)

    def update_info(self, name: str, info: dict):
        self.provider_name = name
        self.provider_info = info
        self.nameLabel.setText(name)
        self.modelLabel.setText(info.get("模型名称", ""))
        self.iconWidget.provider_name = name
        self.iconWidget._init_icon()
        self.iconWidget.update()


class ProviderListSettingCard(ExpandSettingCard):
    providerChanged = pyqtSignal(dict)
    defaultProviderChanged = pyqtSignal(str)
    # 新增信号：用于触发卡片显示
    showAddProviderCard = pyqtSignal()  # 显示添加服务商卡片
    showEditProviderCard = pyqtSignal(str, dict)  # 显示编辑服务商卡片

    def __init__(
        self,
        icon: QIcon,
        configItem: ConfigItem,
        defaultProviderItem: ConfigItem,
        title: str,
        content: str = None,
        parent=None,
        home=None,
    ):
        self.home = home
        super().__init__(icon, title, content, parent)
        self.title = title
        self.configItem = configItem
        self.defaultProviderItem = defaultProviderItem
        self.addProviderButton = PushButton("添加", self, FluentIcon.ADD)
        self.providers = (
            qconfig.get(configItem).copy()
            if isinstance(qconfig.get(configItem), dict)
            else {}
        )
        self.default_provider = qconfig.get(defaultProviderItem) or ""
        self.__initWidget()

    def __initWidget(self):
        # 先添加按钮（会在 expand 按钮之前显示），然后设置布局
        self.addWidget(self.addProviderButton)
        self.viewLayout.setSpacing(0)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        self.view.setStyleSheet("background-color: transparent;")
        self._refresh_items()
        self.addProviderButton.clicked.connect(self._show_add_dialog)
        
        # 更新展开按钮位置：将按钮放到关闭按钮旁边
        self._update_button_position()
    
    def _update_button_position(self):
        """将添加按钮移到关闭按钮旁边"""
        # 展开卡片的 card 是 HeaderSettingCard，包含 hBoxLayout
        card = self.card
        if hasattr(card, 'hBoxLayout'):
            # 从布局中移除按钮
            self.card.hBoxLayout.removeWidget(self.addProviderButton)
            # 在关闭按钮之前插入按钮
            # hBoxLayout 结构: icon, titleLabel, contentLabel, expandButton, spacing
            # 找到 expandButton 的位置，在其前面插入
            for i in range(card.hBoxLayout.count()):
                item = card.hBoxLayout.itemAt(i)
                if item.widget() == card.expandButton:
                    # 找到 expandButton，在其前一个 spacing 之前插入按钮
                    # 先移除最后一个 spacing（19px）
                    card.hBoxLayout.removeItem(card.hBoxLayout.itemAt(i - 1))
                    # 添加按钮和较小的间距
                    card.hBoxLayout.insertWidget(i - 1, self.addProviderButton, 0, Qt.AlignRight)
                    card.hBoxLayout.insertSpacing(i, 4)  # 恢复较小的间距
                    break

    def _refresh_items(self):
        self.providers = (
            qconfig.get(self.configItem).copy()
            if isinstance(qconfig.get(self.configItem), dict)
            else {}
        )
        self.default_provider = qconfig.get(self.defaultProviderItem) or ""
        while self.viewLayout.count() > 0:
            item = self.viewLayout.takeAt(0)
            if item.widget() and item.widget() != self.addProviderButton:
                item.widget().deleteLater()
        for name, info in self.providers.items():
            is_default = name == self.default_provider
            self._add_provider_item(name, info, is_default)

    def _add_provider_item(self, name: str, info: dict, is_default: bool):
        item = ProviderItem(name, info, is_default, self.view)
        item.removed.connect(self._show_confirm_dialog)
        item.selected.connect(lambda i: self._select_provider(i))
        item.editRequested.connect(lambda n, i: self._show_edit_dialog(n, i, item))
        self.viewLayout.addWidget(item)
        item.show()
        self._adjustViewSize()

    def _show_add_dialog(self):
        # 发送信号，让主窗口处理卡片显示
        self.showAddProviderCard.emit()

    def _show_edit_dialog(self, name: str, info: dict, item: ProviderItem):
        # 发送信号，让主窗口处理卡片显示
        self.showEditProviderCard.emit(name, info)

    def _show_confirm_dialog(self, item: ProviderItem):
        title = self.tr("确定要删除这个服务商吗?")
        content = (
            self.tr('删除 "') + item.provider_name + self.tr('" 后将不再出现在列表中。')
        )
        w = Dialog(title, content, self.window())
        w.yesSignal.connect(lambda: self._remove_provider(item))
        w.exec_()

    def _remove_provider(self, item: ProviderItem):
        if item.provider_name not in self.providers:
            return
        del self.providers[item.provider_name]
        qconfig.set(self.configItem, self.providers, save=True)
        self.viewLayout.removeWidget(item)
        item.deleteLater()
        self._adjustViewSize()
        self.providerChanged.emit(self.providers)
        if self.default_provider == item.provider_name:
            keys = list(self.providers.keys())
            self.default_provider = keys[0] if keys else ""
            qconfig.set(self.defaultProviderItem, self.default_provider, save=True)
            self.defaultProviderChanged.emit(self.default_provider)

    def _select_provider(self, item: ProviderItem):
        for i in range(self.viewLayout.count()):
            w = self.viewLayout.itemAt(i).widget()
            if isinstance(w, ProviderItem) and w != item:
                w.radioButton.setChecked(False)
        item.radioButton.setChecked(True)
        self.default_provider = item.provider_name
        qconfig.set(self.defaultProviderItem, self.default_provider, save=True)
        self.defaultProviderChanged.emit(self.default_provider)
