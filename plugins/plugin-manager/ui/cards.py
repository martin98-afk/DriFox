# -*- coding: utf-8 -*-
"""PluginManagerCard 浮动卡片 — 管理已安装插件的启用/禁用/卸载

功能：
- 列出所有已安装插件（系统 + 用户）
- 启用已禁用的插件
- 禁用已启用的用户插件
- 卸载用户插件
- 搜索过滤
- 全部操作异步执行，不阻塞 UI

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 shutil/stdlib 完成
- 基于插件目录的文件结构推理状态
"""

import json
import re
import shutil
import traceback
from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
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
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger

from ._squircle_avatar import SquircleAvatar, PluginIconWidget, extract_initials, name_color


# ── 路径常量 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DRIFOX = _PROJECT_ROOT / ".drifox"
_USER_DRIFOX = Path.home() / ".drifox"


def _drifox_dir() -> Path:
    """查找 .drifox 目录（开发环境优先，兜底用户目录）"""
    if _DEV_DRIFOX.exists():
        return _DEV_DRIFOX
    return _USER_DRIFOX


def _plugins_root() -> Path:
    """项目中的顶级 plugins/ 目录"""
    return _PROJECT_ROOT / "plugins"


# ── 主题色 ──────────────────────────────────────────────


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


# ── 插件发现 ──────────────────────────────────────────────


def _read_plugin_json(plugin_dir: Path) -> Optional[dict]:
    """读取插件目录下的 plugin.json"""
    for meta_dir in (".drifox-plugin", ".claude-plugin"):
        p = plugin_dir / meta_dir / "plugin.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


class PluginInfo:
    """单个插件的运行时信息"""

    __slots__ = ("name", "description", "version", "status", "path")

    def __init__(
        self,
        name: str,
        description: str = "",
        version: str = "",
        status: str = "enabled",
        path: Optional[Path] = None,
    ):
        self.name = name
        self.description = description
        self.version = version
        self.status = status  # "system" | "enabled" | "disabled"
        self.path = path

    @property
    def is_system(self) -> bool:
        return self.status == "system"

    @property
    def is_disabled(self) -> bool:
        return self.status == "disabled"

    @property
    def is_enabled(self) -> bool:
        return self.status == "enabled"

    def status_label(self) -> str:
        return {
            "enabled": "✅ 启用",
            "disabled": "⛔ 禁用",
            "system": "🔒 系统",
        }.get(self.status, self.status)


def _discover_plugins() -> list[PluginInfo]:
    """扫描所有目录，收集已安装插件"""
    seen: dict[str, PluginInfo] = {}

    # 1. 系统插件（插件根目录 plugins/ 下）
    plugins_root = _plugins_root()
    if plugins_root.exists():
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            meta = _read_plugin_json(child)
            if meta is None:
                continue
            name = meta.get("name", child.name)
            if name not in seen:
                seen[name] = PluginInfo(
                    name=name,
                    description=meta.get("description", ""),
                    version=meta.get("version", ""),
                    status="system",
                    path=child,
                )

    # 2. 用户已启用插件
    _user_plugins = _drifox_dir() / "plugins"
    if _user_plugins.exists():
        for child in sorted(_user_plugins.iterdir()):
            if not child.is_dir():
                continue
            meta = _read_plugin_json(child)
            if meta is None:
                continue
            name = meta.get("name", child.name)
            if name in seen:
                continue
            seen[name] = PluginInfo(
                name=name,
                description=meta.get("description", ""),
                version=meta.get("version", ""),
                status="enabled",
                path=child,
            )

    # 3. 用户已禁用插件
    _user_disabled = _drifox_dir() / "plugins-disabled"
    if _user_disabled.exists():
        for child in sorted(_user_disabled.iterdir()):
            if not child.is_dir():
                continue
            meta = _read_plugin_json(child)
            if meta is None:
                continue
            name = meta.get("name", child.name)
            if name in seen:
                continue
            seen[name] = PluginInfo(
                name=name,
                description=meta.get("description", ""),
                version=meta.get("version", ""),
                status="disabled",
                path=child,
            )

    return sorted(seen.values(), key=lambda p: (0 if p.is_system else 1, p.name))


