# -*- coding: utf-8 -*-
"""ShortcutManagerCard 浮动卡片 — 管理系统命令的快捷键

基于 PluginManagerCard 骨架，注入快捷键管理逻辑。
"""

import re
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
    FluentLabelBase,
    IconWidget,
    ScrollArea,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger


# ── 路径常量 ──────────────────────────────────────────────

_USER_CUSTOM_CMD_REL = "plugins/user-custom/commands"


def _safe_filename(cmd_name: str) -> str:
    """将命令名转换为安全文件名（: → __，解决 Windows 文件名非法字符问题）"""
    return cmd_name.replace(":", "__")


def _get_user_custom_cmd_dir() -> Path:
    from app.utils.utils import get_app_data_dir

    return get_app_data_dir() / _USER_CUSTOM_CMD_REL


def _ensure_user_custom_plugin():
    """确保 user-custom 插件有有效的 plugin.json 清单并被 PluginManager 发现

    ShortcutManager 写入自定义快捷键文件到 user-custom/commands/，
    但如果 user-custom 插件没有 .drifox-plugin/plugin.json 清单，
    PluginManager._scan_plugins 不会发现该插件，导致自定义文件不被加载。

    本函数在首次保存时自动创建最小清单并触发插件发现，
    配合 watchfiles 热重载机制使自定义快捷键立即生效。
    """
    import json

    from app.core.plugin_manager import PluginManager
    from app.utils.utils import get_app_data_dir

    custom_dir = get_app_data_dir() / "plugins" / "user-custom"
    manifest_dir = custom_dir / ".drifox-plugin"
    manifest_path = manifest_dir / "plugin.json"

    if manifest_path.exists():
        return

    try:
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": "user-custom",
            "description": "用户自定义配置（命令、MCP、Hooks 等）",
            "version": "1.0.0",
            "type": "user",
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        pm = PluginManager.get_instance()
        if pm.is_initialized() and not pm.has_plugin("user-custom"):
            pm._discover_user_plugins(get_app_data_dir())
            try:
                pm.enable_plugin("user-custom")
            except Exception:
                pass
            logger.info("[ShortcutManager] user-custom 插件清单已自动创建并注册")
    except Exception as e:
        logger.warning(f"[ShortcutManager] 创建 user-custom 插件清单失败: {e}")


def _find_original_cmd_file(cmd_name: str) -> Optional[Path]:
    """查找命令的原始 .md 文件（排除 user-custom 目录中的覆盖文件）

    Windows 兼容：同时尝试原始命令名和安全文件名（: → __）进行匹配。
    """
    from app.core.plugin_manager import PluginManager

    pm = PluginManager.get_instance()
    if not pm.is_initialized():
        return None

    user_custom_dir = _get_user_custom_cmd_dir()
    safe_name = _safe_filename(cmd_name)
    match_names = (cmd_name, safe_name) if safe_name != cmd_name else (cmd_name,)

    for cmd_file in pm.get_command_files():
        if cmd_file.stem in match_names:
            # 优先返回非 user-custom 中的原始文件
            if not str(cmd_file.resolve()).startswith(str(user_custom_dir.resolve())):
                return cmd_file
    # 没找到原始文件，返回 user-custom 中的覆盖文件（如有）
    for cmd_file in pm.get_command_files():
        if cmd_file.stem in match_names:
            return cmd_file
    return None


