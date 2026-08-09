# -*- coding: utf-8 -*-
"""MarketplaceCard 浮动卡片 — 完整的插件市场浏览界面

功能：
- 异步拉取市场列表（不阻塞 UI）
- 异步安装/卸载/更新插件
- 插件搜索过滤
- 版本检测：已安装插件有新版时显示「更新」按钮
- 安装/更新状态实时反馈
"""

import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from PyQt5.QtCore import QObject, QRect, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    FluentLabelBase,
    IconWidget,
    InfoBar,
    LineEdit,
    MaskDialogBase,
    ScrollArea,
    StrongBodyLabel,
    TransparentPushButton,
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


class _MarketFetchWorker(QObject):
    """逐市场拉取：每拉完一个市场源就 emit 一次，不等待全部完成

    实现「远程拉到一个更新一个」：首个市场数据到达即可渲染，
    单个市场失败/超时不阻塞后续市场（fetch_marketplace 内部已捕获异常返回 _error）。
    """

    market_fetched = pyqtSignal(dict)  # 单个市场数据 {"name":..., "plugins":[...]}
    all_done = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, force: bool = False):
        super().__init__()
        self._force = force

    def run(self):
        from .marketplace_manager import get_marketplace_manager

        mgr = get_marketplace_manager()
        try:
            for src in mgr.get_sources():
                data = mgr.fetch_marketplace(src, force=self._force)
                if data.get("_error"):
                    # 单市场失败不阻塞，也不触发无效重建
                    continue
                self.market_fetched.emit(data)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")
        self.all_done.emit()


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


# ── 插件内容收集 / 详情弹窗 ──────────────────────────────────


def _collect_plugin_contents(plugin_dir: Path) -> dict:
    """收集已安装插件的组件内容清单

    Returns:
        {"skills": [...], "mcp": [...], "commands": [...], "agents": [...], "hooks": [...], "themes": [...]}
        空组件不出现。
    """
    contents: dict = {"skills": [], "mcp": [], "commands": [], "agents": [], "hooks": [], "themes": []}

    # 技能：skills/<name>/SKILL.md
    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists():
                contents["skills"].append(child.name)

    # MCP：.mcp.json → mcpServers 键
    mcp_file = plugin_dir / ".mcp.json"
    if mcp_file.exists():
        try:
            data = json.loads(mcp_file.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {}) or {}
            contents["mcp"] = sorted(servers.keys())
        except Exception:
            pass

    # 命令：commands/*.md
    commands_dir = plugin_dir / "commands"
    if commands_dir.is_dir():
        contents["commands"] = sorted(p.stem for p in commands_dir.glob("*.md"))

    # Agents：agents/*.md
    agents_dir = plugin_dir / "agents"
    if agents_dir.is_dir():
        contents["agents"] = sorted(p.stem for p in agents_dir.glob("*.md"))

    # Hooks：hooks/hooks.json → hooks 键（事件名）
    hooks_file = plugin_dir / "hooks" / "hooks.json"
    if hooks_file.exists():
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            contents["hooks"] = sorted((data.get("hooks", {}) or {}).keys())
        except Exception:
            pass

    # 主题：themes/ 子目录
    themes_dir = plugin_dir / "themes"
    if themes_dir.is_dir():
        contents["themes"] = sorted(p.name for p in themes_dir.iterdir() if p.is_dir())

    return {k: v for k, v in contents.items() if v}


