# -*- coding: utf-8 -*-
"""ShortcutManager 浮动卡片 — 管理系统命令的快捷键

功能：
- 显示所有系统命令及其当前快捷键
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

# 用户自定义命令目录（相对 get_app_data_dir）
_USER_CUSTOM_CMD_REL = "plugins/user-custom/commands"


# ── 主题色辅助 ──────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    """fallback 文字颜色（无上下文时使用）"""
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_font(ctx: dict) -> tuple:
    """从上下文提取 font_family 和 font_size"""
    ff = ctx.get("font_family", "Microsoft YaHei")
    fs = ctx.get("font_size", 14)
    return ff, fs


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    """从上下文 colors 中获取文字颜色"""
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    return _text_color(secondary)


def _ctx_border_color(ctx: dict) -> str:
    """从上下文 colors 中获取边框颜色"""
    return ctx.get("colors", {}).get("border", "rgba(128,128,128,0.15)")


def _make_style(color: str, font_family: str = "", font_size: int = 0, extra: str = "") -> str:
    """生成带字体的 QSS 样式串"""
    parts = [f"color: {color};"]
    if font_family:
        parts.append(f"font-family: '{font_family}';")
    if font_size:
        parts.append(f"font-size: {font_size}px;")
    if extra:
        parts.append(extra)
    return " ".join(parts)


# ── 工具函数 ────────────────────────────────────────────


def _get_user_custom_cmd_dir() -> Path:
    """获取 user-custom 插件的 commands 目录路径

    开发环境: .drifox/plugins/user-custom/commands/
    打包环境: ~/.drifox/plugins/user-custom/commands/
    """
    from app.utils.utils import get_app_data_dir

    return get_app_data_dir() / _USER_CUSTOM_CMD_REL


def _display_type_to_yaml(display_type: str) -> str:
    """将 display type 转换为 YAML frontmatter type

    to_display_dict 中的 type:
    - "command" → "function"
    - "prompt" → "prompt"
    - "agent" → "agent"
    - "skill"  → "function"（技能保存为 function 类型，快捷鍵插入 /命令）
    """
    mapping = {
        "command": "function",
        "prompt": "prompt",
        "agent": "agent",
        "skill": "function",
    }
    return mapping.get(display_type, "function")


def _load_all_items() -> list:
    """获取所有命令+技能，排序与命令卡片一致

    排序规则（与 CommandCard._sort_key 保持一致）：
    0 = 系统内建命令 (command)
    1 = UI 插件命令 (command + subtype=ui_plugin)
    2 = 技能 (skill)
    3 = 智能体/提示词 (agent/prompt)

    Returns:
        [{name, description, type, shortcut, subtype?, display_name?}, ...]
    """
    from app.core.command_manager import CommandManager
    from app.core.ui_plugin_registry import UIPluginRegistry
    from app.utils.utils import get_local_skills

    items = []

    # 1. 加载命令
    cmd_mgr = CommandManager.get_instance()
    commands = cmd_mgr.get_all_commands()

    # 标记 UI 插件命令（用于排序）
    ui_cmd_names = UIPluginRegistry.get_instance().get_ui_command_names()
    for cmd in commands:
        if cmd["name"] in ui_cmd_names:
            cmd["subtype"] = "ui_plugin"
    items.extend(commands)

    # 2. 加载技能
    try:
        skills = get_local_skills()
        for s in skills:
            items.append(
                {
                    "name": s.get("qualified_name", s["name"]),
                    "description": s.get("description", ""),
                    "type": "skill",
                }
            )
    except Exception:
        pass

    # 3. 重名检测加后缀（与命令卡片一致）
    name_type_map = {}
    for item in items:
        name_type_map.setdefault(item["name"], set()).add(item["type"])

    suffix_map = {
        "skill": "-skill",
        "prompt": "-prompt",
        "command": "-cmd",
        "agent": "-agent",
    }

    # 标记需要加后缀的项
    for item in items:
        if len(name_type_map.get(item["name"], set())) > 1:
            suffix = suffix_map.get(item["type"], "")
            if suffix:
                item["display_name"] = f"{item['name']}{suffix}"
            else:
                item["display_name"] = item["name"]
        else:
            item["display_name"] = item["name"]

    # 4. 排序（与命令卡片完全一致）
    def _sort_key(item):
        t = item["type"]
        if t == "command" and item.get("subtype") == "ui_plugin":
            return (1, item["display_name"])
        if t == "command":
            return (0, item["display_name"])
        if t == "skill":
            return (2, item["display_name"])
        # agent / prompt
        return (3, item["display_name"])

    items.sort(key=_sort_key)
    return items


def _make_minimal_cmd_file(name: str, description: str, type_str: str, shortcut: str) -> str:
    """生成最小命令文件内容（仅 frontmatter，用于覆盖快捷键）"""
    lines = ["---"]
    lines.append(f"description: {description}")
    lines.append(f"type: {type_str}")
    if shortcut:
        lines.append(f"shortcut: {shortcut}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ── 按键捕获弹出窗 ─────────────────────────────────────


class _KeyCapturePopup(QWidget):
    """弹出按键捕获窗口

    点击编辑后弹出，等待用户按下快捷键组合。
    捕获后自动关闭并发射 key_captured 信号。
    """

    key_captured = pyqtSignal(str)  # 发射 "Ctrl+Shift+C" 或空字符串（取消/ESC）

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(220, 60)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        frame = QFrame(self)
        frame.setObjectName("captureFrame")
        frame.setStyleSheet("""
            #captureFrame {
                background: rgba(40, 40, 50, 0.95);
                border: 2px solid rgba(100, 140, 255, 0.6);
                border-radius: 8px;
            }
        """)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(16, 8, 16, 8)

        self._hint = QLabel("按下快捷键… 按 Esc 取消", frame)
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setStyleSheet("color: white; font-size: 13px; background: transparent;")
        fl.addWidget(self._hint)

        layout.addWidget(frame)

    def showEvent(self, event):
        super().showEvent(event)
        # 捕获全局键盘
        self.grabKeyboard()

    def hideEvent(self, event):
        self.releaseKeyboard()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        # 单独按修饰键时忽略（等待组合键）
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return

        # Esc 取消
        if key == Qt.Key_Escape:
            self.key_captured.emit("")
            self.close()
            return

        # 构建 QKeySequence
        seq = QKeySequence(int(mods) | key)
        key_str = seq.toString(QKeySequence.NativeText)
        self.key_captured.emit(key_str)
        self.close()


# ── 命令行控件 ─────────────────────────────────────────


class _CommandRow(QWidget):
    """单个命令的显示行"""

    edit_clicked = pyqtSignal(str)  # 参数: 命令名
    restore_clicked = pyqtSignal(str)  # 参数: 命令名

    def __init__(
        self,
        cmd_name: str,
        shortcut: str,
        description: str,
        cmd_type_str: str,
        is_customized: bool,
        display_name: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._cmd_name = cmd_name
        self._shortcut = shortcut
        self._description = description
        self._cmd_type_str = cmd_type_str
        self._is_customized = is_customized
        self._display_name = display_name or cmd_name
        self.is_skill = cmd_type_str == "skill"
        self._setup_ui()

    def _setup_ui(self):
        self.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(self)
        hly.setContentsMargins(8, 4, 8, 4)
        hly.setSpacing(8)

        # 命令名：/name
        name_text = f"/{self._display_name}"
        name_lb = QLabel(name_text, self)
        name_lb.setStyleSheet(f"color: {_text_color()}; font-size: 13px; background: transparent;")
        if self.is_skill:
            name_lb.setToolTip("技能命令（暂不支持在此设置快捷键）")
        elif self._is_customized:
            name_lb.setText(f"✦ /{self._display_name}")
            name_lb.setToolTip("已自定义（原系统命令被覆盖）")
        name_lb.setMinimumWidth(140)
        hly.addWidget(name_lb)

        # 类型标签
        type_lb = QLabel(self._cmd_type_str, self)
        type_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 11px; "
            f"background: rgba(128,128,128,0.1); border-radius: 3px; "
            f"padding: 1px 6px;"
        )
        type_lb.setFixedHeight(20)
        hly.addWidget(type_lb)

        hly.addStretch(1)

        # 快捷键显示
        self._shortcut_lb = QLabel(self._shortcut if self._shortcut else "—", self)
        ss = f"color: {_text_color()}; font-size: 13px; background: transparent;"
        if not self._shortcut:
            ss = f"color: {_text_color(secondary=True)}; font-size: 13px; background: transparent; font-style: italic;"
        self._shortcut_lb.setStyleSheet(ss)
        self._shortcut_lb.setMinimumWidth(160)
        self._shortcut_lb.setAlignment(Qt.AlignCenter)
        hly.addWidget(self._shortcut_lb)

        # 技能：仅展示，无操作按钮
        if self.is_skill:
            return

        # 编辑按钮（公开属性，供定位弹窗用）
        self.edit_btn = ToolButton(FluentIcon.EDIT, self)
        self.edit_btn.setToolTip("设置自定义快捷键")
        self.edit_btn.setFixedSize(28, 28)
        self.edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._cmd_name))
        hly.addWidget(self.edit_btn)

        # 恢复按钮（仅自定义的命令显示）
        if self._is_customized:
            self.restore_btn = ToolButton(FluentIcon.RETURN, self)
            self.restore_btn.setToolTip("恢复系统配置")
            self.restore_btn.setFixedSize(28, 28)
            self.restore_btn.clicked.connect(lambda: self.restore_clicked.emit(self._cmd_name))
            hly.addWidget(self.restore_btn)

    def update_shortcut(self, shortcut: str, is_customized: bool):
        """更新快捷鍵顯示"""
        self._shortcut = shortcut
        self._is_customized = is_customized
        self._shortcut_lb.setText(shortcut if shortcut else "—")
        ss = f"color: {_text_color()}; font-size: 13px; background: transparent;"
        if not shortcut:
            ss = f"color: {_text_color(secondary=True)}; font-size: 13px; background: transparent; font-style: italic;"
        self._shortcut_lb.setStyleSheet(ss)


# ── 主卡片 ──────────────────────────────────────────────


class ShortcutManagerCard(QWidget):
    """快捷键管理器浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._all_commands: list = []  # 当前显示的命令列表
        self._rows: list = []  # _CommandRow 列表
        self._capture_popup: Optional[_KeyCapturePopup] = None
        self._pending_cmd: str = ""  # 正在编辑的命令名
        self._header_icon: Optional[IconWidget] = None

        self._setup_ui()

    # ── 上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def show_card(self):
        """卡片显示时刷新主题 + 加载数据"""
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._refresh()
        self.setVisible(True)

    def _apply_plugin_icon(self):
        """从上下文获取插件图标并更新头部图标"""
        if self._context_provider is None or self._header_icon is None:
            return
        try:
            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme, "")
            if icon_path:
                from PyQt5.QtGui import QIcon

                self._header_icon.setIcon(QIcon(icon_path))
        except Exception:
            pass

    def _apply_latest_theme(self):
        """从上下文拉取最新主题色并刷新"""
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

        # 分隔线
        try:
            for sep in self.findChildren(QFrame):
                if sep.frameShape() == QFrame.HLine:
                    sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
        except RuntimeError:
            pass

        # 搜索框
        if hasattr(self, "_search") and self._search is not None:
            try:
                self._search.setStyleSheet(
                    f"background: rgba(128,128,128,0.1); border-radius: 6px; "
                    f"padding: 4px 10px; border: 1px solid {border_c}; " + _make_style(tc, font_family, font_size)
                )
            except RuntimeError:
                pass

    def _retheme(self):
        tc = getattr(self, "_cached_tc", "rgba(255,255,255,0.9)")
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

    # ── 界面 ──

    def _setup_ui(self):
        self.setMinimumHeight(0)
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
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # ── 搜索框 ──
        search_bar = self._build_search_bar()
        root.addWidget(search_bar)

        # ── 滚动内容区 ──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical {"
            "    width: 6px; background: transparent;"
            "}"
            "QScrollBar::handle:vertical {"
            "    background: rgba(255,255,255,0.12);"
            "    border-radius: 3px; min-height: 30px;"
            "}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            "    height: 0;"
            "}"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 4, 0, 4)
        self._content_layout.setSpacing(0)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        # ── 底部提示 ──
        tip = QLabel("💡 点击 ✏ 为命令设置自定义快捷键，点击 ✕ 恢复系统配置。自定义配置保存到 user-custom 插件。", self)
        tip.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 11px; background: transparent; padding: 6px 16px;"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

    def _build_header(self) -> QWidget:
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        self._header_icon = IconWidget(FluentIcon.COMMAND_PROMPT, header)
        self._header_icon.setFixedSize(22, 22)
        hly.addWidget(self._header_icon)

        tl = StrongBodyLabel("快捷键管理器", header)
        tl.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(tl)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        hly.addWidget(self._status_lb)
        hly.addStretch(1)

        # 刷新按钮
        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新命令列表")
        self._refresh_btn.clicked.connect(self._refresh)
        hly.addWidget(self._refresh_btn)

        # 关闭按钮
        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        return header

    def _build_search_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(bar)
        hly.setContentsMargins(16, 6, 16, 6)
        hly.setSpacing(0)

        self._search = QLineEdit(bar)
        self._search.setPlaceholderText("搜索命令…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(28)
        self._search.setStyleSheet(
            "background: rgba(128,128,128,0.1); border-radius: 6px; "
            "padding: 4px 10px; border: 1px solid rgba(128,128,128,0.15);"
        )
        self._search.textChanged.connect(self._on_search)
        hly.addWidget(self._search)

        return bar

    # ── 全屏高度 ──

    def sizeHint(self):
        """返回極大值，讓容器分配所有可用空間"""
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            # 返回 10000，容器 layout 會把剩餘空間全部分配給卡片
            return QSize(max(base.width(), 200), 10000)
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
        """刷新命令/技能列表"""
        self._set_loading(True)

        try:
            self._all_commands = _load_all_items()
            self._render_list()
            count = len(self._all_commands)
            cmd_count = sum(1 for c in self._all_commands if c["type"] != "skill")
            skill_count = count - cmd_count
            parts = [f"{count} 项"]
            if skill_count:
                parts.append(f"{skill_count} 技能")
            self._status_lb.setText(" | ".join(parts))
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
        """搜索过滤"""
        self._render_list(text)

    # ── 渲染列表 ──

    def _render_list(self, filter_text: str = ""):
        """渲染命令列表"""
        # 清空
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows.clear()

        # 获取 user-custom 覆盖的文件名列表
        customized = self._get_customized_names()

        # 过滤
        filtered = self._all_commands
        if filter_text:
            ft = filter_text.strip().lower()
            filtered = [
                c
                for c in self._all_commands
                if ft in c["name"].lower()
                or ft in c.get("display_name", c["name"]).lower()
                or ft in c.get("description", "").lower()
            ]

        for cmd in filtered:
            name = cmd["name"]
            display_name = cmd.get("display_name", name)
            shortcut = cmd.get("shortcut", "")
            description = cmd.get("description", "")
            cmd_type_str = cmd.get("type", "command")
            is_customized = name in customized

            row = _CommandRow(
                cmd_name=name,
                shortcut=shortcut,
                description=description,
                cmd_type_str=cmd_type_str,
                is_customized=is_customized,
                display_name=display_name,
                parent=self._content,
            )
            if not row.is_skill:
                row.edit_clicked.connect(self._on_edit)
                row.restore_clicked.connect(self._on_restore)
            self._content_layout.addWidget(row)
            self._rows.append(row)

        # 空状态
        if not filtered:
            empty = QLabel("没有匹配的命令" if filter_text else "暂无命令数据", self._content)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent; padding: 40px;")
            self._content_layout.addWidget(empty)

        # 末尾弹性空间
        self._content_layout.addStretch(1)

    def _get_customized_names(self) -> set:
        """获取被 user-custom 覆盖的命令名集合"""
        cmd_dir = _get_user_custom_cmd_dir()
        if not cmd_dir.exists():
            return set()
        return {p.stem for p in cmd_dir.glob("*.md")}

    # ── 编辑：捕获快捷键 ──

    def _on_edit(self, cmd_name: str):
        """点击编辑按钮：弹出按键捕获窗口"""
        # 找到对应的命令信息
        cmd_info = None
        for c in self._all_commands:
            if c["name"] == cmd_name:
                cmd_info = c
                break
        if cmd_info is None:
            return

        self._pending_cmd = cmd_name

        # 找到该命令对应的行控件，获取其编辑按钮的位置
        edit_btn = self._find_edit_button(cmd_name)

        popup = _KeyCapturePopup(self.window())
        popup.key_captured.connect(self._on_key_captured)

        # 将 popup 定位在编辑按钮附近（或卡片中央兜底）
        if edit_btn is not None:
            popup.setVisible(True)
            btn_global = edit_btn.mapToGlobal(edit_btn.rect().center())
            popup.move(btn_global.x() - popup.width() // 2, btn_global.y() - popup.height() - 10)
        else:
            # 兜底：居中显示
            parent_win = self.window()
            if parent_win:
                center = parent_win.mapToGlobal(parent_win.rect().center())
                popup.move(center.x() - popup.width() // 2, center.y() - popup.height() // 2)
            popup.setVisible(True)

        self._capture_popup = popup

    def _find_edit_button(self, cmd_name: str) -> Optional[QWidget]:
        """根据命令名找到对应行中的编辑按钮"""
        for row in self._rows:
            if row._cmd_name == cmd_name:
                if hasattr(row, "edit_btn"):
                    return row.edit_btn
        return None

    def _on_key_captured(self, key_str: str):
        """按键捕获完成"""
        self._capture_popup = None

        if not key_str:
            # 用户取消（按 Esc）
            return

        cmd_name = self._pending_cmd
        self._pending_cmd = ""

        # 查找命令信息
        cmd_info = None
        for c in self._all_commands:
            if c["name"] == cmd_name:
                cmd_info = c
                break
        if cmd_info is None:
            return

        # 保存到 user-custom
        display_type = cmd_info.get("type", "command")
        success = self._save_custom_shortcut(
            cmd_name=cmd_name,
            description=cmd_info.get("description", ""),
            display_type=display_type,
            shortcut=key_str,
        )

        if success:
            # 刷新列表
            QTimer.singleShot(500, self._refresh)
            self._status_lb.setText(f"✅ 已设置 /{cmd_name} → {key_str}")

    def _save_custom_shortcut(self, cmd_name: str, description: str, display_type: str, shortcut: str) -> bool:
        """保存自定义快捷键到 user-custom 插件

        Args:
            cmd_name: 命令名
            description: 命令描述
            display_type: 显示类型 "command"/"prompt"/"agent"
            shortcut: 快捷键字符串，如 "Ctrl+Shift+C"
        """
        try:
            cmd_dir = _get_user_custom_cmd_dir()
            cmd_dir.mkdir(parents=True, exist_ok=True)

            type_str = _display_type_to_yaml(display_type)
            content = _make_minimal_cmd_file(cmd_name, description, type_str, shortcut)

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
        """恢复系统配置：删除 user-custom 下对应文件"""
        try:
            cmd_dir = _get_user_custom_cmd_dir()
            cmd_file = cmd_dir / f"{cmd_name}.md"
            if cmd_file.exists():
                cmd_file.unlink()
                logger.info(f"[ShortcutManager] 已恢复系统配置: /{cmd_name}")
                self._status_lb.setText(f"↺ 已恢复 /{cmd_name} 的系统配置")

            # 刷新列表
            QTimer.singleShot(500, self._refresh)
        except Exception as e:
            logger.error(f"[ShortcutManager] 恢复配置失败: {e}")
            self._status_lb.setText(f"❌ 恢复失败: {e}")

    # ── 关闭 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()