def _make_minimal_cmd_file(name: str, description: str, shortcut: str) -> str:
    """生成最小化命令文件（仅用作兜底，当找不到原始文件时）"""
    lines = ["---"]
    lines.append(f"description: {description}")
    lines.append("type: function")
    if shortcut:
        lines.append(f"shortcut: {shortcut}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _upsert_frontmatter_shortcut(content: str, shortcut: str) -> str:
    """在 .md 文件的 YAML frontmatter 中添加或更新 shortcut 字段

    Args:
        content: 原始 .md 文件全部内容
        shortcut: 新的快捷键值（空字符串表示删除 shortcut 行）

    Returns:
        修改后的文件内容
    """
    if not content.startswith("---"):
        return content

    lines = content.splitlines()
    # 找到 frontmatter 范围：第一个 --- 到下一个 ---
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return content

    fm_lines = lines[1:close_idx]
    body_lines = lines[close_idx + 1 :]

    # 检查是否已有 shortcut 行
    has_shortcut = False
    new_fm = []
    for line in fm_lines:
        if re.match(r"^shortcut\s*:", line):
            has_shortcut = True
            if shortcut:
                new_fm.append(f"shortcut: {shortcut}")
            # shortcut 为空 → 删除该行（不添加）
        else:
            new_fm.append(line)

    if not has_shortcut and shortcut:
        # 没有 shortcut → 在 description 之后插入
        inserted = False
        result = []
        for line in new_fm:
            result.append(line)
            if not inserted and re.match(r"^description\s*:", line):
                result.append(f"shortcut: {shortcut}")
                inserted = True
        if not inserted:
            # 没找到 description 行，直接追加到末尾
            result.append(f"shortcut: {shortcut}")
        new_fm = result

    return "---\n" + "\n".join(new_fm) + "\n---\n" + "\n".join(body_lines)


# ── 快捷键冲突检测 ──


def _find_conflicts(shortcut: str, all_commands: list, exclude_cmd: str = "") -> list:
    """查找已分配相同快捷键的命令列表

    Args:
        shortcut: 要检测的快捷键
        all_commands: 所有命令列表（dict，含 name / shortcut 字段）
        exclude_cmd: 排除的命令名（当前正在编辑的命令，不与自己冲突）

    Returns:
        冲突的命令 dict 列表
    """
    conflicts = []
    for cmd in all_commands:
        if cmd["name"] == exclude_cmd:
            continue
        if cmd.get("shortcut", "") and cmd["shortcut"] == shortcut:
            conflicts.append(cmd)
    return conflicts


# ── 数据加载 ──────────────────────────────────────────────


def _load_all_items() -> list:
    """获取系统内建命令 + UI 插件命令列表"""
    from app.core.command_manager import CommandManager
    from app.core.ui_plugin_registry import UIPluginRegistry

    items = []
    cmd_mgr = CommandManager.get_instance()
    commands = cmd_mgr.get_all_commands()
    ui_cmd_names = UIPluginRegistry.get_instance().get_ui_command_names()

    for cmd in commands:
        if cmd.get("type") != "command":
            continue
        if cmd["name"] in ui_cmd_names:
            cmd["subtype"] = "ui_plugin"
        items.append(cmd)

    def _sort_key(item):
        is_plugin = 1 if item.get("subtype") == "ui_plugin" else 0
        return (is_plugin, item["name"])

    items.sort(key=_sort_key)
    return items


# ── 主题色 ──────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _ctx_border_color(ctx: dict) -> str:
    return ctx.get("colors", {}).get("border", "rgba(128,128,128,0.15)")


def _ctx_font(ctx: dict) -> tuple:
    ff = ctx.get("font_family", "")
    fs = ctx.get("font_size", 0)
    return ff, fs or 14


# ── 按键捕获弹窗 ─────────────────────────────────────


class _KeyCapturePopup(QWidget):
    """弹出按键捕获窗口"""

    key_captured = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(240, 68)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        frame = QFrame(self)
        frame.setObjectName("captureFrame")
        frame.setStyleSheet("""
            #captureFrame {
                background: rgba(40, 40, 50, 0.95);
                border: 2px solid rgba(100, 140, 255, 0.6);
                border-radius: 12px;
            }
        """)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(20, 12, 20, 12)

        hint = QLabel("按下快捷键组合…", frame)
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: white; font-size: 14px; font-weight: 500; background: transparent;")
        fl.addWidget(hint)

        esc = QLabel("Esc 取消", frame)
        esc.setAlignment(Qt.AlignCenter)
        esc.setStyleSheet("color: rgba(255,255,255,0.4); font-size: 12px; background: transparent;")
        fl.addWidget(esc)

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


class _CommandRow(QFrame):
    """单个命令的展示行"""

    edit_clicked = pyqtSignal(str)
    restore_clicked = pyqtSignal(str)

    def __init__(
        self,
        cmd_name: str,
        shortcut: str,
        description: str,
        is_customized: bool,
        is_plugin: bool,
        font_size: int,
        parent=None,
    ):
        super().__init__(parent)
        self._cmd_name = cmd_name
        self._shortcut = shortcut
        self._description = description
        self._is_customized = is_customized
        self._is_plugin = is_plugin
        self._font_size = font_size
        self._setup_ui()

    def _setup_ui(self):
        self.setObjectName("cmdRow")
        self.setStyleSheet(
            "#cmdRow { background: transparent;"
            " border: 1px solid rgba(128,128,128,0.15);"
            " border-radius: 8px; padding: 0px; }"
            "#cmdRow:hover { background: rgba(128,128,128,0.05); }"
        )

        # 垂直主布局：上行=名称+快捷键+按钮，下行=描述
        vly = QVBoxLayout(self)
        vly.setContentsMargins(12, 6, 12, 6)
        vly.setSpacing(2)

        # ── 上行 ──
        hly = QHBoxLayout()
        hly.setContentsMargins(0, 0, 0, 0)
        hly.setSpacing(8)

        # 命令名
        name_text = f"/{self._cmd_name}"
        if self._is_customized:
            name_text = f"✦ /{self._cmd_name}"
        name_lb = QLabel(name_text, self)
        name_lb.setObjectName("cmdRowName")
        name_lb.setStyleSheet(f"color: {_text_color()}; font-size: 14px; background: transparent;")
        name_lb.setMinimumWidth(140)
        if self._is_customized:
            name_lb.setToolTip("已自定义（原系统命令被覆盖）")
        hly.addWidget(name_lb)

        hly.addStretch(1)

        # 快捷键
        if self._shortcut:
            keys = self._shortcut.replace("+", "  +  ")
            shortcut_lb = QLabel(keys, self)
            shortcut_lb.setObjectName("cmdRowShortcut")
            shortcut_lb.setStyleSheet(
                f"color: {_text_color()}; font-size: 12px;"
                f" background: rgba(128,128,128,0.08); border: 1px solid rgba(128,128,128,0.12);"
                f" border-radius: 6px; padding: 2px 10px;"
            )
        else:
            shortcut_lb = QLabel("—", self)
            shortcut_lb.setObjectName("cmdRowShortcut")
            shortcut_lb.setStyleSheet(
                f"color: {_text_color(True)}; font-size: 12px; font-style: italic; background: transparent;"
            )
        shortcut_lb.setAlignment(Qt.AlignCenter)
        shortcut_lb.setMinimumWidth(60)
        hly.addWidget(shortcut_lb)
        self._shortcut_lb = shortcut_lb

        # 编辑按钮
        self.edit_btn = ToolButton(FluentIcon.EDIT, self)
        self.edit_btn.setToolTip("设置自定义快捷键")
        self.edit_btn.setFixedSize(28, 28)
        self.edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._cmd_name))
        hly.addWidget(self.edit_btn)

        # 恢复按钮
        if self._is_customized:
            self.restore_btn = ToolButton(FluentIcon.RETURN, self)
            self.restore_btn.setToolTip("恢复系统配置")
            self.restore_btn.setFixedSize(28, 28)
            self.restore_btn.clicked.connect(lambda: self.restore_clicked.emit(self._cmd_name))
            hly.addWidget(self.restore_btn)

        vly.addLayout(hly)

        # ── 下行：描述文字 ──
        if self._description:
            desc_lb = QLabel(self._description, self)
            desc_lb.setObjectName("cmdRowDesc")
            desc_lb.setStyleSheet(f"color: {_text_color(True)}; font-size: 12px; background: transparent;")
            desc_lb.setWordWrap(True)
            vly.addWidget(desc_lb)

    def update_shortcut(self, shortcut: str, is_customized: bool):
        self._shortcut = shortcut
        self._is_customized = is_customized
        if shortcut:
            self._shortcut_lb.setText(shortcut.replace("+", "  +  "))
            self._shortcut_lb.setStyleSheet(
                f"color: {_text_color()}; font-size: 12px;"
                f" background: rgba(128,128,128,0.08); border: 1px solid rgba(128,128,128,0.12);"
                f" border-radius: 6px; padding: 2px 10px;"
            )
        else:
            self._shortcut_lb.setText("—")
            self._shortcut_lb.setStyleSheet(
                f"color: {_text_color(True)}; font-size: 12px; font-style: italic; background: transparent;"
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

    # ── 拉模型上下文注入 ──

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

        # 搜索框
        try:
            self._search.setStyleSheet(
                f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {tc};"
            )
        except RuntimeError:
            pass

        # 分隔线
        try:
            for sep in self.findChildren(QFrame):
                if sep.frameShape() == QFrame.HLine:
                    sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
        except RuntimeError:
            pass

    _CMD_ROW_SIZE_OFFSETS = {
        "cmdRowName": 1,  # 命令名比基准稍大
        "cmdRowDesc": -3,  # 描述文字较小
        "cmdRowShortcut": -1,  # 快捷键适中
    }

    def _retheme(self):
        tc = getattr(self, "_cached_tc", "rgba(255,255,255,0.9)")
        ff = getattr(self, "_cached_font_family", "")
        fs = getattr(self, "_cached_font_size", 14)

        for child in self.findChildren(QLabel):
            try:
                offset = self._CMD_ROW_SIZE_OFFSETS.get(child.objectName(), 0)
                target_fs = max(8, fs + offset) if fs > 0 else 14 + offset

                if isinstance(child, FluentLabelBase) and ff:
                    child.setFont(QFont(ff, target_fs))

                ss = child.styleSheet()
                if not ss:
                    continue

                new_ss = re.sub(r"color:\s*[^;]+;", f"color: {tc};", ss)
                if target_fs:
                    if "font-size:" in new_ss:
                        new_ss = re.sub(r"font-size:\s*[^;]+;", f"font-size: {target_fs}px;", new_ss)
                    else:
                        # 样式表中缺失 font-size 时主动追加，确保所有标签受主题控制
                        new_ss = new_ss.rstrip() + f"\n    font-size: {target_fs}px;"
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

        # 头部
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        ic = IconWidget(FluentIcon.COMMAND_PROMPT, header)
        ic.setFixedSize(22, 22)
        hly.addWidget(ic)
        self._header_icon = ic

        tl = StrongBodyLabel("快捷键管理器", header)
        tl.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(tl)

        self._count_lb = QLabel("", header)
        self._count_lb.setStyleSheet(f"color: {_text_color(True)}; font-size: 12px; background: transparent;")
        hly.addWidget(self._count_lb)
        hly.addStretch(1)

        self._search = QLineEdit(header)
        self._search.setPlaceholderText("搜索命令…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(160)
        self._search.setStyleSheet(
            f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {_text_color()};"
        )
        self._search.textChanged.connect(self._on_search)
        hly.addWidget(self._search)

        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._refresh)
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
        self._empty.setStyleSheet(f"color: {_text_color(True)}; background: transparent;")
        self._empty.setVisible(False)
        root.addWidget(self._empty)

    # ── 高度模式 ──

    def sizeHint(self):
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
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

    # ── 数据加载 ──

    def _refresh(self):
        self._count_lb.setText("加载中…")
        self._refresh_btn.setEnabled(False)

        try:
            self._all_commands = _load_all_items()
            self._render_list()
            self._count_lb.setText(f"共 {len(self._all_commands)} 个")
        except Exception as e:
            logger.error(f"[ShortcutManager] 加载失败: {e}")
            self._count_lb.setText("加载失败")
        finally:
            self._refresh_btn.setEnabled(True)

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
        fs = getattr(self, "_cached_font_size", 14)

        filtered = self._all_commands
        if filter_text:
            ft = filter_text.strip().lower()
            filtered = [
                c for c in self._all_commands if ft in c["name"].lower() or ft in c.get("description", "").lower()
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
                is_customized=is_customized,
                is_plugin=is_plugin,
                font_size=fs,
                parent=self._content,
            )
            row.edit_clicked.connect(self._on_edit)
            row.restore_clicked.connect(self._on_restore)
            self._content_layout.addWidget(row)
            self._rows.append(row)

        self._empty.setText("没有匹配的命令" if filter_text else "暂无命令数据")
        self._empty.setVisible(len(filtered) == 0)

        self._retheme()

    def _get_customized_names(self) -> set:
        """获取已自定义的命令名集合（将安全文件名反向映射为命令名）"""
        cmd_dir = _get_user_custom_cmd_dir()
        if not cmd_dir.exists():
            return set()
        # 安全文件名中的 __ 映射回命令名中的 :
        return {p.stem.replace("__", ":") for p in cmd_dir.glob("*.md")}

    # ── 编辑快捷键 ──

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

    def _find_edit_button(self, cmd_name: str):
        for row in self._rows:
            if row._cmd_name == cmd_name and hasattr(row, "edit_btn"):
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

        # ── 快捷键冲突检测 ──
        conflicts = _find_conflicts(key_str, self._all_commands, exclude_cmd=cmd_name)
        if conflicts:
            conflict_names = "\n".join(f"  • /{c['name']}" for c in conflicts)
            from app.widgets.common_dialogs import ConfirmDialog

            dialog = ConfirmDialog(
                title="快捷键冲突",
                content=(f"快捷键「{key_str}」已被以下命令占用：\n{conflict_names}\n\n仍要分配给 /{cmd_name} 吗？"),
                confirm_text="覆盖",
                cancel_text="取消",
                parent=self.window(),
            )
            confirmed = False

            def _on_confirm():
                nonlocal confirmed
                confirmed = True

            dialog.confirmed.connect(_on_confirm)
            dialog.exec_()
            if not confirmed:
                self._count_lb.setText(f"⛔ 已取消：/{cmd_name}")
                return

            # ── 覆盖冲突：清空被覆盖命令的快捷键 ──
            for conflict in conflicts:
                conflict_info = None
                for c in self._all_commands:
                    if c["name"] == conflict["name"]:
                        conflict_info = c
                        break
                if conflict_info:
                    self._save_custom_shortcut(
                        cmd_name=conflict["name"],
                        description=conflict_info.get("description", ""),
                        shortcut="",
                    )

        success = self._save_custom_shortcut(
            cmd_name=cmd_name,
            description=cmd_info.get("description", ""),
            shortcut=key_str,
        )

        if success:
            QTimer.singleShot(300, self._refresh)
            self._count_lb.setText(f"✅ /{cmd_name} → {key_str}")

    def _save_custom_shortcut(self, cmd_name: str, description: str, shortcut: str) -> bool:
        """保存自定义快捷键：基于原始命令文件完整复制，仅修改 shortcut 字段

        不再使用 _make_minimal_cmd_file（会丢失 argument-hint / parameters / mutex_groups），
        而是找到原始 .md 文件 → 复制全部内容 → 在 frontmatter 中添加/覆盖 shortcut → 保存到 user-custom。
        """
        try:
            cmd_dir = _get_user_custom_cmd_dir()
            cmd_dir.mkdir(parents=True, exist_ok=True)
            safe_name = _safe_filename(cmd_name)
            dest_path = cmd_dir / f"{safe_name}.md"

            # 确保 user-custom 插件被 PluginManager 发现（创建清单如不存在）
            _ensure_user_custom_plugin()

            # 1. 找到原始命令文件
            original = _find_original_cmd_file(cmd_name)
            if original:
                content = original.read_text(encoding="utf-8")
                # 2. 修改 frontmatter 中的 shortcut
                content = _upsert_frontmatter_shortcut(content, shortcut)
            else:
                # 兜底：找不到原始文件时生成最小化文件
                logger.warning(f"[ShortcutManager] 未找到 /{cmd_name} 的原始文件，使用最小化模板")
                content = _make_minimal_cmd_file(cmd_name, description, shortcut)

            dest_path.write_text(content, encoding="utf-8")
            logger.info(f"[ShortcutManager] 已保存: /{cmd_name} → {shortcut}")
            # 强制立即重载命令缓存，不依赖 watchfiles 异步热更新（watchfiles 有 2 秒防抖）
            from app.core.builtin_commands import reload_all_commands

            reload_all_commands()
            return True
        except Exception as e:
            logger.error(f"[ShortcutManager] 保存失败: {e}")
            self._count_lb.setText(f"❌ 保存失败: {e}")
            return False

    # ── 恢复系统配置 ──

    def _on_restore(self, cmd_name: str):
        try:
            cmd_dir = _get_user_custom_cmd_dir()
            safe_name = _safe_filename(cmd_name)
            # 尝试安全文件名和原始名（不同时才分别尝试，兼容旧文件）
            candidates = {safe_name, cmd_name}
            for name in candidates:
                cmd_file = cmd_dir / f"{name}.md"
                if cmd_file.exists():
                    cmd_file.unlink()
                    logger.info(f"[ShortcutManager] 已恢复: /{cmd_name}")
                    self._count_lb.setText(f"↺ 已恢复 /{cmd_name}")
                    # 强制立即重载命令缓存，不依赖 watchfiles 异步热更新
                    from app.core.builtin_commands import reload_all_commands

                    reload_all_commands()
                    break
            QTimer.singleShot(300, self._refresh)
        except Exception as e:
            logger.error(f"[ShortcutManager] 恢复失败: {e}")
            self._count_lb.setText(f"❌ 恢复失败: {e}")

    # ── 关闭 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()