# ── 文件操作 ──────────────────────────────────────────────


def _do_enable(plugin: PluginInfo) -> bool:
    """启用已禁用的插件"""
    drifox_dir = _drifox_dir()
    src = drifox_dir / "plugins-disabled" / plugin.name
    if not src.exists():
        return False
    try:
        (drifox_dir / "plugins").mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(drifox_dir / "plugins" / plugin.name))
        return True
    except Exception as e:
        logger.error(f"[PluginManager] 启用失败 {plugin.name}: {e}")
        return False


def _do_disable(plugin: PluginInfo) -> bool:
    """禁用已启用的用户插件"""
    drifox_dir = _drifox_dir()
    src = drifox_dir / "plugins" / plugin.name
    if not src.exists():
        return False
    try:
        (drifox_dir / "plugins-disabled").mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(drifox_dir / "plugins-disabled" / plugin.name))
        return True
    except Exception as e:
        logger.error(f"[PluginManager] 禁用失败 {plugin.name}: {e}")
        return False


def _do_uninstall(plugin: PluginInfo) -> bool:
    """卸载用户插件"""
    drifox_dir = _drifox_dir()
    for d in (drifox_dir / "plugins" / plugin.name, drifox_dir / "plugins-disabled" / plugin.name):
        if d.exists():
            try:
                shutil.rmtree(d)
            except Exception as e:
                logger.error(f"[PluginManager] 卸载失败 {plugin.name}: {e}")
                return False
    return True


# ── 异步工作器 ──────────────────────────────────────────


class _Worker(QObject):
    """后台执行阻塞操作"""

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


# ── 插件行组件 ──────────────────────────────────────────


