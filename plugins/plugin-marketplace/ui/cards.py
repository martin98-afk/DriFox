# -*- coding: utf-8 -*-
"""MarketplaceCard 浮动卡片 — 完整的插件市场浏览界面

功能：
- 异步拉取市场列表（不阻塞 UI）
- 异步安装/卸载/更新插件
- 插件搜索过滤
- 版本检测：已安装插件有新版时显示「更新」按钮
- 安装/更新状态实时反馈
"""

import re
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QRect, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    FluentLabelBase,
    IconWidget,
    InfoBar,
    LineEdit,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    TransparentToolButton,
    isDarkTheme,
)

from .data import get_marketplace
from .installer import get_installer
from .marketplace_manager import get_marketplace_manager
from ._squircle_avatar import SquircleAvatar, PluginIconWidget, extract_initials, name_color

# ── 主题色辅助 ──────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    """（已废弃，保留向后兼容）请改用卡片注入的 context 主题色"""
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    """从上下文 colors 中获取文字颜色，无上下文则回退到 _text_color()"""
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    return _text_color(secondary)


def _ctx_border_color(ctx: dict) -> str:
    """从上下文 colors 中获取边框颜色"""
    return ctx.get("colors", {}).get("border", "rgba(128,128,128,0.15)")


def _ctx_font(ctx: dict) -> tuple:
    """从上下文提取 font_family 和 font_size"""
    ff = ctx.get("font_family", "")
    fs = ctx.get("font_size", 0)
    return ff, fs or 14


# ── 异步工作器 ──────────────────────────────────────────────


