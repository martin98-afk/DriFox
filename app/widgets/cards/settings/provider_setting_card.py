# -*- coding: utf-8 -*-
from loguru import logger
from PyQt5.QtCore import QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ConfigItem,
    ExpandSettingCard,
    FluentIcon,
    IconWidget,
    PushButton,
    SearchLineEdit,
    ToolButton,
    qconfig,
)

from app.plugins.registries.provider_registry import ProviderRegistry
from app.utils.design_tokens import ButtonStyles, Colors, Sizes, font_size_css, scale_font_size, scale_icon_size
from app.utils.provider_icons import get_provider_icon
from app.utils.utils import get_font_family_css, get_unified_font
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin


class ProviderIconWidget(IconWidget):
    def __init__(self, provider_name: str, size: int = 32, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        self._base_size = size
        self.setFixedSize(scale_icon_size(size), scale_icon_size(size))
        self._init_icon()

    def _init_icon(self):
        # 插件图标优先（<插件>/providers/icons/ 主题感知 + 回退 qrc），
        # 找不到再走字母回退
        icon = get_provider_icon(self.provider_name)
        if icon and not icon.isNull():
            self.setIcon(icon)
            self._text = ""  # 清空回退文本，避免 paintEvent 在图标模式下走自定义绘制
            return
        # 字母回退：取每个有内容 part 的首个字母/汉字字符，
        # 跳过 #/&/数字 等非字母字符，避免出现 "C#"/"A&" 之类难看的首字母
        letters = ""
        for part in self.provider_name.split():
            if not part or part in ("(", ")", "（", "）"):
                continue
            for ch in part:
                if ch.isalpha():  # 涵盖 ASCII 字母 + 中文/日文/韩文 等 Unicode letter
                    letters += ch
                    break
            if len(letters) >= 2:
                break
        self._text = letters

    def refresh_style(self):
        """随系统字号缩放图标大小"""
        s = scale_icon_size(self._base_size)
        self.setFixedSize(s, s)
        self.update()

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
        # 按图标宽度的 0.45 倍计算字号，并跟随系统字号缩放
        # 之前使用 width // 3 在 20px 图标上只有 6px，几乎不可见且不随系统字号变化
        font_size = scale_font_size(int(self.width() * 0.45))
        painter.setFont(QFont(get_unified_font().family(), font_size, QFont.Bold))
        painter.drawText(QRect(0, 0, self.width(), self.height()), Qt.AlignCenter, self._text)

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
    editRequested = pyqtSignal(str, dict)  # config_id, provider_info

    def __init__(self, config_id: str, provider_info: dict, is_default: bool, parent=None):
        super().__init__(parent=parent)
        self.config_id = config_id
        # 从 provider_info 中获取服务商名称，如果没有则使用 config_id
        self.provider_name = provider_info.get("provider_name", config_id)
        self.provider_info = provider_info
        self.is_default = is_default
        # 同名分组的后缀索引：0=不显示，1+=显示 "#2"、"#3"...
        self.suffix_index = provider_info.get("_suffix_index", 0)
        self._is_selected = is_default  # 选中态标记，供 refresh_style 恢复高亮
        self._setup_ui()
        # 保存默认样式用于 highlight/恢复切换
        self._default_style = self.styleSheet()
        self._connect_signals()

    def _setup_ui(self):
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
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

        self.iconWidget = ProviderIconWidget(self.provider_name, 32)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        # 显示配置名称（如果存在），否则显示服务商名称
        display_name = self.provider_info.get("name", "") or self.provider_name
        # 同名分组时附加后缀，让用户能区分多个同服务商配置
        if self.suffix_index >= 1:
            display_name = f"{display_name} #{self.suffix_index + 1}"
        self.nameLabel = QLabel(display_name)
        self.nameLabel.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(14)} font-weight: 500; {get_font_family_css()}"
        )
        self.modelLabel = QLabel(self._subtitle_text())
        self.modelLabel.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(12)}; {get_font_family_css()}")

        info_layout.addWidget(self.nameLabel)
        info_layout.addWidget(self.modelLabel)

        main_layout.addWidget(self.iconWidget, 0, Qt.AlignLeft | Qt.AlignVCenter)
        # 健康状态点（无状态数据 / 无刷新能力时不画）
        status_color = self._status_dot_color()
        if status_color:
            self.statusDot = QLabel()
            self.statusDot.setFixedSize(8, 8)
            self.statusDot.setStyleSheet(f"background-color: {status_color}; border-radius: 4px;")
            self.statusDot.setToolTip(self._refresh_tooltip())
            main_layout.addWidget(self.statusDot, 0, Qt.AlignLeft | Qt.AlignVCenter)
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

    def _subtitle_text(self) -> str:
        """副标题：「N 个模型」。

        默认模型语义已随星标退役（「模型名称」由保存链自动维护，对用户无意义），
        不再展示；无「模型列表」键（词典兜底态）返回空串。
        """
        models = self.provider_info.get("模型列表")
        if isinstance(models, list):
            return f"{len(models)} 个模型"
        return ""

    def _has_refresh_capability(self) -> bool:
        """是否具备刷新能力（有 models_hook 或有 API_URL）；无能力不画状态点。"""
        try:
            p = ProviderRegistry.get_instance().get(self.provider_name)
            if p is not None and callable(p.capabilities.get("models_hook")):
                return True
        except Exception:
            pass
        return bool(str(self.provider_info.get("API_URL", "") or "").strip())

    def _status_dot_color(self) -> str:
        """状态点颜色；无「模型刷新状态」或无刷新能力 → 空串（不画点）。"""
        status = str(self.provider_info.get("模型刷新状态", "") or "")
        if not status or not self._has_refresh_capability():
            return ""
        return {
            "ok": "#2ecc71",
            "unreachable": "#95a5a6",
            "auth_failed": "#e74c3c",
        }.get(status, "#95a5a6")

    def _refresh_tooltip(self) -> str:
        """刷新状态 tooltip：开关状态 + 上次刷新相对时间"""
        from app.utils.session_preview import format_relative_time

        auto = "开" if self.provider_info.get("自动刷新模型") else "关"
        last = str(self.provider_info.get("上次模型刷新", "") or "")
        when = format_relative_time(last) if last else "从未"
        return f"自动刷新：{auto} · 上次刷新 {when}"

    def _connect_signals(self):
        self.removeButton.clicked.connect(lambda: self.removed.emit(self))
        self.editButton.clicked.connect(self._on_edit)

    def mousePressEvent(self, event):
        """点击整项（非按钮区域）设为默认服务商"""
        self.selected.emit(self)
        super().mousePressEvent(event)

    def _on_edit(self):
        self.editRequested.emit(self.config_id, self.provider_info)

    def refresh_style(self):
        """主题/字体变更时刷新服务商名称、模型名称的字体与颜色"""
        Colors.refresh()
        self.nameLabel.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(14)} font-weight: 500; {get_font_family_css()}"
        )
        self.modelLabel.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(12)}; {get_font_family_css()}")
        self.setStyleSheet(f"""
            ProviderItem {{
                background-color: transparent;
                border-radius: 8px;
            }}
            ProviderItem:hover {{
                background-color: {Colors.HOVER_BG};
            }}
        """)
        # 如果是选中态，重新应用高亮样式
        if getattr(self, "_is_selected", False):
            indicator_style = f"""
                ProviderItem {{
                    background-color: transparent;
                    border-radius: 8px;
                    border-left: 3px solid {Colors.SYSTEM_ACCENT};
                }}
                ProviderItem:hover {{
                    background-color: {Colors.HOVER_BG};
                }}
            """
            self.setStyleSheet(indicator_style)
        # 同步刷新服务商图标大小
        if hasattr(self.iconWidget, "refresh_style"):
            self.iconWidget.refresh_style()


