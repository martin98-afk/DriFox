# -*- coding: utf-8 -*-
"""ShortcutManager 浮动卡片 — 管理系统命令的快捷键

功能：
- 显示系统内建命令及 UI 插件命令
- 自定义快捷键：覆盖同名命令到 user-custom 插件
- 恢复系统配置：删除 user-custom 下的对应文件
- 实时搜索过滤

设计约束（闭包）：
- 不导入 app.widgets 内部的任何模块
- 直接文件操作（创建/删除 user-custom 命令文件）
"""

from pathlib import Path
from typing import Callable, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QKeySequence
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    ScrollArea,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger


# ── 常量 ────────────────────────────────────────────────

_USER_CUSTOM_CMD_REL = "plugins/user-custom/commands"

# 主题色定义
_LIGHT = {
    "card_bg": "rgba(255,255,255,0.92)",
    "card_border": "rgba(0,0,0,0.06)",
    "row_hover": "rgba(0,0,0,0.03)",
    "row_active": "rgba(0,0,0,0.06)",
    "text_primary": "rgba(0,0,0,0.82)",
    "text_secondary": "rgba(0,0,0,0.45)",
    "text_muted": "rgba(0,0,0,0.28)",
    "shortcut_bg": "rgba(0,0,0,0.04)",
    "shortcut_border": "rgba(0,0,0,0.08)",
    "badge_bg": "rgba(0,0,0,0.05)",
    "badge_text": "rgba(0,0,0,0.50)",
    "badge_plugin_bg": "rgba(66,133,244,0.08)",
    "badge_plugin_text": "rgba(66,133,244,0.72)",
    "search_bg": "rgba(0,0,0,0.03)",
    "search_border": "rgba(0,0,0,0.06)",
    "search_focus_border": "rgba(66,133,244,0.35)",
    "sep_color": "rgba(0,0,0,0.05)",
    "tip_text": "rgba(0,0,0,0.38)",
    "empty_text": "rgba(0,0,0,0.30)",
}

_DARK = {
    "card_bg": "rgba(30,30,40,0.94)",
    "card_border": "rgba(255,255,255,0.06)",
    "row_hover": "rgba(255,255,255,0.04)",
    "row_active": "rgba(255,255,255,0.07)",
    "text_primary": "rgba(255,255,255,0.88)",
    "text_secondary": "rgba(255,255,255,0.50)",
    "text_muted": "rgba(255,255,255,0.25)",
    "shortcut_bg": "rgba(255,255,255,0.06)",
    "shortcut_border": "rgba(255,255,255,0.08)",
    "badge_bg": "rgba(255,255,255,0.06)",
    "badge_text": "rgba(255,255,255,0.45)",
    "badge_plugin_bg": "rgba(66,133,244,0.12)",
    "badge_plugin_text": "rgba(130,170,255,0.80)",
    "search_bg": "rgba(255,255,255,0.04)",
    "search_border": "rgba(255,255,255,0.06)",
    "search_focus_border": "rgba(100,140,255,0.35)",
    "sep_color": "rgba(255,255,255,0.05)",
    "tip_text": "rgba(255,255,255,0.32)",
    "empty_text": "rgba(255,255,255,0.22)",
}


def _theme() -> dict:
    return _DARK if isDarkTheme() else _LIGHT


def _ctx_font(ctx: dict) -> tuple:
    ff = ctx.get("font_family", "Microsoft YaHei")
    fs = ctx.get("font_size", 14)
    return ff, fs


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    t = _theme()
    return t["text_secondary"] if secondary else t["text_primary"]


def _ctx_border_color(ctx: dict) -> str:
    return ctx.get("colors", {}).get("border", _theme()["card_border"])


# ── 工具函数 ────────────────────────────────────────────


def _get_user_custom_cmd_dir() -> Path:
    from app.utils.utils import get_app_data_dir
    return get_app_data_dir() / _USER_CUSTOM_CMD_REL