class _MarketplaceWorker(QObject):
    """在后台线程执行阻塞操作，通过信号返回结果"""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── 路径解析 ──────────────────────────────────────────────


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）

    开发环境: 当前目录/.drifox
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox
    """
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        return Path(".drifox")
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask

            paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
                NSApplicationSupportDirectory, NSUserDomainMask
            )
            if paths:
                app_support_path = paths[0].fileSystemRepresentation().decode("utf-8")
                app_support = Path(app_support_path) / "Drifox"
                app_support.mkdir(parents=True, exist_ok=True)
                return app_support / ".drifox"
        except Exception:
            pass
    return Path.home() / ".drifox"


def _highlight_html(text: str, query: str) -> str:
    """HTML 转义并对搜索词加高亮背景"""
    if not query or not text:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(
        re.escape(query),
        lambda m: f'<span style="background:rgba(255,167,38,0.25);border-radius:2px;padding:0 1px;">{m.group()}</span>',
        escaped,
        flags=re.IGNORECASE,
    )


class _FlowLayout(QLayout):
    """简易流式布局：子控件按宽度自动换行排列"""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._spacing = spacing
        self._items: list = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Vertical

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size + QSize(2 * self._spacing, 2 * self._spacing)

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        for item in self._items:
            w = item.widget()
            if w is None:
                continue
            hint = item.sizeHint()
            next_x = x + hint.width()
            if next_x > rect.right() and x > rect.x():
                x = rect.x()
                y += line_height + self._spacing
                line_height = 0
                next_x = x + hint.width()
            if not test_only:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x = next_x + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


# ── 单行插件卡片 ────────────────────────────────────────────


class _PluginRow(QFrame):
    """单个插件的展示行（简约卡片风格）

    状态说明：
    - 未安装：显示「安装」按钮
    - 已安装 & 最新版：显示「已安装」按钮（禁用）
    - 已安装 & 有新版：显示「更新」按钮（橙色）
    - 操作中：显示「处理中…」按钮（禁用）
    """

    installRequested = pyqtSignal(dict)  # plugin_meta
    updateRequested = pyqtSignal(dict)  # plugin_meta（有新版时触发）

    def __init__(
        self,
        plugin_meta: dict,
        installed: bool,
        has_update: bool = False,
        local_version: Optional[str] = None,
        parent=None,
        font_size: int = 0,
        search_query: str = "",
    ):
        super().__init__(parent)
        self._meta = plugin_meta
        self._installed = installed
        self._has_update = has_update
        self._local_version = local_version
        self._busy = False
        self._font_size = font_size  # 上下文字体大小（用于头像自适应）
        self._btn_font_size = max(13, font_size) if font_size > 0 else 14
        self._avatar = None
        self._search_query = search_query
        self._tags = self._compute_tags(plugin_meta)
        self._setup_ui()

    def _setup_ui(self):
        self.setObjectName("pluginRow")
        self.setStyleSheet(
            "#pluginRow { background: transparent; border: 1px solid rgba(128,128,128,0.12); border-radius: 8px; }"
        )
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # 插件图标：SVG icon 优先，无图标则用缩写头像
        self._avatar = self._create_icon_widget()
        layout.addWidget(self._avatar)

        # 信息区
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        name = self._meta.get("name", "未知")
        remote_ver = self._meta.get("version", "")

        # 标题行：名称 + 版本，搜索词高亮（HTML 内嵌字号，因 QLabel 富文本忽略 QSS font-size）
        ver_suffix = ""
        if self._has_update and self._local_version and remote_ver:
            ver_suffix = f"  v{self._local_version} → v{remote_ver}"
        elif remote_ver:
            ver_suffix = f"  v{remote_ver}"
        self._name_raw = name
        self._version_suffix = ver_suffix
        title_fs = max(9, self._font_size - 2) if self._font_size > 0 else 13
        name_html = _highlight_html(name, self._search_query)
        name_text = f'<span style="font-size:{title_fs}pt;">{name_html}{ver_suffix}</span>'
        self._title_label = QLabel(name_text, self)
        self._title_label.setObjectName("pluginRowTitle")
        self._title_label.setStyleSheet(f"color: {_text_color()}; font-weight: bold; background: transparent;")
        info_layout.addWidget(self._title_label)

        # 版本更新提示小标签（仅在有更新时显示）
        if self._has_update:
            update_tag = QLabel("🔄 有新版本", self)
            update_tag.setStyleSheet("color: #FFA726; font-size: 11px; background: transparent;")
            info_layout.addWidget(update_tag)

        desc = self._meta.get("description", "")
        self._desc_label = None
        self._desc_raw = ""
        if desc:
            self._desc_raw = desc[:120]
            desc_text = _highlight_html(self._desc_raw, self._search_query)
            self._desc_label = QLabel(desc_text, self)
            self._desc_label.setWordWrap(True)
            self._desc_label.setObjectName("pluginRowDesc")
            self._desc_label.setStyleSheet(
                f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
            )
            info_layout.addWidget(self._desc_label)

        # Tag 标签行（FlowLayout 自动换行，不撑大卡片宽度）
        self._tag_labels: list = []
        if self._tags:
            tags_widget = QWidget(self)
            tags_layout = _FlowLayout(tags_widget, spacing=4)
            tags_layout.setContentsMargins(0, 2, 0, 0)
            for tag in self._tags:
                tag_text = (
                    _highlight_html(tag, self._search_query)
                    if (self._search_query and self._search_query.lower() in tag.lower())
                    else tag.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                )
                lbl = QLabel(tag_text, tags_widget)
                lbl.setObjectName("pluginRowTag")
                lbl.setStyleSheet(self._tag_stylesheet())
                tags_layout.addWidget(lbl)
                self._tag_labels.append(lbl)
            info_layout.addWidget(tags_widget)

        # 市场来源标签
        marketplace = self._meta.get("_marketplace", "")
        if marketplace:
            mp_label = QLabel(f"📦 {marketplace}", self)
            mp_label.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 10px; background: transparent;")
            info_layout.addWidget(mp_label)

        layout.addLayout(info_layout, 1)

        # 操作按钮
        self._btn = PushButton(self)
        self._btn.setFixedWidth(100)
        # 保存 FluentUI 默认样式，仅追加 font-size 不改其他
        self._original_btn_style = self._btn.styleSheet()
        self._update_btn_text()
        self._btn.clicked.connect(self._on_click)
        layout.addWidget(self._btn)

    def _update_btn_text(self):

        fs = self._btn_font_size
        btn_font = self._btn.font()
        btn_font.setPixelSize(fs)
        self._btn.setFont(btn_font)

        if self._busy:
            self._btn.setText("处理中…")
            self._btn.setEnabled(False)
            self._btn.setStyleSheet(self._original_btn_style)
        elif self._has_update:
            self._btn.setText("更新")
            self._btn.setEnabled(True)
            self._btn.setStyleSheet(
                "PushButton { background: rgba(255, 167, 38, 0.2); "
                "color: #FFA726; border: 1px solid rgba(255, 167, 38, 0.3); "
                "border-radius: 4px; }"
                "PushButton:hover { background: rgba(255, 167, 38, 0.35); }"
            )
        elif self._installed:
            self._btn.setText("已安装")
            self._btn.setEnabled(False)
            self._btn.setStyleSheet(self._original_btn_style)
        else:
            self._btn.setText("安装")
            self._btn.setEnabled(True)
            self._btn.setStyleSheet(self._original_btn_style)

    def _on_click(self):
        if self._busy:
            return
        if not self._installed:
            # 未安装 → 安装
            self._busy = True
            self._update_btn_text()
            self.installRequested.emit(self._meta)
        elif self._has_update:
            # 已安装且有新版 → 更新
            self._busy = True
            self._update_btn_text()
            self.updateRequested.emit(self._meta)

    def set_installed(self, installed: bool):
        """安装/更新完成后刷新状态"""
        self._installed = installed
        self._has_update = False
        self._busy = False
        self._update_btn_text()

    def set_has_update(self, has_update: bool):
        """设置是否有可用更新"""
        self._has_update = has_update
        self._update_btn_text()

    def set_error(self):
        """安装/更新失败后恢复按钮"""
        self._busy = False
        self._update_btn_text()

    def _create_icon_widget(self) -> QWidget:
        """创建插件图标组件：优先检查本地已安装的 SVG 图标"""
        plugin_name = self._meta.get("name", "?")
        local_path = self._find_local_plugin_path(plugin_name)
        if local_path:
            import json as _json

            for _meta_dir in (".drifox-plugin", ".claude-plugin"):
                _mp = local_path / _meta_dir / "plugin.json"
                if _mp.exists():
                    try:
                        _m = _json.loads(_mp.read_text(encoding="utf-8"))
                        return PluginIconWidget(
                            plugin_dir=local_path,
                            manifest=_m,
                            font_size=self._font_size,
                            parent=self,
                        )
                    except Exception:
                        pass
                    break
        # Fallback to initials avatar
        return SquircleAvatar(
            extract_initials(plugin_name),
            name_color(plugin_name),
            self,
            font_size=self._font_size,
        )

    @staticmethod
    def _find_local_plugin_path(name: str) -> Optional[Path]:
        """在本地插件目录查找指定名称的插件"""
        dev = Path(__file__).resolve().parent.parent.parent.parent / "plugins" / name
        if dev.is_dir():
            return dev
        drifox = _drifox_dir()
        for base in (drifox / "plugins", drifox / "plugins-disabled"):
            p = base / name
            if p.is_dir():
                return p
        return None

    def set_font_size(self, font_size: int):
        """根据上下文字体大小动态调整头像尺寸"""
        self._font_size = font_size
        if self._avatar is not None and hasattr(self._avatar, "set_font_size"):
            self._avatar.set_font_size(font_size)

    # ── Tag 相关 ─────────────────────────────────────────────

    @staticmethod
    def _compute_tags(meta: dict) -> list:
        """合并 categories / category / keywords 为展示标签（去重）"""
        seen: set = set()
        tags: list = []
        # categories（数组）
        for c in (meta.get("categories", []) or []):
            if c and c not in seen:
                tags.append(c)
                seen.add(c)
        # category（单数字段，如 "productivity"）
        cat = meta.get("category")
        if isinstance(cat, str) and cat and cat not in seen:
            tags.append(cat)
            seen.add(cat)
        # keywords（数组）
        for k in (meta.get("keywords", []) or []):
            if k and k not in seen:
                tags.append(k)
                seen.add(k)
        return tags

    def _tag_stylesheet(self) -> str:
        """tag 标签 QSS 样式"""
        tc = _text_color(secondary=True)
        return (
            f"background: rgba(128,128,128,0.12); color: {tc}; border-radius: 4px; padding: 1px 6px; font-size: 10px;"
        )

    def update_search_highlight(self, query: str):
        """搜索词变化时更新标题/描述/tag 的高亮 HTML，无需重建 widget"""
        if query == self._search_query:
            return
        self._search_query = query

        # 标题
        title_fs = max(9, self._font_size - 2) if self._font_size > 0 else 13
        name_html = _highlight_html(self._name_raw, query)
        self._title_label.setText(f'<span style="font-size:{title_fs}pt;">{name_html}{self._version_suffix}</span>')

        # 描述
        if self._desc_label is not None:
            self._desc_label.setText(_highlight_html(self._desc_raw, query))

        # Tag
        for i, tag in enumerate(self._tags):
            if i < len(self._tag_labels):
                tag_text = (
                    _highlight_html(tag, query)
                    if query and query.lower() in tag.lower()
                    else tag.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                )
                self._tag_labels[i].setText(tag_text)


# ── 市场主卡片 ──────────────────────────────────────────────


class MarketplaceCard(QWidget):
    """插件市场浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_MarketplaceWorker] = None
        self._plugin_data: list = []
        self._matched_plugins: list = []
        self._rendered_count: int = 0
        self._row_map: dict = {}  # plugin_name → _PluginRow
        self._all_plugins: list = []
        self._all_loaded: bool = False  # 是否已全部渲染完
        self._current_filter: str = "all"
        self._header_icon: Optional[IconWidget] = None
        self._setup_ui()
        # 首次显示时由 show_card 触发加载，__init__ 不再自动加载

    # ── 拉模型上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）"""
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：用最新上下文刷新主题色 + 延迟加载数据"""
        self.setVisible(True)
        self._apply_latest_theme()
        self._apply_plugin_icon()
        # 清除旧搜索状态、防抖定时器
        self._search_edit.clear()
        self._search_debounce.stop()
        # 延迟 50ms 启动后台刷新，避免阻塞 show 过程
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(50, self._async_refresh)

    def _apply_plugin_icon(self):
        """从上下文获取插件图标并更新头部图标"""
        if self._context_provider is None or self._header_icon is None:
            return
        try:
            from PyQt5.QtGui import QIcon

            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme, "")
            if icon_path:
                self._header_icon.setIcon(QIcon(icon_path))
        except Exception:
            pass

    def _apply_latest_theme(self):
        """从上下文拉取最新主题色 + 字体并刷新全部子控件样式

        策略：
        - font-family 通过 self.setFont(QFont(family, 0)) 级联（size=0 不覆盖原有字号）
        - 颜色：只替换 stylesheet 中的 color 值，保留 font-size/font-weight 等原有属性
        - 动态创建的子控件创建后应调用 _retheme() 刷新
        """
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
        except Exception:
            return

        # ── 缓存上下文值（供动态创建的子控件使用） ──
        font_family, font_size = _ctx_font(ctx)
        tc = _ctx_text_color(ctx)
        tcs = _ctx_text_color(ctx, secondary=True)
        border_c = _ctx_border_color(ctx)
        self._cached_tc = tc
        self._cached_tcs = tcs
        self._cached_font_family = font_family
        self._cached_font_size = font_size

        # ── 把 font_size 传播到已存在的行（动态调整头像大小） ──
        for row in self.findChildren(_PluginRow):
            row.set_font_size(font_size)

        # ── 字体（通过 QFont 级联，使用系统字体大小） ──
        if font_family:
            self.setFont(QFont(font_family, font_size if font_size else 14))

        # ── 颜色：替换现有 labels 的 color 值，保留其他属性 ──
        self._retheme()

        # 更新搜索框
        try:
            self._search_edit.setStyleSheet(
                f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {tc};"
            )
        except RuntimeError:
            pass

        # 更新分隔线
        for sep in self.findChildren(QFrame):
            try:
                if sep.frameShape() == QFrame.HLine:
                    sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
            except RuntimeError:
                pass

    # objectName → font-size 偏移（用于插件行的标题/描述/tag）
    # pluginRowTitle: 标题用 font_size - 2（让标题比上下文默认小 2 号）
    # pluginRowDesc:  描述用 font_size - 4（再小 2 号，作为辅助文字）
    # pluginRowTag:   tag 标签用 font_size - 5（最小号辅助信息）
    _PLUGIN_ROW_SIZE_OFFSETS = {
        "pluginRowTitle": -2,
        "pluginRowDesc": -4,
        "pluginRowTag": -5,
    }

    def _retheme(self):
        """刷新所有已有子控件的颜色 + 字号 + 字体（对动态创建的内容也要调）

        关键：同时替换 QSS 中的 color 和 font-size，因为 QSS 的 font-size
        优先级高于 QFont 级联。如果不替换，原来写了 font-size: 11px 的
        标签会始终保持 11px，而不是跟随系统字体大小。

        插件行标题/描述：通过 objectName 识别，按 _PLUGIN_ROW_SIZE_OFFSETS
        应用 font_size 偏移。其它标签使用 font_size 本身。
        """
        tc = getattr(self, "_cached_tc", "rgba(255,255,255,0.9)")
        tcs = getattr(self, "_cached_tcs", "rgba(255,255,255,0.55)")
        ff = getattr(self, "_cached_font_family", "")
        fs = getattr(self, "_cached_font_size", 14)

        for child in self.findChildren(QLabel):
            try:
                # 标题/描述应用 font_size 偏移
                offset = self._PLUGIN_ROW_SIZE_OFFSETS.get(child.objectName(), 0)
                target_fs = max(8, fs + offset) if fs > 0 else 14 + offset

                # StrongBodyLabel 等 FluentLabelBase 内部 self.setFont() 覆盖了
                # 父级 QFont 级联，需要直接 setFont 覆盖
                if isinstance(child, FluentLabelBase) and ff:
                    child.setFont(QFont(ff, target_fs))

                ss = child.styleSheet()
                if not ss:
                    continue
                import re

                new_ss = re.sub(r"color:\s*[^;]+;", f"color: {tc};", ss)
                # 替换 font-size（QSS 优先级高于 QFont，必须替换）
                if target_fs:
                    new_ss = re.sub(r"font-size:\s*[^;]+;", f"font-size: {target_fs}px;", new_ss)
                # 追加 font-family（如果原样式没有）
                if ff and f"font-family: '{ff}'" not in new_ss:
                    new_ss += f" font-family: '{ff}';"
                child.setStyleSheet(new_ss)
            except RuntimeError:
                pass

    # ── 界面搭建 ──

    def _setup_ui(self):
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("MarketplaceCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部（全局固定，切换标签不变）──
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 4)
        header_layout.setSpacing(8)

        icon = IconWidget(FluentIcon.SHOPPING_CART, header)
        icon.setFixedSize(22, 22)
        header_layout.addWidget(icon)
        self._header_icon = icon

        title = StrongBodyLabel("插件市场", header)
        title.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        header_layout.addWidget(title)

        # ── 浏览/市场切换（标题行内）──
        from qfluentwidgets import Pivot

        self._tab_bar = Pivot(header)
        self._tab_bar.addItem("browse", "浏览", None, None)
        self._tab_bar.addItem("markets", "市场", None, None)
        self._tab_bar.setCurrentItem("browse")
        self._tab_bar.currentItemChanged.connect(self._on_tab_changed)
        header_layout.addWidget(self._tab_bar)

        header_layout.addStretch(1)

        self._status_label = QLabel("", header)
        self._status_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        header_layout.addWidget(self._status_label)

        # 刷新 + 关闭（刷新在左，关闭在右）
        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setFixedSize(24, 24)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh)
        header_layout.addWidget(self._refresh_btn)

        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        self._close_btn.setFixedSize(24, 24)
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self._on_close)
        header_layout.addWidget(self._close_btn)

        root.addWidget(header)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # ── 页面堆叠 ──
        from PyQt5.QtWidgets import QStackedWidget

        self._page_stack = QStackedWidget(self)
        self._page_stack.setStyleSheet("background: transparent;")

        # ===== 浏览页 =====
        self._browse_page = QWidget(self._page_stack)
        browse_root = QVBoxLayout(self._browse_page)
        browse_root.setContentsMargins(0, 0, 0, 0)
        browse_root.setSpacing(0)

        # ── 筛选标签（全部/已安装/未安装/待更新）──
        self._filter_bar = Pivot(self._browse_page)
        self._filter_bar.addItem("all", "全部", None, None)
        self._filter_bar.addItem("installed", "已安装", None, None)
        self._filter_bar.addItem("uninstalled", "未安装", None, None)
        self._filter_bar.addItem("updates", "待更新", None, None)
        self._filter_bar.setCurrentItem("all")
        self._filter_bar.currentItemChanged.connect(self._on_filter_changed)
        browse_root.addWidget(self._filter_bar)

        # ── 搜索框 ──
        search_row = QWidget(self._browse_page)
        search_layout = QHBoxLayout(search_row)
        search_layout.setContentsMargins(16, 4, 16, 4)

        self._search_edit = LineEdit(search_row)
        self._search_edit.setPlaceholderText("搜索插件…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setStyleSheet(
            f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {_text_color()};"
        )
        # 防抖 300ms，避免每敲一个字就全量重建
        from PyQt5.QtCore import QTimer
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self._filter_plugins)
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(self._search_edit)
        browse_root.addWidget(search_row)

        self._content_stack = QStackedWidget(self._browse_page)
        self._content_stack.setStyleSheet("background: transparent;")

        self._scroll = ScrollArea(self._browse_page)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 8, 12, 8)
        self._content_layout.setSpacing(6)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        self._content_stack.addWidget(self._scroll)

        self._empty_label = StrongBodyLabel("暂无可用插件", self._browse_page)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent;")
        self._content_stack.addWidget(self._empty_label)

        self._content_stack.setCurrentIndex(0)
        browse_root.addWidget(self._content_stack, 1)

        self._page_stack.addWidget(self._browse_page)

        # ===== 市场管理页 =====
        self._markets_page = QWidget(self._page_stack)
        markets_root = QVBoxLayout(self._markets_page)
        markets_root.setContentsMargins(0, 0, 0, 0)
        markets_root.setSpacing(0)

        add_row = QWidget(self._markets_page)
        add_layout = QHBoxLayout(add_row)
        add_layout.setContentsMargins(16, 12, 16, 4)
        add_layout.setSpacing(8)

        self._market_url_edit = LineEdit(add_row)
        self._market_url_edit.setPlaceholderText("owner/repo 或 URL，如 claude-market/marketplace")
        self._market_url_edit.setClearButtonEnabled(True)
        add_layout.addWidget(self._market_url_edit)

        add_btn = PushButton("添加", add_row)
        add_btn.setFixedWidth(80)
        add_btn.clicked.connect(self._on_add_marketplace)
        add_layout.addWidget(add_btn)

        markets_root.addWidget(add_row)

        self._markets_scroll = ScrollArea(self._markets_page)
        self._markets_scroll.setWidgetResizable(True)
        self._markets_scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._markets_content = QWidget(self._markets_scroll)
        self._markets_content.setStyleSheet("background: transparent;")
        self._markets_content_layout = QVBoxLayout(self._markets_content)
        self._markets_content_layout.setContentsMargins(16, 4, 16, 8)
        self._markets_content_layout.setSpacing(6)
        self._markets_content_layout.setAlignment(Qt.AlignTop)
        self._markets_scroll.setWidget(self._markets_content)
        markets_root.addWidget(self._markets_scroll, 1)

        self._page_stack.addWidget(self._markets_page)

        self._page_stack.setCurrentIndex(0)
        root.addWidget(self._page_stack, 1)

    # ── 高度模式 ──

    def sizeHint(self):
        """与 SystemCardFrame proportional 模式一致：返回窗口高度的 85%"""
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):
        """显示时安装窗口 resize 事件过滤器，窗口缩放时通知容器重新展开"""
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        """监听窗口 resize，触发 updateGeometry → CardContainer 重算高度"""
        from PyQt5.QtCore import QEvent

        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    # ── 异步刷新 ──

    def _async_refresh(self, force: bool = False):
        """在后台线程拉取市场数据

        Args:
            force: 是否强制拉取远程（跳过缓存）
        """
        self._set_loading(True)
        self._cleanup_worker()
        if force:
            self._worker = _MarketplaceWorker(lambda: get_marketplace().list_plugins(force=True))
        else:
            self._worker = _MarketplaceWorker(lambda: get_marketplace().list_plugins())
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_refresh_done)
        self._worker.error.connect(self._on_refresh_error)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_refresh_done(self, plugins: list):
        """刷新完成"""
        self._plugin_data = plugins or []
        self._set_loading(False)
        self._render_plugins(self._plugin_data)

    def _on_refresh_error(self, err: str):
        """刷新失败"""
        self._set_loading(False)
        self._status_label.setText("加载失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._clear_plugin_list()
        self._empty_label.setText(f"无法加载市场数据：{err[:60]}")
        self._content_stack.setCurrentIndex(1)

    def _set_loading(self, loading: bool):
        """设置加载状态"""
        if loading:
            self._status_label.setText("加载中…")
            self._status_label.setStyleSheet(
                f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
            )
        else:
            self._status_label.setText("")

    # ── 渲染 ──

    _RENDER_BATCH = 30

    def _render_plugins(self, plugins: list):
        """渲染插件列表：首屏分批防卡死，全部加载后搜索用 show/hide"""
        self._render_gen = getattr(self, "_render_gen", 0) + 1
        query = self._search_edit.text().strip().lower()
        installer = get_installer()
        filter_mode = self._current_filter

        # 缓存 tags 到插件 dict（避免重复计算）
        for p in plugins:
            if "_cached_tags" not in p:
                p["_cached_tags"] = _PluginRow._compute_tags(p)

        # ── 批量缓存已安装状态和版本号（避免每次逐个磁盘 IO） ──
        self._installed_set: set = set()
        self._version_map: dict = {}
        for p in plugins:
            name = p.get("name", "")
            if not name:
                continue
            if installer.is_installed(name):
                self._installed_set.add(name)
                ver = installer.get_installed_version(name)
                if ver:
                    self._version_map[name] = ver

        # 数据变了 → 全量重建
        data_changed = not self._all_plugins or self._all_plugins is not plugins
        if data_changed:
            self._all_plugins = plugins
            self._matched_plugins = []
            for p in plugins:
                self._matched_plugins.append(self._plugin_matches(p, query, filter_mode))
            self._clear_plugin_list()
            self._rendered_count = 0
            self._all_loaded = False
            self._render_next_batch(query)
            self._retheme()
            return

        # 全部加载完 → show/hide（零重建，极快）+ 更新高亮
        if self._all_loaded:
            any_visible = False
            for p in plugins:
                name = p.get("name", "")
                if name in self._row_map:
                    row = self._row_map[name]
                    matches, _, _, _ = self._plugin_matches(p, query, filter_mode)
                    row.setVisible(matches)
                    row.update_search_highlight(query)
                    if matches:
                        any_visible = True
            if not any_visible:
                self._empty_label.setText("没有匹配的插件" if query else "暂无可用插件")
                self._content_stack.setCurrentIndex(1)
            else:
                self._content_stack.setCurrentIndex(0)
            return

        # 还未全部加载但需要刷（筛选标签切换等）
        self._matched_plugins = []
        for p in plugins:
            self._matched_plugins.append(self._plugin_matches(p, query, filter_mode))
        self._clear_plugin_list()
        self._rendered_count = 0

        if query:
            # 搜索模式：只渲染匹配的插件，分批显示 + 加载更多按钮
            self._matching_indices = [i for i, (m, _, _, _) in enumerate(self._matched_plugins) if m]
            self._rendered_count = 0
            self._render_search_batch(query)
            visible_count = len(self._matching_indices)
            if visible_count == 0:
                self._empty_label.setText("没有匹配的插件")
                self._content_stack.setCurrentIndex(1)
            else:
                self._status_label.setText(f"找到 {visible_count} 个匹配结果")
                self._status_label.setStyleSheet(
                    f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
                )
        else:
            # 非搜索（清空搜索/切换筛选）：全量加载，后续 show/hide 快速路径
            while self._rendered_count < len(self._matched_plugins):
                self._render_next_batch(query)
            self._all_loaded = True
            self._remove_load_more_button()
            visible_count = sum(1 for m in self._matched_plugins if m[0])
            if visible_count == 0:
                self._empty_label.setText("暂无可用插件")
                self._content_stack.setCurrentIndex(1)

        self._retheme()

    def _plugin_matches(self, p: dict, query: str, filter_mode: str) -> tuple:
        """检查插件是否匹配当前搜索/筛选（使用批量缓存的安装状态）。
        Returns: (matches: bool, installed: bool, has_update: bool, local_ver: str|None)
        """
        name = p.get("name", "")

        if query:
            if query not in name.lower() and query not in (p.get("description", "")).lower():
                tags = p.get("_cached_tags", [])
                if not any(query in t.lower() for t in tags):
                    return (False, False, False, None)

        installed = name in self._installed_set
        has_update = False
        local_ver = self._version_map.get(name)
        if installed and local_ver:
            remote_ver = p.get("version", "")
            if remote_ver:
                from .data import compare_versions
                has_update = compare_versions(local_ver, remote_ver) < 0

        if filter_mode == "installed" and not installed:
            return (False, installed, has_update, local_ver)
        if filter_mode == "uninstalled" and installed:
            return (False, installed, has_update, local_ver)
        if filter_mode == "updates" and not has_update:
            return (False, installed, has_update, local_ver)

        return (True, installed, has_update, local_ver)

    def _render_next_batch(self, query: str):
        """渲染下一批 30 个插件"""
        start = self._rendered_count
        end = min(start + self._RENDER_BATCH, len(self._matched_plugins))
        self._render_batch(start, end, query)
        self._rendered_count = end

        remaining = len(self._matched_plugins) - self._rendered_count
        if remaining > 0:
            self._add_load_more_button(remaining)
        else:
            self._remove_load_more_button()
            self._all_loaded = True

    def _render_batch(self, start: int, end: int, query: str):
        """渲染 [start, end) 范围的插件行"""
        fs = self._cached_font_size
        for i in range(start, end):
            matches, installed, has_update, local_ver = self._matched_plugins[i]
            p = self._all_plugins[i]
            row = _PluginRow(
                p,
                installed,
                has_update=has_update,
                local_version=local_ver,
                parent=self._content,
                font_size=fs,
                search_query=query,
            )
            row.installRequested.connect(self._async_install)
            row.updateRequested.connect(self._async_update)
            row.setVisible(matches)
            self._content_layout.addWidget(row)
            self._row_map[p.get("name", "")] = row
        # 首屏批次补 stretch
        if start == 0:
            self._content_layout.addStretch(1)

    def _render_search_batch(self, query: str):
        """搜索模式：从匹配索引列表中渲染下一批"""
        start = self._rendered_count
        end = min(start + self._RENDER_BATCH, len(self._matching_indices))
        fs = self._cached_font_size
        for idx in range(start, end):
            i = self._matching_indices[idx]
            _, installed, has_update, local_ver = self._matched_plugins[i]
            p = self._all_plugins[i]
            row = _PluginRow(
                p, installed, has_update=has_update, local_version=local_ver,
                parent=self._content, font_size=fs, search_query=query,
            )
            row.installRequested.connect(self._async_install)
            row.updateRequested.connect(self._async_update)
            row.setVisible(True)
            self._content_layout.addWidget(row)
            self._row_map[p.get("name", "")] = row
        self._rendered_count = end
        remaining = len(self._matching_indices) - self._rendered_count
        if remaining > 0:
            self._add_load_more_button(remaining)
        else:
            self._remove_load_more_button()
            # 搜索全部加载完 → 标记全量，后续 show/hide
            if not self._all_loaded:
                self._all_loaded = True

    def _add_load_more_button(self, remaining: int):
        """在列表底部添加「加载更多」按钮（stretch 之前）"""
        self._remove_load_more_button()
        btn = PushButton(f"加载更多 ({remaining} 个)", self._content)
        btn.setStyleSheet(
            "PushButton { background: rgba(128,128,128,0.1); border-radius: 6px; padding: 6px; }"
            "PushButton:hover { background: rgba(128,128,128,0.2); }"
        )
        btn.clicked.connect(self._on_load_more)
        count = self._content_layout.count()
        last_item = self._content_layout.itemAt(count - 1) if count > 0 else None
        if last_item and last_item.widget() is None:
            self._content_layout.insertWidget(count - 1, btn)
        else:
            self._content_layout.addWidget(btn)

    def _remove_load_more_button(self):
        """移除现有的「加载更多」按钮"""
        for i in range(self._content_layout.count() - 1, -1, -1):
            item = self._content_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, PushButton) and "更多" in (w.text() or ""):
                self._content_layout.takeAt(i)
                w.deleteLater()
                return

    def _on_load_more(self):
        """手动加载下一批（搜索模式用 _matching_indices，普通模式用全索引）"""
        self._remove_load_more_button()
        query = self._search_edit.text().strip().lower()
        if query and hasattr(self, "_matching_indices") and self._rendered_count < len(self._matching_indices):
            self._render_search_batch(query)
        else:
            self._render_next_batch(query)
        self._retheme()

    def _clear_plugin_list(self):
        """清空插件列表"""
        self._row_map.clear()
        self._all_loaded = False
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_search_text_changed(self):
        """搜索文本变化 → 防抖后触发过滤"""
        self._search_debounce.start(300)

    def _filter_plugins(self):
        """搜索过滤（复用已有数据）"""
        self._render_plugins(self._plugin_data)

    # ── 异步安装 ──

    def _async_install(self, plugin_meta: dict):
        """在后台线程安装插件"""
        self._status_label.setText("安装中…")
        self._status_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        name = plugin_meta.get("name", "")

        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda m=plugin_meta: get_installer().install(m))
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ok: self._on_install_done(name, bool(ok)))
        self._worker.error.connect(lambda e: self._on_install_error(name, e))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_install_done(self, name: str, success: bool):
        """安装完成"""
        self._status_label.setText("")
        if success:
            self._update_row_state(name, installed=True)
            InfoBar.success(f"{name} 安装成功", "", duration=2000, parent=self)
        else:
            self._update_row_state(name, installed=False, error=True)
            InfoBar.error(f"{name} 安装失败", "请检查网络或插件源", duration=3000, parent=self)

    def _on_install_error(self, name: str, err: str):
        """安装出错"""
        self._status_label.setText("安装失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._update_row_state(name, installed=False, error=True)
        # 提取简洁错误信息
        import re as _re

        msg = err
        m = _re.search(r"Command\s*'\[.*?\]'\s*returned.*", err)
        if m:
            msg = m.group(0)[:120]
        elif len(err) > 120:
            msg = err[:120] + "..."
        InfoBar.error(f"{name} 安装失败", msg, duration=5000, parent=self)

    # ── 异步更新 ────────────────────────────────────────

    def _async_update(self, plugin_meta: dict):
        """在后台线程更新插件"""
        name = plugin_meta.get("name", "")
        self._status_label.setText("更新中…")
        self._status_label.setStyleSheet("color: #FFA726; font-size: 12px; background: transparent;")

        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda m=plugin_meta: get_installer().update(m))
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ok: self._on_update_done(name, bool(ok)))
        self._worker.error.connect(lambda e: self._on_update_error(name, e))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_update_done(self, name: str, success: bool):
        """更新完成"""
        self._status_label.setText("")
        if success:
            self._render_plugins(self._plugin_data)
            InfoBar.success(f"{name} 更新成功", "", duration=2000, parent=self)
        else:
            self._update_row_state(name, installed=True, error=True)
            InfoBar.error(f"{name} 更新失败", "请检查网络或插件源", duration=3000, parent=self)

    def _on_update_error(self, name: str, err: str):
        """更新出错"""
        self._status_label.setText("更新失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._update_row_state(name, installed=True, error=True)
        import re as _re

        msg = err
        m = _re.search(r"Command\s*'\[.*?\]'\s*returned.*", err)
        if m:
            msg = m.group(0)[:120]
        elif len(err) > 120:
            msg = err[:120] + "..."
        InfoBar.error(f"{name} 更新失败", msg, duration=5000, parent=self)

    def _update_row_state(self, name: str, installed: bool, error: bool = False, updated: bool = False):
        """更新某插件行的状态

        Args:
            name: 插件名称
            installed: 是否已安装
            error: 操作是否出错
            updated: 是否刚完成更新（需刷新版本显示）
        """
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), _PluginRow):
                row: _PluginRow = item.widget()
                if row._meta.get("name") == name:
                    if updated:
                        # 更新成功后：设为已安装，清除更新标记
                        row.set_installed(True)
                    elif error and not installed:
                        row.set_error()
                    elif error and installed:
                        # 更新失败：恢复按钮（保留更新标记）
                        row._busy = False
                        row._update_btn_text()
                    else:
                        row.set_installed(installed)
                    break

    # ── 标签切换 ──

    def _on_tab_changed(self, key: str):
        """标签切换"""
        if key == "browse":
            self._page_stack.setCurrentIndex(0)
        elif key == "markets":
            self._build_markets_page()
            self._page_stack.setCurrentIndex(1)

    def _on_filter_changed(self, key: str):
        """筛选标签切换"""
        self._current_filter = key
        if self._plugin_data:
            self._render_plugins(self._plugin_data)

    # ── 市场管理 ──

    def _build_markets_page(self):
        """构建市场管理页面"""
        # 清空旧内容
        while self._markets_content_layout.count():
            item = self._markets_content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        mgr = get_marketplace_manager()
        for src in mgr.get_sources():
            row = self._create_market_row(src)
            self._markets_content_layout.addWidget(row)

        self._markets_content_layout.addStretch()

    def _create_market_row(self, src_def: dict) -> QWidget:
        """创建单个市场源的行组件"""
        row = QWidget(self._markets_content)
        row.setStyleSheet("background: rgba(128,128,128,0.08); border-radius: 8px;")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(8)

        # 名称 + 来源
        info = QVBoxLayout()
        info.setSpacing(2)

        name_text = src_def["name"]
        if src_def.get("builtin"):
            name_text += " (内置)"
        name_label = QLabel(name_text, row)
        name_label.setStyleSheet(
            f"color: {_text_color()}; font-weight: bold; font-size: 18px; background: transparent;"
        )
        info.addWidget(name_label)

        src = src_def.get("source", {})
        src_type = src.get("source", "url")
        src_text = src.get("repo", src.get("url", "unknown"))
        if len(src_text) > 60:
            src_text = src_text[:57] + "..."
        url_label = QLabel(src_text, row)
        url_label.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 14px; background: transparent;")
        info.addWidget(url_label)

        h.addLayout(info, 1)

        # 打开网页按钮
        link_url = ""
        if src_type == "github":
            link_url = f"https://github.com/{src.get('repo', '')}"
        elif src_type == "url":
            u = src.get("url", "")
            # raw URL 转成网页 URL
            if "raw.githubusercontent.com" in u:
                parts = u.replace("https://raw.githubusercontent.com/", "").split("/")
                if len(parts) >= 3:
                    link_url = f"https://github.com/{parts[0]}/{parts[1]}"
            else:
                link_url = u.replace(".git", "")

        if link_url:
            link_btn = TransparentToolButton(FluentIcon.LINK, row)
            link_btn.setFixedSize(28, 28)
            link_btn.setToolTip(f"打开 {link_url}")
            link_btn.clicked.connect(lambda checked, u=link_url: self._open_url(u))
            h.addWidget(link_btn)

        # 删除按钮（内置市场不可删）
        if not src_def.get("builtin"):
            del_btn = TransparentToolButton(FluentIcon.DELETE, row)
            del_btn.setFixedSize(28, 28)
            del_btn.setToolTip("移除市场")
            del_btn.clicked.connect(lambda checked, n=src_def["name"]: self._on_remove_marketplace(n))
            h.addWidget(del_btn)

        return row

    def _on_add_marketplace(self):
        """添加市场源"""
        text = self._market_url_edit.text().strip()
        if not text:
            return

        mgr = get_marketplace_manager()

        # 判断类型
        if text.startswith(("http://", "https://")):
            # GitHub repo URL → github 类型
            m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", text)
            if m:
                source = {"source": "github", "repo": m.group(1)}
            elif "raw.githubusercontent.com" in text:
                # raw URL 直接当 url 类型
                source = {"source": "url", "url": text}
            elif text.endswith(".json"):
                source = {"source": "url", "url": text}
            else:
                # 其他 URL，尝试追加 marketplace.json
                source = {"source": "url", "url": text.rstrip("/") + "/.claude-plugin/marketplace.json"}
        elif "/" in text and " " not in text:
            parts = text.split("/")
            if len(parts) == 2:
                source = {"source": "github", "repo": text}
            else:
                source = {"source": "url", "url": text}
        else:
            source = {"source": "url", "url": text}

        # 名称取最后 / 后的部分
        market_name = text.rstrip("/").split("/")[-1].replace(".git", "").replace(".json", "")
        if not market_name:
            market_name = text

        # 已存在则提示，不重复添加
        existing = {s["name"] for s in mgr.get_sources()}
        if market_name in existing:
            self._status_label.setText(f"{market_name} 已存在")
            return

        mgr.add_source(market_name, source, auto_update=False)
        self._market_url_edit.clear()
        self._build_markets_page()
        self._status_label.setText(f"已添加 {market_name}")
        self._status_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )

    def _on_remove_marketplace(self, name: str):
        """移除市场源"""
        mgr = get_marketplace_manager()
        mgr.remove_source(name)
        self._build_markets_page()

    def _open_url(self, url: str):
        """在浏览器中打开 URL"""
        import webbrowser

        webbrowser.open(url)

    # ── 清理 ──

    def _on_close(self):
        """关闭卡片"""
        self.setVisible(False)
        self.closed.emit()

    def _on_refresh(self):
        """强制刷新所有市场"""
        self._status_label.setText("刷新中…")
        self._async_refresh(force=True)

    def _cleanup_worker(self):
        """安全清理旧的 worker/thread"""
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()