# 服务商列表分组顺序（首个命中判据即归该组）
_GROUP_ORDER = ("OAuth", "Coding Plan", "本地", "API")
_GROUP_COLORS = {
    "OAuth": "#9b59b6",
    "Coding Plan": "#f39c12",
    "本地": "#2ecc71",
    "API": "#0078d4",
}


def _group_of(provider_name: str, info: dict) -> str:
    """判定一条服务商配置所属分组

    判据全部来自插件声明（零服务商硬编码）：
      ① OAuth       ← capabilities["login_hook"] 存在
      ② Coding Plan ← coding_plan_fetcher 非空，或名称含 "-coding"（自定义命名兜底）
      ③ 本地        ← 认证方式 none 或 API_URL 含 localhost
      ④ API         ← 其余
    """
    try:
        p = ProviderRegistry.get_instance().get(provider_name)
    except Exception:
        p = None
    if p is not None and p.capabilities.get("login_hook"):
        return "OAuth"
    if (p is not None and p.coding_plan_fetcher) or "-coding" in str(provider_name or "").lower():
        return "Coding Plan"
    api_url = str(info.get("API_URL", "") or "").lower()
    if str(info.get("认证方式", "") or "").lower() == "none" or "localhost" in api_url:
        return "本地"
    return "API"


class ProviderListSettingCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    providerChanged = pyqtSignal(dict)
    defaultProviderChanged = pyqtSignal(str)
    # 新增信号：用于触发卡片显示
    showAddProviderCard = pyqtSignal()  # 显示添加服务商卡片
    showEditProviderCard = pyqtSignal(str, dict)  # config_id, provider_info

    # 重入屏障：防止 qconfig.set() → valueChanged → _refresh_items 重入同一对象
    _is_deleting = False

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
        self.providers = qconfig.get(configItem).copy() if isinstance(qconfig.get(configItem), dict) else {}
        self.default_provider = qconfig.get(defaultProviderItem) or ""
        # 搜索关键词（空 = 不过滤）与无匹配空态标签（惰性创建）
        self._keyword = ""
        self._empty_label = None
        self._group_headers: list = []  # 组头标签，主题切换时重刷配色
        self.__initWidget()

    def __initWidget(self):
        # 先添加按钮（会在 expand 按钮之前显示），然后设置布局
        self.addWidget(self.addProviderButton)
        self.viewLayout.setSpacing(0)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        self.view.setStyleSheet("background-color: transparent;")
        self._build_search_bar()
        self.viewLayout.addWidget(self._search_bar)
        self._refresh_items()
        self.addProviderButton.clicked.connect(self._show_add_dialog)

        # 更新展开按钮位置：将按钮放到关闭按钮旁边
        self._update_button_position()

    def _update_button_position(self):
        """将添加按钮移到关闭按钮旁边"""
        # 展开卡片的 card 是 HeaderSettingCard，包含 hBoxLayout
        card = self.card
        if hasattr(card, "hBoxLayout"):
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
        # 重入屏障：_remove_provider 执行期间由值变更触发的同步调用直接返回，
        # 真正的刷新由 _remove_provider 的 finally 块在 _is_deleting 恢复后调度
        if self._is_deleting:
            return
        self.providers = qconfig.get(self.configItem).copy() if isinstance(qconfig.get(self.configItem), dict) else {}
        self.default_provider = qconfig.get(self.defaultProviderItem) or ""
        self._rebuild_rows(self._compute_suffix_map())

    def _rebuild_rows(self, suffix_map: dict[str, int]):
        """清空并按「关键词过滤 → 四组分组」重建全部服务商行"""
        # 搜索框 / 空态标签是常驻控件，重建时复用（takeAt 会把它们从布局摘下）
        for i in range(self.viewLayout.count()):
            w = self.viewLayout.itemAt(i).widget()
            if w is None or w is self._search_bar or w is self._empty_label or w is self.addProviderButton:
                continue
            w.deleteLater()
        while self.viewLayout.count() > 0:
            self.viewLayout.takeAt(0)
        # 搜索框常驻列表首位
        self.viewLayout.addWidget(self._search_bar)
        if self._empty_label is not None:
            self._empty_label.hide()
        self._group_headers.clear()

        # 过滤 + 按组归集（保持配置字典插入序）
        grouped: dict[str, list[tuple[str, dict]]] = {}
        for config_id, info in self.providers.items():
            # 附加后缀索引到 info（供 ProviderItem 显示使用，不持久化到配置）
            display_info = dict(info)
            display_info["_suffix_index"] = suffix_map.get(config_id, 0)
            if not self._match_keyword(display_info, config_id):
                continue
            group = _group_of(info.get("provider_name", config_id), info)
            grouped.setdefault(group, []).append((config_id, display_info))

        if not grouped:
            self._show_empty_state()
            self._adjust_view_size()
            return

        for group in _GROUP_ORDER:
            rows = grouped.get(group)
            if not rows:
                continue
            self.viewLayout.addWidget(self._make_group_header(group, len(rows)))
            for config_id, display_info in rows:
                # 判断是否为默认服务商：比较配置 ID 或服务商名称
                info = self.providers[config_id]
                is_default = (config_id == self.default_provider) or (
                    info.get("provider_name") == self.default_provider
                )
                self._add_provider_item(config_id, display_info, is_default)
        self._adjust_view_size()

    def _build_search_bar(self):
        """搜索框：按显示名 / 服务商名 / 模型名过滤（150ms 防抖，与技能卡同款）"""
        self._search_bar = SearchLineEdit(self)
        self._search_bar.setPlaceholderText("搜索服务商 / 模型")
        self._search_bar.setClearButtonEnabled(True)
        self._search_bar.setFixedHeight(32)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_filter)
        self._search_bar.textChanged.connect(self._on_search_text_changed)

    def _on_search_text_changed(self, text: str):
        self._keyword = (text or "").strip()
        self._search_timer.start()

    def _apply_filter(self):
        """防抖到期：按当前关键词重建列表"""
        self._rebuild_rows(self._compute_suffix_map())

    def _compute_suffix_map(self) -> dict[str, int]:
        """同名配置的后缀索引映射（与 _refresh_items 同一口径）"""
        name_groups: dict[str, list[str]] = {}
        for cid, info in self.providers.items():
            name_groups.setdefault(info.get("provider_name", cid), []).append(cid)
        suffix_map: dict[str, int] = {}
        for cids in name_groups.values():
            if len(cids) == 1:
                suffix_map[cids[0]] = 0
            else:
                for idx, cid in enumerate(cids):
                    suffix_map[cid] = idx
        return suffix_map

    def _match_keyword(self, info: dict, config_id: str) -> bool:
        """关键词命中：显示名 / 服务商名 / 模型名称（大小写不敏感子串）"""
        if not self._keyword:
            return True
        kw = self._keyword.lower()
        pname = str(info.get("provider_name", config_id) or "")
        display_name = str(info.get("name", "") or pname)
        if info.get("_suffix_index", 0) >= 1:
            display_name = f"{display_name} #{info['_suffix_index'] + 1}"
        return kw in display_name.lower() or kw in pname.lower() or kw in str(info.get("模型名称", "") or "").lower()

    def _make_group_header(self, group: str, count: int) -> QWidget:
        """组头：左色条 + 组名（数量）"""
        header = QWidget(self.view)
        header.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 8, 12, 2)
        layout.setSpacing(8)

        anchor = QWidget(header)
        anchor.setFixedSize(3, 12)
        anchor.setStyleSheet(f"background: {_GROUP_COLORS[group]}; border: none; border-radius: 1px;")
        layout.addWidget(anchor)

        label = QLabel(f"{group}（{count}）", header)
        label.setStyleSheet(self._group_label_style())
        layout.addWidget(label)
        layout.addStretch(1)
        self._group_headers.append(label)
        return header

    @staticmethod
    def _group_label_style() -> str:
        return f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: 600; {get_font_family_css()}"

    def _show_empty_state(self):
        """空态提示：区分「库中本无服务商」与「关键词无匹配」两种文案"""
        if self._empty_label is None:
            self._empty_label = QLabel("", self.view)
            self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} {get_font_family_css()}; padding: 16px 0;"
        )
        if self._keyword:
            self._empty_label.setText(f"未找到匹配「{self._keyword}」的服务商")
        else:
            self._empty_label.setText("暂无服务商")
        self.viewLayout.addWidget(self._empty_label)
        self._empty_label.show()

    def _add_provider_item(self, config_id: str, info: dict, is_default: bool):
        item = ProviderItem(config_id, info, is_default, self.view)
        item.removed.connect(self._show_confirm_dialog)
        item.selected.connect(lambda i: self._select_provider(i))
        # editRequested 信号传递 config_id 和 provider_info
        item.editRequested.connect(lambda n, i: self._show_edit_dialog(n, i, item))
        # 如果是默认服务商，立即应用选中样式
        if is_default and hasattr(item, "_default_style"):
            indicator_style = f"""
                ProviderItem {{
                    background-color: transparent;
                    border-radius: 8px;
                    border-left: 3px solid {Colors.SYSTEM_ACCENT};
                }}
                ProviderItem:hover {{
                    background-color: {Colors.HOVER_BG};
                }}
            """
            item.setStyleSheet(indicator_style)
        self.viewLayout.addWidget(item)
        item.show()

    def _show_add_dialog(self):
        # 发送信号，让主窗口处理卡片显示
        self.showAddProviderCard.emit()

    def _show_edit_dialog(self, config_id: str, info: dict, item: ProviderItem):
        # 发送信号，让主窗口处理卡片显示，传递配置 ID 和配置信息
        self.showEditProviderCard.emit(config_id, info)

    def _show_confirm_dialog(self, item: ProviderItem):
        from app.widgets.common_dialogs import ConfirmDialog

        _confirmed: list[bool] = [False]

        def _on_confirm():
            _confirmed[0] = True

        dialog = ConfirmDialog(
            title="删除服务商",
            content=f"确定要删除服务商「{item.provider_name}」吗？\n删除后将不再出现在列表中。",
            confirm_text="删除",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(_on_confirm)
        dialog.exec_()
        if _confirmed[0]:
            self._remove_provider(item)

    def _remove_provider(self, item: ProviderItem):
        if self._is_deleting:
            return  # 防止递归调用
        if item.config_id not in self.providers:
            return
        self._is_deleting = True
        try:
            del self.providers[item.config_id]
            qconfig.set(self.configItem, self.providers, save=True)
            self.viewLayout.removeWidget(item)
            item.deleteLater()
            self._adjust_view_size()
            # 同步清理该条目的凭证库残留（历史上只删配置，keyring 条目永久驻留）
            self._purge_credentials(item.config_id)
            self.providerChanged.emit(self.providers)
            # 如果删除的是默认服务商，则更新默认服务商
            if self.default_provider == item.config_id or self.default_provider == item.provider_name:
                keys = list(self.providers.keys())
                self.default_provider = keys[0] if keys else ""
                qconfig.set(self.defaultProviderItem, self.default_provider, save=True)
                self.defaultProviderChanged.emit(self.default_provider)
        except Exception as e:
            logger.error(f"[ProviderList] 删除服务商失败: {e}")
            raise
        finally:
            self._is_deleting = False
            # 在 _is_deleting 恢复后延迟刷新列表，确保 _refresh_items 不被屏障拦截
            QTimer.singleShot(0, self._refresh_items)

    @staticmethod
    def _purge_credentials(config_id: str):
        """清理被删服务商在系统凭证库里的残留条目（失败不影响删除流程）

        背景：历史上删除只移除 app.config 里的配置，OS 凭证库条目永久驻留
        （用户换 key 或删服务商后，旧密钥仍留在系统里）。
        """
        if not config_id:
            return
        try:
            from app.utils.secret_store import SecretStore, provider_account

            SecretStore().delete(provider_account(config_id))
        except Exception as e:
            logger.warning(f"[ProviderList] 清理凭证库失败 config_id={config_id}: {e}")

    def _select_provider(self, item: ProviderItem):
        # 取消旧选中项的样式标记
        for i in range(self.viewLayout.count()):
            w = self.viewLayout.itemAt(i).widget()
            if isinstance(w, ProviderItem) and w != item:
                if hasattr(w, "_is_selected"):
                    w._is_selected = False
                if hasattr(w, "_default_style"):
                    w.setStyleSheet(w._default_style)
        # 标记新选中项
        item._is_selected = True
        # 为新选中项添加标记样式（左边框高亮）
        if not hasattr(item, "_default_style"):
            item._default_style = item.styleSheet()
        indicator_style = f"""
            ProviderItem {{
                background-color: transparent;
                border-radius: 8px;
                border-left: 3px solid {Colors.SYSTEM_ACCENT};
            }}
            ProviderItem:hover {{
                background-color: {Colors.HOVER_BG};
            }}
        """
        item.setStyleSheet(indicator_style)
        # 默认服务商使用配置 ID
        self.default_provider = item.config_id
        qconfig.set(self.defaultProviderItem, self.default_provider, save=True)
        self.defaultProviderChanged.emit(self.default_provider)

    def refresh_style(self):
        """主题/字体变更时刷新所有服务商行与组头的字体颜色"""
        Colors.refresh()
        for label in self._group_headers:
            try:
                label.setStyleSheet(self._group_label_style())
            except RuntimeError:
                pass
        for i in range(self.viewLayout.count()):
            w = self.viewLayout.itemAt(i).widget()
            if isinstance(w, ProviderItem) and hasattr(w, "refresh_style"):
                try:
                    w.refresh_style()
                except RuntimeError:
                    pass

    def _get_focus_item(self):
        """展开卡片时滚到当前默认 provider 的 item（找不到默认则回退到第一个 item）

        被 LLMSettingsCard._scroll_focus_item_to_top 通过约定接口调用。
        """
        first_item = None
        for i in range(self.viewLayout.count()):
            w = self.viewLayout.itemAt(i).widget()
            if not isinstance(w, ProviderItem):
                continue
            if first_item is None:
                first_item = w
            if getattr(w, "is_default", False):
                return w
        return first_item
