# -*- coding: utf-8 -*-
"""插件设置主从页：左侧插件清单（全部插件，含无配置项）+ 右侧详情。

设计目标（2026-11-07）：
- 列表的原子单位是「插件」，不是「配置卡」。原来所有 config_schema 自动卡平铺在
  一列，插件多了只能靠滚动找，且不同插件的同名配置块无法区分。
- 无 config_schema 的插件也要出现：它们的权限（manifest.capabilities）与关于
  信息同样需要可查，只是配置 tab 显示空态。
- 详情三页签：配置 / 权限 / 关于。配置表单复用 PluginConfigForm，与折叠卡同一套
  渲染与保存逻辑。
- 搜索覆盖插件名、描述、配置字段 label、能力 id；待配置（必填缺失）置顶。

插件自行注册的一级设置大 tab（register_settings_tab）不在此页范围，保持原机制。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PillPushButton,
    PrimaryPushButton,
    SearchLineEdit,
    SegmentedWidget,
    StrongBodyLabel,
    isDarkTheme,
)

from loguru import logger

from app.plugins.capabilities.catalog import CATALOG
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.utils.design_tokens import Colors
from app.widgets.cards.settings.plugin_config_form import PluginConfigForm, evaluate_config_status
from app.widgets.cards.settings.plugin_components_card import _component_display_name

_SEARCH_DEBOUNCE_MS = 180
_LIST_WIDTH = 236
_STATE_RANK = {"missing": 0, "unconfigured": 1, "ok": 2, "none": 3}
_LEVEL_CN = {"low": "低", "medium": "中", "high": "高", "none": "无"}


@dataclass
class PluginEntry:
    """列表条目：插件快照 + 配置状态（构建时算一次，避免每帧读盘）"""

    info: Any
    enabled: bool
    state: str = "none"
    missing: List[str] = field(default_factory=list)
    has_schema: bool = False
    components: List[str] = field(default_factory=list)
    # 插件通过 register_settings_card 注册的自定义设置卡（非 config_schema 自动卡）。
    # 它们比自动表单更贴合插件语义（如 git-panel 的多行提示词编辑器），详情页优先渲染。
    custom_cards: List[Any] = field(default_factory=list)

    @property
    def has_config(self) -> bool:
        return self.has_schema or bool(self.custom_cards)


class _PluginListRow(QWidget):
    """插件列表行：图标 + 名称 + 状态副标题；点击切换右侧详情。"""

    clicked = pyqtSignal(str)  # plugin_name

    def __init__(self, entry: PluginEntry, parent=None):
        super().__init__(parent)
        self._entry = entry
        self._selected = False
        self.setObjectName("pluginListRow")
        # QWidget 子类默认不绘制样式表背景：不设 WA_StyledBackground 时
        # #pluginListRow 的 background/border-left 全部不生效（选中态看不见）
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(58)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(10)

        self._icon = QLabel(self)
        self._icon.setFixedSize(24, 24)
        pix = _plugin_icon(entry.info, 24)
        if pix is not None:
            self._icon.setPixmap(pix)
        layout.addWidget(self._icon, 0, Qt.AlignVCenter)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(1)
        self._name = BodyLabel(entry.info.name, self)
        self._sub = CaptionLabel(self._subtitle_text(entry), self)
        text_box.addWidget(self._name)
        text_box.addWidget(self._sub)
        layout.addLayout(text_box, 1)

        self._dot = QLabel(self)
        self._dot.setFixedSize(8, 8)
        self._dot.setStyleSheet(f"background: {_state_color(entry)}; border-radius: 4px;")
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)

        _font_px(self._name, 1)
        _font_px(self._sub, -1)
        self._apply_style()

    @staticmethod
    def _subtitle_text(entry: PluginEntry) -> str:
        if entry.state == "missing":
            return f"待填 {len(entry.missing)} 项 · v{entry.info.version}"
        if entry.state == "unconfigured":
            return f"未配置 · v{entry.info.version}"
        if not entry.enabled:
            return f"已停用 · v{entry.info.version}"
        if entry.custom_cards and not entry.has_schema:
            return f"v{entry.info.version} · 自定义配置"
        return f"v{entry.info.version} · {len(entry.components)} 类组件"

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self._apply_style()

    def _apply_style(self) -> None:
        if self._selected:
            self.setStyleSheet(
                f"""
                #pluginListRow {{
                    background: {Colors.TAB_ACTIVE_BG};
                    border-left: 3px solid {Colors.TEXT_ACCENT};
                    border-radius: 6px;
                }}
                """
            )
        else:
            self.setStyleSheet(
                f"""
                #pluginListRow {{
                    background: transparent;
                    border-left: 3px solid transparent;
                    border-radius: 6px;
                }}
                #pluginListRow:hover {{ background: {Colors.HOVER_BG}; }}
                """
            )

    def update_entry(self, state: str, missing: list) -> None:
        """配置保存后刷新副标题与状态点（不重建行，避免搜索框丢焦点）"""
        self._entry.state = state
        self._entry.missing = missing
        self._sub.setText(self._subtitle_text(self._entry))
        self._dot.setStyleSheet(f"background: {_state_color(self._entry)}; border-radius: 4px;")

    def matches(self, keyword: str) -> bool:
        kw = (keyword or "").strip().lower()
        if not kw:
            return True
        info = self._entry.info
        haystack = [info.name, info.description, " ".join(self._entry.components)]
        schema = PluginConfigRegistry.get_instance().get(info.name)
        if schema is not None:
            haystack.append(schema.title)
            for f in schema.fields:
                haystack.extend((f.label, f.key))
        for cap in info.manifest.get("capabilities") or []:
            haystack.append(str(cap))
        return any(kw in str(text).lower() for text in haystack if text)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._entry.info.name)
        super().mouseReleaseEvent(event)


class _KeyValueRow(QWidget):
    """关于页键值行：左标签定宽，右值自动换行。"""

    def __init__(self, key: str, value: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(12)
        label = CaptionLabel(key, self)
        label.setMinimumWidth(96)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        val = BodyLabel(value or "-", self)
        val.setWordWrap(True)
        layout.addWidget(label, 0, Qt.AlignLeft | Qt.AlignTop)
        layout.addWidget(val, 1)


class _EmptyHint(QWidget):
    """空态提示：一句结论 + 一句操作建议。"""

    def __init__(self, title: str, detail: str = "", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 32, 24, 32)
        layout.setSpacing(6)
        layout.addWidget(BodyLabel(title, self), 0, Qt.AlignCenter)
        if detail:
            hint = CaptionLabel(detail, self)
            hint.setWordWrap(True)
            hint.setAlignment(Qt.AlignCenter)
            layout.addWidget(hint, 0, Qt.AlignCenter)
        layout.addStretch(1)


def _apply_font(widget: QWidget) -> None:
    """对齐设置页全局字号（新构造的表单不在启动期 apply 范围内）"""
    try:
        from app.utils.design_tokens import apply_font_size_to_widget, get_ui_font_size

        apply_font_size_to_widget(widget, get_ui_font_size())
    except Exception:
        pass


def _font_px(widget: QWidget, delta: int) -> None:
    """在全局字号基础上偏移（层级用：标题放大、辅助文字缩小）

    qfluentwidgets 的 QSS 硬编码 font-size，setFont 会被覆盖，只能写 stylesheet。
    """
    try:
        from app.utils.design_tokens import get_ui_font_size, scale_font_size

        px = scale_font_size(max(9, get_ui_font_size() + delta))
        widget.setStyleSheet(f"font-size: {px}px;")
    except Exception:
        pass


def _plugin_icon(info: Any, size: int) -> Optional[QPixmap]:
    """插件图标 → 指定尺寸 pixmap；缺失/解析失败返回 None（不留空图标框）"""
    try:
        cfg = getattr(info, "icon_config", None)
        if not cfg:
            return None
        path = cfg.get("dark" if isDarkTheme() else "light")
        if not path:
            return None
        pix = QIcon(str(path)).pixmap(size, size)
        return pix if not pix.isNull() else None
    except Exception:
        return None


def _state_color(entry: PluginEntry) -> str:
    if entry.state in ("missing", "unconfigured"):
        return Colors.ERROR
    if not entry.enabled:
        return Colors.TEXT_MUTED
    return Colors.SUCCESS


class _PluginDetailView(QWidget):
    """右侧详情：插件头部 + 配置 / 权限 / 关于 三页签。"""

    configChanged = pyqtSignal(object)  # PluginEntry：字段保存后状态变化

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entry: Optional[PluginEntry] = None
        self._form: Optional[PluginConfigForm] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 6, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        self._icon = QLabel(self)
        self._icon.setFixedSize(32, 32)
        header.addWidget(self._icon, 0, Qt.AlignVCenter)
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(1)
        self._title = StrongBodyLabel("", self)
        self._meta = CaptionLabel("", self)
        title_box.addWidget(self._title)
        title_box.addWidget(self._meta)
        header.addLayout(title_box, 1)
        layout.addLayout(header)

        self._seg = SegmentedWidget(self)
        self._seg.addItem("config", "配置")
        self._seg.addItem("perms", "权限")
        self._seg.addItem("about", "关于")
        self._seg.currentItemChanged.connect(self._on_segment_changed)
        layout.addWidget(self._seg)

        self._stack = QStackedWidget(self)
        self._stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._stack, 1)

        self._pages = {
            "config": self._make_scroll_page(),
            "perms": self._make_scroll_page(),
            "about": self._make_scroll_page(),
        }
        for page in self._pages.values():
            self._stack.addWidget(page)

    @staticmethod
    def _make_scroll_page() -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        return area

    def show_entry(self, entry: PluginEntry) -> None:
        self._entry = entry
        info = entry.info
        self._title.setText(info.name)
        meta = f"v{info.version} · {'内置' if info.is_system else '用户'} · {'已启用' if entry.enabled else '已停用'}"
        self._meta.setText(meta)
        _font_px(self._title, 3)
        _font_px(self._meta, -1)
        pix = _plugin_icon(info, 32)
        self._icon.clear()
        if pix is not None:
            self._icon.setPixmap(pix)

        self._set_page("config", self._build_config_page(entry))
        self._set_page("perms", self._build_perms_page(entry))
        self._set_page("about", self._build_about_page(entry))
        self._rebuild_segments(
            has_config=entry.has_config,
            has_perms=bool(entry.info.manifest.get("capabilities")),
        )
        _apply_font(self)

    def _rebuild_segments(self, has_config: bool, has_perms: bool) -> None:
        """按插件实际能力重建页签

        - 有 config_schema / 自定义设置卡 → 配置
        - manifest 声明了 capabilities → 权限
        - 关于始终存在
        一个插件既没配置也没权限时只显示「关于」，不留空页签。
        """
        self._seg.clear()
        routes = []
        if has_config:
            routes.append("config")
        if has_perms:
            routes.append("perms")
        routes.append("about")
        for key, text in (("config", "配置"), ("perms", "权限"), ("about", "关于")):
            if key in routes:
                self._seg.addItem(key, text)
        first = routes[0]
        self._seg.setCurrentItem(first)
        self._stack.setCurrentWidget(self._pages[first])

    def _set_page(self, key: str, widget: QWidget) -> None:
        area = self._pages[key]
        old = area.takeWidget()
        if old is not None:
            old.deleteLater()
        area.setWidget(widget)

    def _on_segment_changed(self, key: str) -> None:
        area = self._pages.get(key)
        if area is not None:
            self._stack.setCurrentWidget(area)

    # ── 配置页 ────────────────────────────────────────────

    def _build_config_page(self, entry: PluginEntry) -> QWidget:
        self._form = None
        if entry.custom_cards:
            return self._build_custom_cards_page(entry)
        schema = PluginConfigRegistry.get_instance().get(entry.info.name)
        if schema is None:
            return _EmptyHint(
                "此插件没有可配置项",
                "插件可在 manifest 的 config_schema 里声明字段，宿主会自动生成配置表单。",
            )
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 4, 8, 12)
        layout.setSpacing(6)
        if entry.state == "missing":
            warn = CaptionLabel(f"待填写：{'、'.join(entry.missing)}", body)
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {Colors.ERROR};")
            layout.addWidget(warn)
        form = PluginConfigForm(entry.info.name, body)
        _apply_font(form)
        form.valueChanged.connect(lambda _k, _e=entry: self._on_value_changed(_e))
        self._form = form
        layout.addWidget(form)
        layout.addWidget(CaptionLabel("改动即时保存，无需确认按钮", body))
        layout.addStretch(1)
        return body

    def _build_custom_cards_page(self, entry: PluginEntry) -> QWidget:
        """插件自带设置卡：按注册顺序实例化，默认展开（详情页即配置面）"""
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 4, 8, 12)
        layout.setSpacing(8)
        for info in entry.custom_cards:
            try:
                card = info.widget_class(parent=body)
                _apply_font(card)
            except Exception as e:  # noqa: BLE001 - 单卡失败不拖垮详情页
                logger.warning(f"[PluginSettingsPage] 自定义设置卡构建失败 {info.card_id}: {e}")
                continue
            # ExpandSettingCard 默认折叠；详情页里展开，避免「点开插件还要再点一次」
            try:
                if hasattr(card, "setExpand") and not getattr(card, "isExpand", False):
                    card.setExpand(True)
            except Exception:
                pass
            layout.addWidget(card)
        if layout.count() == 0:
            layout.addWidget(_EmptyHint("此插件的自定义设置卡加载失败", "可尝试重载插件或查看日志。"))
        layout.addStretch(1)
        return body

    def _on_value_changed(self, entry: PluginEntry) -> None:
        """字段保存后刷新条目状态（列表行不重建，避免焦点丢失）"""
        state, missing = evaluate_config_status(entry.info.name)
        entry.state = state
        entry.missing = missing
        self.configChanged.emit(entry)

    # ── 权限页 ────────────────────────────────────────────

    def _build_perms_page(self, entry: PluginEntry) -> QWidget:
        caps = [str(c) for c in (entry.info.manifest.get("capabilities") or [])]
        if not caps:
            return _EmptyHint(
                "此插件未声明任何能力",
                "未声明即无法调用宿主能力；插件需要什么就在 manifest 的 capabilities 里列什么。",
            )
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 4, 8, 12)
        layout.setSpacing(6)
        layout.addWidget(CaptionLabel(f"共 {len(caps)} 项能力，按 manifest 声明授权", body))
        for cap in caps:
            spec = CATALOG.get(cap)
            level = getattr(spec.level, "value", spec.level) if spec is not None else "none"
            level_cn = _LEVEL_CN.get(str(level).lower(), str(level))
            color = {
                "high": Colors.ERROR,
                "medium": Colors.WARNING,
                "low": Colors.SUCCESS,
            }.get(str(level).lower(), Colors.TEXT_MUTED)
            row = QWidget(body)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 3, 0, 3)
            row_layout.setSpacing(10)
            badge = CaptionLabel(level_cn, row)
            badge.setFixedWidth(28)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(f"color: {color}; border: 1px solid {color}; border-radius: 4px;")
            row_layout.addWidget(badge, 0, Qt.AlignTop)
            text_box = QVBoxLayout()
            text_box.setContentsMargins(0, 0, 0, 0)
            text_box.setSpacing(1)
            text_box.addWidget(BodyLabel(cap, row))
            summary = spec.summary if spec is not None else "未在能力目录登记（插件自建能力，宿主不做语义校验）"
            hint = CaptionLabel(summary, row)
            hint.setWordWrap(True)
            text_box.addWidget(hint)
            row_layout.addLayout(text_box, 1)
            layout.addWidget(row)
        layout.addStretch(1)
        return body

    # ── 关于页 ────────────────────────────────────────────

    def _build_about_page(self, entry: PluginEntry) -> QWidget:
        info = entry.info
        manifest = info.manifest
        author = manifest.get("author") or {}
        if isinstance(author, dict):
            author_text = str(author.get("name") or "")
            if author.get("url"):
                author_text = f"{author_text} ({author['url']})"
        else:
            author_text = str(author)
        comps = "、".join(_component_display_name(c) for c in entry.components) or "无"

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 4, 8, 12)
        layout.setSpacing(2)
        layout.addWidget(_KeyValueRow("描述", info.description, body))
        layout.addWidget(_KeyValueRow("版本", str(info.version), body))
        layout.addWidget(_KeyValueRow("作者", author_text, body))
        layout.addWidget(_KeyValueRow("许可证", str(manifest.get("license") or "-"), body))
        layout.addWidget(_KeyValueRow("来源", "内置插件" if info.is_system else "用户插件", body))
        layout.addWidget(_KeyValueRow("状态", "已启用" if entry.enabled else "已停用", body))
        layout.addWidget(_KeyValueRow("组件", comps, body))
        layout.addWidget(_KeyValueRow("宿主要求", f"api {manifest.get('api_version', '-')} / 最低 {manifest.get('min_host_version', '-')}", body))
        layout.addWidget(_KeyValueRow("目录", str(getattr(info, "path", "")), body))

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 10, 0, 0)
        open_btn = PrimaryPushButton("打开插件目录", body)
        open_btn.clicked.connect(lambda: self._open_dir(info))
        btn_row.addWidget(open_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        layout.addStretch(1)
        return body

    @staticmethod
    def _open_dir(info: Any) -> None:
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(info.path)))
        except Exception as e:
            logger.warning(f"[PluginSettingsPage] 打开插件目录失败: {e}")


class PluginSettingsPage(QWidget):
    """插件设置页：搜索 + 状态筛选 + 插件列表 + 详情。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[PluginEntry] = []
        self._rows: List[_PluginListRow] = []
        self._keyword = ""
        self._filter = "configured"
        self._current: Optional[str] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._search = SearchLineEdit(self)
        self._search.setPlaceholderText("搜索插件、配置项、能力…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(32)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._apply_filter)
        self._search.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self._search)

        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        self._chips = {}
        # 默认只看「有配置」：插件页的主要任务是配置，无配置插件与全量放后面
        for key, text in (
            ("configured", "有配置"),
            ("todo", "待配置"),
            ("on", "已启用"),
            ("off", "已停用"),
            ("none", "无配置"),
            ("all", "全部"),
        ):
            chip = PillPushButton(text, self)
            chip.setCheckable(True)
            chip.setChecked(key == "configured")
            chip.clicked.connect(lambda _c=False, k=key: self._on_chip_clicked(k))
            chips.addWidget(chip)
            self._chips[key] = chip
        chips.addStretch(1)
        self._count_label = CaptionLabel("", self)
        chips.addWidget(self._count_label)
        layout.addLayout(chips)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        self._list_area = QScrollArea(self)
        self._list_area.setWidgetResizable(True)
        self._list_area.setFrameShape(QFrame.NoFrame)
        self._list_area.setFixedWidth(_LIST_WIDTH)
        self._list_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        list_body = QWidget()
        self._list_layout = QVBoxLayout(list_body)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch(1)
        self._list_area.setWidget(list_body)
        body.addWidget(self._list_area)

        detail_frame = QFrame(self)
        detail_frame.setObjectName("pluginDetailFrame")
        detail_frame.setStyleSheet(
            f"#pluginDetailFrame {{ background: {Colors.CARD_BG.format(alpha=140)}; border: 1px solid {Colors.BORDER}; border-radius: 8px; }}"
        )
        detail_layout = QVBoxLayout(detail_frame)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self._empty = _EmptyHint("选择左侧插件查看详情", "配置 / 权限 / 关于都在这里。")
        self._detail = _PluginDetailView(detail_frame)
        self._detail.configChanged.connect(self.refresh_entry_state)
        self._detail.hide()
        detail_layout.addWidget(self._empty)
        detail_layout.addWidget(self._detail)
        body.addWidget(detail_frame, 1)

        layout.addLayout(body, 1)
        # 页面挂在设置页的 QScrollArea 里，不给最小高度会被压成 sizeHint 高度
        self.setMinimumHeight(480)
        _apply_font(self)

    # ── 数据与刷新 ────────────────────────────────────────

    def reload(self, force: bool = False) -> None:
        """重建条目与列表（插件装卸 / 热重载 / 设置页打开时调用）"""
        entries = self._collect_entries()
        if not force and self._signature(entries) == getattr(self, "_sig", None):
            self._update_counts()
            return
        self._sig = self._signature(entries)
        self._entries = entries
        self._rebuild_rows()
        self._update_counts()
        if self._current and any(e.info.name == self._current for e in entries):
            self._select(self._current, force=True)
        elif entries:
            self._select(entries[0].info.name)

    @staticmethod
    def _signature(entries: List[PluginEntry]) -> tuple:
        return tuple((e.info.name, e.enabled, e.state, len(e.components)) for e in entries)

    def _collect_entries(self) -> List[PluginEntry]:
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                return []
            plugins = pm.list_plugins()
        except Exception as e:
            logger.warning(f"[PluginSettingsPage] 读取插件清单失败: {e}")
            return []
        custom_map: dict = {}
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            for card_info in UIPluginRegistry.get_instance().get_settings_cards():
                if (card_info.metadata or {}).get("auto_config_card"):
                    continue
                custom_map.setdefault(card_info.plugin_name, []).append(card_info)
            for cards in custom_map.values():
                cards.sort(key=lambda c: -getattr(c, "priority", 0))
        except Exception as e:  # noqa: BLE001 - 注册表不可用时按无自定义卡处理
            logger.warning(f"[PluginSettingsPage] 读取插件自定义设置卡失败: {e}")

        entries: List[PluginEntry] = []
        for p in plugins:
            schema = PluginConfigRegistry.get_instance().get(p.name)
            state, missing = evaluate_config_status(p.name) if schema is not None else ("none", [])
            comps = sorted(k for k, v in (p.components or {}).items() if v)
            entries.append(
                PluginEntry(
                    info=p,
                    enabled=pm.is_enabled(p.name),
                    state=state,
                    missing=missing,
                    has_schema=schema is not None,
                    components=comps,
                    custom_cards=list(custom_map.get(p.name, [])),
                )
            )
        entries.sort(key=lambda e: (_STATE_RANK.get(e.state, 3), not e.info.is_system, e.info.name.lower()))
        return entries

    def _rebuild_rows(self) -> None:
        for row in self._rows:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        self._rows = []
        # 分帧挂载：60+ 行含图标解码，一帧建完会在打开设置时顿卡
        entries = list(self._entries)

        def _build_next(idx: int) -> None:
            if idx >= len(entries):
                return
            entry = entries[idx]
            row = _PluginListRow(entry, self._list_area.widget())
            row.clicked.connect(self._select)
            row.setVisible(row.matches(self._keyword) and self._match_filter(entry))
            if entry.info.name == self._current:
                row.set_selected(True)
            insert_at = self._list_layout.count() - 1
            self._list_layout.insertWidget(insert_at, row)
            self._rows.append(row)
            _apply_font(row)
            QTimer.singleShot(0, lambda i=idx + 1: _build_next(i))

        QTimer.singleShot(0, lambda: _build_next(0))

    # ── 筛选与搜索 ────────────────────────────────────────

    def _on_search_text_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _apply_filter(self) -> None:
        self._keyword = self._search.text().strip()
        for row in self._rows:
            visible = row.matches(self._keyword) and self._match_filter(row._entry)
            row.setVisible(visible)
        self._update_counts()

    def _match_filter(self, entry: PluginEntry) -> bool:
        if self._filter == "configured":
            return entry.has_config
        if self._filter == "none":
            return not entry.has_config
        if self._filter == "todo":
            return entry.state in ("missing", "unconfigured")
        if self._filter == "on":
            return entry.enabled
        if self._filter == "off":
            return not entry.enabled
        return True

    def _on_chip_clicked(self, key: str) -> None:
        self._filter = key
        for k, chip in self._chips.items():
            chip.setChecked(k == key)
        self._apply_filter()

    def _update_counts(self) -> None:
        total = len(self._entries)
        todo = sum(1 for e in self._entries if e.state in ("missing", "unconfigured"))
        on = sum(1 for e in self._entries if e.enabled)
        off = total - on
        configured = sum(1 for e in self._entries if e.has_config)
        self._chips["configured"].setText(f"有配置 {configured}")
        self._chips["todo"].setText(f"待配置 {todo}")
        self._chips["on"].setText(f"已启用 {on}")
        self._chips["off"].setText(f"已停用 {off}")
        self._chips["none"].setText(f"无配置 {total - configured}")
        self._chips["all"].setText(f"全部 {total}")
        if self._keyword:
            shown = sum(1 for r in self._rows if not r.isHidden())
            self._count_label.setText(f"命中 {shown} / {total}")
        else:
            self._count_label.setText(f"共 {total} 个插件")

    # ── 选中与详情 ────────────────────────────────────────

    def _select(self, name: str, force: bool = False) -> None:
        if not force and name == self._current:
            return
        entry = next((e for e in self._entries if e.info.name == name), None)
        if entry is None:
            return
        self._current = name
        for row in self._rows:
            row.set_selected(row._entry.info.name == name)
        self._empty.hide()
        self._detail.show()
        self._detail.show_entry(entry)

    def refresh_style(self) -> None:
        """主题切换后刷新行样式（设置面板统一调用）"""
        for row in self._rows:
            row._apply_style()

    def refresh_entry_state(self, entry: PluginEntry) -> None:
        """字段保存后同步列表行、排序签名与筛选计数（详情页信号回调）"""
        for row in self._rows:
            if row._entry.info.name == entry.info.name:
                row.update_entry(entry.state, entry.missing)
                break
        self._entries.sort(key=lambda e: (_STATE_RANK.get(e.state, 3), not e.info.is_system, e.info.name.lower()))
        self._sig = self._signature(self._entries)
        self._update_counts()