def _make_minimal_cmd_file(name: str, description: str, shortcut: str) -> str:
    lines = ["---"]
    lines.append(f"description: {description}")
    lines.append("type: function")
    if shortcut:
        lines.append(f"shortcut: {shortcut}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _load_all_items() -> list:
    """获取系统内建命令 + UI 插件命令列表。

    Returns:
        [{name, description, type, shortcut?, subtype?}, ...]
    """
    from app.core.command_manager import CommandManager
    from app.core.ui_plugin_registry import UIPluginRegistry

    items = []

    cmd_mgr = CommandManager.get_instance()
    commands = cmd_mgr.get_all_commands()

    # 只保留 type="command"（系统内建 + UI 插件命令），过滤掉 agent/prompt
    ui_cmd_names = UIPluginRegistry.get_instance().get_ui_command_names()
    for cmd in commands:
        if cmd.get("type") != "command":
            continue
        if cmd["name"] in ui_cmd_names:
            cmd["subtype"] = "ui_plugin"
        items.append(cmd)

    # 排序：系统内建 → UI 插件，同组按名称字母序
    def _sort_key(item):
        is_plugin = 1 if item.get("subtype") == "ui_plugin" else 0
        return (is_plugin, item["name"])

    items.sort(key=_sort_key)
    return items


# ── 按键捕获弹出窗 ─────────────────────────────────────


class _KeyCapturePopup(QWidget):
    """弹出按键捕获窗口"""

    key_captured = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(240, 68)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        frame = QFrame(self)
        frame.setObjectName("captureFrame")
        t = _theme()
        bg = t["card_bg"]
        border = t["search_focus_border"]
        frame.setStyleSheet(f"""
            #captureFrame {{
                background: {bg};
                border: 2px solid {border};
                border-radius: 12px;
            }}
        """)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(20, 12, 20, 12)

        self._hint = QLabel("按下快捷键组合…", frame)
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setStyleSheet(
            f"color: {t['text_primary']}; font-size: 14px; font-weight: 500; background: transparent;"
        )
        fl.addWidget(self._hint)

        esc_hint = QLabel("Esc 取消", frame)
        esc_hint.setAlignment(Qt.AlignCenter)
        esc_hint.setStyleSheet(
            f"color: {t['text_muted']}; font-size: 12px; background: transparent;"
        )
        fl.addWidget(esc_hint)

        layout.addWidget(frame)

    def showEvent(self, event):
        super().showEvent(event)
        self.grabKeyboard()

    def hideEvent(self, event):
        self.releaseKeyboard()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return
        if key == Qt.Key_Escape:
            self.key_captured.emit("")
            self.close()
            return

        seq = QKeySequence(int(mods) | key)
        self.key_captured.emit(seq.toString(QKeySequence.NativeText))
        self.close()


# ── 命令行控件 ─────────────────────────────────────────


class _CommandRow(QWidget):
    """单个命令的显示行"""

    edit_clicked = pyqtSignal(str)
    restore_clicked = pyqtSignal(str)

    def __init__(
        self,
        cmd_name: str,
        shortcut: str,
        description: str,
        cmd_type_str: str,
        is_customized: bool,
        is_plugin: bool = False,
        font_size: int = 14,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._cmd_name = cmd_name
        self._shortcut = shortcut
        self._description = description
        self._cmd_type_str = cmd_type_str
        self._is_customized = is_customized
        self._is_plugin = is_plugin
        self._font_size = font_size
        self._hovered = False

        self.setMouseTracking(True)
        self._setup_ui()

    def _setup_ui(self):
        t = _theme()
        fs = self._font_size

        self.setFixedHeight(40)
        self.setStyleSheet("""
            _CommandRow {
                background: transparent;
                border-radius: 6px;
            }
        """)

        hly = QHBoxLayout(self)
        hly.setContentsMargins(12, 0, 8, 0)
        hly.setSpacing(8)

        # ── 左侧：自定义标记 + 命令名 ──
        if self._is_customized:
            star = QLabel("✦", self)
            star.setStyleSheet(
                f"color: #f5a623; font-size: {fs - 1}px; background: transparent; padding-right: 2px;"
            )
            star.setToolTip("已自定义（原系统命令被覆盖）")
            hly.addWidget(star)

        name_text = f"/{self._cmd_name}"
        name_lb = QLabel(name_text, self)
        font_style = "font-weight: 600;" if self._is_customized else ""
        name_lb.setStyleSheet(
            f"color: {t['text_primary']}; font-size: {fs - 1}px; {font_style} background: transparent;"
        )
        name_lb.setMinimumWidth(120)
        hly.addWidget(name_lb)

        # ── 类型徽章 ──
        badge_label = "插件" if self._is_plugin else "系统"
        badge_bg = t["badge_plugin_bg"] if self._is_plugin else t["badge_bg"]
        badge_text_c = t["badge_plugin_text"] if self._is_plugin else t["badge_text"]
        badge_fs = max(fs - 4, 9)

        type_lb = QLabel(badge_label, self)
        type_lb.setFixedHeight(18)
        type_lb.setStyleSheet(
            f"color: {badge_text_c}; font-size: {badge_fs}px; font-weight: 500; "
            f"background: {badge_bg}; border-radius: 4px; "
            f"padding: 0 7px;"
        )
        type_lb.setAlignment(Qt.AlignCenter)
        hly.addWidget(type_lb)

        hly.addStretch(1)

        # ── 快捷键 pill ──
        self._shortcut_lb = QLabel(self)
        self._shortcut_lb.setFixedHeight(26)
        self._shortcut_lb.setAlignment(Qt.AlignCenter)
        self._update_shortcut_style()
        hly.addWidget(self._shortcut_lb)

        # ── 操作按钮 ──
        self.edit_btn = ToolButton(FluentIcon.EDIT, self)
        self.edit_btn.setToolTip("设置自定义快捷键")
        self.edit_btn.setFixedSize(28, 28)
        self.edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._cmd_name))
        hly.addWidget(self.edit_btn)

        if self._is_customized:
            self.restore_btn = ToolButton(FluentIcon.RETURN, self)
            self.restore_btn.setToolTip("恢复系统配置")
            self.restore_btn.setFixedSize(28, 28)
            self.restore_btn.clicked.connect(lambda: self.restore_clicked.emit(self._cmd_name))
            hly.addWidget(self.restore_btn)

    def _update_shortcut_style(self):
        t = _theme()
        fs = self._font_size
        if self._shortcut:
            keys = self._shortcut.replace("+", "  +  ")
            self._shortcut_lb.setText(keys)
            self._shortcut_lb.setStyleSheet(
                f"color: {t['text_primary']}; font-size: {fs - 2}px; font-weight: 500; "
                f"background: {t['shortcut_bg']}; border: 1px solid {t['shortcut_border']}; "
                f"border-radius: 6px; padding: 0 10px;"
            )
            self._shortcut_lb.setMinimumWidth(60)
        else:
            self._shortcut_lb.setText("—")
            self._shortcut_lb.setStyleSheet(
                f"color: {t['text_muted']}; font-size: {fs - 2}px; font-style: italic; "
                f"background: transparent; border: none; padding: 0 8px;"
            )
            self._shortcut_lb.setMinimumWidth(30)

    def update_shortcut(self, shortcut: str, is_customized: bool):
        self._shortcut = shortcut
        self._is_customized = is_customized
        self._update_shortcut_style()

    # ── 悬停效果 ──

    def enterEvent(self, event):
        self._hovered = True
        self._apply_hover()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._apply_hover()
        super().leaveEvent(event)

    def _apply_hover(self):
        t = _theme()
        if self._hovered:
            self.setStyleSheet(
                f"_CommandRow {{ background: {t['row_hover']}; border-radius: 6px; }}"
            )
        else:
            self.setStyleSheet(
                "_CommandRow { background: transparent; border-radius: 6px; }"
            )