class _PluginRow(QFrame):
    """单个插件的展示行"""

    actionRequested = pyqtSignal(str, object)  # action, PluginInfo

    def __init__(self, plugin: PluginInfo, parent=None, font_size: int = 0):
        super().__init__(parent)
        self._plugin = plugin
        self._busy = False
        self._font_size = font_size  # 上下文字体大小（用于头像自适应）
        self._avatar = None
        self._setup_ui()

    # ── 界面 ──

    def _setup_ui(self):
        self.setObjectName("pluginRow")
        self.setStyleSheet(
            "#pluginRow { background: transparent;"
            " border: 1px solid rgba(128,128,128,0.15);"
            " border-radius: 8px; padding: 0px; }"
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

        title_w = QWidget(self)
        title_w.setStyleSheet("background: transparent;")
        title_ly = QHBoxLayout(title_w)
        title_ly.setContentsMargins(0, 0, 0, 0)
        title_ly.setSpacing(6)

        ver = self._plugin.version
        title_str = f"{self._plugin.name}  v{ver}" if ver else self._plugin.name
        title_lb = StrongBodyLabel(title_str, title_w)
        # objectName "pluginRowTitle" → _retheme 使用 font_size - 2
        title_lb.setObjectName("pluginRowTitle")
        title_lb.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        title_ly.addWidget(title_lb)

        # 状态标签
        status_tag = QLabel(self._plugin.status_label(), title_w)
        status_tag.setStyleSheet(
            f"color: {self._status_color()}; font-size: 11px; font-weight: bold; background: transparent;"
        )
        title_ly.addWidget(status_tag)
        title_ly.addStretch(1)
        info_layout.addWidget(title_w)

        desc = self._plugin.description
        if desc:
            dl = QLabel(desc[:160], self)
            dl.setWordWrap(True)
            # objectName "pluginRowDesc" → _retheme 强制使用 font_size - 4
            dl.setObjectName("pluginRowDesc")
            dl.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")
            info_layout.addWidget(dl)

        type_lb = QLabel("系统插件" if self._plugin.is_system else "用户插件", self)
        type_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 11px; background: transparent;")
        info_layout.addWidget(type_lb)

        layout.addLayout(info_layout, 1)

        # 操作按钮
        self._btn_layout = QHBoxLayout()
        self._btn_layout.setSpacing(4)
        layout.addLayout(self._btn_layout)
        self._build_buttons()

    def _status_color(self) -> str:
        if self._plugin.status == "enabled":
            return "#4CAF50"
        elif self._plugin.status == "disabled":
            return "#FF9800"
        return "#2196F3"

    # ── 按钮构建 ──

    def _build_buttons(self):
        while self._btn_layout.count():
            item = self._btn_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._busy:
            lbl = QLabel("处理中…", self)
            lbl.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px;")
            self._btn_layout.addWidget(lbl)
            return

        if self._plugin.is_system:
            lbl = QLabel("🔒", self)
            lbl.setToolTip("系统插件只读")
            self._btn_layout.addWidget(lbl)
            return

        if self._plugin.is_disabled:
            self._add_action_btn("启用", "#4CAF50", self._on_enable)
            self._add_action_btn("卸载", "#F44336", self._on_uninstall)
        else:
            self._add_action_btn("禁用", "#FF9800", self._on_disable)
            self._add_action_btn("卸载", "#F44336", self._on_uninstall)

    def _add_action_btn(self, text: str, color: str, slot):
        btn = PushButton(text, self)
        btn.setFixedWidth(60)
        btn.setStyleSheet(
            f"PushButton {{ color: {color}; border: 1px solid {color};"
            f" border-radius: 4px; padding: 4px 8px; font-size: 12px; background: transparent; }}"
            f"PushButton:hover {{ background: rgba({','.join(str(int(color[i : i + 2], 16)) for i in (1, 3, 5))},0.1); }}"
        )
        btn.clicked.connect(slot)
        self._btn_layout.addWidget(btn)

    # ── 操作触发 ──

    def _on_enable(self):
        self._set_busy(True)
        self.actionRequested.emit("enable", self._plugin)

    def _on_disable(self):
        self._set_busy(True)
        self.actionRequested.emit("disable", self._plugin)

    def _on_uninstall(self):
        reply = QMessageBox.question(
            self,
            "确认卸载",
            f"确定要卸载插件「{self._plugin.name}」吗？\n此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._set_busy(True)
            self.actionRequested.emit("uninstall", self._plugin)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._build_buttons()

    def refresh_state(self, plugin: PluginInfo):
        self._plugin = plugin
        self._busy = False
        self._build_buttons()

    def _create_icon_widget(self) -> QWidget:
        """创建插件图标组件：SVG icon 优先，无图标则用缩写头像"""
        plugin = self._plugin
        if hasattr(plugin, "path") and plugin.path:
            import json as _json

            for _meta_dir in (".drifox-plugin", ".claude-plugin"):
                _mp = plugin.path / _meta_dir / "plugin.json"
                if _mp.exists():
                    try:
                        _m = _json.loads(_mp.read_text(encoding="utf-8"))
                        return PluginIconWidget(
                            plugin_dir=plugin.path,
                            manifest=_m,
                            font_size=self._font_size,
                            parent=self,
                        )
                    except Exception:
                        pass
                    break
        # Fallback to initials avatar
        return SquircleAvatar(
            extract_initials(plugin.name or "?"),
            name_color(plugin.name or "?"),
            self,
            font_size=self._font_size,
        )

    def set_font_size(self, font_size: int):
        """根据上下文字体大小动态调整头像尺寸"""
        self._font_size = font_size
        if self._avatar is not None and hasattr(self._avatar, "set_font_size"):
            self._avatar.set_font_size(font_size)


# ── 主卡片 ──────────────────────────────────────────────


class PluginManagerCard(QWidget):
    """插件管理器浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        self._plugins: list[PluginInfo] = []
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
            self._search.setStyleSheet(
                f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {tc};"
            )
        except RuntimeError:
            pass

        # 更新分隔线
        try:
            for sep in self.findChildren(QFrame):
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

    # ── 界面 ──

    def _setup_ui(self):
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("PluginManagerCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 头部
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        ic = IconWidget(FluentIcon.ALIGNMENT, header)
        ic.setFixedSize(22, 22)
        hly.addWidget(ic)
        self._header_icon = ic

        tl = StrongBodyLabel("插件管理", header)
        tl.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(tl)

        self._count_lb = QLabel("", header)
        self._count_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")
        hly.addWidget(self._count_lb)
        hly.addStretch(1)

        self._search = LineEdit(header)
        self._search.setPlaceholderText("搜索插件…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(160)
        self._search.setStyleSheet(
            f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {_text_color()};"
        )
        self._search.textChanged.connect(self._filter_plugins)
        hly.addWidget(self._search)

        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._async_refresh)
        hly.addWidget(self._refresh_btn)

        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

        # 分隔线
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # 滚动内容
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
        root.addWidget(self._scroll, 1)

        # 空状态
        self._empty = StrongBodyLabel("", self)
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent;")
        self._empty.setVisible(False)
        root.addWidget(self._empty)

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
        self._set_loading(True)
        self._cleanup_worker()
        w = _Worker(_discover_plugins)
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(self._on_refresh_done)
        w.error.connect(self._on_refresh_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._worker, self._worker_thread = w, t
        t.start()

    def _on_refresh_done(self, plugins: list[PluginInfo]):
        self._plugins = plugins or []
        self._set_loading(False)
        total = len(self._plugins)
        sc = sum(1 for p in self._plugins if p.is_system)
        self._count_lb.setText(f"共 {total} 个")
        self._render_plugins(self._plugins)

    def _on_refresh_error(self, err: str):
        self._set_loading(False)
        self._empty.setText(f"扫描失败：{err[:60]}")
        self._empty.setVisible(True)

    def _set_loading(self, loading: bool):
        self._refresh_btn.setEnabled(not loading)
        if loading:
            self._count_lb.setText("扫描中…")

    def _render_plugins(self, plugins: list[PluginInfo]):
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        query = self._search.text().strip().lower()
        count = 0
        for p in plugins:
            if query and query not in p.name.lower() and query not in p.description.lower():
                continue
            row = _PluginRow(p, self._content, font_size=self._cached_font_size)
            row.actionRequested.connect(self._on_action_requested)
            self._content_layout.addWidget(row)
            count += 1

        self._empty.setText("没有匹配的插件" if query else "暂无已安装的插件")
        self._empty.setVisible(count == 0)

        # 对动态创建的子控件应用主题
        self._retheme()

    def _filter_plugins(self):
        self._render_plugins(self._plugins)

    # ── 管理操作 ──

    def _on_action_requested(self, action: str, plugin: PluginInfo):
        names = {"enable": "启用中", "disable": "禁用中", "uninstall": "卸载中"}
        self._count_lb.setText(names.get(action, "处理中…"))

        fn_map = {
            "enable": lambda p=plugin: _do_enable(p),
            "disable": lambda p=plugin: _do_disable(p),
            "uninstall": lambda p=plugin: _do_uninstall(p),
        }
        fn = fn_map.get(action)
        if fn is None:
            return

        self._cleanup_worker()
        w = _Worker(fn)
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)

        def _done(ok):
            ok_bool = bool(ok)
            if ok_bool:
                self._async_refresh()
            else:
                self._count_lb.setText(f"{action} 失败")
                self._count_lb.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(3000, self._restore_count)
                # 刷新恢复按钮状态
                self._async_refresh()

        w.finished.connect(_done)
        w.error.connect(lambda e: _done(False))
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._worker, self._worker_thread = w, t
        t.start()

    def _restore_count(self):
        total = len(self._plugins)
        sc = sum(1 for p in self._plugins if p.is_system)
        self._count_lb.setText(f"共 {total} 个")
        self._count_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")

    # ── 清理 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _cleanup_worker(self):
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