class _PluginDetailDialog(MaskDialogBase):
    """插件详情弹窗：完整信息 + 底部操作按钮

    所有插件（含未安装）可查看：完整描述、作者、license、分类、
    来源市场、官网；已安装额外展示组件内容清单（技能/智能体/命令等，
    滚动查看）。底部主操作按状态给出：安装 / 更新 / 已安装（禁用）。
    """

    installRequested = pyqtSignal(dict)
    updateRequested = pyqtSignal(dict)

    def __init__(
        self,
        parent,
        plugin_meta: dict,
        installed: bool,
        has_update: bool,
        local_version: Optional[str],
        *,
        tc: str,
        tcs: str,
        ff: str,
        fs: int,
        accent_bg: str,
        card_bg: str,
        border_c: str,
    ):
        super().__init__(parent)
        self._meta = plugin_meta
        self._installed = installed
        self._has_update = has_update
        self._local_version = local_version
        self._init_ui(tc, tcs, ff, fs, accent_bg, card_bg, border_c)

    def _init_ui(self, tc, tcs, ff, fs, accent_bg, card_bg, border_c):
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("marketPluginDetail")
        self.widget.setStyleSheet(
            f"""
            #marketPluginDetail {{
                background-color: {card_bg};
                border: 1px solid {border_c};
                border-radius: 8px;
            }}
            """
        )

        ff_qss = f'font-family: "{ff}";' if ff else ""
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(0)

        # 标题：名称 + 版本徽标
        name = self._meta.get("name", "未知")
        remote_ver = self._meta.get("version", "")
        if self._has_update and self._local_version and remote_ver:
            ver_html = f'<span style="color:#FFA726;">🔄 v{self._local_version} → v{remote_ver}</span>'
        elif self._installed and self._local_version:
            ver_html = f'<span style="color:#4CAF50;">✓ v{self._local_version}</span>'
        elif remote_ver:
            ver_html = f'<span style="color:{tcs};">v{remote_ver}</span>'
        else:
            ver_html = ""
        title_lb = QLabel(
            f'<span style="font-size:{max(8, fs + 2)}px; font-weight:bold; color:{tc};">{name}</span>'
            f' <span style="font-size:{max(8, fs)}px;">{ver_html}</span>',
            self.widget,
        )
        title_lb.setWordWrap(True)
        title_lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title_lb.setStyleSheet(f"background: transparent; {ff_qss}")
        layout.addWidget(title_lb)

        # 完整描述
        desc = self._meta.get("description", "") or "（暂无描述）"
        desc_lb = QLabel(desc, self.widget)
        desc_lb.setWordWrap(True)
        desc_lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        desc_lb.setStyleSheet(
            f"color: {tcs}; background: transparent; {ff_qss} font-size: {max(8, fs - 1)}px; line-height: 1.5;"
        )
        layout.addWidget(desc_lb)
        layout.addSpacing(14)

        # 信息区（滚动，字段多时不撑爆）
        scroll = ScrollArea(self.widget)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent; border: none;")
        info_widget = QWidget(scroll)
        info_widget.setStyleSheet("background: transparent;")
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(2, 2, 8, 2)
        info_layout.setSpacing(8)

        rows = []
        author = self._meta.get("author", "")
        if author:
            rows.append(("👤 作者", author))
        license_ = self._meta.get("license", "")
        if license_:
            rows.append(("📜 License", license_))
        tags = self._meta.get("_cached_tags", []) or []
        if tags:
            rows.append(("🏷 标签", "，".join(tags)))
        marketplace = self._meta.get("_marketplace", "")
        if marketplace:
            rows.append(("📦 来源市场", marketplace))
        homepage = _PluginRow._compute_homepage(self._meta)
        if homepage:
            rows.append(("🔗 官网", f'<a href="{homepage}" style="color:{accent_bg};">{homepage}</a>'))
        if not rows:
            rows.append(("ℹ️ 信息", "该插件未提供更多信息"))

        for label, value in rows:
            row_lb = QLabel(f"<b>{label}</b>：{value}", info_widget)
            row_lb.setWordWrap(True)
            row_lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row_lb.setOpenExternalLinks(True)
            row_lb.setStyleSheet(
                f"color: {tc}; background: transparent; {ff_qss} font-size: {max(8, fs - 1)}px; line-height: 1.6;"
            )
            info_layout.addWidget(row_lb)

        # 已安装：展示组件内容清单（技能/智能体/命令等，滚动查看）
        if self._installed:
            info_layout.addSpacing(4)
            contents = _collect_plugin_contents(_PluginRow._find_local_plugin_path(name))
            content_rows = []
            if contents.get("skills"):
                content_rows.append(("🧩 技能", "，".join(contents["skills"])))
            if contents.get("mcp"):
                content_rows.append(("🔌 MCP", "，".join(contents["mcp"])))
            if contents.get("commands"):
                content_rows.append(("📁 命令", "，".join(contents["commands"])))
            if contents.get("agents"):
                content_rows.append(("🤖 Agents", "，".join(contents["agents"])))
            if contents.get("hooks"):
                content_rows.append(("🔗 Hooks", "，".join(contents["hooks"])))
            if contents.get("themes"):
                content_rows.append(("🎨 主题", "，".join(contents["themes"])))
            if not content_rows:
                content_rows.append(("ℹ️ 内容", "该插件未声明可展示的组件"))

            sec_lb = QLabel("<b>📦 组件内容</b>", info_widget)
            sec_lb.setStyleSheet(
                f"color: {accent_bg}; background: transparent; {ff_qss} font-size: {max(8, fs - 1)}px;"
            )
            info_layout.addWidget(sec_lb)
            for label, value in content_rows:
                row_lb = QLabel(f"<b>{label}</b>：{value}", info_widget)
                row_lb.setWordWrap(True)
                row_lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
                row_lb.setStyleSheet(
                    f"color: {tc}; background: transparent; {ff_qss} font-size: {max(8, fs - 1)}px; line-height: 1.6;"
                )
                info_layout.addWidget(row_lb)

        info_layout.addStretch()
        scroll.setWidget(info_widget)
        layout.addWidget(scroll, 1)

        layout.addSpacing(12)

        # 底部操作按钮（主按钮居中）
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        # 主操作：按状态给出（已安装且无更新 → 禁用态「已安装」）
        if self._has_update:
            main_text, main_fn = "更新", self._on_update
        elif self._installed:
            main_text, main_fn = "已安装", None
        else:
            main_text, main_fn = "安装", self._on_install

        main_btn = TransparentPushButton(main_text, self.widget)
        main_btn.setCursor(Qt.PointingHandCursor)
        main_btn.setFixedHeight(36)
        if main_fn is None:
            main_btn.setEnabled(False)
            main_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background: rgba(76, 175, 80, 0.12);
                    color: #4CAF50;
                    border: 1px solid rgba(76, 175, 80, 0.3);
                    border-radius: 8px;
                    padding: 4px 24px;
                    {ff_qss}
                    font-size: {max(8, fs - 1)}px;
                    font-weight: bold;
                }}
                """
            )
        else:
            main_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {accent_bg};
                    color: #ffffff;
                    border: none;
                    border-radius: 8px;
                    padding: 4px 24px;
                    {ff_qss}
                    font-size: {max(8, fs - 1)}px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: {accent_bg};
                }}
                """
            )
            main_btn.clicked.connect(main_fn)

        close_btn = TransparentPushButton("关闭", self.widget)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setFixedHeight(36)
        close_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: rgba(128,128,128,0.15);
                color: {tc};
                border: none;
                border-radius: 8px;
                padding: 4px 24px;
                {ff_qss}
                font-size: {max(8, fs - 1)}px;
            }}
            QPushButton:hover {{
                background: rgba(128,128,128,0.25);
            }}
            """
        )
        close_btn.clicked.connect(self.close)

        btn_layout.addStretch(1)
        btn_layout.addWidget(main_btn)
        btn_layout.addWidget(close_btn)
        btn_layout.addStretch(1)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(600, 540)

    def _on_install(self):
        self.installRequested.emit(self._meta)
        self.close()

    def _on_update(self):
        self.updateRequested.emit(self._meta)
        self.close()


class _TagFilterDialog(MaskDialogBase):
    """Tag 多选面板：toggle pill 胶囊按钮 + 流式换行（hook 编辑卡片风格）

    与搜索/筛选 AND 叠加；选中项用淡主题色高亮。
    """

    def __init__(
        self,
        parent,
        tag_counts: dict,
        active_tags: set,
        *,
        tc: str,
        tcs: str,
        ff: str,
        fs: int,
        accent_bg: str,
        card_bg: str,
        border_c: str,
    ):
        super().__init__(parent)
        self._tag_counts = tag_counts
        self._checkboxes: dict = {}
        self._init_ui(active_tags, tc, tcs, ff, fs, accent_bg, card_bg, border_c)

    def _init_ui(self, active_tags, tc, tcs, ff, fs, accent_bg, card_bg, border_c):
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("marketTagFilter")
        self.widget.setStyleSheet(
            f"""
            #marketTagFilter {{
                background-color: {card_bg};
                border: 1px solid {border_c};
                border-radius: 8px;
            }}
            """
        )

        ff_qss = f'font-family: "{ff}";' if ff else ""
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(0)

        title_lb = QLabel("按标签过滤", self.widget)
        title_lb.setStyleSheet(
            f"color: {tc}; background: transparent; {ff_qss} font-size: {max(8, fs + 2)}px; font-weight: bold;"
        )
        layout.addWidget(title_lb)

        hint_lb = QLabel("可点选多个标签，与搜索、筛选条件叠加", self.widget)
        hint_lb.setStyleSheet(f"color: {tcs}; background: transparent; {ff_qss} font-size: {max(8, fs - 1)}px;")
        layout.addWidget(hint_lb)
        layout.addSpacing(10)

        scroll = ScrollArea(self.widget)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent; border: none;")
        content = QWidget(scroll)
        content.setStyleSheet("background: transparent;")

        # toggle pill 样式（参考 hook 编辑卡片的 matcher 勾选框）
        _accent_rgba = accent_bg
        if accent_bg.startswith("#") and len(accent_bg) == 7:
            _r, _g, _b = int(accent_bg[1:3], 16), int(accent_bg[3:5], 16), int(accent_bg[5:7], 16)
            _accent_rgba = f"rgba({_r}, {_g}, {_b}, 0.15)"
        _toggle_style = (
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid {border_c};"
            f"  border-radius: 12px;"
            f"  padding: 3px 12px;"
            f"  color: {tcs};"
            f"  {ff_qss} font-size: {max(8, fs - 1)}px;"
            f"  text-align: center;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border-color: {accent_bg};"
            f"  color: {tc};"
            f"}}"
            f"QPushButton:checked {{"
            f"  background: {_accent_rgba};"
            f"  border-color: {accent_bg};"
            f"  color: {accent_bg};"
            f"  font-weight: bold;"
            f"}}"
        )

        # FlowLayout 直接作为 content 布局（不嵌套 QVBoxLayout，避免宽度计算异常）
        tags_flow = _FlowLayout(content, spacing=6)
        tags_flow.setContentsMargins(2, 4, 2, 4)
        for tag in sorted(self._tag_counts, key=lambda t: (-self._tag_counts[t], t)):
            pill = TransparentPushButton(f"{tag} ({self._tag_counts[tag]})", content)
            pill.setCheckable(True)
            pill.setCursor(Qt.PointingHandCursor)
            pill.setFixedHeight(26)
            pill.setStyleSheet(_toggle_style)
            pill.setChecked(tag in active_tags)
            tags_flow.addWidget(pill)
            self._checkboxes[tag] = pill
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        layout.addSpacing(12)
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        clear_btn = TransparentPushButton("清除", self.widget)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setFixedHeight(32)
        clear_btn.setStyleSheet(
            f"QPushButton {{ background: rgba(128,128,128,0.15); color: {tc}; border: none;"
            f" border-radius: 8px; padding: 2px 18px; {ff_qss} font-size: {max(8, fs - 1)}px; }}"
            "QPushButton:hover { background: rgba(128,128,128,0.25); }"
        )
        clear_btn.clicked.connect(lambda: [cb.setChecked(False) for cb in self._checkboxes.values()])

        ok_btn = TransparentPushButton("确定", self.widget)
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setFixedHeight(32)
        ok_btn.setStyleSheet(
            f"QPushButton {{ background-color: {accent_bg}; color: #ffffff; border: none;"
            f" border-radius: 8px; padding: 2px 24px; {ff_qss} font-size: {max(8, fs - 1)}px; font-weight: bold; }}"
        )
        ok_btn.clicked.connect(self.accept)

        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(560, 600)

    def selected_tags(self) -> list:
        """返回勾选的 tag 列表"""
        return [t for t, cb in self._checkboxes.items() if cb.isChecked()]


class _ConfirmUninstallDialog(MaskDialogBase):
    """卸载确认弹窗 — 与 plugin-manager 确认对话框风格一致"""

    def __init__(
        self,
        parent,
        name: str,
        *,
        tc: str,
        tcs: str,
        ff: str,
        fs: int,
        accent_bg: str,
        card_bg: str,
        border_c: str,
    ):
        super().__init__(parent)
        self._init_ui(name, tc, tcs, ff, fs, accent_bg, card_bg, border_c)

    def _init_ui(self, name, tc, tcs, ff, fs, accent_bg, card_bg, border_c):
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("confirmUninstall")
        self.widget.setStyleSheet(
            f"""
            #confirmUninstall {{
                background-color: {card_bg};
                border: 1px solid {border_c};
                border-radius: 8px;
            }}
            """
        )

        ff_qss = f'font-family: "{ff}";' if ff else ""
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(0)

        title_lb = BodyLabel("确认卸载", self.widget)
        title_lb.setStyleSheet(
            f"color: {tc}; background: transparent; {ff_qss} font-size: {max(8, fs + 2)}px; font-weight: bold;"
        )
        layout.addWidget(title_lb)
        layout.addSpacing(6)

        content_lb = BodyLabel(
            f"确定要卸载插件「{name}」吗？\n此操作不可恢复。",
            self.widget,
        )
        content_lb.setWordWrap(True)
        content_lb.setStyleSheet(
            f"color: {tcs}; background: transparent; {ff_qss} font-size: {max(8, fs - 1)}px; line-height: 1.6;"
        )
        layout.addWidget(content_lb)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        cancel_btn = TransparentPushButton("取消", self.widget)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedHeight(36)
        cancel_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {card_bg};
                color: {tc};
                border: 1px solid {border_c};
                border-radius: 8px;
                padding: 4px 28px;
                {ff_qss}
                font-size: {max(8, fs - 1)}px;
            }}
            QPushButton:hover {{
                background: rgba(128,128,128,0.15);
            }}
            """
        )
        cancel_btn.setDefault(True)
        cancel_btn.clicked.connect(self._on_cancel)

        confirm_btn = TransparentPushButton("卸载", self.widget)
        confirm_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn.setFixedHeight(36)
        confirm_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {accent_bg};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 4px 28px;
                {ff_qss}
                font-size: {max(8, fs - 1)}px;
                font-weight: bold;
            }}
            """
        )
        confirm_btn.clicked.connect(self._on_confirm)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(400, 200)

    def _on_confirm(self):
        """确认卸载：accept() 使 exec_() 返回 1（close() 会被 QDialog 转为 reject）"""
        self.accept()

    def _on_cancel(self):
        self.reject()


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
    openUrlRequested = pyqtSignal(str)  # 打开插件官网 URL
    openDirRequested = pyqtSignal(str)  # 打开插件所在本地目录
    detailRequested = pyqtSignal(dict)  # 打开插件详情面板
    enableRequested = pyqtSignal(dict)  # 启用已禁用插件
    disableRequested = pyqtSignal(dict)  # 禁用已启用插件
    uninstallRequested = pyqtSignal(dict)  # 卸载插件

    def __init__(
        self,
        plugin_meta: dict,
        installed: bool,
        has_update: bool = False,
        local_version: Optional[str] = None,
        status: str = "",
        parent=None,
        font_size: int = 0,
        search_query: str = "",
        tc: Optional[str] = None,
        tcs: Optional[str] = None,
        font_family: str = "",
    ):
        super().__init__(parent)
        self._meta = plugin_meta
        self._installed = installed
        self._has_update = has_update
        self._local_version = local_version
        self._status = status  # "enabled" | "disabled" | "system" | ""（未安装）
        self._busy = False
        self._font_size = font_size  # 上下文字体大小（用于头像自适应 + 行内字号派生）
        self._ff = font_family  # 上下文字体家族
        self._btn_font_size = max(13, font_size) if font_size > 0 else 14
        self._avatar = None
        self._search_query = search_query
        # 上下文主题色（无缓存时回退全局主题），避免依赖事后 _retheme 全树遍历
        self._tc = tc or _text_color()
        self._tcs = tcs or _text_color(secondary=True)
        self._tags = self._compute_tags(plugin_meta)
        self._setup_ui()

    # ── 字号派生（与卡片 _PLUGIN_ROW_SIZE_OFFSETS 保持一致） ──
    # 描述 fs-4、tag fs-5、更新标签 fs-3、市场标签 fs+0；无上下文时回退固定值

    def _derive_size(self, base_px: int, offset: int) -> int:
        """按上下文字体大小派生行内标签字号（无上下文时用固定 base_px）"""
        if self._font_size > 0:
            return max(8, self._font_size + offset)
        return base_px

    def _font_qss(self, size_px: int) -> str:
        """生成 font-size + font-family 的 QSS 片段"""
        qss = f"font-size: {size_px}px;"
        if self._ff:
            qss += f" font-family: '{self._ff}';"
        return qss

    def _apply_child_fonts(self):
        """字号/字体变化时更新行内各标签（替代原 _retheme 全树遍历）"""
        if self._desc_label is not None:
            self._desc_label.setStyleSheet(
                f"color: {self._tcs}; {self._font_qss(self._derive_size(12, -4))} background: transparent;"
            )
        for lbl in self._tag_labels:
            lbl.setStyleSheet(self._tag_stylesheet())
        if getattr(self, "_mp_label", None) is not None:
            self._mp_label.setStyleSheet(
                f"color: {self._tcs}; {self._font_qss(self._derive_size(10, 0))} background: transparent;"
            )

    # ── 状态标签（启用/禁用/系统） ──────────────────────────

    def _status_text(self) -> str:
        """按安装状态生成状态标签文本"""
        if self._status == "disabled":
            return "⛔ 已禁用"
        if self._status == "system":
            return "🔒 系统插件"
        if self._status == "enabled":
            return "✅ 已启用"
        return ""

    def _status_color(self) -> str:
        """状态标签颜色"""
        if self._status == "disabled":
            return "#FF9800"
        if self._status == "system":
            return "#2196F3"
        if self._status == "enabled":
            return "#4CAF50"
        return self._tcs

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
        self._name_raw = name
        self._title_label = QLabel("", self)
        self._title_label.setObjectName("pluginRowTitle")
        ff_qss = f" font-family: '{self._ff}';" if self._ff else ""
        self._title_label.setStyleSheet(f"color: {self._tc}; font-weight: bold;{ff_qss} background: transparent;")
        self._refresh_title()
        info_layout.addWidget(self._title_label)

        # 版本更新徽标已内嵌在标题（🔄 v1.0 → v2.0），不再单独建标签

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
                f"color: {self._tcs}; {self._font_qss(self._derive_size(12, -4))} background: transparent;"
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

        # 元信息行：市场来源（状态标签已并入标题版本号后）
        meta_row = QWidget(self)
        meta_row.setStyleSheet("background: transparent;")
        meta_layout = QHBoxLayout(meta_row)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(10)

        marketplace = self._meta.get("_marketplace", "")
        self._mp_label = None
        if marketplace:
            self._mp_label = QLabel(f"📦 {marketplace}", meta_row)
            self._mp_label.setStyleSheet(
                f"color: {self._tcs}; {self._font_qss(self._derive_size(10, 0))} background: transparent;"
            )
            meta_layout.addWidget(self._mp_label)

        if marketplace:
            meta_layout.addStretch(1)
            info_layout.addWidget(meta_row)

        layout.addLayout(info_layout, 1)

        # 图标按钮列（目录/官网/信息）：置顶排列，不垂直居中
        icon_col = QVBoxLayout()
        icon_col.setSpacing(4)

        # 打开插件所在文件夹按钮（仅已安装时可见，未安装无本地目录）
        self._dir_btn = None
        local_path = self._find_local_plugin_path(self._meta.get("name", ""))
        if local_path:
            self._dir_btn = TransparentToolButton(FluentIcon.FOLDER, self)
            self._dir_btn.setFixedSize(28, 28)
            self._dir_btn.setToolTip(f"打开插件所在文件夹 {local_path}")
            self._dir_btn.clicked.connect(lambda checked, p=local_path: self.openDirRequested.emit(str(p)))
            self._dir_btn.setVisible(self._installed)
            icon_col.addWidget(self._dir_btn)

        # 官网链接按钮（仅未安装时显示；已安装可在详情弹窗查看官网）
        homepage = self._compute_homepage(self._meta)
        if homepage and not self._installed:
            link_btn = TransparentToolButton(FluentIcon.LINK, self)
            link_btn.setFixedSize(28, 28)
            link_btn.setToolTip(f"打开官网 {homepage}")
            link_btn.clicked.connect(lambda checked, u=homepage: self.openUrlRequested.emit(u))
            icon_col.addWidget(link_btn)

        # 详情按钮（未安装也能查看完整信息）
        detail_btn = TransparentToolButton(FluentIcon.INFO, self)
        detail_btn.setFixedSize(28, 28)
        detail_btn.setToolTip("查看详情")
        detail_btn.clicked.connect(lambda checked, m=self._meta: self.detailRequested.emit(m))
        icon_col.addWidget(detail_btn)

        icon_col.addStretch(1)
        layout.addLayout(icon_col)

        # 操作列：主按钮在上，管理按钮（启用/禁用/卸载）竖排在下
        action_col = QVBoxLayout()
        action_col.setSpacing(4)

        # 管理按钮区（启用/禁用/卸载，已安装用户插件时显示）
        self._manage_layout = QVBoxLayout()
        self._manage_layout.setSpacing(4)

        # 操作按钮（查看/安装/更新）
        self._btn = TransparentPushButton(self)
        self._btn.setFixedSize(100, 30)
        # 保存 FluentUI 默认样式，仅追加 font-size 不改其他
        self._original_btn_style = self._btn.styleSheet()
        self._update_btn_text()
        self._btn.clicked.connect(self._on_click)
        action_col.addWidget(self._btn)

        action_col.addLayout(self._manage_layout)
        action_col.addStretch(1)

        layout.addLayout(action_col)

    def _update_btn_text(self):

        fs = self._btn_font_size
        btn_font = self._btn.font()
        btn_font.setPixelSize(fs)
        self._btn.setFont(btn_font)

        if self._busy:
            self._btn.setText(getattr(self, "_busy_text", "处理中…"))
            self._btn.setEnabled(False)
            self._btn.setStyleSheet(self._original_btn_style)
            self._btn.setVisible(True)
        elif self._has_update:
            self._btn.setText("更新")
            self._btn.setEnabled(True)
            self._btn.setStyleSheet(
                "PushButton { background: rgba(255, 167, 38, 0.2); "
                "color: #FFA726; border: 1px solid rgba(255, 167, 38, 0.3); "
                "border-radius: 4px; }"
                "PushButton:hover { background: rgba(255, 167, 38, 0.35); }"
            )
            self._btn.setVisible(True)
        elif self._installed:
            # 已安装且无更新：主按钮隐藏（左侧详情按钮已可查看完整信息）
            self._btn.setVisible(False)
        else:
            self._btn.setText("安装")
            self._btn.setEnabled(True)
            # 与管理按钮（禁用/卸载）统一描边风格，不再用 Fluent 默认透明样式
            self._btn.setStyleSheet(self._outline_btn_style("#2196F3"))
            self._btn.setVisible(True)
        self._update_manage_buttons()

    # ── 管理按钮（启用/禁用/卸载，行内直接操作） ─────────────

    def _outline_btn_style(self, color: str) -> str:
        """描边按钮样式（安装/启用/禁用/卸载统一）：同色边框+彩色文字，hover 浅色底"""
        r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
        return (
            f"PushButton {{ color: {color}; border: 1px solid {color};"
            " border-radius: 4px; padding: 2px 6px; background: transparent; }"
            f"PushButton:hover {{ background: rgba({r},{g},{b},0.12); }}"
        )

    def _make_manage_btn(self, text: str, color: str, slot) -> TransparentPushButton:
        """创建紧凑管理按钮（与主按钮同宽同高，竖排对齐）"""
        btn = TransparentPushButton(text, self)
        btn.setFixedSize(100, 30)
        btn.setStyleSheet(
            self._outline_btn_style(color)
            + f" PushButton {{ font-size: {max(10, self._btn_font_size - 2)}px; }}"
        )
        btn.clicked.connect(slot)
        return btn

    def _update_manage_buttons(self):
        """按安装状态刷新管理按钮（系统/未安装隐藏；禁用→启用；启用→禁用）"""
        while self._manage_layout.count():
            item = self._manage_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._busy or not self._installed:
            return
        if self._status == "system":
            # 系统插件只读，行内不提供管理操作（详情弹窗同样无）
            return

        if self._status == "disabled":
            self._manage_layout.addWidget(self._make_manage_btn("启用", "#4CAF50", self._on_enable))
        else:
            self._manage_layout.addWidget(self._make_manage_btn("禁用", "#FF9800", self._on_disable))
        self._manage_layout.addWidget(self._make_manage_btn("卸载", "#F44336", self._on_uninstall))

    def _on_enable(self):
        self.enableRequested.emit(self._meta)

    def _on_disable(self):
        self.disableRequested.emit(self._meta)

    def _on_uninstall(self):
        self.uninstallRequested.emit(self._meta)

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
        else:
            # 已安装且无新版 → 打开详情弹窗（查看内容等）
            self.detailRequested.emit(self._meta)

    def set_installed(self, installed: bool):
        """安装/卸载完成后刷新状态（清除更新标记）"""
        self.apply_state(installed, has_update=False, local_version=None, status="enabled" if installed else "")

    def set_has_update(self, has_update: bool):
        """设置是否有可用更新"""
        self._has_update = has_update
        self._update_btn_text()

    def apply_state(self, installed: bool, has_update: bool, local_version: Optional[str], status: str = ""):
        """按最新扫描结果刷新整行状态（版本后缀 + 按钮 + 文件夹按钮）"""
        changed = (
            installed != self._installed
            or has_update != self._has_update
            or local_version != self._local_version
            or status != self._status
        )
        self._installed = installed
        self._has_update = has_update
        self._local_version = local_version
        self._status = status
        self._busy = False
        if changed:
            self._refresh_title()
        # 文件夹按钮仅已安装时可见
        if self._dir_btn is not None:
            self._dir_btn.setVisible(installed)
        self._update_btn_text()

    def set_downloading(self):
        """更新流程：点击后立即标记为「下载中」（未安装态 + 禁用按钮）"""
        self._installed = False
        self._has_update = False
        self._status = ""
        self._busy = True
        self._busy_text = "下载中…"
        if self._dir_btn is not None:
            self._dir_btn.setVisible(False)
        self._update_btn_text()

    def _refresh_title(self):
        """按当前状态重算标题：名称 + 彩色版本徽标 + 状态标签

        - 有更新：🔄 v{local} → v{remote}（橙色）
        - 已安装且最新：✓ v{local}（绿色）
        - 未安装：v{remote}（次级色）
        - 已安装：追加状态标签（✅ 已启用 / ⛔ 已禁用 / 🔒 系统插件）
        """
        remote_ver = self._meta.get("version", "")
        ver_html = ""
        if self._has_update and self._local_version and remote_ver:
            ver_html = f' <span style="color:#FFA726;">🔄 v{self._local_version} → v{remote_ver}</span>'
        elif self._installed and self._local_version:
            ver_html = f' <span style="color:#4CAF50;">✓ v{self._local_version}</span>'
        elif remote_ver:
            ver_html = f' <span style="color:{self._tcs};">v{remote_ver}</span>'
        # 状态标签：版本号之后
        status_html = ""
        if self._installed and not self._busy:
            st = self._status_text()
            if st:
                status_html = f' <span style="color:{self._status_color()}; font-weight:bold;">{st}</span>'
        self._version_suffix = ver_html + status_html
        title_fs = max(9, self._font_size - 2) if self._font_size > 0 else 13
        name_html = _highlight_html(self._name_raw, self._search_query)
        self._title_label.setText(f'<span style="font-size:{title_fs}pt;">{name_html}{ver_html}{status_html}</span>')

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
        """根据上下文字体大小动态调整头像尺寸 + 行内标签字号"""
        if font_size <= 0:
            return
        self._font_size = font_size
        if self._avatar is not None and hasattr(self._avatar, "set_font_size"):
            self._avatar.set_font_size(font_size)
        self._refresh_title()  # 标题 HTML 内嵌字号
        self._apply_child_fonts()  # 描述/tag/更新标签/市场标签 QSS 字号

    # ── Tag 相关 ─────────────────────────────────────────────

    @staticmethod
    def _compute_tags(meta: dict) -> list:
        """合并 categories / category / keywords 为展示标签（去重）"""
        seen: set = set()
        tags: list = []
        # categories（数组）
        for c in meta.get("categories", []) or []:
            if c and c not in seen:
                tags.append(c)
                seen.add(c)
        # category（单数字段，如 "productivity"）
        cat = meta.get("category")
        if isinstance(cat, str) and cat and cat not in seen:
            tags.append(cat)
            seen.add(cat)
        # keywords（数组）
        for k in meta.get("keywords", []) or []:
            if k and k not in seen:
                tags.append(k)
                seen.add(k)
        return tags

    # ── 官网链接 ─────────────────────────────────────────────

    @staticmethod
    def _compute_homepage(meta: dict) -> str:
        """解析插件官网 URL

        优先级：homepage / website / url 字段 → source.repo（github 主页）
        → source.url（git-subdir / url 类型，raw 地址转仓库主页）
        """
        for key in ("homepage", "website", "url"):
            v = meta.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
        source = meta.get("source", {}) or {}
        if not isinstance(source, dict):
            return ""
        repo = source.get("repo")
        if isinstance(repo, str) and repo:
            return f"https://github.com/{repo}"
        u = source.get("url", "")
        if isinstance(u, str) and u:
            # raw URL 转成仓库主页
            if "raw.githubusercontent.com" in u:
                parts = u.replace("https://raw.githubusercontent.com/", "").split("/")
                if len(parts) >= 3:
                    return f"https://github.com/{parts[0]}/{parts[1]}"
            return u.replace(".git", "")
        return ""

    def _tag_stylesheet(self) -> str:
        """tag 标签 QSS 样式（字号跟随上下文派生）"""
        return (
            f"background: rgba(128,128,128,0.12); color: {self._tcs}; border-radius: 4px; padding: 1px 6px; "
            f"{self._font_qss(self._derive_size(10, -5))}"
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
        self._matched: list = []
        self._rendered_count: int = 0
        self._row_map: dict = {}  # plugin_name → _PluginRow
        self._all_plugins: list = []
        self._all_loaded: bool = False  # 匹配列表是否已全部渲染完
        self._current_filter: str = "all"
        self._active_tags: set = set()  # 激活的 tag 集合（AND 过滤）
        self._source_filter: str = ""  # 市场来源过滤（"" = 全部）
        self._sort_mode: str = "default"
        self._load_more_btn: Optional[QPushButton] = None
        self._header_icon: Optional[IconWidget] = None
        self._setup_ui()
        # 首次显示时由 show_card 触发加载，__init__ 不再自动加载

    # ── 拉模型上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）"""
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：用最新上下文刷新主题色 + 延迟加载数据

        策略：
        1. 先渲染本地已安装插件（不依赖远程，立即可见）
        2. 再后台逐市场拉取远程，每到一个市场增量刷新
        """
        self.setVisible(True)
        self._apply_latest_theme()
        self._apply_plugin_icon()
        # 清除旧搜索状态、防抖定时器
        self._search_edit.clear()
        self._search_debounce.stop()
        # 延迟 50ms 启动加载，避免阻塞 show 过程
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(50, self._start_load)

    def _start_load(self):
        """本地已安装先行渲染 + 后台逐市场拉取"""
        self._render_local_installed()
        self._async_refresh()

    def _render_local_installed(self):
        """仅用本地扫描数据渲染「已安装」视图（远程未就绪也可用）

        逻辑：把 _all_plugins 置空，走 _render_plugins 的本地并入分支，
        「已安装」筛选下 _build_local_extra_plugins() 会产出全部本地插件。
        """
        inst = get_installer()
        inst_map = inst.get_installed_map()
        self._installed_set = set(inst_map)
        self._version_map = inst_map
        self._status_map = inst.get_status_map()
        self._all_plugins = []
        self._plugin_data = []
        self._render_plugins(self._plugin_data)

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
        - 主题 key（颜色/字体/字号）未变化时跳过 _retheme 全树遍历（打开卡片/切 tab 不卡）
        - 字号变化仍传播到已渲染行（头像自适应）
        - font-family 通过 self.setFont(QFont(family, 0)) 级联（size=0 不覆盖原有字号）
        - 动态创建的子控件创建时直接用缓存主题色，无需事后 _retheme
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

        theme_key = (tc, tcs, font_family, font_size)
        theme_changed = getattr(self, "_last_theme_key", None) != theme_key
        self._last_theme_key = theme_key

        self._cached_tc = tc
        self._cached_tcs = tcs
        self._cached_font_family = font_family
        self._cached_font_size = font_size
        self._cached_theme_colors = ctx.get("colors", {})

        # ── 字号变化 → 传播到已存在的行（动态调整头像大小） ──
        if font_size != getattr(self, "_last_font_size", None):
            for row in self.findChildren(_PluginRow):
                row.set_font_size(font_size)
            self._last_font_size = font_size

        # ── 字体（通过 QFont 级联，使用系统字体大小） ──
        if font_family:
            self.setFont(QFont(font_family, font_size if font_size else 14))

        # 主题真变化时才全树 re-theme + 更新搜索框/分隔线（日常打开/切换不触发）
        if theme_changed:
            self._retheme()
            try:
                self._search_edit.setStyleSheet(
                    f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {tc};"
                )
            except RuntimeError:
                pass
            self._style_sort_combo()
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
        "pluginRowUpdateTag": -3,
        "marketRowName": -1,  # 市场行名称 18px → fs-1
        "marketRowUrl": -2,  # 市场行 URL   14px → fs-2
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

        # QPushButton 字体（"加载更多"按钮等）
        for child in self.findChildren(QPushButton):
            try:
                cur = child.styleSheet()
                btn_fs = max(fs - 2, 11)
                style_extra = f" font-size: {btn_fs}px;"
                # 仅在上下文提供字体家族时追加，否则保持系统默认字体
                if ff:
                    style_extra += f" font-family: '{ff}';"
                child.setStyleSheet(cur + style_extra)
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

        # ── 筛选行：Pivot + 待更新角标 + 排序 ──
        filter_row = QWidget(self._browse_page)
        filter_row.setStyleSheet("background: transparent;")
        filter_layout = QHBoxLayout(filter_row)
        filter_layout.setContentsMargins(12, 2, 12, 0)
        filter_layout.setSpacing(8)

        self._filter_bar = Pivot(filter_row)
        self._filter_bar.addItem("all", "全部", None, None)
        self._filter_bar.addItem("installed", "已安装", None, None)
        self._filter_bar.addItem("uninstalled", "未安装", None, None)
        self._updates_item = self._filter_bar.addItem("updates", "待更新", None, None)
        self._filter_bar.setCurrentItem("all")
        self._filter_bar.currentItemChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._filter_bar)

        filter_layout.addStretch(1)

        # 待更新 InfoBadge：挂在「待更新」tab 右上角（无更新时隐藏）
        from qfluentwidgets import InfoBadge, InfoBadgeManager, InfoBadgePosition

        self._updates_badge = InfoBadge.info("0", self._updates_item)
        self._updates_badge.setVisible(False)
        try:
            InfoBadgeManager.make(InfoBadgePosition.TOP_RIGHT, self._updates_item, self._updates_badge)
        except Exception as e:
            logger.warning(f"[Marketplace] InfoBadge 挂载失败: {e}")

        # 搜索框（排序下拉左侧）
        self._search_edit = LineEdit(filter_row)
        self._search_edit.setPlaceholderText("搜索插件…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setFixedWidth(160)
        self._search_edit.setStyleSheet(
            f"background: rgba(128,128,128,0.1); border-radius: 8px; padding: 4px 8px; color: {_text_color()};"
        )
        # 防抖 300ms，避免每敲一个字就全量重建
        from PyQt5.QtCore import QTimer

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self._filter_plugins)
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        filter_layout.addWidget(self._search_edit)

        # 排序下拉
        self._sort_combo = QComboBox(filter_row)
        self._sort_combo.addItem("默认排序", "default")
        self._sort_combo.addItem("名称 A-Z", "name_asc")
        self._sort_combo.addItem("名称 Z-A", "name_desc")
        self._sort_combo.addItem("版本最新优先", "version")
        self._sort_combo.setFixedWidth(110)
        # 与搜索框同高（LineEdit 视觉高度 33px）
        self._sort_combo.setFixedHeight(33)
        self._style_sort_combo()
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        filter_layout.addWidget(self._sort_combo)

        browse_root.addWidget(filter_row)

        # ── 市场来源过滤栏（横向滚动，数据加载后构建）──
        # ── 市场来源过滤行（来源: 标签 + 横向滚动按钮）──
        source_row = QWidget(self._browse_page)
        source_row.setStyleSheet("background: transparent;")
        source_row_layout = QHBoxLayout(source_row)
        source_row_layout.setContentsMargins(12, 0, 12, 0)
        source_row_layout.setSpacing(6)

        self._source_label = QLabel("来源:", source_row)
        self._source_label.setFixedWidth(44)
        self._source_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._source_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 11px; background: transparent;"
        )
        source_row_layout.addWidget(self._source_label)

        self._source_bar = ScrollArea(source_row)
        self._source_bar.setWidgetResizable(True)
        self._source_bar.setFixedHeight(34)
        self._source_bar.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._source_bar.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._source_bar.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._source_content = QWidget(self._source_bar)
        self._source_content.setStyleSheet("background: transparent;")
        self._source_layout = QHBoxLayout(self._source_content)
        self._source_layout.setContentsMargins(0, 0, 0, 2)
        self._source_layout.setSpacing(6)
        self._source_bar.setWidget(self._source_content)
        source_row_layout.addWidget(self._source_bar, 1)
        browse_root.addWidget(source_row)
        source_row.setVisible(False)  # 无数据时隐藏

        # ── Tag 过滤行（类型: 标签 + 横向滚动按钮）──
        tag_row = QWidget(self._browse_page)
        tag_row.setStyleSheet("background: transparent;")
        tag_row_layout = QHBoxLayout(tag_row)
        tag_row_layout.setContentsMargins(12, 0, 12, 0)
        tag_row_layout.setSpacing(6)

        self._tag_label = QLabel("类型:", tag_row)
        self._tag_label.setFixedWidth(44)
        self._tag_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._tag_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 11px; background: transparent;"
        )
        tag_row_layout.addWidget(self._tag_label)

        self._tag_bar = ScrollArea(tag_row)
        self._tag_bar.setWidgetResizable(True)
        self._tag_bar.setFixedHeight(38)
        self._tag_bar.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._tag_bar.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tag_bar.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._tag_content = QWidget(self._tag_bar)
        self._tag_content.setStyleSheet("background: transparent;")
        self._tag_layout = QHBoxLayout(self._tag_content)
        self._tag_layout.setContentsMargins(0, 0, 0, 2)
        self._tag_layout.setSpacing(6)
        self._tag_bar.setWidget(self._tag_content)
        tag_row_layout.addWidget(self._tag_bar, 1)
        browse_root.addWidget(tag_row)
        tag_row.setVisible(False)  # 无 tag 数据时隐藏

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

        add_btn = TransparentPushButton("添加", add_row)
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
        """后台逐市场拉取，每到一个市场增量合并渲染

        Args:
            force: 是否强制拉取远程（跳过缓存）
        """
        self._set_loading(True)
        self._cleanup_worker()
        self._worker = _MarketFetchWorker(force=force)
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.market_fetched.connect(self._on_market_fetched)
        self._worker.all_done.connect(self._on_market_all_done)
        self._worker.error.connect(self._on_refresh_error)
        # 全部市场拉完（或出错）后才退出线程并清理
        self._worker.all_done.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.all_done.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_market_fetched(self, market_data: dict):
        """单个市场拉取完成：合并进全量数据并增量渲染"""
        market_name = market_data.get("name", "")
        plugins = market_data.get("plugins", []) or []
        # 合并：移除该市场旧数据，追加新数据（同名插件按市场覆盖）
        merged = [p for p in self._plugin_data if p.get("_marketplace") != market_name]
        merged.extend(plugins)
        self._plugin_data = merged
        # 非「已安装」视图也保留本地插件并入（远程数据到达后同样生效）
        self._render_plugins(self._plugin_data)

    def _on_market_all_done(self):
        """全部市场拉取完成"""
        self._set_loading(False)
        # 远程全部失败时，本地已安装视图仍保留（不清空）
        if not self._plugin_data:
            self._status_label.setText("远程市场不可用")
            self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")

    def _on_refresh_error(self, err: str):
        """拉取出错：不打断本地视图，仅记录"""
        logger.warning(f"[Marketplace] 市场拉取错误: {err[:120]}")

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
        """渲染插件列表：匹配才渲染 + 分段加载

        - 条件（搜索 + 激活 tag + 筛选模式 + 排序）变化 → 计算匹配列表
        - 同步渲染前 _RENDER_BATCH 个匹配行 + 「加载更多」按钮（手动点击分批）
        - 不匹配的行不创建 widget（插件多时省内存）
        - 数据内容变化时重建 tag 栏与待更新角标
        """
        query = self._search_edit.text().strip().lower()
        filter_mode = self._current_filter

        # 缓存 tags（避免重复计算）
        for p in plugins:
            if "_cached_tags" not in p:
                p["_cached_tags"] = _PluginRow._compute_tags(p)

        # 批量安装状态：一次 scandir + manifest 读取（TTL 缓存）
        inst_map = get_installer().get_installed_map()
        self._installed_set = set(inst_map)
        self._version_map = inst_map
        self._status_map = get_installer().get_status_map()

        # 数据内容变化 → 更新全量数据 + 重建 tag 栏 / 来源栏
        data_changed = not self._all_plugins or not self._plugins_same(self._all_plugins, plugins)
        if data_changed:
            self._all_plugins = plugins
            self._rebuild_tag_bar()
            self._rebuild_source_bar()

        # 「已安装」视图：并入市场列表外的本地插件（系统/禁用/手动安装）
        # 任何筛选下都并入，由 _plugin_matches 按 filter_mode 过滤（uninstalled 会剔除已装）
        view_plugins = list(self._all_plugins) + self._build_local_extra_plugins()

        # 计算匹配列表（搜索 + tag + 筛选）并排序
        matched = [p for p in view_plugins if self._plugin_matches(p, query, filter_mode)]
        self._apply_sort(matched)
        self._matched = matched

        # 重建渲染：清空 + 渲染首批 + 加载更多按钮
        self._clear_plugin_list()
        self._rendered_count = 0
        self._all_loaded = False
        self._update_empty_state()
        self._update_status()
        self._update_update_badge()
        self._render_next_batch()

    @staticmethod
    def _plugins_same(old: list, new: list) -> bool:
        """判断两次拉取的市场数据是否内容相同（name+version 逐项比较）

        拉取总是新建 dict/list（json 反序列化），引用必然不同；
        内容相同时跳过 tag 栏/角标重建。
        """
        if old is new:
            return True
        if len(old) != len(new):
            return False
        for a, b in zip(old, new):
            if a.get("name") != b.get("name") or a.get("version") != b.get("version"):
                return False
        return True

    def _has_update(self, p: dict, installed: bool) -> bool:
        """判断插件是否有可用更新（本地版本 < 远程版本）"""
        if not installed:
            return False
        local_ver = self._version_map.get(p.get("name", ""))
        remote_ver = p.get("version", "")
        if not local_ver or not remote_ver:
            return False
        from .data import compare_versions

        return compare_versions(local_ver, remote_ver) < 0

    def _row_state(self, p: dict) -> tuple:
        """计算行的 (installed, has_update, local_ver, status)"""
        name = p.get("name", "")
        installed = name in self._installed_set
        status = self._status_map.get(name, "")
        return installed, self._has_update(p, installed), self._version_map.get(name), status

    def _build_local_extra_plugins(self) -> list:
        """构建市场列表外的本地插件条目（系统/禁用/手动安装）

        把不在市场数据中、但本地已安装的插件补进列表，
        让用户能查看/启用/禁用/卸载它们（plugin-manager 能力）。

        缓存：以 (installer 状态 map 引用, 市场名集合) 为 key——
        状态 map TTL 内复用 + 市场数据不变时复用；任一变化自动重建。
        """
        sm = get_installer().get_status_map()
        market_names = frozenset(p.get("name", "") for p in self._all_plugins)
        cache_key = (sm, market_names)
        if getattr(self, "_local_extras_key", None) == cache_key:
            return getattr(self, "_local_extras_cache", [])

        extras = []
        for name, status in sm.items():
            if name in market_names:
                continue
            meta = self._build_local_meta(name, status)
            if meta is not None:
                extras.append(meta)
        self._local_extras_key = cache_key
        self._local_extras_cache = extras
        return extras

    def _build_local_meta(self, name: str, status: str) -> Optional[dict]:
        """从本地插件目录读取 manifest，构造市场行兼容的 meta dict

        Returns:
            None 表示本地目录读取失败（跳过该插件）
        """
        path = _PluginRow._find_local_plugin_path(name)
        if path is None:
            return None
        meta: dict = {
            "name": name,
            "description": "",
            "version": self._version_map.get(name) or "",
            "author": "",
            "license": "",
            "categories": [],
            "keywords": [],
            "homepage": "",
            "_local_only": True,
            "_status": status,
            "_marketplace": "本地",
        }
        try:
            import json as _json

            for _meta_dir in (".drifox-plugin", ".claude-plugin"):
                _mp = path / _meta_dir / "plugin.json"
                if _mp.exists():
                    m = _json.loads(_mp.read_text(encoding="utf-8"))
                    meta["description"] = m.get("description", "")
                    meta["author"] = (
                        m.get("author", {}).get("name", "")
                        if isinstance(m.get("author"), dict)
                        else m.get("author", "")
                    )
                    meta["license"] = m.get("license", "")
                    meta["categories"] = m.get("categories", []) or []
                    meta["keywords"] = m.get("keywords", []) or []
                    meta["homepage"] = m.get("homepage", "")
                    break
        except Exception:
            pass
        meta["_cached_tags"] = _PluginRow._compute_tags(meta)
        return meta

    def _plugin_matches(self, p: dict, query: str, filter_mode: str) -> bool:
        """检查插件是否匹配当前搜索/tag/筛选（AND 叠加）

        Returns: bool
        """
        name = p.get("name", "")

        if query:
            if query not in name.lower() and query not in (p.get("description", "")).lower():
                tags = p.get("_cached_tags", [])
                if not any(query in t.lower() for t in tags):
                    return False

        # 激活 tag 过滤（AND）
        if self._active_tags:
            tags = set(p.get("_cached_tags", []) or [])
            if not (tags & self._active_tags):
                return False

        # 市场来源过滤
        source_filter = getattr(self, "_source_filter", "")
        if source_filter and p.get("_marketplace", "") != source_filter:
            return False

        installed = name in self._installed_set
        has_update = self._has_update(p, installed)

        if filter_mode == "installed" and not installed:
            return False
        if filter_mode == "uninstalled" and installed:
            return False
        if filter_mode == "updates" and not has_update:
            return False
        return True

    def _apply_sort(self, matched: list):
        """按当前排序模式排序匹配列表（就地）"""
        mode = getattr(self, "_sort_mode", "default")
        if mode == "name_asc":
            matched.sort(key=lambda p: (p.get("name", "") or "").lower())
        elif mode == "name_desc":
            matched.sort(key=lambda p: (p.get("name", "") or "").lower(), reverse=True)
        elif mode == "version":
            from functools import cmp_to_key

            from .data import compare_versions

            matched.sort(
                key=cmp_to_key(lambda a, b: compare_versions(a.get("version", "0"), b.get("version", "0"))),
                reverse=True,
            )

    def _render_next_batch(self):
        """渲染下一批匹配插件（同步，每批 _RENDER_BATCH 个足够快，停住等手动加载）

        点击「加载更多」时只推进一批，不自动跑完全部。
        """
        start = self._rendered_count
        end = min(start + self._RENDER_BATCH, len(self._matched))
        if start >= end:
            self._all_loaded = True
            self._remove_load_more_button()
            return
        self._render_batch(start, end)
        self._rendered_count = end
        if self._rendered_count < len(self._matched):
            self._set_load_more_button(len(self._matched) - self._rendered_count)
        else:
            self._all_loaded = True
            self._remove_load_more_button()

    def _set_load_more_button(self, remaining: int):
        """创建或更新「加载更多」按钮（stretch 之前）"""
        btn = getattr(self, "_load_more_btn", None)
        if btn is None:
            self._remove_load_more_button()
            btn = TransparentPushButton(self._content)
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
            self._load_more_btn = btn
        btn.setText(f"加载更多 ({remaining} 个)")
        btn.show()

    def _remove_load_more_button(self):
        """移除「加载更多」按钮"""
        btn = getattr(self, "_load_more_btn", None)
        if btn is not None:
            try:
                self._content_layout.removeWidget(btn)
                btn.deleteLater()
            except RuntimeError:
                pass
            self._load_more_btn = None

    def _on_load_more(self):
        """手动加载下一批匹配插件（点一次只加载一批，不自动跑完）

        新行插入按钮之前，按钮随内容自然下移到列表末尾；
        不滚动视口，滚动位置保持用户原样。
        """
        self._render_next_batch()

    def _render_batch(self, start: int, end: int):
        """渲染 [start, end) 范围的匹配插件行"""
        fs = getattr(self, "_cached_font_size", 0)
        tc = getattr(self, "_cached_tc", None)
        tcs = getattr(self, "_cached_tcs", None)
        ff = getattr(self, "_cached_font_family", "") or ""
        query = self._search_edit.text().strip().lower()
        for i in range(start, end):
            try:
                p = self._matched[i]
                installed, has_update, local_ver, status = self._row_state(p)
                row = _PluginRow(
                    p,
                    installed,
                    has_update=has_update,
                    local_version=local_ver,
                    status=status,
                    parent=self._content,
                    font_size=fs,
                    search_query=query,
                    tc=tc,
                    tcs=tcs,
                    font_family=ff,
                )
            except Exception as e:
                # 单行渲染失败（异常市场数据）不中断整个批次
                logger.warning(f"[Marketplace] 插件行渲染失败: {e}")
                continue
            row.installRequested.connect(self._async_install)
            row.updateRequested.connect(self._async_update)
            row.openUrlRequested.connect(self._open_url)
            row.openDirRequested.connect(self._on_open_plugin_dir)
            row.detailRequested.connect(self._on_plugin_detail)
            row.enableRequested.connect(self._async_enable)
            row.disableRequested.connect(self._async_disable)
            row.uninstallRequested.connect(self._async_uninstall)
            # 新行插入「加载更多」按钮之前：按钮随新行自然下移，
            # 避免新行跑到 stretch 后面造成按钮卡在中间（不滚动视口）
            btn = getattr(self, "_load_more_btn", None)
            if btn is not None:
                self._content_layout.insertWidget(self._content_layout.indexOf(btn), row)
            else:
                self._content_layout.addWidget(row)
            self._row_map[p.get("name", "")] = row
        # 首屏批次补 stretch
        if start == 0:
            self._content_layout.addStretch(1)

    def _update_empty_state(self):
        """匹配为空时显示空态提示"""
        if not self._matched:
            query = self._search_edit.text().strip()
            self._empty_label.setText("没有匹配的插件" if (query or self._active_tags) else "暂无可用插件")
            self._content_stack.setCurrentIndex(1)
        else:
            self._content_stack.setCurrentIndex(0)

    def _update_status(self):
        """更新状态栏：搜索/过滤结果计数"""
        query = self._search_edit.text().strip().lower()
        if query:
            self._status_label.setText(f"找到 {len(self._matched)} 个匹配结果")
            tcs = getattr(self, "_cached_tcs", "") or _text_color(secondary=True)
            self._status_label.setStyleSheet(f"color: {tcs}; font-size: 12px; background: transparent;")
        else:
            self._status_label.setText("")

    def _clear_plugin_list(self):
        """清空插件列表"""
        self._row_map.clear()
        self._all_loaded = False
        self._remove_load_more_button()
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            item = None  # 释放 QLayoutItem 引用

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
        # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        if success:
            self._refresh_row_states()
            InfoBar.success(f"{name} 安装成功", "", duration=2000, parent=bar_parent)
        else:
            self._update_row_state(name, installed=False, error=True)
            InfoBar.error(f"{name} 安装失败", "请检查网络或插件源", duration=3000, parent=bar_parent)

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
        # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(f"{name} 安装失败", msg, duration=5000, parent=bar_parent)

    # ── 异步更新 ────────────────────────────────────────

    def _async_update(self, plugin_meta: dict):
        """更新插件：点击后立即删除旧版并反馈（后台先删旧版再下载新版）

        点击瞬间将该行置为「下载中」（未安装态 + 禁用按钮），
        后台线程第一步即 rmtree 旧版目录，满足「点了立即删除现有的」。
        """
        name = plugin_meta.get("name", "")
        self._status_label.setText("更新中…")
        self._status_label.setStyleSheet("color: #FFA726; font-size: 12px; background: transparent;")
        # 立即反馈：该行变为未安装态 + 「下载中…」
        self._set_row_downloading(name)

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

    def _set_row_downloading(self, name: str):
        """将该插件行立即置为「下载中」（未安装态，旧版已删/将删）"""
        row = self._row_map.get(name)
        if row is not None:
            row.set_downloading()

    def _on_update_done(self, name: str, success: bool):
        """更新完成"""
        self._status_label.setText("")
        # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        if success:
            self._refresh_row_states()
            InfoBar.success(f"{name} 更新成功", "", duration=2000, parent=bar_parent)
        else:
            # 旧版已删、新版下载失败 → 行恢复「安装」按钮（可重新安装）
            self._update_row_state(name, installed=False, error=True)
            InfoBar.error(f"{name} 更新失败", "旧版已移除，可点击「安装」重试", duration=3000, parent=bar_parent)

    def _on_update_error(self, name: str, err: str):
        """更新出错"""
        self._status_label.setText("更新失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        # 旧版已删、新版下载失败 → 行恢复「安装」按钮（可重新安装）
        self._update_row_state(name, installed=False, error=True)
        import re as _re

        msg = err
        m = _re.search(r"Command\s*'\[.*?\]'\s*returned.*", err)
        if m:
            msg = m.group(0)[:120]
        elif len(err) > 120:
            msg = err[:120] + "..."
        # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(f"{name} 更新失败", msg, duration=5000, parent=bar_parent)

    # ── 启用 / 禁用 / 卸载 ────────────────────────────────

    def _set_row_manage_busy(self, name: str, busy_text: str):
        """将指定插件行置为「处理中…」状态（操作期间禁用按钮）"""
        row = self._row_map.get(name)
        if row is not None:
            row._busy = True
            row._busy_text = busy_text
            row._update_btn_text()

    def _async_enable(self, plugin_meta: dict):
        """在后台线程启用已禁用的插件"""
        name = plugin_meta.get("name", "")
        self._status_label.setText("启用中…")
        self._status_label.setStyleSheet("color: #4CAF50; font-size: 12px; background: transparent;")
        self._set_row_manage_busy(name, "启用中…")

        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda n=name: get_installer().enable(n))
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ok: self._on_manage_done(name, "启用", bool(ok)))
        self._worker.error.connect(lambda e: self._on_manage_error(name, "启用", e))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _async_disable(self, plugin_meta: dict):
        """在后台线程禁用已启用的插件"""
        name = plugin_meta.get("name", "")
        self._status_label.setText("禁用中…")
        self._status_label.setStyleSheet("color: #FF9800; font-size: 12px; background: transparent;")
        self._set_row_manage_busy(name, "禁用中…")

        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda n=name: get_installer().disable(n))
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ok: self._on_manage_done(name, "禁用", bool(ok)))
        self._worker.error.connect(lambda e: self._on_manage_error(name, "禁用", e))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _async_uninstall(self, plugin_meta: dict):
        """在后台线程卸载插件（确认后）"""
        name = plugin_meta.get("name", "")
        if not self._confirm_uninstall(name):
            return
        self._status_label.setText("卸载中…")
        self._status_label.setStyleSheet("color: #F44336; font-size: 12px; background: transparent;")
        self._set_row_manage_busy(name, "卸载中…")

        self._cleanup_worker()
        self._worker = _MarketplaceWorker(lambda n=name: get_installer().uninstall(n))
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ok: self._on_manage_done(name, "卸载", bool(ok)))
        self._worker.error.connect(lambda e: self._on_manage_error(name, "卸载", e))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _confirm_uninstall(self, name: str) -> bool:
        """卸载确认弹窗（MaskDialogBase 风格，parent 为 tab 顶层窗口）"""
        tc = getattr(self, "_cached_tc", "rgba(255,255,255,0.9)")
        tcs = getattr(self, "_cached_tcs", "rgba(255,255,255,0.55)")
        ff = getattr(self, "_cached_font_family", "")
        fs = getattr(self, "_cached_font_size", 14) or 14
        theme_colors = getattr(self, "_cached_theme_colors", {}) or {}
        accent_bg = theme_colors.get("accent", "#62a0ea")
        card_bg = theme_colors.get("content_bg", "#2a2a2e")
        border_c = theme_colors.get("border", "rgba(128,128,128,0.15)")

        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        dialog = _ConfirmUninstallDialog(
            bar_parent,
            name,
            tc=tc,
            tcs=tcs,
            ff=ff,
            fs=fs,
            accent_bg=accent_bg,
            card_bg=card_bg,
            border_c=border_c,
        )
        return dialog.exec_() == 1

    def _on_manage_done(self, name: str, action: str, success: bool):
        """启用/禁用/卸载完成"""
        self._status_label.setText("")
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        if success:
            self._refresh_row_states()
            InfoBar.success(f"{name} {action}成功", "", duration=2000, parent=bar_parent)
        else:
            self._refresh_row_states()
            InfoBar.error(f"{name} {action}失败", "", duration=3000, parent=bar_parent)

    def _on_manage_error(self, name: str, action: str, err: str):
        """启用/禁用/卸载出错"""
        self._status_label.setText(f"{action}失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._refresh_row_states()
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(f"{name} {action}失败", str(err)[:120], duration=5000, parent=bar_parent)

    def _refresh_row_states(self):
        """安装/更新/卸载后刷新：同步已渲染行状态 + 隐藏不再匹配的行

        不重建列表（保留滚动位置），仅同步状态、可见性与匹配计数。
        """
        query = self._search_edit.text().strip().lower()
        filter_mode = self._current_filter
        # 最新安装状态（installer 缓存已被安装/更新操作失效）
        inst_map = get_installer().get_installed_map()
        self._installed_set = set(inst_map)
        self._version_map = inst_map
        self._status_map = get_installer().get_status_map()

        # 重算匹配列表（不重建 widget）
        view_plugins = list(self._all_plugins) + self._build_local_extra_plugins()
        self._matched = [p for p in view_plugins if self._plugin_matches(p, query, filter_mode)]
        self._apply_sort(self._matched)

        for name, row in self._row_map.items():
            p = row._meta
            installed, has_update, local_ver, status = self._row_state(p)
            row.apply_state(installed, has_update, local_ver, status)
            row.setVisible(self._plugin_matches(p, query, filter_mode))
            row.update_search_highlight(query)

        self._rendered_count = len(self._row_map)
        if self._rendered_count >= len(self._matched):
            self._all_loaded = True
        if self._rendered_count < len(self._matched):
            self._set_load_more_button(len(self._matched) - self._rendered_count)
        else:
            self._remove_load_more_button()
        self._update_empty_state()
        self._update_status()
        self._update_update_badge()

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
                        # 更新失败但旧版仍在：恢复按钮（保留更新标记）
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

    # ── 排序 / 角标 ──

    def _style_sort_combo(self):
        """按上下文主题刷新排序下拉样式（与搜索框一致的圆角/内边距/无边框 + 全局字体）"""
        tc = getattr(self, "_cached_tc", "") or _text_color()
        ff = getattr(self, "_cached_font_family", "") or ""
        fs = getattr(self, "_cached_font_size", 14) or 14
        theme = getattr(self, "_cached_theme_colors", {}) or {}
        card_bg = theme.get("content_bg", "#ffffff" if not isDarkTheme() else "#2a2a2e")
        border_c = theme.get("border", "rgba(128,128,128,0.15)")
        # 全局字体：font-family + font-size（下拉主体与弹出列表都应用）
        combo_font = f" font-family: '{ff}';" if ff else ""
        combo_font += f" font-size: {max(11, fs)}px;"
        try:
            self._sort_combo.setStyleSheet(
                f"QComboBox {{ background: rgba(128,128,128,0.1); color: {tc};"
                f" border: none; border-radius: 8px; padding: 4px 8px;{combo_font} }}"
                "QComboBox::drop-down { border: none; width: 18px; }"
                f"QComboBox QAbstractItemView {{ background: {card_bg}; color: {tc};"
                f" border: 1px solid {border_c}; border-radius: 6px;{combo_font}"
                " selection-background-color: rgba(40,120,220,0.3); outline: none; }"
                "QComboBox QAbstractItemView::item { padding: 4px 8px; }"
                "QComboBox QAbstractItemView::item:hover { background: rgba(128,128,128,0.15); }"
            )
        except RuntimeError:
            pass

    def _on_sort_changed(self, index: int):
        """排序方式变化"""
        self._sort_mode = self._sort_combo.itemData(index) if index >= 0 else "default"
        if self._plugin_data:
            self._render_plugins(self._plugin_data)

    def _update_update_badge(self):
        """统计可更新插件数，更新「待更新」tab 右上角 InfoBadge"""
        n = 0
        for p in self._all_plugins or []:
            if p.get("name") in self._installed_set and self._has_update(p, True):
                n += 1
        try:
            if n > 0:
                self._updates_badge.setText(str(n))
                self._updates_badge.setVisible(True)
            else:
                self._updates_badge.setVisible(False)
        except RuntimeError:
            pass

    # ── Tag 过滤 ──

    def _rebuild_tag_bar(self):
        """从全量数据聚合 tag 计数，重建横向标签栏（数据变化时调用）"""
        counts: dict = {}
        for p in self._all_plugins or []:
            for t in p.get("_cached_tags", []) or []:
                if t:
                    counts[t] = counts.get(t, 0) + 1
        self._tag_counts = counts

        # 清空旧按钮
        while self._tag_layout.count():
            item = self._tag_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not counts:
            self._tag_bar.setVisible(False)
            self._tag_bar.parent().setVisible(False)
            return

        # 主文字色 + 加粗，确保清晰可读
        tc = getattr(self, "_cached_tc", "") or _text_color()

        # 「更多」放最前：展开全部标签的多选面板，方便快速访问
        more_btn = TransparentPushButton("更多…", self._tag_content)
        more_btn.setCursor(Qt.PointingHandCursor)
        more_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {tc}; border: 1px dashed rgba(128,128,128,0.4);"
            " border-radius: 6px; padding: 3px 10px; font-size: 12px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(128,128,128,0.15); }"
        )
        more_btn.clicked.connect(self._on_tag_more)
        self._tag_layout.addWidget(more_btn)

        for tag, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]:
            btn = TransparentPushButton(f"{tag} ({cnt})", self._tag_content)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(tag in self._active_tags)
            btn.setStyleSheet(
                f"QPushButton {{ background: rgba(128,128,128,0.1); color: {tc};"
                " border: none; border-radius: 6px; padding: 3px 10px; font-size: 12px; font-weight: bold; }"
                "QPushButton:hover { background: rgba(128,128,128,0.2); }"
                "QPushButton:checked { background: rgba(40,120,220,0.25); color: #62a0ea;"
                " border: 1px solid rgba(98,160,234,0.5); font-weight: bold; }"
            )
            btn.clicked.connect(lambda checked, t=tag, b=btn: self._on_tag_toggled(t, b))
            self._tag_layout.addWidget(btn)

        self._tag_layout.addStretch(1)

        self._tag_bar.setVisible(True)
        self._tag_bar.parent().setVisible(True)

    def _rebuild_source_bar(self):
        """从全量数据聚合市场来源，重建来源过滤栏（数据变化时调用）"""
        # 聚合来源（含本地插件的「本地」）
        sources = {}
        for p in self._all_plugins or []:
            src = p.get("_marketplace", "") or "未知"
            sources[src] = sources.get(src, 0) + 1
        for p in self._build_local_extra_plugins():
            src = p.get("_marketplace", "") or "未知"
            sources[src] = sources.get(src, 0) + 1

        # 清空旧按钮
        while self._source_layout.count():
            item = self._source_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not sources:
            self._source_bar.setVisible(False)
            self._source_bar.parent().setVisible(False)
            return

        tc = getattr(self, "_cached_tc", "") or _text_color()

        def _make_source_btn(text: str, value: str):
            btn = TransparentPushButton(text, self._source_content)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(value == getattr(self, "_source_filter", ""))
            btn.setStyleSheet(
                f"QPushButton {{ background: rgba(128,128,128,0.1); color: {tc};"
                " border: none; border-radius: 6px; padding: 3px 10px; font-size: 12px; font-weight: bold; }"
                "QPushButton:hover { background: rgba(128,128,128,0.2); }"
                "QPushButton:checked { background: rgba(40,120,220,0.25); color: #62a0ea;"
                " border: 1px solid rgba(98,160,234,0.5); font-weight: bold; }"
            )
            btn.clicked.connect(lambda checked, v=value, b=btn: self._on_source_toggled(v, b))
            return btn

        # 「全部」在前
        self._source_layout.addWidget(_make_source_btn(f"全部 ({len(sources)})", ""))
        for src, cnt in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0])):
            self._source_layout.addWidget(_make_source_btn(f"{src} ({cnt})", src))

        self._source_layout.addStretch(1)
        self._source_bar.setVisible(True)
        self._source_bar.parent().setVisible(True)

    def _sync_source_buttons(self):
        """同步来源按钮选中状态"""
        for i in range(self._source_layout.count()):
            item = self._source_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, QPushButton) and w.isCheckable():
                w.setChecked(w.text().split(" (")[0] == getattr(self, "_source_filter", ""))

    def _on_source_toggled(self, source: str, btn: QPushButton):
        """来源按钮点击：单选过滤（点击同一按钮取消）"""
        if btn.isChecked():
            self._source_filter = source
        else:
            self._source_filter = ""
        self._sync_source_buttons()
        if self._plugin_data:
            self._render_plugins(self._plugin_data)

    def _sync_tag_buttons(self):
        """同步标签栏按钮的选中状态（「更多」面板选择后调用）"""
        for i in range(self._tag_layout.count()):
            item = self._tag_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, QPushButton) and w.isCheckable():
                w.setChecked(w.text().split(" (")[0] in self._active_tags)

    def _on_tag_toggled(self, tag: str, btn: QPushButton):
        """横向标签点击：toggle 激活"""
        if btn.isChecked():
            self._active_tags.add(tag)
        else:
            self._active_tags.discard(tag)
        if self._plugin_data:
            self._render_plugins(self._plugin_data)

    def _on_tag_more(self):
        """「更多」：全部 tag 多选面板（parent 为 tab 顶层窗口）"""
        counts = getattr(self, "_tag_counts", {})
        if not counts:
            return
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        dialog = _TagFilterDialog(
            bar_parent,
            counts,
            self._active_tags,
            tc=getattr(self, "_cached_tc", "") or _text_color(),
            tcs=getattr(self, "_cached_tcs", "") or _text_color(secondary=True),
            ff=getattr(self, "_cached_font_family", ""),
            fs=getattr(self, "_cached_font_size", 14) or 14,
            accent_bg=(getattr(self, "_cached_theme_colors", {}) or {}).get("accent", "#62a0ea"),
            card_bg=getattr(self, "_cached_theme_colors", {}).get("content_bg", "#2a2a2e"),
            border_c=(getattr(self, "_cached_theme_colors", {}) or {}).get("border", "rgba(128,128,128,0.15)"),
        )
        if dialog.exec_():
            self._active_tags = set(dialog.selected_tags())
            self._sync_tag_buttons()
            if self._plugin_data:
                self._render_plugins(self._plugin_data)

    # ── 插件详情 ──

    def _on_plugin_detail(self, plugin_meta: dict):
        """打开插件详情面板

        parent 用 tab 管理器顶层窗口（遮罩覆盖整个 tab 而非卡片区域），
        与 InfoBar 挂载策略一致。
        """
        installed, has_update, local_ver, _status = self._row_state(plugin_meta)
        theme_colors = getattr(self, "_cached_theme_colors", {}) or {}
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        dialog = _PluginDetailDialog(
            bar_parent,
            plugin_meta,
            installed,
            has_update,
            local_ver,
            tc=getattr(self, "_cached_tc", "") or _text_color(),
            tcs=getattr(self, "_cached_tcs", "") or _text_color(secondary=True),
            ff=getattr(self, "_cached_font_family", ""),
            fs=getattr(self, "_cached_font_size", 14) or 14,
            accent_bg=theme_colors.get("accent", "#62a0ea"),
            card_bg=theme_colors.get("content_bg", "#2a2a2e"),
            border_c=theme_colors.get("border", "rgba(128,128,128,0.15)"),
        )
        dialog.installRequested.connect(self._async_install)
        dialog.updateRequested.connect(self._async_update)
        dialog.exec_()

    # ── 市场管理 ──

    def _build_markets_page(self, force: bool = False):
        """构建市场管理页面（首次构建后缓存，增删市场源时 force 重建）

        之前每次切换到「市场」tab 都全量重建行 + _retheme 全树遍历，
        是切 tab 卡顿的主因之一。
        """
        if getattr(self, "_markets_built", False) and not force:
            return
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
        self._markets_built = True

    def _create_market_row(self, src_def: dict) -> QWidget:
        """创建单个市场源的行组件（直接用缓存主题色，无需事后 re-theme）"""
        tc = getattr(self, "_cached_tc", None) or _text_color()
        tcs = getattr(self, "_cached_tcs", None) or _text_color(secondary=True)
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
        name_label.setObjectName("marketRowName")
        name_label.setStyleSheet(f"color: {tc}; font-weight: bold; font-size: 18px; background: transparent;")
        info.addWidget(name_label)

        src = src_def.get("source", {})
        src_type = src.get("source", "url")
        src_text = src.get("repo", src.get("url", "unknown"))
        if len(src_text) > 60:
            src_text = src_text[:57] + "..."
        url_label = QLabel(src_text, row)
        url_label.setObjectName("marketRowUrl")
        url_label.setStyleSheet(f"color: {tcs}; font-size: 14px; background: transparent;")
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
        self._build_markets_page(force=True)
        self._status_label.setText(f"已添加 {market_name}")
        self._status_label.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )

    def _on_remove_marketplace(self, name: str):
        """移除市场源"""
        mgr = get_marketplace_manager()
        mgr.remove_source(name)
        self._build_markets_page(force=True)

    def _open_url(self, url: str):
        """在浏览器中打开 URL"""
        import webbrowser

        webbrowser.open(url)

    def _on_open_plugin_dir(self, path: str):
        """在系统文件管理器中打开插件所在目录（并选中该目录）"""
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            logger.warning(f"[Marketplace] 打开插件目录失败: {e}")

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
