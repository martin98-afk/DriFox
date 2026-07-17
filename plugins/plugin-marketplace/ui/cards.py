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
import traceback
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    FluentLabelBase,
    IconWidget,
    LineEdit,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    TransparentToolButton,
    isDarkTheme,
)

from .data import get_marketplace
from .installer import get_installer
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
    ):
        super().__init__(parent)
        self._meta = plugin_meta
        self._installed = installed
        self._has_update = has_update
        self._local_version = local_version
        self._busy = False
        self._original_btn_style: str = ""  # 在 _setup_ui 中保存 FluentUI 默认样式
        self._font_size = font_size  # 上下文字体大小（用于头像自适应）
        self._avatar = None
        self._setup_ui()

    def _setup_ui(self):
        self.setObjectName("pluginRow")
        # 透明背景 + 悬停微亮
        self.setStyleSheet(
            "#pluginRow { background: transparent; border: 1px solid rgba(128,128,128,0.15); border-radius: 8px; padding: 0px; }"
            "#pluginRow:hover { background: rgba(128,128,128,0.05); }"
        )
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

        # 标题行：显示名称 + 版本信息（字号 = font_size - 2，由 _retheme 通过 objectName 识别）
        if self._has_update and self._local_version and remote_ver:
            # 有新版：显示 v旧版 → v新版
            title = StrongBodyLabel(f"{name}  v{self._local_version} → v{remote_ver}", self)
        elif remote_ver:
            title = StrongBodyLabel(f"{name}  v{remote_ver}", self)
        else:
            title = StrongBodyLabel(name, self)
        title.setObjectName("pluginRowTitle")
        title.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        info_layout.addWidget(title)

        # 版本更新提示小标签（仅在有更新时显示）
        if self._has_update:
            update_tag = QLabel("🔄 有新版本", self)
            update_tag.setStyleSheet("color: #FFA726; font-size: 11px; background: transparent;")
            info_layout.addWidget(update_tag)

        desc = self._meta.get("description", "")
        if desc:
            desc_label = QLabel(desc[:120], self)
            desc_label.setWordWrap(True)
            # objectName "pluginRowDesc" → _retheme 强制使用 font_size - 4
            desc_label.setObjectName("pluginRowDesc")
            desc_label.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")
            info_layout.addWidget(desc_label)

        layout.addLayout(info_layout, 1)

        # 操作按钮
        self._btn = PushButton(self)
        self._btn.setFixedWidth(100)
        # 保存 FluentUI 默认样式，切换状态时恢复（仅「更新」状态使用自定义橙色样式）
        self._original_btn_style = self._btn.styleSheet()
        self._update_btn_text()
        self._btn.clicked.connect(self._on_click)
        layout.addWidget(self._btn)

    def _update_btn_text(self):
        if self._busy:
            self._btn.setText("处理中…")
            self._btn.setEnabled(False)
            self._btn.setStyleSheet(self._original_btn_style)
        elif self._has_update:
            self._btn.setText("更新")
            self._btn.setEnabled(True)
            # 橙色按钮风格 — 提示有新版本可更新
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
        from pathlib import Path

        dev = Path(__file__).resolve().parent.parent.parent.parent / "plugins" / name
        if dev.is_dir():
            return dev
        for base in (Path.home() / ".drifox" / "plugins", Path.home() / ".drifox" / "plugins-disabled"):
            p = base / name
            if p.is_dir():
                return p
        return None

    def set_font_size(self, font_size: int):
        """根据上下文字体大小动态调整头像尺寸"""
        self._font_size = font_size
        if self._avatar is not None and hasattr(self._avatar, "set_font_size"):
            self._avatar.set_font_size(font_size)


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
        self._header_icon: Optional[IconWidget] = None
        self._setup_ui()
        # 首次显示时由 show_card 触发加载，__init__ 不再自动加载

    # ── 拉模型上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）"""
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：用最新上下文刷新主题色 + 加载数据"""
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._async_refresh()
        self.setVisible(True)

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

    # objectName → font-size 偏移（用于插件行的标题/描述）
    # pluginRowTitle: 标题用 font_size - 2（让标题比上下文默认小 2 号）
    # pluginRowDesc:  描述用 font_size - 4（再小 2 号，作为辅助文字）
    _PLUGIN_ROW_SIZE_OFFSETS = {
        "pluginRowTitle": -2,
        "pluginRowDesc": -4,
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
        # 半透明背景
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("MarketplaceCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部 ──
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

        header_layout.addStretch(1)

        # 状态标签（加载中/安装中标记）
        self._status_label = QLabel("", header)
        self._status_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        header_layout.addWidget(self._status_label)

        # 搜索框
        self._search_edit = LineEdit(header)
        self._search_edit.setPlaceholderText("搜索插件…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setFixedWidth(160)
        self._search_edit.setStyleSheet(
            f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {_text_color()};"
        )
        self._search_edit.textChanged.connect(self._filter_plugins)
        header_layout.addWidget(self._search_edit)

        # 注意：没有刷新按钮，每次 show_card 自动强制拉取最新数据

        # 关闭按钮
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

        # ── 内容区（滚动列表 + 居中空状态用 QStackedWidget）──
        from PyQt5.QtWidgets import QStackedWidget

        self._content_stack = QStackedWidget(self)
        self._content_stack.setStyleSheet("background: transparent;")

        # 页面 0：滚动列表
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
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

        # 页面 1：居中空状态
        self._empty_label = StrongBodyLabel("暂无可用插件", self._content_stack)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent;")
        self._content_stack.addWidget(self._empty_label)

        self._content_stack.setCurrentIndex(0)  # 默认显示滚动列表
        root.addWidget(self._content_stack, 1)

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

    def _async_refresh(self):
        """在后台线程拉取市场数据"""
        self._set_loading(True)
        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda: get_marketplace().list_plugins(force=True))
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

    def _render_plugins(self, plugins: list):
        """渲染插件列表（含版本检测）"""
        self._clear_plugin_list()
        query = self._search_edit.text().strip().lower()
        installer = get_installer()
        count = 0
        for p in plugins:
            name = p.get("name", "")
            if query and query not in name.lower() and query not in (p.get("description", "")).lower():
                continue
            installed = installer.is_installed(name)
            # 检查是否有版本更新
            has_update = False
            local_ver = None
            if installed:
                has_update, local_ver, _ = installer.check_update(p)
            row = _PluginRow(
                p,
                installed,
                has_update=has_update,
                local_version=local_ver,
                parent=self._content,
                font_size=self._cached_font_size,
            )
            row.installRequested.connect(self._async_install)
            row.updateRequested.connect(self._async_update)
            self._content_layout.addWidget(row)
            count += 1

        if count == 0:
            self._empty_label.setText("没有匹配的插件" if query else "暂无可用插件")
            self._content_stack.setCurrentIndex(1)
        else:
            self._content_stack.setCurrentIndex(0)

        # 对动态创建的子控件应用主题
        self._retheme()

    def _clear_plugin_list(self):
        """清空插件列表"""
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

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
        else:
            self._update_row_state(name, installed=False, error=True)

    def _on_install_error(self, name: str, err: str):
        """安装出错"""
        self._status_label.setText("安装失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._update_row_state(name, installed=False, error=True)

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
        """更新完成 — 刷新列表确保版本信息正确"""
        self._status_label.setText("")
        if success:
            # 重新渲染整个列表以刷新版本显示（v旧版→v新版 → 变为已安装）
            self._render_plugins(self._plugin_data)
        else:
            self._update_row_state(name, installed=True, error=True)

    def _on_update_error(self, name: str, err: str):
        """更新出错"""
        self._status_label.setText("更新失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._update_row_state(name, installed=True, error=True)

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

    # ── 清理 ──

    def _on_close(self):
        """关闭卡片"""
        self.setVisible(False)
        self.closed.emit()

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