# ── 主卡片 ──────────────────────────────────────────────


class ShortcutManagerCard(QWidget):
    """快捷键管理器浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._all_commands: list = []
        self._rows: list = []
        self._capture_popup: Optional[_KeyCapturePopup] = None
        self._pending_cmd: str = ""
        self._header_icon: Optional[IconWidget] = None

        self._setup_ui()

    # ── 上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def show_card(self):
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._refresh()
        self.setVisible(True)

    def _apply_plugin_icon(self):
        if self._context_provider is None or self._header_icon is None:
            return
        try:
            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme_key = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme_key, "")
            if icon_path:
                from PyQt5.QtGui import QIcon
                self._header_icon.setIcon(QIcon(icon_path))
        except Exception:
            pass

    def _apply_latest_theme(self):
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
        except Exception:
            return

        font_family, font_size = _ctx_font(ctx)
        tc = _ctx_text_color(ctx)
        tcs = _ctx_text_color(ctx, secondary=True)
        border_c = _ctx_border_color(ctx)

        self._cached_tc = tc
        self._cached_tcs = tcs
        self._cached_font_family = font_family
        self._cached_font_size = font_size

        if font_family:
            self.setFont(QFont(font_family, font_size if font_size else 14))

        self._retheme()

    def _retheme(self):
        """主题切换时刷新子控件样式（字体/颜色动态适配）"""
        tc = getattr(self, "_cached_tc", _theme()["text_primary"])
        ff = getattr(self, "_cached_font_family", "")
        fs = getattr(self, "_cached_font_size", 14)

        for child in self.findChildren(QLabel):
            try:
                from qfluentwidgets import FluentLabelBase

                if isinstance(child, FluentLabelBase) and ff:
                    child.setFont(QFont(ff, fs))

                ss = child.styleSheet()
                if not ss:
                    continue
                import re

                new_ss = re.sub(r"color:\s*[^;]+;", f"color: {tc};", ss)
                if fs:
                    new_ss = re.sub(r"font-size:\s*[^;]+;", f"font-size: {fs}px;", new_ss)
                if ff and f"font-family: '{ff}'" not in new_ss:
                    new_ss += f" font-family: '{ff}';"
                child.setStyleSheet(new_ss)
            except RuntimeError:
                pass

        # 刷新搜索框样式
        if hasattr(self, "_search") and self._search is not None:
            try:
                t = _theme()
                sf = getattr(self, "_cached_font_size", 14)
                self._search.setStyleSheet(
                    f"QLineEdit {{"
                    f"  background: {t['search_bg']}; border: 1px solid {t['search_border']}; "
                    f"  border-radius: 8px; padding: 6px 12px; "
                    f"  color: {t['text_primary']}; font-size: {sf - 1}px; "
                    f"}}"
                    f"QLineEdit:focus {{"
                    f"  border: 1px solid {t['search_focus_border']}; "
                    f"}}"
                )
            except RuntimeError:
                pass

        # 刷新行样式
        for row in self._rows:
            try:
                row._update_shortcut_style()
                row._apply_hover()
            except RuntimeError:
                pass

    # ── 界面 ──

    def _setup_ui(self):
        t = _theme()

        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("ShortcutManagerCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部 ──
        header = self._build_header()
        root.addWidget(header)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background: {t['sep_color']}; max-height: 1px;")
        root.addWidget(sep)

        # ── 滚动内容区 ──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { width: 6px; background: transparent; }"
            "QScrollBar::handle:vertical {"
            "    background: rgba(128,128,128,0.15); border-radius: 3px; min-height: 30px;"
            "}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(2)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        # ── 底部提示 ──
        fs = getattr(self, "_cached_font_size", 14)
        tip = QLabel("点击 ✏ 为命令设置自定义快捷键  ·  点击 ↩ 恢复系统配置", self)
        tip.setStyleSheet(
            f"color: {t['tip_text']}; font-size: {fs - 3}px; background: transparent; "
            f"padding: 8px 16px;"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

    def _build_header(self) -> QWidget:
        t = _theme()
        fs = getattr(self, "_cached_font_size", 14)

        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 14, 16, 14)
        hly.setSpacing(10)

        self._header_icon = IconWidget(FluentIcon.COMMAND_PROMPT, header)
        self._header_icon.setFixedSize(22, 22)
        hly.addWidget(self._header_icon)

        tl = StrongBodyLabel("快捷键管理器", header)
        tl.setStyleSheet(f"color: {t['text_primary']}; background: transparent; font-size: {fs}px;")
        hly.addWidget(tl)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {t['text_secondary']}; font-size: {fs - 2}px; background: transparent;"
        )
        hly.addWidget(self._status_lb)
        hly.addStretch(1)

        # 搜索框放在 title 行
        self._search = QLineEdit(header)
        self._search.setPlaceholderText("搜索命令…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedSize(160, 30)
        self._search.setStyleSheet(
            f"QLineEdit {{"
            f"  background: {t['search_bg']}; border: 1px solid {t['search_border']}; "
            f"  border-radius: 8px; padding: 4px 10px; "
            f"  color: {t['text_primary']}; font-size: {fs - 1}px; "
            f"}}"
            f"QLineEdit:focus {{"
            f"  border: 1px solid {t['search_focus_border']}; "
            f"}}"
        )
        self._search.textChanged.connect(self._on_search)
        hly.addWidget(self._search)

        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新命令列表")
        self._refresh_btn.clicked.connect(self._refresh)
        hly.addWidget(self._refresh_btn)

        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        return header

    # ── 比例高度 ──

    def sizeHint(self):
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            h = min(base.height(), int(win.height() * 0.85))
            h = max(h, 220)
            return QSize(max(base.width(), 200), h)
        return base

    def showEvent(self, event):
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent

        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    # ── 加载数据 ──

    def _refresh(self):
        self._set_loading(True)

        try:
            self._all_commands = _load_all_items()
            self._render_list()
            count = len(self._all_commands)
            self._status_lb.setText(f"{count} 个命令")
        except Exception as e:
            logger.error(f"[ShortcutManager] 加载命令失败: {e}")
            self._status_lb.setText("加载失败")
        finally:
            self._set_loading(False)

    def _set_loading(self, loading: bool):
        if hasattr(self, "_refresh_btn"):
            self._refresh_btn.setEnabled(not loading)
        self._status_lb.setText("加载中…" if loading else "")

    # ── 搜索过滤 ──

    def _on_search(self, text: str):
        self._render_list(text)

    # ── 渲染列表 ──

    def _render_list(self, filter_text: str = ""):
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows.clear()

        customized = self._get_customized_names()

        filtered = self._all_commands
        if filter_text:
            ft = filter_text.strip().lower()
            filtered = [
                c
                for c in self._all_commands
                if ft in c["name"].lower() or ft in c.get("description", "").lower()
            ]

        for cmd in filtered:
            name = cmd["name"]
            shortcut = cmd.get("shortcut", "")
            description = cmd.get("description", "")
            is_customized = name in customized
            is_plugin = cmd.get("subtype") == "ui_plugin"

            row = _CommandRow(
                cmd_name=name,
                shortcut=shortcut,
                description=description,
                cmd_type_str="command",
                is_customized=is_customized,
                is_plugin=is_plugin,
                font_size=getattr(self, "_cached_font_size", 14),
                parent=self._content,
            )
            row.edit_clicked.connect(self._on_edit)
            row.restore_clicked.connect(self._on_restore)
            self._content_layout.addWidget(row)
            self._rows.append(row)

        # 空状态
        if not filtered:
            t = _theme()
            fs = getattr(self, "_cached_font_size", 14)
            empty = QLabel("没有匹配的命令" if filter_text else "暂无命令数据", self._content)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                f"color: {t['empty_text']}; font-size: {fs - 1}px; background: transparent; padding: 40px;"
            )
            self._content_layout.addWidget(empty)

    def _get_customized_names(self) -> set:
        cmd_dir = _get_user_custom_cmd_dir()
        if not cmd_dir.exists():
            return set()
        return {p.stem for p in cmd_dir.glob("*.md")}

    # ── 编辑：捕获快捷键 ──

    def _on_edit(self, cmd_name: str):
        cmd_info = None
        for c in self._all_commands:
            if c["name"] == cmd_name:
                cmd_info = c
                break
        if cmd_info is None:
            return

        self._pending_cmd = cmd_name
        edit_btn = self._find_edit_button(cmd_name)

        popup = _KeyCapturePopup(self.window())
        popup.key_captured.connect(self._on_key_captured)

        if edit_btn is not None:
            popup.setVisible(True)
            btn_global = edit_btn.mapToGlobal(edit_btn.rect().center())
            popup.move(btn_global.x() - popup.width() // 2, btn_global.y() - popup.height() - 8)
        else:
            parent_win = self.window()
            if parent_win:
                center = parent_win.mapToGlobal(parent_win.rect().center())
                popup.move(center.x() - popup.width() // 2, center.y() - popup.height() // 2)
            popup.setVisible(True)

        self._capture_popup = popup

    def _find_edit_button(self, cmd_name: str) -> Optional[QWidget]:
        for row in self._rows:
            if row._cmd_name == cmd_name:
                if hasattr(row, "edit_btn"):
                    return row.edit_btn
        return None

    def _on_key_captured(self, key_str: str):
        self._capture_popup = None

        if not key_str:
            return

        cmd_name = self._pending_cmd
        self._pending_cmd = ""

        cmd_info = None
        for c in self._all_commands:
            if c["name"] == cmd_name:
                cmd_info = c
                break
        if cmd_info is None:
            return

        success = self._save_custom_shortcut(
            cmd_name=cmd_name,
            description=cmd_info.get("description", ""),
            shortcut=key_str,
        )

        if success:
            QTimer.singleShot(300, self._refresh)
            self._status_lb.setText(f"✅ /{cmd_name} → {key_str}")

    def _save_custom_shortcut(self, cmd_name: str, description: str, shortcut: str) -> bool:
        try:
            cmd_dir = _get_user_custom_cmd_dir()
            cmd_dir.mkdir(parents=True, exist_ok=True)

            content = _make_minimal_cmd_file(cmd_name, description, shortcut)

            cmd_file = cmd_dir / f"{cmd_name}.md"
            cmd_file.write_text(content, encoding="utf-8")
            logger.info(f"[ShortcutManager] 已保存自定义快捷键: /{cmd_name} → {shortcut}")
            return True
        except Exception as e:
            logger.error(f"[ShortcutManager] 保存快捷键失败: {e}")
            self._status_lb.setText(f"❌ 保存失败: {e}")
            return False

    # ── 恢复系统配置 ──

    def _on_restore(self, cmd_name: str):
        try:
            cmd_dir = _get_user_custom_cmd_dir()
            cmd_file = cmd_dir / f"{cmd_name}.md"
            if cmd_file.exists():
                cmd_file.unlink()
                logger.info(f"[ShortcutManager] 已恢复系统配置: /{cmd_name}")
                self._status_lb.setText(f"↺ 已恢复 /{cmd_name} 的系统配置")

            QTimer.singleShot(300, self._refresh)
        except Exception as e:
            logger.error(f"[ShortcutManager] 恢复配置失败: {e}")
            self._status_lb.setText(f"❌ 恢复失败: {e}")

    # ── 关闭 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()
