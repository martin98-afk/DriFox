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
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from PyQt5 import sip

from PyQt5.QtCore import QObject, QRect, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    FluentIcon,
    FluentLabelBase,
    IconWidget,
    InfoBar,
    LineEdit,
    MaskDialogBase,
    PushButton,
    ScrollArea,
    SingleDirectionScrollArea,
    StrongBodyLabel,
    SwitchButton,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
)

from .data import get_marketplace
from .downloads import get_downloads_fetcher
from .installer import get_installer
from .marketplace_manager import get_marketplace_manager
from .proxy import get_proxy_config
from .records import get_records
from ._squircle_avatar import (
    SquircleAvatar,
    PluginIconWidget,
    extract_initials,
    name_color,
    resolve_remote_icon_urls,
)

# 模块级孤儿线程容器：跨卡片生命周期持有已 quit 的线程
# （防止 running 线程随卡片析构被连带销毁 → Qt abort 0xC0000409）
_orphan_threads: list = []

# 下载量实时查询开关（False = 暂时关闭，2026-08 决策）
# 官方源 downloads 靠 market.json 自带字段；社区源 key 在 CountAPI 上不存在
# （大量 404）且服务无批量接口 → 逐个查询开销大。恢复时置 True。
_DOWNLOADS_LIVE_QUERY_ENABLED = False

# ── 主题色辅助 ──────────────────────────────────────────────


def _scroll_area_qss() -> str:
    """滚动容器统一 QSS：透明背景 + 主程序统一滚动条样式

    与 app.utils.design_tokens 约定一致（tab_panel/message_card 同款），
    滚动条宽度 6px 薄样式，视觉随主程序主题。
    """
    from app.utils.design_tokens import get_unified_scrollbar_style

    return (
        "QScrollArea { background: transparent; border: none; }"
        "QScrollArea > QWidget > QWidget { background: transparent; }" + get_unified_scrollbar_style(6)
    )


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

    market_fetched = pyqtSignal(dict, int)  # (市场数据, gen) 单个市场数据 {"name":..., "plugins":[...]}
    market_failed = pyqtSignal(list, int)  # (失败源名列表, gen) 拉取失败的源名列表（仅标记，不进列表）
    all_done = pyqtSignal(int)  # gen
    error = pyqtSignal(str, int)  # (错误信息, gen)

    def __init__(self, force: bool = False, gen: int = 0):
        super().__init__()
        self._force = force
        self._gen = gen

    def run(self):
        from .marketplace_manager import get_marketplace_manager

        mgr = get_marketplace_manager()
        failed: list = []
        try:
            for src in mgr.get_sources():
                data = mgr.fetch_marketplace(src, force=self._force)
                if data.get("_error"):
                    # 失败源：绝不进入插件列表，仅收集名称供 UI 做失败标记
                    failed.append(src["name"])
                    continue
                self.market_fetched.emit(data, self._gen)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}", self._gen)
        if failed:
            self.market_failed.emit(failed, self._gen)
        self.all_done.emit(self._gen)


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
        # 下载量：紧跟来源市场之后展示，突出数字（0 或缺失不显示）
        downloads = self._meta.get("downloads", 0)
        if downloads:
            rows.append(("下载量", f"{downloads:,}"))
        homepage = _PluginRow._compute_homepage(self._meta)
        if homepage:
            rows.append(("🔗 官网", f'<a href="{homepage}" style="color:{accent_bg};">{homepage}</a>'))
        if not rows:
            rows.append(("ℹ️ 信息", "该插件未提供更多信息"))

        for label, value in rows:
            if label == "下载量":
                # 下载量行特殊样式：加粗强调数字
                html = (
                    f'<b style="color:{accent_bg};">下载量</b>：'
                    f'<span style="color:{accent_bg}; font-weight:bold; '
                    f'font-size:{max(10, fs + 1)}px;">{value}</span> 次安装'
                )
            else:
                html = f"<b>{label}</b>：{value}"
            row_lb = QLabel(html, info_widget)
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
        # 本地路径单次解析并缓存：图标/目录按钮复用（避免每行多轮磁盘扫描）
        self._local_path = self._resolve_local_path()
        self._setup_ui()

    # ── 字号派生（与卡片 _PLUGIN_ROW_SIZE_OFFSETS 保持一致） ──
    # 描述 fs-4、tag fs-5、更新标签 fs-3、市场标签 fs+0；无上下文时回退固定值

    def _derive_size(self, base_px: int, offset: int) -> int:
        """按上下文字体大小派生行内标签字号（无上下文时用固定 base_px）"""
        if self._font_size > 0:
            return max(8, self._font_size + offset)
        return base_px

    def sizeHint(self):
        """行 sizeHint：已布局时按当前宽度用 heightForWidth 计算

        修复「加载更多」按钮下大段空白：QLabel(wordWrap) 的默认 sizeHint
        按理想宽度（未约束）计算换行高度，比实际布局高度大（实测 75 vs
        67，行多时累积成 stretch 空白区）。已布局（width>0）时返回
        heightForWidth(当前宽度)，与 QVBoxLayout 布局行时使用的高度一致；
        未布局（width=0）时回退默认（该状态行处于隐藏、不参与布局
        sizeHint 累加，无影响）。
        """
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        lay = self.layout()
        if lay is not None and self.width() > 0:
            h = lay.heightForWidth(self.width())
            if h > 0:
                return QSize(base.width(), h)
        return base

    def _font_qss(self, size_px: int) -> str:
        """生成 font-size + font-family 的 QSS 片段"""
        qss = f"font-size: {size_px}px;"
        if self._ff:
            qss += f" font-family: '{self._ff}';"
        return qss

    def _apply_child_fonts(self):
        """字号/字体变化时更新行内各标签（替代原 _retheme 全树遍历）

        优化：缓存已设置的 QSS 字符串，无变化时跳过 setStyleSheet 调用
        （QSS 解析 + 触发布局重算在 30+ 行时是滚动卡顿主因之一）。
        """
        if self._desc_label is not None:
            new_qss = f"color: {self._tcs}; {self._font_qss(self._derive_size(12, -4))} background: transparent;"
            if getattr(self, "_desc_qss_cached", None) != new_qss:
                self._desc_qss_cached = new_qss
                self._desc_label.setStyleSheet(new_qss)
        new_tag_qss = self._tag_stylesheet()
        if getattr(self, "_tag_qss_cached", None) != new_tag_qss:
            self._tag_qss_cached = new_tag_qss
            for lbl in self._tag_labels:
                lbl.setStyleSheet(new_tag_qss)
        if getattr(self, "_mp_label", None) is not None:
            new_mp_qss = f"color: {self._tcs}; {self._font_qss(self._derive_size(10, 0))} background: transparent;"
            if getattr(self, "_mp_qss_cached", None) != new_mp_qss:
                self._mp_qss_cached = new_mp_qss
                self._mp_label.setStyleSheet(new_mp_qss)
        if getattr(self, "_dl_label", None) is not None:
            new_dl_qss = f"color: {self._tcs}; {self._font_qss(self._derive_size(11, 0))} background: transparent;"
            if getattr(self, "_dl_qss_cached", None) != new_dl_qss:
                self._dl_qss_cached = new_dl_qss
                self._dl_label.setStyleSheet(new_dl_qss)

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
        self._info_layout = info_layout
        # info 在行布局中的固定 index（heightForWidth 手动计算用）
        self._info_index = 1

        name = self._meta.get("name", "未知")
        self._name_raw = name
        # 标题行：插件名靠左，下载量靠右（卡片右上角，醒目）
        title_row = QWidget(self)
        title_row.setStyleSheet("background: transparent;")
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(8)
        self._title_label = QLabel("", title_row)
        self._title_label.setObjectName("pluginRowTitle")
        ff_qss = f" font-family: '{self._ff}';" if self._ff else ""
        self._title_label.setStyleSheet(f"color: {self._tc}; font-weight: bold;{ff_qss} background: transparent;")
        self._refresh_title()
        title_layout.addWidget(self._title_label)
        # 下载量：右上角醒目展示，下载 icon + 橙色加粗数字（0 或缺失不显示）
        downloads = self._meta.get("downloads", 0)
        self._dl_label = None
        if downloads:
            title_layout.addStretch(1)
            dl_icon = IconWidget(FluentIcon.DOWNLOAD, title_row)
            dl_icon.setFixedSize(14, 14)
            title_layout.addWidget(dl_icon)
            self._dl_label = QLabel(
                f'<span style="color:#FFA726; font-weight:bold;">{downloads:,}</span>',
                title_row,
            )
            self._dl_label.setStyleSheet(
                f"color: {self._tcs}; {self._font_qss(self._derive_size(11, 0))} background: transparent;"
            )
            title_layout.addWidget(self._dl_label)
        info_layout.addWidget(title_row)

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

        # 元信息行：市场来源（状态标签已并入标题版本号后，下载量已移至右上角）
        meta_row = QWidget(self)
        meta_row.setStyleSheet("background: transparent;")
        meta_layout = QHBoxLayout(meta_row)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(8)

        marketplace = self._meta.get("_marketplace", "")
        self._mp_label = None
        if marketplace:
            self._mp_label = QLabel(f"📦 {marketplace}", meta_row)
            self._mp_label.setStyleSheet(
                f"color: {self._tcs}; {self._font_qss(self._derive_size(10, 0))} background: transparent;"
            )
            meta_layout.addWidget(self._mp_label)
        info_layout.addWidget(meta_row)

        layout.addLayout(info_layout, 1)

        # 图标按钮列（目录/官网/信息）：置顶排列，不垂直居中
        icon_col = QVBoxLayout()
        icon_col.setSpacing(4)

        # 打开插件所在文件夹按钮（仅已安装时可见，未安装无本地目录）
        self._dir_btn = None
        local_path = self._local_path
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
            self._outline_btn_style(color) + f" PushButton {{ font-size: {max(10, self._btn_font_size - 2)}px; }}"
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
            self._busy_text = "安装中…"
            self._update_btn_text()
            self.installRequested.emit(self._meta)
        elif self._has_update:
            # 已安装且有新版 → 更新
            self._busy = True
            self._busy_text = "更新中…"
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
        self._busy_text = ""  # 清除操作中的残留文案（如「卸载中…」），避免下次点击误显示
        if changed:
            self._refresh_title()
        # 文件夹按钮仅已安装时可见
        if self._dir_btn is not None:
            self._dir_btn.setVisible(installed)
        self._update_btn_text()

    def set_downloading(self):
        """更新流程：点击后立即标记为「下载中」（保留已安装态，仅禁用按钮）

        更新策略为「下载成功后再替换旧版」，旧版在下载期间仍可用，
        故行保持已安装显示（版本徽标/文件夹按钮不动），仅按钮变
        「更新中…」禁用。
        """
        self._busy = True
        self._busy_text = "下载中…"
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
        self._busy_text = ""  # 清除操作中残留文案
        self._update_btn_text()

    def _create_icon_widget(self) -> QWidget:
        """创建插件图标组件

        优先级：
        1. 本地已安装插件的 SVG（plugin_dir 下存在）
        2. marketplace 元数据中的 icon 字段 + GitHub raw URL（未安装，但
           manifest.icon + source 指向 git-subdir GitHub 源）
        3. SquircleAvatar 缩写头像（兜底）

        卡片行内图标按 font_size * 2.4 放大（最小 36px），让图标在列表中
        更醒目。当前 font_size=14 → 36px（之前 23px）。
        """
        plugin_name = self._meta.get("name", "?")
        # 卡片行内放大：font_size > 0 时按 2.4 倍率放大，下限 36px
        icon_size = max(36, int(self._font_size * 2.4)) if self._font_size > 0 else 36

        local_path = self._local_path
        if local_path:
            # 优先复用 installer 扫描缓存中的 manifest（一次 IO），未命中兜底直读
            manifest = get_installer()._manifest_cache.get(plugin_name)
            if manifest is None:
                import json as _json

                for _meta_dir in (".drifox-plugin", ".claude-plugin"):
                    _mp = local_path / _meta_dir / "plugin.json"
                    if _mp.exists():
                        try:
                            manifest = _json.loads(_mp.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                        break
            if manifest:
                return PluginIconWidget(
                    plugin_dir=local_path,
                    manifest=manifest,
                    font_size=self._font_size,
                    parent=self,
                    icon_size=icon_size,
                )

        # 未安装：尝试从 marketplace 元数据构造远程 icon URL
        remote_urls = resolve_remote_icon_urls(self._meta)
        if remote_urls:
            # 用插件自身的 meta 作为 manifest（至少含 name + icon 字段）
            manifest = {
                "name": plugin_name,
                "icon": self._meta.get("icon"),
            }
            return PluginIconWidget(
                manifest=manifest,
                font_size=self._font_size,
                parent=self,
                remote_urls=remote_urls,
                icon_size=icon_size,
            )

        # Fallback to initials avatar
        return SquircleAvatar(
            extract_initials(plugin_name),
            name_color(plugin_name),
            self,
            size=icon_size,
            font_size=self._font_size,
        )

    def _resolve_local_path(self) -> Optional[Path]:
        """解析本地插件路径并缓存到行实例（B3：避免每行多轮磁盘扫描）

        已安装插件优先直拼用户启用目录（一次 stat 命中即返回）；
        未命中（如禁用/系统目录）回退全目录扫描，保证功能不回退。
        """
        name = self._meta.get("name", "")
        if not name:
            return None
        if self._installed:
            p = _drifox_dir() / "plugins" / name
            if p.is_dir():
                return p
        return self._find_local_plugin_path(name)

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
        if self._avatar is not None:
            # 卡片行内图标按字号 2.4 倍放大（最小 36px），与 _create_icon_widget 保持一致
            icon_size = max(36, int(font_size * 2.4))
            if hasattr(self._avatar, "set_icon_size"):
                self._avatar.set_icon_size(icon_size)
            elif hasattr(self._avatar, "set_size"):
                self._avatar.set_size(icon_size)
            elif hasattr(self._avatar, "set_font_size"):
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


class _MarketListContent(QWidget):
    """列表内容容器（QScrollArea 的 widget）

    覆盖 sizeHint()：已布局时直接返回当前几何高度（由 _reveal_rows 按
    实际行高维护），绕开 QWidget::sizeHint()/QVBoxLayout::sizeHint 的
    totalSizeHint 缓存（可能被冻结为异常值：QLabel(wordWrap) 未布局时
    按理想宽度计算、行 sizeHint 与实际布局高度偏差，QScrollArea 用该
    值撑开内容 → 列表底部大段空白）。
    """

    def sizeHint(self):
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        if self.height() > 0:
            return QSize(base.width(), self.height())
        lay = self.layout()
        if lay is not None:
            return lay.sizeHint()
        return base


class MarketplaceCard(QWidget):
    """插件市场浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        # 安装/更新/启用/禁用/卸载：串行任务队列（一次只跑一个 worker 线程，
        # 避免并发 git 进程互抢 cache/目标目录；完成自动启动下一个任务）
        self._task_queue: list = []  # [{kind,name,fn,busy_text,status_text,status_color}]
        self._task_active: bool = False
        # 当前正在运行的任务（_run_next_task 从队列 pop 后保存）：
        # 行全量重建（切 tab 回来重渲染）后用于恢复该行的 busy 状态
        self._active_task: Optional[dict] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_MarketplaceWorker] = None
        # 市场拉取 worker：与任务 worker 分离，刷新市场不打断正在安装的任务
        self._fetch_thread: Optional[QThread] = None
        self._fetch_worker: Optional[_MarketFetchWorker] = None
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
        # ── 渲染合并与线程清理状态（B2/B4） ──
        self._render_pending = False  # 市场数据已合并待渲染（同帧合并标志）
        self._initial_view_done = False  # 首屏是否已渲染（本地/合并）
        self._failed_sources: set = set()  # 拉取失败的源名（仅用于 UI 标记，不进列表）
        self._worker_gen: int = 0  # worker 代次标记：递增，旧 worker 迟到信号按 gen 丢弃
        self._market_status_labels: dict = {}  # 市场名 → 状态徽标 QLabel（「市场」页）
        self._initial_render_timer: Optional["QTimer"] = None  # 首屏 300ms 合并窗口
        self._flush_timer: Optional["QTimer"] = None  # 市场渲染合并 timer（self 子对象，销毁自动取消）
        self._reveal_timer: Optional["QTimer"] = None  # 渲染完成延迟显示 timer（防压缩帧）
        self._load_timer: Optional["QTimer"] = None  # show_card 延迟加载 timer（同上）
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
        # 延迟 50ms 启动加载，避免阻塞 show 过程（self 子 timer，销毁自动取消）
        if self._load_timer is None:
            from PyQt5.QtCore import QTimer

            self._load_timer = QTimer(self)
            self._load_timer.setSingleShot(True)
            self._load_timer.timeout.connect(self._start_load)
        self._load_timer.start(50)
        # 兜底：已有合并数据但上次因不可见未渲染 → 立即补渲染
        if self._plugin_data and self._initial_view_done and not self._render_pending:
            self._schedule_render()

    def _start_load(self):
        """本地已安装先行渲染 + 后台逐市场拉取（首屏合并窗口 300ms）

        本地视图延迟 300ms 渲染：若首个市场在此窗口内到达（缓存命中/快速源），
        直接渲染合并数据，跳过本地独立渲染（避免两次全量重建）。
        """
        if not self._alive():
            return  # 卡片已销毁
        self._async_refresh()
        if self._initial_render_timer is None:
            from PyQt5.QtCore import QTimer

            self._initial_render_timer = QTimer(self)
            self._initial_render_timer.setSingleShot(True)
            self._initial_render_timer.timeout.connect(self._render_initial_view)
        self._initial_render_timer.start(300)

    def _render_initial_view(self):
        """首屏渲染（仅首次执行一次）：有市场数据渲染合并结果，否则渲染本地"""
        if not self._alive():
            return  # 卡片已销毁，放弃首屏渲染
        if self._initial_view_done:
            return
        self._initial_view_done = True
        if self._plugin_data:
            self._render_plugins(self._plugin_data)
        else:
            self._render_local_installed()

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
        "proxySectionTitle": -1,  # 加速页段标题（启用/配置/帮助）
        "proxyStatus": -2,  # 加速页状态行
        "proxyHelp": -2,  # 加速页帮助正文
        "proxyRecordTitle": -1,  # 记录区段标题
        "proxyRecordRow": -2,  # 记录行正文（动作/插件名/原因）
        "proxyRecordTime": -3,  # 记录行时间
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
            if child.objectName() == "updatesBadge":
                continue  # 亮黄 badge 固定黑字，不跟随主题文字色
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
        self._tab_bar.addItem("proxy", "代理", None, None)
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

        # 待更新 InfoBadge：亮黄色、挂在「待更新」tab 右侧垂直居中（无更新时隐藏）
        # 注意 parent 必须与 target 同级（filter_row），否则坐标参考系错乱导致 badge 落在 tab 左侧
        from qfluentwidgets import InfoBadge, InfoBadgePosition

        self._updates_badge = None
        try:
            # custom 亮黄背景（浅色 #FFC107 / 深色 #FFD54F）+ 固定黑字保证对比度
            self._updates_badge = InfoBadge.custom(
                "0",
                QColor(255, 193, 7),
                QColor(255, 213, 79),
                parent=filter_row,
                target=self._updates_item,
                position=InfoBadgePosition.RIGHT,
            )
            self._updates_badge.setObjectName("updatesBadge")
            # 注销主题自动重放 + 固定黑字：亮黄背景配白字不可读，主题切换时也不能被改回
            from qfluentwidgets.common.style_sheet import styleSheetManager

            styleSheetManager.deregister(self._updates_badge)
            self._updates_badge.setStyleSheet("InfoBadge { color: #1a1a1a; padding: 1px 3px 1px 3px; }")
            self._updates_badge.setVisible(False)
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
        self._sort_combo.addItem("下载量最多优先", "downloads")
        self._sort_combo.addItem("名称 A-Z", "name_asc")
        self._sort_combo.addItem("名称 Z-A", "name_desc")
        self._sort_combo.addItem("版本最新优先", "version")
        self._sort_combo.setFixedWidth(120)
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

        self._source_bar = SingleDirectionScrollArea(source_row, Qt.Horizontal)
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

        self._tag_bar = SingleDirectionScrollArea(tag_row, Qt.Horizontal)
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

        self._scroll = QScrollArea(self._browse_page)
        # widgetResizable=False：QScrollArea 按 sizeHint 自动管理内容高度，
        # 但 QLabel(wordWrap) 的 sizeHint 与实际布局高度有系统性偏差且
        # C++ 侧无法被 Python override 修正 → 内容被撑高 → 按钮下空白。
        # 改为手动管理 content 尺寸（_sync_content_size）。
        self._scroll.setWidgetResizable(False)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(_scroll_area_qss())
        self._content = _MarketListContent(self._scroll)
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

        # ===== 加速配置页 =====
        self._proxy_page = QWidget(self._page_stack)
        proxy_root = QVBoxLayout(self._proxy_page)
        proxy_root.setContentsMargins(16, 12, 16, 12)
        proxy_root.setSpacing(10)
        self._page_stack.addWidget(self._proxy_page)

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
        try:
            win = self.window()
            if win:
                win.removeEventFilter(self)  # 先移除再安装：防止重复安装导致 resize 重复回调
                win.installEventFilter(self)
                self.updateGeometry()
            # 监听视口 resize（widgetResizable=False 手动管理尺寸）：
            # 滚动条出现/消失、容器展开动画等都会改变视口尺寸 → 行重排
            # （wordWrap 换行数变化）→ 行高变化 → content 高度需重新同步，
            # 否则底部出现空白
            vp = self._scroll.viewport()
            vp.removeEventFilter(self)
            vp.installEventFilter(self)
            self._sync_content_size()
        except RuntimeError:
            pass  # 窗口/卡片已销毁

    def eventFilter(self, obj, event):
        """监听窗口/视口 resize，同步 content 尺寸（widgetResizable=False）"""
        from PyQt5.QtCore import QEvent

        if event.type() == QEvent.Resize:
            if obj is self.window():
                # 窗口尺寸变化：容器重算高度 + 同步内容尺寸
                self._sync_content_size()
                self.updateGeometry()
            elif obj is self._scroll.viewport():
                # 视口尺寸变化（滚动条出现/消失、容器动画）：同步内容尺寸。
                # 第一次同步改变 content 宽度 → 行重排（wordWrap 换行数
                # 变化）→ 行高变化，第二次同步校正高度
                self._sync_content_size()
                self._sync_content_size()
        return super().eventFilter(obj, event)

    # ── 异步刷新 ──

    def _async_refresh(self, force: bool = False):
        """后台逐市场拉取，每到一个市场增量合并渲染

        Args:
            force: 是否强制拉取远程（跳过缓存）
        """
        self._set_loading(True)
        self._failed_sources = set()  # 每轮刷新重置失败标记（重新收集）
        self._cleanup_fetch_worker()
        self._worker_gen += 1
        self._fetch_worker = _MarketFetchWorker(force=force, gen=self._worker_gen)
        self._fetch_thread = QThread(self)
        self._fetch_worker.moveToThread(self._fetch_thread)
        self._fetch_thread.started.connect(self._fetch_worker.run)
        self._fetch_worker.market_fetched.connect(self._on_market_fetched)
        self._fetch_worker.market_failed.connect(self._on_market_failed)
        self._fetch_worker.all_done.connect(self._on_market_all_done)
        self._fetch_worker.error.connect(self._on_refresh_error)
        # 全部市场拉完（或出错）后才退出线程并清理
        self._fetch_worker.all_done.connect(self._fetch_thread.quit)
        self._fetch_worker.error.connect(self._fetch_thread.quit)
        self._fetch_worker.all_done.connect(self._fetch_worker.deleteLater)
        self._fetch_worker.error.connect(self._fetch_worker.deleteLater)
        self._fetch_thread.finished.connect(self._fetch_thread.deleteLater)
        self._fetch_thread.start()

    def _merge_market_data(self, market_data: dict):
        """合并单个市场数据到 _plugin_data 并触发渲染（worker 回调复用）

        校验按钮/浏览刷新共用：数据合并 + 状态徽标刷新 + 渲染调度。
        """
        # 源名优先：marketplace.json 自带 name 可能与源名不一致
        # （如 claude-plugins-community 数据 name 是 "claude-community"），
        # 而插件 _marketplace 标记统一用源名；用错 name 会导致旧数据
        # 不移除、数据翻倍 + 状态徽标查不到。
        market_name = market_data.get("_marketplace") or market_data.get("name", "")
        plugins = market_data.get("plugins", []) or []
        # 合并：移除该市场旧数据，追加新数据（同名插件按市场覆盖）
        merged = [p for p in self._plugin_data if p.get("_marketplace") != market_name]
        merged.extend(plugins)
        self._plugin_data = merged  # 数据始终合并，不因不可见丢失
        # 同步刷新「市场」页该源状态徽标
        self._refresh_market_status_label(market_name)
        if not self.isVisible():
            return  # 仅跳过本次渲染（数据已在 _plugin_data）
        self._schedule_render()

    def _on_market_fetched(self, market_data: dict, gen: int):
        """单个市场拉取完成：合并进全量数据并标记待渲染（同帧合并一次）

        数据合并无条件前置（不可见时也合并不丢弃）；isVisible 只控制渲染。
        """
        if not self._alive():
            return  # 卡片已销毁，丢弃迟到数据
        if gen != self._worker_gen:
            return  # 旧 worker 迟到信号，丢弃（防幽灵渲染）
        self._merge_market_data(market_data)

    def _schedule_render(self):
        """合并短时间窗口内多次市场到达为一次渲染（_render_pending 防重复排队）

        80ms 合并窗口：刷新时逐市场拉取，间隔 <80ms 的市场数据合并为一次
        全量重建，避免每个市场到达都清空+重建列表（列表闪 N 次）。
        用 self 子对象 QTimer（销毁自动取消）：避免无父临时 timer 在卡片
        销毁后仍触发回调，触碰已删除 Qt 控件（原生崩溃 0xC0000409）。
        """
        if self._render_pending:
            return
        self._render_pending = True
        if self._flush_timer is None:
            from PyQt5.QtCore import QTimer

            self._flush_timer = QTimer(self)
            self._flush_timer.setSingleShot(True)
            self._flush_timer.timeout.connect(self._flush_render)
        self._flush_timer.start(80)

    def _flush_render(self):
        """执行挂起的渲染：首屏未渲染时交给 _render_initial_view 统一完成"""
        if not self._alive():
            return  # 卡片已销毁，放弃渲染
        self._render_pending = False
        if not self.isVisible():
            return  # 不可见仅合并不渲染（数据已入 _plugin_data，下次显示自动渲染）
        if not self._initial_view_done:
            return  # 首屏由 _render_initial_view 渲染（合并本地/远程窗口）
        self._render_plugins(self._plugin_data)

    def _on_market_failed(self, failed: list, gen: int):
        """部分市场源拉取失败：仅更新源标记（绝不触碰 _plugin_data/_row_map）"""
        if not self._alive():
            return  # 卡片已销毁
        if gen != self._worker_gen:
            return  # 旧 worker 迟到信号，丢弃（防幽灵渲染）
        self._failed_sources = set(failed)
        # 市场管理页已构建 → 轻量刷新状态徽标（失败信息已持久化在 manager）
        if getattr(self, "_markets_built", False):
            self._refresh_all_market_status()

    def _on_market_all_done(self, gen: int):
        """全部市场拉取完成"""
        if not self._alive():
            return  # 卡片已销毁
        if gen != self._worker_gen:
            return  # 旧 worker 迟到信号，丢弃（防幽灵渲染）
        # 兜底：有合并数据且首屏已渲染 → 必 flush（flush 内部清 pending 防重）
        if self._plugin_data and self._initial_view_done:
            self._flush_render()
        self._set_loading(False)
        # 全部拉取完成后刷新市场页状态徽标（覆盖缓存命中/失败回退场景）
        self._refresh_all_market_status()
        # 远程全部失败时，本地已安装视图仍保留（不清空）
        if not self._plugin_data:
            self._status_label.setText("远程市场不可用")
            self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")

    def _on_refresh_error(self, err: str, gen: int):
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

        # 社区插件（无 downloads 字段）→ 后台实时查询下载量（缓存 + 防抖）
        self._schedule_downloads_fetch(view_plugins)

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
        self._refresh_filter_counts()
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

    # ── 社区插件下载量实时查询 ──

    def _schedule_downloads_fetch(self, plugins: list):
        """聚合缺 downloads 的插件 → 后台查询 → 回填 → 数据变化则重渲染

        - 只查可见列表（view_plugins），避免查全量隐藏插件
        - 后台线程执行，回填后若 downloads 有新增才触发一次重渲染
        - 重渲染后 downloads 已有值 → 不再进查询分支（无死循环）
        - 防重入：查询进行中时，新请求合并到同一批（不重复起线程）
        - 当前暂时关闭（_DOWNLOADS_LIVE_QUERY_ENABLED=False）：下载量纯靠
          market.json 自带字段，不向 CountAPI 实时查询
        """
        if not _DOWNLOADS_LIVE_QUERY_ENABLED:
            return
        missing = [p for p in plugins if not p.get("downloads")]
        if not missing:
            return
        names = [p["name"] for p in missing if p.get("name")]

        # 已有 in-flight 查询：合并名字后返回（查询完成后统一回填）
        if getattr(self, "_dl_fetch_thread", None) is not None:
            pending = getattr(self, "_dl_fetch_pending", [])
            self._dl_fetch_pending = list(dict.fromkeys(pending + names))
            return

        fetcher = get_downloads_fetcher()
        self._dl_fetch_pending = list(names)

        def _run():
            # 批量查询（缓存命中 + 失败防抖由 fetcher 内部处理）
            return fetcher.fetch_missing(list(self._dl_fetch_pending))

        self._cleanup_dl_fetch_worker()
        self._dl_worker = _MarketplaceWorker(_run)
        self._dl_fetch_thread = QThread(self)
        self._dl_worker.moveToThread(self._dl_fetch_thread)
        self._dl_fetch_thread.started.connect(self._dl_worker.run)
        self._dl_worker.finished.connect(self._on_downloads_fetched)
        self._dl_worker.error.connect(lambda e: self._on_downloads_fetched(None))
        self._dl_worker.finished.connect(self._dl_fetch_thread.quit)
        self._dl_worker.error.connect(self._dl_fetch_thread.quit)
        self._dl_worker.finished.connect(self._dl_worker.deleteLater)
        self._dl_worker.error.connect(self._dl_worker.deleteLater)
        self._dl_fetch_thread.finished.connect(self._dl_fetch_thread.deleteLater)
        self._dl_fetch_thread.start()

    def _on_downloads_fetched(self, result):
        """查询完成：回填 downloads；有新增值则重渲染"""
        if not self._alive():
            return
        # 处理 in-flight 期间合并进来的新名字
        names = getattr(self, "_dl_fetch_pending", []) or []
        self._dl_fetch_pending = []
        if not result:
            # 新合并的名字未被本次查询覆盖 → 再调度一次
            if names:
                plugins = getattr(self, "_all_plugins", []) or []
                self._schedule_downloads_fetch(plugins)
            return

        changed = False
        plugin_by_name = {}
        for p in getattr(self, "_all_plugins", []) or []:
            plugin_by_name[p.get("name", "")] = p
        for name, count in result.items():
            p = plugin_by_name.get(name)
            if p is not None and not p.get("downloads") and count:
                p["downloads"] = count
                changed = True
        if changed:
            self._render_plugins(getattr(self, "_all_plugins", []) or [])

    def _cleanup_dl_fetch_worker(self):
        """安全清理下载量查询 worker/thread（复用 _orphan_worker_thread 剥离模式）

        与任务/market 拉取 worker 对齐：quit + setParent(None) + 孤儿列表强引用，
        不阻塞主线程（原 wait(500) 在扫描>500ms 且卡片析构时留崩溃窗口）。
        """
        thread = getattr(self, "_dl_fetch_thread", None)
        self._dl_fetch_thread = None
        self._dl_worker = None
        self._orphan_worker_thread(thread)

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

    def _build_local_meta_bg(self, name: str, status: str, version_map: dict) -> Optional[dict]:
        """纯函数版 _build_local_meta：后台线程读取 manifest（不触碰 Qt/self 状态）

        与 _build_local_meta 的差异：version 从参数 version_map 取（不读
        self._version_map），其余逻辑一致。staticmethod 化不可行（内部
        需要类上下文），保持实例方法但仅依赖传入参数 + 类静态辅助。
        """
        path = _PluginRow._find_local_plugin_path(name)
        if path is None:
            return None
        meta: dict = {
            "name": name,
            "description": "",
            "version": version_map.get(name) or "",
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

    def _build_local_extra_plugins_bg(self, status_map: dict, market_names: frozenset, version_map: dict) -> list:
        """纯函数版 _build_local_extra_plugins：后台线程构建本地 extra 条目

        Args:
            status_map: installer.get_status_map() 结果（后台已取好）
            market_names: 市场插件名集合快照（主线程 _all_plugins 派生）
            version_map: installer.get_installed_map() 结果（版本来源）

        Returns:
            extras 列表（纯数据，无 Qt widget，供主线程回回调渲染）
        """
        extras = []
        for name, status in status_map.items():
            if name in market_names:
                continue
            meta = self._build_local_meta_bg(name, status, version_map)
            if meta is not None:
                extras.append(meta)
        return extras

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
        elif mode == "downloads":
            matched.sort(key=lambda p: p.get("downloads", 0), reverse=True)
        elif mode == "version":
            from functools import cmp_to_key

            from .data import compare_versions

            matched.sort(
                key=cmp_to_key(lambda a, b: compare_versions(a.get("version", "0"), b.get("version", "0"))),
                reverse=True,
            )

    def _render_next_batch(self):
        """渲染下一批缺失的匹配行（同步，每批 _RENDER_BATCH 个足够快，停住等手动加载）

        不依赖 _rendered_count 连续索引：搜索/筛选复用行后，_row_map 的行集合
        与 _matched 前部可能不一致（旧行 ≠ 新匹配列表前 N 个）。若按「已渲染
        计数」续渲染 _matched[start:end]，匹配列表前部新出现的插件会永远
        缺行 → 搜索不全。改为按 _matched 顺序补齐「尚无 widget 的插件」。
        """
        missing = [p for p in self._matched if p.get("name", "") not in self._row_map]
        if not missing:
            self._all_loaded = True
            self._remove_load_more_button()
            return
        batch = missing[: self._RENDER_BATCH]
        self._render_rows(batch)
        self._rendered_count = len(self._row_map)
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

    def _create_row(self, p: dict) -> Optional[_PluginRow]:
        """创建插件行 widget 并连接全部信号

        行创建 + 信号连接抽成单方法，供 _render_batch 与行补齐场景共用；
        单行渲染失败不中断整个批次。
        """
        try:
            fs = getattr(self, "_cached_font_size", 0)
            tc = getattr(self, "_cached_tc", None)
            tcs = getattr(self, "_cached_tcs", None)
            ff = getattr(self, "_cached_font_family", "") or ""
            query = self._search_edit.text().strip().lower()
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
            return None
        row.installRequested.connect(self._async_install)
        row.updateRequested.connect(self._async_update)
        row.openUrlRequested.connect(self._open_url)
        row.openDirRequested.connect(self._on_open_plugin_dir)
        row.detailRequested.connect(self._on_plugin_detail)
        row.enableRequested.connect(self._async_enable)
        row.disableRequested.connect(self._async_disable)
        row.uninstallRequested.connect(self._async_uninstall)
        return row

    def _render_rows(self, rows: list):
        """创建并插入插件行（压缩帧防护同旧 _render_batch）

        压缩帧防护：新行创建后先隐藏，渲染完成由 _reveal_rows 延迟一帧统一显示。

        原因：QScrollArea(widgetResizable) 的内容 widget 高度更新滞后于行创建
        （QVBoxLayout 的 sizeHint 缓存惰性刷新），若行立即可见，首帧会按
        「视口高度 / 行数」被压缩成几 px 高的窄条堆在左上角，随后才展开到
        正常高度（表现为刷新列表时内容先从左上角出现再展开）。隐藏行不参与
        布局几何分配（sizeHint 仍计入，滚动范围正确），统一显示时直接以正确
        高度出现，跳过压缩帧。
        """
        for p in rows:
            row = self._create_row(p)
            if row is None:
                continue
            row.hide()  # 防 QScrollArea 未扩展前的压缩帧
            # 新行插入「加载更多」按钮之前：按钮随新行自然下移，
            # 避免新行跑到 stretch 后面造成按钮卡在中间（不滚动视口）
            btn = getattr(self, "_load_more_btn", None)
            if btn is not None:
                self._content_layout.insertWidget(self._content_layout.indexOf(btn), row)
            else:
                # 无按钮（全部渲染完，如搜索过滤后补行）：新行必须插到
                # stretch 之前。否则 addWidget 会追加到 stretch 后面 →
                # stretch 被夹在行中间吃满剩余空间 → 中间大段空白、
                # 新行被推到最底部。
                count = self._content_layout.count()
                last = self._content_layout.itemAt(count - 1) if count > 0 else None
                if last is not None and last.widget() is None and last.spacerItem() is not None:
                    self._content_layout.insertWidget(count - 1, row)
                else:
                    self._content_layout.addWidget(row)
            self._row_map[p.get("name", "")] = row
        # 布局无 stretch（全量重建清空后首批）→ 补 stretch
        if rows and not self._layout_has_stretch():
            self._content_layout.addStretch(1)
        if rows:
            # 新行已入 _row_map：恢复任务行 busy（全量重建后行对象是新的，
            # _busy 丢失；排队/运行中任务的行必须保持禁用防重复提交）
            self._restore_task_busy()
            self._schedule_reveal()

    def _layout_has_stretch(self) -> bool:
        """布局是否已含 stretch（全量重建清空后无；复用行路径已有）"""
        for i in range(self._content_layout.count()):
            if self._content_layout.itemAt(i).spacerItem() is not None:
                return True
        return False

    def _schedule_reveal(self):
        """渲染完成后延迟显示新行（等 QScrollArea 内容扩展稳定后一次到位）

        50ms 窗口：QScrollArea 的内容 widget 高度扩展依赖布局 sizeHint 缓存
        刷新（需多轮事件循环），过早 show 会让行在未扩展的受限高度下布局
        （重新出现压缩帧）。50ms 后人眼无感，且保证 show 时行直接以正确
        高度出现。
        用 self 子 QTimer（销毁自动取消），避免无父临时 timer 在卡片
        销毁后触发回调触碰已删除控件（原生崩溃 0xC0000409）。
        """
        if self._reveal_timer is None:
            from PyQt5.QtCore import QTimer

            self._reveal_timer = QTimer(self)
            self._reveal_timer.setSingleShot(True)
            self._reveal_timer.timeout.connect(self._reveal_rows)
        self._reveal_timer.start(50)

    def _reveal_rows(self):
        """显示渲染期间隐藏的插件行（_alive 防护）

        QScrollArea(widgetResizable) 的内容高度扩展依赖 widget sizeHint
        缓存刷新（滞后于行创建），若只 show 行，行会在旧高度（视口高）下
        被压缩布局一帧再展开。因此 show 后立即按最新 sizeHint 手动同步
        resize _content：QVBoxLayout 随即在正确高度下重布局，行一次到位，
        无压缩帧（QScrollArea 后续按相同 sizeHint 覆盖，结果一致）。
        """
        if not self._alive():
            return  # 卡片已销毁，放弃显示
        # 只显示仍匹配当前搜索/筛选的行：reveal timer 可能与搜索过滤
        # 防抖交错（补行会重启 reveal → 晚于 _reconcile_rows 执行），
        # 无差别 show() 会把已被 setVisible(False) 过滤掉的行重新显示
        # （搜索白过滤，表现为「输入后列表没有正确过滤」）。
        # 首屏/刷新（query 空）时全部行匹配 → 行为不变。
        query = self._search_edit.text().strip().lower()
        filter_mode = self._current_filter
        for row in self._row_map.values():
            try:
                if self._plugin_matches(row._meta, query, filter_mode):
                    row.show()
            except RuntimeError:
                pass  # 行已被销毁（并发清理），忽略
        try:
            self._content_layout.activate()
            # widgetResizable=False：手动同步 content 尺寸到实际内容高度，
            # 不依赖 layout.sizeHint()（QLabel wordWrap 的 sizeHint 与实际
            # 布局高度有每行 ~8px 偏差，行多时累积成 stretch 空白区）
            self._sync_content_size()
            # 同步后 content 宽度/高度变化 → 行按新宽度重排（wordWrap
            # 换行数可能变化）→ 行高变化 → 二次同步确保高度准确
            self._sync_content_size()
            # 刷新 QWidget::sizeHint 缓存：布局激活后缓存可能被冻结为
            # 旧值/异常值，不清除会让 QScrollArea 在窗口 resize 等时机
            # 用错误高度撑开 content → 列表底部大段空白
            self._content.updateGeometry()
        except RuntimeError:
            pass  # 卡片已销毁

    def _content_height(self) -> int:
        """按布局内可见行理想高度累加内容高度（不依赖 C++ sizeHint 缓存）

        用 Python 侧 w.sizeHint()（_PluginRow override：已布局时返回
        heightForWidth(当前宽度) = 与实际布局一致的理想高度），避免：
        1. C++ QWidgetItem::sizeHint（QLabel wordWrap 放大值）
        2. 行在受限 content 高度下被压缩后的实际几何高度
        """
        lay = self._content_layout
        spacing = lay.spacing()
        mg = lay.contentsMargins()
        total = 0
        vis = 0
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget()
            if w is not None and w.isVisible():
                total += w.sizeHint().height()
                vis += 1
        return total + spacing * max(0, vis - 1) + mg.top() + mg.bottom()

    def _sync_content_size(self):
        """手动同步列表内容 widget 尺寸（widgetResizable=False 路径）

        两阶段：先用行 sizeHint（heightForWidth）初算撑开 content 高度
        （行此时可能未按新宽度重排，sizeHint 与实际布局有偏差），resize
        触发行重排后，再按行实际几何高度累加校正——保证 content 高度与
        布局实际一致（底部无 stretch 空白区）。宽度 = 视口宽；行少时
        高度保持视口高，stretch 填满底部不出现空白。
        """
        try:
            vp = self._scroll.viewport()
            h1 = self._content_height()
            self._content.resize(vp.width(), max(h1, vp.height()))
            # resize 后行按新宽度重排 → 用实际几何高度校正
            self._content_layout.activate()
            h2 = self._content_height_real()
            if abs(h2 - h1) > 4:
                self._content.resize(vp.width(), max(h2, vp.height()))
        except RuntimeError:
            pass  # 卡片已销毁

    def _content_height_real(self) -> int:
        """按布局内可见行实际几何高度累加内容高度（布局稳定后调用）"""
        lay = self._content_layout
        spacing = lay.spacing()
        mg = lay.contentsMargins()
        total = 0
        vis = 0
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget()
            if w is not None and w.isVisible():
                total += w.height()
                vis += 1
        return total + spacing * max(0, vis - 1) + mg.top() + mg.bottom()

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
                # 立即隐藏：takeAt 移出布局后 widget 无几何管理，deleteLater 前
                # 会残留原位置造成闪帧（与压缩帧同源）
                item.widget().hide()
                item.widget().deleteLater()
            item = None  # 释放 QLayoutItem 引用
        # 同步收缩内容高度 + 刷新 sizeHint 缓存：QScrollArea 的收缩依赖
        # LayoutRequest（滞后多轮事件循环），残留旧高度会让滚动范围保留旧值，
        # 在下一批行 reveal 之前用户可滚到大段空白区（加载更多/刷新竞态场景）
        try:
            vp = self._scroll.viewport()
            self._content.resize(vp.width(), vp.height())
            self._content.updateGeometry()
        except RuntimeError:
            pass  # 卡片已销毁

    def _on_search_text_changed(self):
        """搜索文本变化 → 防抖后触发过滤"""
        self._search_debounce.start(300)

    def _filter_plugins(self):
        """搜索过滤（复用已有行，不重建 widget）"""
        self._reconcile_rows()

    def _reconcile_rows(self):
        """按当前匹配列表协调已渲染行：显隐 + 高亮 + 补行（不重建已有行）

        与 _refresh_row_states 同范式；匹配顺序保持行创建顺序（暂不重排，
        排序轻微不一致可接受）。原实现走 _render_plugins 全量重建，
        行数多时每次搜索都销毁/重建首屏。
        """
        query = self._search_edit.text().strip().lower()
        filter_mode = self._current_filter

        # 刷新安装状态（TTL 3s 缓存，成本低）
        inst_map = get_installer().get_installed_map()
        self._installed_set = set(inst_map)
        self._version_map = inst_map
        self._status_map = get_installer().get_status_map()

        # 重算匹配列表（不重建 widget）
        view_plugins = list(self._all_plugins) + self._build_local_extra_plugins()
        self._matched = [p for p in view_plugins if self._plugin_matches(p, query, filter_mode)]
        self._apply_sort(self._matched)

        # 复用行：可见性 + 搜索高亮（行按匹配顺序创建，布局顺序与匹配一致）
        for name, row in list(self._row_map.items()):
            row.setVisible(self._plugin_matches(row._meta, query, filter_mode))
            row.update_search_highlight(query)

        # 补齐缺失行（无条件调用：_render_next_batch 按 _matched 顺序补
        # 尚无 widget 的插件 + 维护按钮/计数；匹配为空时内部收尾）
        self._render_next_batch()
        # 行显隐变化 → 内容高度变化 → 手动同步（widgetResizable=False 下
        # QScrollArea 不再自动收缩；不同步会残留旧高度 → 底部大段空白）
        self._sync_content_size()
        self._update_empty_state()
        self._update_status()
        self._update_update_badge()

    # ── 异步任务队列（安装/更新/启用/禁用/卸载 串行执行） ──

    @staticmethod
    def _wrapped_task_fn(fn: Callable[[], bool]) -> Callable[[], bool]:
        """包装任务函数：执行后在后台线程预热插件状态缓存

        安装/更新/启用/禁用/卸载都会 invalidate installed/status 缓存。
        若等主线程完成回调（_refresh_row_states）再全量扫描磁盘（数十个
        插件逐个读 manifest）会卡 UI。这里在任务线程内先扫一次填充 TTL
        缓存，主线程完成回调命中缓存（3s TTL）不再阻塞。

        GIL 保护 dict/float 赋值，跨线程共享 TTL 缓存安全（与安装线程
        调 invalidate_installed_cache 同理）。
        """

        def _run():
            ok = fn()
            try:
                get_installer().get_installed_map()
                get_installer().get_status_map()
            except Exception:
                pass  # 预热失败不影响结果（主线程回调自行扫描兜底）
            return ok

        return _run

    def _submit_task(
        self,
        *,
        kind: str,
        name: str,
        fn: Callable[[], bool],
        busy_text: str,
        status_text: str,
        status_color: str,
        meta: Optional[dict] = None,
    ):
        """提交后台任务到串行队列

        队列保证同一时刻只有一个任务线程在跑，避免多个 git 进程并发
        互抢 cache/目标目录（此前多任务共用单个 worker 槽位，新任务
        会 quit 旧线程导致进行中的安装静默终止）。任务完成自动启动
        下一个；行在任务期间保持 busy（按钮禁用，防重复提交）。

        Args:
            kind: "install" | "update" | "enable" | "disable" | "uninstall"
            name: 插件名
            fn: 后台线程执行的阻塞函数，返回 bool
            busy_text: 行按钮 busy 文案（如「安装中…」）
            status_text: 头部状态栏文案
            status_color: 状态栏文字颜色
            meta: 插件元数据（install/update 时传入，用于失败记录一键重试）
        """
        self._task_queue.append(
            {
                "kind": kind,
                "name": name,
                "fn": self._wrapped_task_fn(fn),
                "busy_text": busy_text,
                "status_text": status_text,
                "status_color": status_color,
                "meta": meta,
            }
        )
        # 行立即置 busy（行可能未渲染/已隐藏，忽略）
        self._set_row_busy(name, busy_text)
        if not self._task_active:
            self._run_next_task()

    def _run_next_task(self):
        """从队列取出下一个任务并启动 worker 线程（无任务则停）"""
        if not self._task_queue:
            self._task_active = False
            self._active_task = None
            self._worker_thread = None
            self._worker = None
            return
        self._task_active = True
        task = self._task_queue.pop(0)
        self._active_task = task
        name = task["name"]
        self._status_label.setText(task["status_text"])
        self._status_label.setStyleSheet(f"color: {task['status_color']}; font-size: 12px; background: transparent;")
        # 任务真正开始时行再确认 busy（排队期间可能被其他操作刷新）
        self._set_row_busy(name, task["busy_text"])

        self._worker = _MarketplaceWorker(task["fn"])
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ok, t=task: self._on_task_done(t, bool(ok)))
        self._worker.error.connect(lambda e, t=task: self._on_task_error(t, e))
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_task_done(self, task: dict, success: bool):
        """任务完成：分发到对应类型的完成处理 + 启动下一个任务"""
        if not self._alive():
            return
        kind, name = task["kind"], task["name"]
        # 当前任务已结束（_active_task 清空，防行重建恢复 busy 时误置已完成任务）。
        # 用 is 比较而非无条件清空：每个任务入队都是独立 dict 对象，闭包
        # t=task 捕获的引用与 _active_task 指向一致；is 可防止异常时序下
        # 误清下一个任务的 active 状态。
        if self._active_task is task:
            self._active_task = None
        # 该任务自身行任务已结束：解除 busy，让完成处理能刷新它；
        # 其他排队任务的行保持 busy（_refresh_row_states 跳过逻辑保护）
        self._release_row_busy(name)
        # 记录安装/更新结果（成功/失败 + 失败原因），供代理页「下载/更新记录」展示
        self._record_task_result(task, success)
        if kind == "install":
            self._on_install_done(name, success)
        elif kind == "update":
            self._on_update_done(name, success)
        else:
            action = {"enable": "启用", "disable": "禁用", "uninstall": "卸载"}[kind]
            self._on_manage_done(name, action, success)
        self._run_next_task()

    def _on_task_error(self, task: dict, err: str):
        """任务出错：分发到对应类型的错误处理 + 启动下一个任务"""
        if not self._alive():
            return
        kind, name = task["kind"], task["name"]
        if self._active_task is task:
            self._active_task = None
        self._release_row_busy(name)
        # 记录安装/更新失败（异常路径，err 为完整错误信息）
        self._record_task_result(task, False, err)
        if kind == "install":
            self._on_install_error(name, err)
        elif kind == "update":
            self._on_update_error(name, err)
        else:
            action = {"enable": "启用", "disable": "禁用", "uninstall": "卸载"}[kind]
            self._on_manage_error(name, action, err)
        self._run_next_task()

    def _record_task_result(self, task: dict, success: bool, err: str = ""):
        """记录安装/更新结果到持久化记录（代理页「下载/更新记录」展示 + 失败重试）

        仅记录 install/update 动作（enable/disable/uninstall 不记录）。
        失败原因：异常路径用 err；installer 内部返回 False 时取
        installer.last_error（_download_and_move 已格式化 stderr）。
        """
        kind = task.get("kind")
        if kind not in ("install", "update"):
            return
        name = task.get("name", "")
        if not name:
            return
        error = ""
        if not success:
            error = (err or get_installer().last_error or "操作失败")[:500]
        try:
            get_records().add(kind, name, success, error=error, meta=task.get("meta"))
        except Exception as e:
            logger.warning(f"[Marketplace] 记录下载/更新结果失败: {e}")
        self._refresh_records_ui()

    def _release_row_busy(self, name: str):
        """解除指定插件行的 busy（仅任务自身行；行不存在/未 busy 忽略）"""
        row = self._row_map.get(name)
        if row is not None and row._busy:
            row._busy = False
            row._busy_text = ""
            row._update_btn_text()

    def _set_row_busy(self, name: str, busy_text: str):
        """将指定插件行置为 busy（任务入队/开始时调用，行不存在则忽略）

        行已 busy 时仅刷新文案不重复禁用（避免覆盖更精确的状态）。
        """
        row = self._row_map.get(name)
        if row is not None:
            if not row._busy:
                row._busy = True
                row._busy_text = busy_text
                row._update_btn_text()
            elif row._busy_text != busy_text:
                row._busy_text = busy_text
                row._update_btn_text()

    def _restore_task_busy(self):
        """行重建/补行后恢复任务行的 busy 状态（下载中/安装中等）

        行对象销毁重建（_render_plugins 全量重建，如切 tab 回来 show_card
        触发重渲染）后 _busy 状态丢失，任务实际仍在队列/线程中运行；
        这里从任务队列 + 当前活动任务恢复 busy，防止「下载中」状态消失、
        按钮被误点重复提交同一插件。
        已完成任务不在队列/_active_task 中，不会误恢复。
        """
        tasks = list(self._task_queue)
        if self._active_task is not None:
            tasks.insert(0, self._active_task)
        for task in tasks:
            name = task.get("name")
            if not name:
                continue  # 插件数据缺 name：无行可恢复，跳过
            self._set_row_busy(name, task.get("busy_text", ""))

    # ── 异步安装 ──

    def _async_install(self, plugin_meta: dict):
        """安装插件（入串行队列，后台线程执行）"""
        name = plugin_meta.get("name", "")
        self._submit_task(
            kind="install",
            name=name,
            fn=lambda m=plugin_meta: get_installer().install(m),
            busy_text="安装中…",
            status_text="安装中…",
            status_color=_text_color(secondary=True),
            meta=plugin_meta,
        )

    def _on_install_done(self, name: str, success: bool):
        """安装完成"""
        if not self._alive():
            return  # 卡片已销毁
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
        if not self._alive():
            return  # 卡片已销毁
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
        """更新插件：先下载新版，下载成功后再替换旧版（入串行队列）

        点击后该行置为「下载中…」（保持已安装态，旧版未删）；
        下载成功才备份旧版并替换，失败保留旧版插件可用。
        """
        name = plugin_meta.get("name", "")
        # 立即反馈：该行置为「下载中…」（旧版仍可用，仅按钮禁用）
        self._set_row_downloading(name)
        # 主线程先卸载旧版 UI 组件（显示中卡片自动关闭）；下载成功替换前
        # installer 内还会 purge 模块缓存释放文件句柄
        self._unload_plugin_ui_on_gui(name)

        self._submit_task(
            kind="update",
            name=name,
            fn=lambda m=plugin_meta: get_installer().update(m),
            busy_text="下载中…",
            status_text="更新中…",
            status_color="#FFA726",
            meta=plugin_meta,
        )

    def _set_row_downloading(self, name: str):
        """将该插件行立即置为「下载中…」（保留已安装态，旧版未删）"""
        row = self._row_map.get(name)
        if row is not None:
            row.set_downloading()

    def _on_update_done(self, name: str, success: bool):
        """更新完成"""
        if not self._alive():
            return  # 卡片已销毁
        self._status_label.setText("")
        # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        if success:
            # 更新替换了整个插件目录：_async_update 下载前已卸载旧 UI，
            # 此处必须主动重载（watchfiles 对目录级替换识别为空组件，不触发
            # UI 重载——否则插件 UI 保持卸载态直到重启/禁用再启用）。
            self._reload_plugin_ui_on_gui(name)
            self._refresh_row_states()
            InfoBar.success(f"{name} 更新成功", "", duration=2000, parent=bar_parent)
        else:
            # 下载失败：旧版保留（未删），行恢复「更新」按钮可重试；
            # UI 已在下载前被卸载，同样需要重载旧版组件恢复可用。
            self._reload_plugin_ui_on_gui(name)
            self._update_row_state(name, installed=True, error=True)
            InfoBar.error(f"{name} 更新失败", "已保留旧版，可稍后重试", duration=3000, parent=bar_parent)

    def _on_update_error(self, name: str, err: str):
        """更新出错"""
        if not self._alive():
            return  # 卡片已销毁
        self._status_label.setText("更新失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        # 下载失败：旧版保留（未删），行恢复「更新」按钮可重试
        self._update_row_state(name, installed=True, error=True)
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

    def _reload_plugin_ui_on_gui(self, name: str):
        """主线程：更新后重新加载插件 UI 组件（对称于 _unload_plugin_ui_on_gui）

        更新流程在下载前就调用了 _unload_plugin_ui_on_gui 卸载 UI，但替换目录
        产生的 watchfiles 事件是目录级（deleted/added 落在插件根路径），组件识别
        为空 → _reload_single_plugin(name, "") 空组件跳过所有子系统，UI 不会自动
        回来，只能等重启或禁用再启用。因此更新完成（无论成功失败）必须在此
        显式重载：先 rescan 刷新 PluginManager 元数据，再 load_plugin 加载新版
        （失败时目录仍是旧版，重载旧版恢复可用）。load_plugin 对已加载插件先
        卸载再加载，幂等；重复调用无副作用。
        """
        try:
            from app.core.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            pm.rescan_plugin(name)
            plugin = pm.get_plugin(name)
            if plugin is not None and plugin.has_component("ui"):
                from app.core.ui_plugin_registry import UIPluginRegistry

                UIPluginRegistry.get_instance().load_plugin(name, plugin.path)
        except Exception as e:
            logger.debug(f"[Marketplace] 更新后重载插件 UI({name}) 失败（忽略）: {e}")

    def _unload_plugin_ui_on_gui(self, name: str):
        """主线程：卸载插件 UI 组件（含显示中卡片的自动关闭）

        必须在主线程执行（QThread worker 中操作 Qt 控件会闪退）：
        UI 插件加载后其浮动卡片 widget / 命令 / 渲染回调引用 plugin ui 模块与
        控件；若卡片处于显示状态，直接后台删目录会因引用/句柄失败甚至崩溃。
        本方法先调用 UIPluginRegistry.unload_plugin —— 内部会遍历检测该插件
        所有已创建卡片实例，显示中的自动关闭（hide + 容器移除 + deleteLater），
        随后清理注册表项与命令。

        调用方保证：在启动删除目录的后台线程之前调用本方法。
        """
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().unload_plugin(name)
        except Exception as e:
            logger.debug(f"[Marketplace] unload_plugin UI({name}) 失败（忽略）: {e}")

    def _async_enable(self, plugin_meta: dict):
        """启用已禁用的插件（入串行队列）"""
        name = plugin_meta.get("name", "")
        self._submit_task(
            kind="enable",
            name=name,
            fn=lambda n=name: get_installer().enable(n),
            busy_text="启用中…",
            status_text="启用中…",
            status_color="#4CAF50",
        )

    def _async_disable(self, plugin_meta: dict):
        """禁用已启用的插件（入串行队列）"""
        name = plugin_meta.get("name", "")
        # 主线程先卸载 UI 组件（显示中卡片自动关闭），再后台 move 目录
        self._unload_plugin_ui_on_gui(name)

        self._submit_task(
            kind="disable",
            name=name,
            fn=lambda n=name: get_installer().disable(n),
            busy_text="禁用中…",
            status_text="禁用中…",
            status_color="#FF9800",
        )

    def _async_uninstall(self, plugin_meta: dict):
        """卸载插件（入串行队列，确认后）"""
        name = plugin_meta.get("name", "")
        if not self._confirm_uninstall(name):
            return
        # 主线程先卸载 UI 组件（显示中卡片自动关闭），再后台删目录
        self._unload_plugin_ui_on_gui(name)

        self._submit_task(
            kind="uninstall",
            name=name,
            fn=lambda n=name: get_installer().uninstall(n),
            busy_text="卸载中…",
            status_text="卸载中…",
            status_color="#F44336",
        )

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
        if not self._alive():
            return  # 卡片已销毁
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
        if not self._alive():
            return  # 卡片已销毁
        self._status_label.setText(f"{action}失败")
        self._status_label.setStyleSheet("color: rgba(255,80,80,0.7); font-size: 12px; background: transparent;")
        self._refresh_row_states()
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()
        InfoBar.error(f"{name} {action}失败", str(err)[:120], duration=5000, parent=bar_parent)

    def _refresh_row_states(self):
        """安装/更新/卸载后刷新：扫描段后台执行，主线程回调只做 UI 段

        不重建列表（保留滚动位置），仅同步状态、可见性与匹配计数。

        扫描段（get_installed_map/get_status_map/本地 extras manifest 读取）
        移到后台 worker（复用 _MarketplaceWorker + QThread），完成后回主线程
        只做 UI 段（行状态同步/可见性/补行/计数）——根治主线程磁盘扫描
        （TTL miss 兜底 + _build_local_extra_plugins 读 manifest）卡顿。
        """
        # in-flight 合并：已有扫描 worker 在跑 → 记 pending，完成后自动补扫
        if getattr(self, "_rs_fetch_thread", None) is not None:
            self._rs_fetch_pending = True
            return

        # 快照主线程状态（worker 线程只读快照，不碰 self 可变 UI 态）
        all_plugins = list(self._all_plugins)
        market_names = frozenset(p.get("name", "") for p in all_plugins)

        def _run():
            try:
                inst_map = get_installer().get_installed_map()
                status_map = get_installer().get_status_map()
                extras = self._build_local_extra_plugins_bg(status_map, market_names, inst_map)
                return inst_map, status_map, extras
            except Exception as e:
                logger.warning(f"[Marketplace] 行状态扫描失败: {e}")
                return None

        self._cleanup_rs_worker()
        self._rs_fetch_pending = False
        self._rs_worker = _MarketplaceWorker(_run)
        self._rs_fetch_thread = QThread(self)
        self._rs_worker.moveToThread(self._rs_fetch_thread)
        self._rs_fetch_thread.started.connect(self._rs_worker.run)
        self._rs_worker.finished.connect(self._on_row_states_scanned)
        self._rs_worker.error.connect(lambda e: self._on_row_states_scanned(None))
        self._rs_worker.finished.connect(self._rs_fetch_thread.quit)
        self._rs_worker.error.connect(self._rs_fetch_thread.quit)
        self._rs_worker.finished.connect(self._rs_worker.deleteLater)
        self._rs_worker.error.connect(self._rs_worker.deleteLater)
        self._rs_fetch_thread.finished.connect(self._rs_fetch_thread.deleteLater)
        self._rs_fetch_thread.start()

    def _on_row_states_scanned(self, result):
        """后台扫描完成：更新状态字段 + 只做 UI 段（主线程）"""
        if not self._alive():
            return
        # 本轮 worker 已结束：释放引用（deleteLater 链由 connect 处理）
        self._rs_fetch_thread = None
        self._rs_worker = None
        # in-flight 期间又有新请求 → 用最新状态补扫一次（不重复做 UI 段）
        if getattr(self, "_rs_fetch_pending", False):
            self._rs_fetch_pending = False
            self._refresh_row_states()
            return
        if result is None:
            return
        inst_map, status_map, extras = result
        self._installed_set = set(inst_map)
        self._version_map = inst_map
        self._status_map = status_map
        # 同步 extras 缓存（与 _build_local_extra_plugins 的 key 语义一致，
        # 供 _reconcile_rows 等主线程路径复用，避免重复扫描）
        market_names = frozenset(p.get("name", "") for p in self._all_plugins)
        self._local_extras_key = (status_map, market_names)
        self._local_extras_cache = extras

        # ── UI 段（原 _refresh_row_states 后半）──
        query = self._search_edit.text().strip().lower()
        filter_mode = self._current_filter
        # 重算匹配列表（不重建 widget）
        view_plugins = list(self._all_plugins) + extras
        self._matched = [p for p in view_plugins if self._plugin_matches(p, query, filter_mode)]
        self._apply_sort(self._matched)

        for name, row in self._row_map.items():
            if row._busy:
                # 任务进行中/排队中的行保持 busy（按钮禁用），
                # 不被其他任务完成回调重置为「安装」——并发安装时
                # 一个装完不能把其他待安装的按钮恢复
                continue
            p = row._meta
            installed, has_update, local_ver, status = self._row_state(p)
            row.apply_state(installed, has_update, local_ver, status)
            row.setVisible(self._plugin_matches(p, query, filter_mode))
            row.update_search_highlight(query)

        # 补齐缺失行 + 更新按钮/计数（无条件调用：安装/卸载后匹配列表
        # 可能新增行，按 _matched 顺序补尚无 widget 的插件）
        self._render_next_batch()
        # 行显隐/按钮变化 → 内容高度变化 → 手动同步（widgetResizable=False）
        self._sync_content_size()
        self._update_empty_state()
        self._update_status()
        self._update_update_badge()

    def _cleanup_rs_worker(self):
        """安全清理行状态扫描 worker/thread（复用 _orphan_worker_thread 剥离模式）

        与任务/market 拉取 worker 对齐：quit + setParent(None) + 孤儿列表强引用，
        不阻塞主线程；若线程仍 running 且卡片析构，剥离父子防止随卡片销毁
        （"QThread: Destroyed while thread is still running" 崩溃窗口）。
        """
        thread = getattr(self, "_rs_fetch_thread", None)
        self._rs_fetch_thread = None
        self._rs_worker = None
        self._orphan_worker_thread(thread)

    def _update_row_state(self, name: str, installed: bool, error: bool = False, updated: bool = False):
        """更新某插件行的状态

        Args:
            name: 插件名称
            installed: 是否已安装
            error: 操作是否出错
            updated: 是否刚完成更新（需刷新版本显示）
        """
        row = self._row_map.get(name)
        if row is None:
            return
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

    # ── 加速配置 ──

    def _build_proxy_page(self):
        """构建「加速」tab（单一外框：启用 / 配置 / 帮助三段，分隔线隔开）

        首次构建后缓存；再次切换不重载表单（保留用户未保存的
        开关/地址状态，避免切走再切回时被磁盘旧值覆盖）。
        """
        if getattr(self, "_proxy_built", False):
            return
        tc = getattr(self, "_cached_tc", None) or _text_color()
        tcs = getattr(self, "_cached_tcs", None) or _text_color(secondary=True)
        card_bg = getattr(self, "_cached_theme_colors", {}).get("content_bg", "#2a2a2e")
        border_c = getattr(self, "_cached_theme_colors", {}).get("border", "rgba(128,128,128,0.15)")
        accent = getattr(self, "_cached_theme_colors", {}).get("accent", "#62a0ea")
        # 字号/字体跟随 UI 上下文（fs 为基础字号，段标题 fs-1，正文/状态 fs-2）
        fs = getattr(self, "_cached_font_size", 0) or 14
        ff = getattr(self, "_cached_font_family", "") or ""
        fs_title = max(10, fs - 1)
        fs_body = max(9, fs - 2)
        ff_qss = f" font-family: '{ff}';" if ff else ""

        root = self._proxy_page.layout()

        # ── 外部单一容器框（唯一带边框/背景的卡片）──
        # 关键：QSS 必须用 ID 选择器限定自身，否则 border/background 会级联
        # 应用到所有子控件（QLabel/SwitchButton 全部带边框）。
        outer = QWidget(self._proxy_page)
        outer.setObjectName("proxyOuter")
        outer.setAttribute(Qt.WA_StyledBackground, True)
        outer.setStyleSheet(
            f"#proxyOuter {{ background: {card_bg}; border: 1px solid {border_c}; border-radius: 8px; }}"
        )
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(14, 4, 14, 4)
        outer_layout.setSpacing(0)

        # ── 段1：启用 ──
        enable_row = QWidget(outer)
        enable_layout = QHBoxLayout(enable_row)
        enable_layout.setContentsMargins(0, 8, 0, 8)
        title_lb = QLabel("⚡ 启用", enable_row)
        title_lb.setObjectName("proxySectionTitle")
        title_lb.setStyleSheet(f"color: {tcs}; font-size: {fs_title}px; background: transparent;{ff_qss}")
        enable_layout.addWidget(title_lb)
        enable_layout.addStretch(1)
        # 连接状态行（原测试连接按钮下方）移到这里：开关左侧显示
        self._proxy_status_label = QLabel("", enable_row)
        self._proxy_status_label.setObjectName("proxyStatus")
        self._proxy_status_label.setStyleSheet(
            f"color: {accent}; font-size: {fs_body}px; background: transparent;{ff_qss}"
        )
        self._proxy_status_label.setWordWrap(True)
        enable_layout.addWidget(self._proxy_status_label)
        self._proxy_switch = SwitchButton(enable_row)
        # 初始 setChecked 不应触发 toggled 自动保存（后续控件尚未创建）
        self._proxy_switch.blockSignals(True)
        self._proxy_switch.setChecked(False)
        self._proxy_switch.blockSignals(False)
        self._proxy_switch.checkedChanged.connect(self._on_proxy_switch_toggled)
        enable_layout.addWidget(self._proxy_switch)
        outer_layout.addWidget(enable_row)

        # ── 分隔线1 ──
        sep1 = QFrame(outer)
        sep1.setFrameShape(QFrame.HLine)
        sep1.setStyleSheet(f"background: {border_c}; max-height: 1px; border: none;")
        outer_layout.addWidget(sep1)

        # ── 段2：配置 ──
        config_widget = QWidget(outer)
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(0, 10, 0, 8)
        config_layout.setSpacing(8)

        cfg_title = QLabel("⚙ 配置", config_widget)
        cfg_title.setObjectName("proxySectionTitle")
        cfg_title.setStyleSheet(f"color: {tcs}; font-size: {fs_title}px; background: transparent;{ff_qss}")
        config_layout.addWidget(cfg_title)

        self._proxy_mode_combo = ComboBox(config_widget)
        self._proxy_mode_combo.addItem("前缀加速站", userData="prefix")
        self._proxy_mode_combo.addItem("自建代理服务", userData="selfhost")
        self._proxy_mode_combo.addItem("HTTP 正向代理", userData="http")
        self._proxy_mode_combo.setCurrentIndex(0)
        self._proxy_mode_combo.currentIndexChanged.connect(self._on_proxy_mode_changed)
        config_layout.addWidget(self._proxy_mode_combo)

        self._proxy_addr_edit = LineEdit(config_widget)
        self._proxy_addr_edit.setPlaceholderText("https://ghfast.top/")
        self._proxy_addr_edit.setClearButtonEnabled(True)
        config_layout.addWidget(self._proxy_addr_edit)

        btn_row = QWidget(config_widget)
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)
        btn_layout.addStretch(1)  # 右对齐：按钮组靠右
        test_btn = PushButton("测试连接", btn_row, FluentIcon.CAFE)
        test_btn.clicked.connect(self._on_proxy_test)
        btn_layout.addWidget(test_btn)
        save_btn = PushButton("保存", btn_row, FluentIcon.SAVE)
        save_btn.clicked.connect(self._on_proxy_save)
        btn_layout.addWidget(save_btn)
        config_layout.addWidget(btn_row)

        outer_layout.addWidget(config_widget)

        # ── 分隔线2 ──
        sep2 = QFrame(outer)
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet(f"background: {border_c}; max-height: 1px; border: none;")
        outer_layout.addWidget(sep2)

        # ── 段3：帮助 ──
        help_widget = QWidget(outer)
        help_layout = QVBoxLayout(help_widget)
        help_layout.setContentsMargins(0, 10, 0, 8)
        help_layout.setSpacing(4)
        help_title = QLabel("💡 帮助", help_widget)
        help_title.setObjectName("proxySectionTitle")
        help_title.setStyleSheet(f"color: {tcs}; font-size: {fs_title}px; background: transparent;{ff_qss}")
        help_layout.addWidget(help_title)
        self._proxy_help_label = QLabel("", help_widget)
        self._proxy_help_label.setObjectName("proxyHelp")
        self._proxy_help_label.setStyleSheet(f"color: {tcs}; font-size: {fs_body}px; background: transparent;{ff_qss}")
        self._proxy_help_label.setWordWrap(True)
        help_layout.addWidget(self._proxy_help_label)
        outer_layout.addWidget(help_widget)

        root.addWidget(outer)

        # ── 段4：下载 / 更新记录 ──
        # 独立外框（与代理配置框平级）：标题行 + 清空 + 固定高滚动列表
        records_outer = QWidget(self._proxy_page)
        records_outer.setObjectName("proxyRecordsOuter")
        records_outer.setAttribute(Qt.WA_StyledBackground, True)
        records_outer.setStyleSheet(
            f"#proxyRecordsOuter {{ background: {card_bg}; border: 1px solid {border_c}; border-radius: 8px; }}"
        )
        records_layout = QVBoxLayout(records_outer)
        records_layout.setContentsMargins(14, 4, 14, 8)
        records_layout.setSpacing(4)

        # 标题行：标题 + 清空按钮（右对齐）
        rec_title_row = QWidget(records_outer)
        rec_title_layout = QHBoxLayout(rec_title_row)
        rec_title_layout.setContentsMargins(0, 8, 0, 4)
        rec_title = QLabel("📜 下载 / 更新记录", rec_title_row)
        rec_title.setObjectName("proxyRecordTitle")
        rec_title.setStyleSheet(f"color: {tcs}; font-size: {fs_title}px; background: transparent;{ff_qss}")
        rec_title_layout.addWidget(rec_title)
        rec_title_layout.addStretch(1)
        clear_btn = QPushButton("清空", rec_title_row)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setFixedHeight(22)
        clear_btn.setStyleSheet(
            f"QPushButton {{ background: rgba(128,128,128,0.12); color: {tcs};"
            f" border: none; border-radius: 5px; padding: 0 10px;{ff_qss}"
            f" font-size: {max(8, fs - 2)}px; }}"
            "QPushButton:hover { background: rgba(128,128,128,0.22); }"
        )
        clear_btn.clicked.connect(self._on_clear_records)
        rec_title_layout.addWidget(clear_btn)
        records_layout.addWidget(rec_title_row)

        # 记录滚动列表（占满代理页剩余空间，行多时滚动）
        rec_scroll = QScrollArea(records_outer)
        rec_scroll.setWidgetResizable(True)
        rec_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rec_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._records_container = QWidget(rec_scroll)
        self._records_container.setStyleSheet("background: transparent;")
        self._records_layout = QVBoxLayout(self._records_container)
        self._records_layout.setContentsMargins(0, 0, 4, 0)
        self._records_layout.setSpacing(2)
        rec_scroll.setWidget(self._records_container)
        records_layout.addWidget(rec_scroll, 1)

        root.addWidget(records_outer, 1)
        self._proxy_built = True
        self._load_proxy_form()
        self._refresh_records_ui()

    # ── 下载 / 更新记录 ─────────────────────────────────

    def _refresh_records_ui(self):
        """重建代理页「下载/更新记录」列表（代理页未构建时跳过）

        记录在任务完成回调（_record_task_result）与首次构建代理页时
        触发；行太多时靠外层固定高度滚动区浏览。
        """
        container = getattr(self, "_records_container", None)
        if container is None:
            return
        layout = self._records_layout
        # 清空旧行
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        tc = getattr(self, "_cached_tc", None) or _text_color()
        tcs = getattr(self, "_cached_tcs", None) or _text_color(secondary=True)
        fs = getattr(self, "_cached_font_size", 0) or 14
        ff = getattr(self, "_cached_font_family", "") or ""
        ff_qss = f" font-family: '{ff}';" if ff else ""
        fs_row = max(8, fs - 2)
        fs_time = max(7, fs - 3)

        records = get_records().get(limit=50)
        if not records:
            empty = QLabel("暂无安装 / 更新记录", container)
            empty.setObjectName("proxyRecordRow")
            empty.setStyleSheet(f"color: {tcs}; font-size: {fs_row}px; background: transparent;{ff_qss}")
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty)
            layout.addStretch(1)
            return

        for rec in records:
            layout.addWidget(self._build_record_row(rec, tc, tcs, ff, ff_qss, fs_row, fs_time))
        layout.addStretch(1)

    def _build_record_row(
        self, rec: dict, tc: str, tcs: str, ff: str, ff_qss: str, fs_row: int, fs_time: int
    ) -> QWidget:
        """构建一条记录行：状态点 + 动作 + 插件名 + 失败原因 + 时间 +（失败时）重试按钮"""
        row = QWidget(self._records_container)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)

        success = bool(rec.get("success"))
        color = "#4caf50" if success else "#ef5350"
        dot = QLabel("✓" if success else "✗", row)
        dot.setObjectName("proxyRecordRow")
        dot.setStyleSheet(f"color: {color}; font-size: {fs_row}px; background: transparent;{ff_qss}")
        lay.addWidget(dot)

        action = "安装" if rec.get("action") == "install" else "更新"
        act_lb = QLabel(action, row)
        act_lb.setObjectName("proxyRecordRow")
        act_lb.setStyleSheet(f"color: {tcs}; font-size: {fs_row}px; background: transparent;{ff_qss}")
        lay.addWidget(act_lb)

        name = rec.get("name", "")
        name_lb = QLabel(name, row)
        name_lb.setObjectName("proxyRecordRow")
        name_lb.setStyleSheet(f"color: {tc}; font-size: {fs_row}px; background: transparent;{ff_qss}")
        name_lb.setMaximumWidth(160)
        name_lb.setToolTip(name)
        lay.addWidget(name_lb)

        error = rec.get("error", "")
        if error:
            err_lb = QLabel(error, row)
            err_lb.setObjectName("proxyRecordRow")
            err_lb.setStyleSheet(f"color: #ef5350; font-size: {fs_row}px; background: transparent;{ff_qss}")
            err_lb.setMaximumWidth(180)
            err_lb.setToolTip(error)
            lay.addWidget(err_lb)

        lay.addStretch(1)

        ts = rec.get("time", 0)
        time_lb = QLabel(time.strftime("%m-%d %H:%M", time.localtime(ts)), row)
        time_lb.setObjectName("proxyRecordTime")
        time_lb.setStyleSheet(f"color: {tcs}; font-size: {fs_time}px; background: transparent;{ff_qss}")
        lay.addWidget(time_lb)

        if not success:
            retry_btn = QPushButton("重试", row)
            retry_btn.setCursor(Qt.PointingHandCursor)
            retry_btn.setFixedHeight(22)
            retry_btn.setStyleSheet(
                f"QPushButton {{ background: rgba(64,158,255,0.18); color: #62a0ea;"
                f" border: none; border-radius: 5px; padding: 0 12px;{ff_qss}"
                f" font-size: {fs_row}px; }}"
                "QPushButton:hover { background: rgba(64,158,255,0.32); }"
            )
            if rec.get("meta"):
                retry_btn.clicked.connect(lambda checked=False, r=rec: self._retry_record(r))
            else:
                retry_btn.setEnabled(False)
                retry_btn.setToolTip("缺少插件元数据，无法重试")
            lay.addWidget(retry_btn)

        return row

    def _retry_record(self, rec: dict):
        """失败记录一键重试：重试原动作（安装→重新安装，更新→重新更新）"""
        meta = rec.get("meta")
        if not meta:
            self._show_proxy_info("缺少插件元数据，无法重试", error=True)
            return
        if rec.get("action") == "install":
            self._async_install(meta)
        else:
            self._async_update(meta)

    def _on_clear_records(self):
        """清空下载/更新记录"""
        try:
            get_records().clear()
        except Exception as e:
            logger.warning(f"[Marketplace] 清空记录失败: {e}")
        self._refresh_records_ui()
        self._show_proxy_info("已清空记录")

    def _load_proxy_form(self):
        """把当前配置填充到表单"""
        proxy = get_proxy_config()
        # blockSignals：加载磁盘值不应触发 _on_proxy_switch_toggled 的自动保存
        self._proxy_switch.blockSignals(True)
        self._proxy_switch.setChecked(proxy.enabled)
        self._proxy_switch.blockSignals(False)
        idx = self._proxy_mode_combo.findData(proxy.mode)
        if idx >= 0:
            self._proxy_mode_combo.setCurrentIndex(idx)
        self._proxy_addr_edit.setText(proxy.address)
        self._update_proxy_help(proxy.mode)
        if proxy.enabled:
            self._proxy_status_label.setText("已启用 · 上次保存后未改动")
        else:
            self._proxy_status_label.setText("")

    def _on_proxy_switch_toggled(self, checked: bool):
        """开关切换即保存 enabled 状态（地址非法时回弹，避免开启一个无效配置）"""
        mode = self._proxy_mode_combo.currentData()
        address = self._proxy_addr_edit.text().strip()
        ok, msg = get_proxy_config().validate(mode, address)
        if not ok:
            # 非法地址：回弹开关并提示先填地址
            self._proxy_switch.blockSignals(True)
            self._proxy_switch.setChecked(False)
            self._proxy_switch.blockSignals(False)
            self._proxy_status_label.setStyleSheet(self._proxy_status_style("#ef5350"))
            self._proxy_status_label.setText(f"请先填写有效地址: {msg}")
            return
        if get_proxy_config().save(checked, mode, address):
            color = "#4caf50" if checked else "rgba(255,255,255,0.5)"
            self._proxy_status_label.setStyleSheet(self._proxy_status_style(color))
            self._proxy_status_label.setText(
                f"已启用 {time.strftime('%H:%M:%S')}" if checked else f"已停用 {time.strftime('%H:%M:%S')}"
            )

    def _on_proxy_mode_changed(self, index: int):
        mode = self._proxy_mode_combo.itemData(index)
        self._update_proxy_help(mode)

    def _update_proxy_help(self, mode: str):
        texts = {
            "prefix": "前缀加速站：填公共加速站地址，如 https://ghfast.top/。\n请求会自动拼接为 加速站地址 + 原 GitHub 链接。",
            "selfhost": "自建代理服务：填你自己部署的代理地址，如 https://my-proxy.deno.dev/。\n适合官方/公共加速站不可用的网络环境。",
            "http": "HTTP 正向代理：填代理工具地址（含端口），如 http://127.0.0.1:7890。\n需先启动 Clash 等代理工具。",
        }
        placeholders = {
            "prefix": "https://ghfast.top/",
            "selfhost": "https://my-proxy.deno.dev/",
            "http": "http://127.0.0.1:7890",
        }
        self._proxy_help_label.setText(texts.get(mode, ""))
        self._proxy_addr_edit.setPlaceholderText(placeholders.get(mode, ""))

    def _on_proxy_test(self):
        """用当前表单值测试连通性（后台线程，不落盘）"""
        mode = self._proxy_mode_combo.currentData()
        address = self._proxy_addr_edit.text().strip()
        self._proxy_status_label.setText("测试中…")
        self._proxy_status_label.repaint()
        proxy = get_proxy_config()

        def _run():
            return proxy.test_connection(mode, address)

        self._cleanup_proxy_worker()
        self._proxy_worker = _MarketplaceWorker(_run)
        self._proxy_thread = QThread(self)
        self._proxy_worker.moveToThread(self._proxy_thread)
        self._proxy_thread.started.connect(self._proxy_worker.run)
        self._proxy_worker.finished.connect(self._on_proxy_test_done)
        self._proxy_worker.error.connect(lambda e: self._on_proxy_test_done(("✗ 失败", e)))
        self._proxy_worker.finished.connect(self._proxy_thread.quit)
        self._proxy_worker.error.connect(self._proxy_thread.quit)
        self._proxy_worker.finished.connect(self._proxy_worker.deleteLater)
        self._proxy_worker.error.connect(self._proxy_worker.deleteLater)
        self._proxy_thread.finished.connect(self._proxy_thread.deleteLater)
        self._proxy_thread.start()

    def _on_proxy_test_done(self, result):
        if not self._alive():
            return
        if isinstance(result, tuple) and len(result) == 2:
            ok, msg = result
        else:
            ok, msg = False, str(result)
        color = "#4caf50" if ok else "#ef5350"
        self._proxy_status_label.setStyleSheet(self._proxy_status_style(color))
        self._proxy_status_label.setText(msg)

    def _cleanup_proxy_worker(self):
        if getattr(self, "_proxy_thread", None) is not None:
            try:
                self._proxy_thread.quit()
                self._proxy_thread.wait(500)
            except RuntimeError:
                pass
            self._proxy_thread = None
        self._proxy_worker = None

    def _on_proxy_save(self):
        """保存配置；非法地址 InfoBar 提示不保存"""
        enabled = self._proxy_switch.isChecked()
        mode = self._proxy_mode_combo.currentData()
        address = self._proxy_addr_edit.text().strip()
        ok, msg = get_proxy_config().validate(mode, address)
        if not ok:
            self._proxy_status_label.setStyleSheet(self._proxy_status_style("#ef5350"))
            self._proxy_status_label.setText(msg)
            self._show_proxy_info(msg, error=True)
            return
        if get_proxy_config().save(enabled, mode, address):
            self._proxy_status_label.setStyleSheet(self._proxy_status_style("#4caf50"))
            self._proxy_status_label.setText(f"已保存 {time.strftime('%H:%M:%S')}")
            self._show_proxy_info("代理配置已保存")
        else:
            self._show_proxy_info("保存失败", error=True)

    def _proxy_status_style(self, color: str) -> str:
        """状态行 QSS：字号/字体跟随 UI 上下文（fs-2），仅替换颜色"""
        fs = getattr(self, "_cached_font_size", 0) or 14
        ff = getattr(self, "_cached_font_family", "") or ""
        ff_qss = f" font-family: '{ff}';" if ff else ""
        return f"color: {color}; font-size: {max(9, fs - 2)}px; background: transparent;{ff_qss}"

    def _show_proxy_info(self, text: str, error: bool = False):
        """InfoBar 统一挂到 tab 管理器顶层窗口（与市场其它提示一致）"""
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            parent = TabManagerWindow.get_instance() or self.window()
            if error:
                InfoBar.error(text, "", duration=3000, parent=parent)
            else:
                InfoBar.success(text, "", duration=2000, parent=parent)
        except Exception:
            pass

    # ── 标签切换 ──

    def _on_tab_changed(self, key: str):
        """标签切换"""
        if key == "browse":
            self._page_stack.setCurrentIndex(0)
            # 浏览页可能在校验/后台刷新期间以「不可见」状态完成渲染：
            # _reveal_rows 在浏览页隐藏时执行，行宽度未布局 → content 高度
            # 被压成视口高 → 行压缩堆叠（压缩帧）。切回浏览页后 QStackedWidget
            # 切页不改变视口尺寸，不会触发 viewport resize 事件过滤器重新
            # 同步 → 行保持压缩。这里强制重新 reveal + sync 一次修正高度。
            self._schedule_reveal()
        elif key == "markets":
            self._build_markets_page()
            self._page_stack.setCurrentIndex(1)
        elif key == "proxy":
            self._build_proxy_page()
            self._page_stack.setCurrentIndex(2)

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
                # 下拉列表滚动条：对齐主程序 ComboBoxStyles.dark_combo_dropdown 规范
                f"QComboBox QAbstractItemView QScrollBar:vertical {{ background: {card_bg};"
                " border: none; width: 14px; margin: 4px 2px 4px 2px; }"
                "QComboBox QAbstractItemView QScrollBar::add-line:vertical,"
                " QComboBox QAbstractItemView QScrollBar::sub-line:vertical { height: 0px; }"
                "QComboBox QAbstractItemView QScrollBar::add-page:vertical,"
                " QComboBox QAbstractItemView QScrollBar::sub-page:vertical { background: none; }"
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
        if self._updates_badge is None:
            return
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

        # 「全部」在前：显示当前 tab 插件总数（而非来源市场数）
        total = len(self._all_plugins or []) + len(self._build_local_extra_plugins())
        self._source_layout.addWidget(_make_source_btn(f"全部 ({total})", ""))
        for src, cnt in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0])):
            self._source_layout.addWidget(_make_source_btn(f"{src} ({cnt})", src))

        self._source_layout.addStretch(1)
        self._source_bar.setVisible(True)
        self._source_bar.parent().setVisible(True)

    def _refresh_filter_counts(self):
        """按当前 tab（filter_mode）刷新来源栏 / tag 栏计数（不重建按钮）

        切 tab 只走 _render_plugins 不重建 tag/source 栏（仅数据内容变化时
        重建），计数会停留在全量数据上。这里按当前 filter_mode 重新统计
        并更新按钮文本，保持「全部」= 当前 tab 插件总数。
        """
        if not self._all_plugins:
            return
        view_plugins = list(self._all_plugins) + self._build_local_extra_plugins()
        fm = self._current_filter

        def _in_tab(p: dict) -> bool:
            name = p.get("name", "")
            installed = name in self._installed_set
            if fm == "installed":
                return installed
            if fm == "uninstalled":
                return not installed
            if fm == "updates":
                return installed and self._has_update(p, installed)
            return True

        tab_plugins = [p for p in view_plugins if _in_tab(p)]

        # 来源计数（当前 tab 维度）
        sources = {}
        for p in tab_plugins:
            src = p.get("_marketplace", "") or "未知"
            sources[src] = sources.get(src, 0) + 1
        for i in range(self._source_layout.count()):
            item = self._source_layout.itemAt(i)
            w = item.widget() if item else None
            if not isinstance(w, QPushButton) or not w.isCheckable():
                continue
            text = w.text()
            name = text.split(" (")[0]
            if name == "全部":
                w.setText(f"全部 ({len(tab_plugins)})")
            else:
                w.setText(f"{name} ({sources.get(name, 0)})")

        # tag 计数（当前 tab 维度）
        tag_counts = {}
        for p in tab_plugins:
            for t in p.get("_cached_tags", []) or []:
                if t:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
        for i in range(self._tag_layout.count()):
            item = self._tag_layout.itemAt(i)
            w = item.widget() if item else None
            if not isinstance(w, QPushButton) or not w.isCheckable():
                continue
            text = w.text()
            if text == "更多…":
                continue
            name = text.split(" (")[0]
            w.setText(f"{name} ({tag_counts.get(name, 0)})")

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
            # 缓存命中：仅刷新状态徽标（拉取可能已完成/失败）
            self._refresh_all_market_status()
            return
        # 清空旧内容
        while self._markets_content_layout.count():
            item = self._markets_content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._market_status_labels.clear()

        mgr = get_marketplace_manager()
        for src in mgr.get_sources():
            row = self._create_market_row(src)
            self._markets_content_layout.addWidget(row)

        self._markets_content_layout.addStretch()
        self._markets_built = True
        # 构建后立即刷新状态徽标（读 manager 持久化状态）
        self._refresh_all_market_status()

    def _create_market_row(self, src_def: dict) -> QWidget:
        """创建单个市场源的行组件（直接用缓存主题色，无需事后 re-theme）

        行内显示拉取状态徽标：● 拉取成功（绿）/ ● 拉取失败（红，tooltip 原因）
        / ● 未拉取（灰）；失败时额外显示 ⚠ 提示。右侧提供「校验」按钮
        可单独强制重拉该源。
        """
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

        # 名称行：名称 + 状态徽标（横向）
        name_row = QWidget(row)
        name_row.setStyleSheet("background: transparent;")
        name_row_layout = QHBoxLayout(name_row)
        name_row_layout.setContentsMargins(0, 0, 0, 0)
        name_row_layout.setSpacing(6)

        name_label = QLabel(name_text, name_row)
        name_label.setObjectName("marketRowName")
        name_label.setStyleSheet(f"color: {tc}; font-weight: bold; font-size: 18px; background: transparent;")
        name_row_layout.addWidget(name_label)

        # 拉取状态徽标（读 manager 持久化状态；无记录 → 未拉取）
        status_lb = QLabel("", name_row)
        status_lb.setStyleSheet("background: transparent;")
        name_row_layout.addWidget(status_lb)
        self._market_status_labels[src_def["name"]] = status_lb
        self._refresh_market_status_label(src_def["name"])

        name_row_layout.addStretch(1)
        info.addWidget(name_row)

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

        # 校验按钮：单独强制重拉该源
        check_btn = TransparentToolButton(FluentIcon.SYNC, row)
        check_btn.setFixedSize(28, 28)
        check_btn.setToolTip(f"重新拉取 {src_def['name']}")
        check_btn.clicked.connect(lambda checked, n=src_def["name"]: self._check_source(n))
        h.addWidget(check_btn)

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

    def _refresh_market_status_label(self, name: str):
        """刷新单个市场源的状态徽标（● 成功绿 / ● 失败红 / ● 未拉取灰）

        失败时 tooltip 展示错误原因；成功时展示插件数与时间。
        """
        label = self._market_status_labels.get(name)
        if label is None:
            return
        status = get_marketplace_manager().get_source_status(name)
        tcs = getattr(self, "_cached_tcs", None) or _text_color(secondary=True)
        if status is None:
            label.setText(f'<span style="color:{tcs};">● 未拉取</span>')
            label.setToolTip("尚未拉取过该市场源")
            return
        plugins = status.get("plugins", 0)
        when = time.strftime("%m-%d %H:%M", time.localtime(status.get("time", 0))) if status.get("time") else "—"
        if status.get("ok"):
            if status.get("from_cache"):
                label.setText('<span style="color:#66bb6a;">● 缓存</span>')
                label.setToolTip(f"缓存数据可用 · {plugins} 个插件 · {when}")
            else:
                label.setText('<span style="color:#4caf50;">● 拉取成功</span>')
                label.setToolTip(f"拉取成功 · {plugins} 个插件 · {when}")
        else:
            label.setText('<span style="color:#ef5350;">● 拉取失败</span>')
            err = (status.get("error") or "未知错误").strip()
            if len(err) > 120:
                err = err[:117] + "..."
            label.setToolTip(f"{err}\n{when}")

    def _refresh_all_market_status(self):
        """刷新「市场」页所有源的状态徽标"""
        for name in list(self._market_status_labels.keys()):
            self._refresh_market_status_label(name)

    def _check_source(self, name: str):
        """单独强制拉取某个市场源（校验按钮）

        独立 worker，不干扰浏览页正在进行的刷新/安装任务。
        """
        mgr = get_marketplace_manager()
        src_def = next((s for s in mgr.get_sources() if s["name"] == name), None)
        if src_def is None:
            return
        label = self._market_status_labels.get(name)
        if label is not None:
            tcs = getattr(self, "_cached_tcs", None) or _text_color(secondary=True)
            label.setText(f'<span style="color:{tcs};">● 校验中…</span>')
            label.setToolTip("正在重新拉取该市场源…")

        self._cleanup_check_worker()
        self._check_worker = _MarketplaceWorker(lambda d=src_def: mgr.fetch_marketplace(d, force=True))
        self._check_thread = QThread(self)
        self._check_worker.moveToThread(self._check_thread)
        self._check_thread.started.connect(self._check_worker.run)
        self._check_worker.finished.connect(lambda r: self._on_check_source_done(name, r))
        self._check_worker.error.connect(lambda e: self._on_check_source_done(name, None, err=e))
        self._check_worker.finished.connect(self._check_thread.quit)
        self._check_worker.error.connect(self._check_thread.quit)
        self._check_worker.finished.connect(self._check_worker.deleteLater)
        self._check_worker.error.connect(self._check_worker.deleteLater)
        self._check_thread.finished.connect(self._check_thread.deleteLater)
        self._check_thread.start()

    def _on_check_source_done(self, name: str, result, err: str = ""):
        """单源校验完成：刷新徽标；成功则把数据合并进插件列表

        校验（强制拉取）返回的市场数据与浏览页刷新同样处理——
        合并进 _plugin_data 并触发渲染，否则会出现「状态显示成功但
        插件列表无数据」的不一致。
        """
        if not self._alive():
            return  # 卡片已销毁，丢弃迟到结果
        self._refresh_market_status_label(name)
        if err:
            return
        if isinstance(result, dict) and not result.get("_error"):
            self._merge_market_data(result)

    def _cleanup_check_worker(self):
        """安全清理市场校验 worker/thread"""
        if getattr(self, "_check_thread", None) is not None:
            try:
                self._check_thread.quit()
                self._check_thread.wait(500)
            except RuntimeError:
                pass
            self._check_thread = None
        self._check_worker = None

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

    def _alive(self) -> bool:
        """卡片 C++ 对象是否存活（销毁后迟到回调防护）"""
        try:
            return not sip.isdeleted(self)
        except RuntimeError, TypeError:
            return False

    def _orphan_worker_thread(self, thread):
        """安全剥离 worker 线程为孤儿（不阻塞主线程）

        - 判活：旧线程 C++ 对象可能已被上一轮 finished→deleteLater 链销毁
          （删 wait 后事件循环会立即处理 deleteLater），若属性仍持悬垂 wrapper，
          再次 quit()/connect 会抛 RuntimeError（可升级原生崩溃 0xC0000409）
        - 存活线程：quit 后剥离父子（防卡片析构连带销毁 running 线程）+
          孤儿列表强引用持有（防 GC），finished → _release_orphan 自清理
        - 迟到信号由回调 sender/判活检查丢弃
        """
        if thread is None:
            return
        try:
            if sip.isdeleted(thread):
                # C++ 对象已被前轮 finished→deleteLater 销毁：仅清理悬垂引用
                try:
                    if thread in _orphan_threads:
                        _orphan_threads.remove(thread)
                except RuntimeError:
                    pass
                return
        except RuntimeError:
            return
        try:
            thread.quit()
            thread.setParent(None)
            _orphan_threads.append(thread)
            try:
                thread.finished.connect(lambda t=thread: self._release_orphan(t))
            except RuntimeError:
                pass  # 竞态：前一轮 finished 已排队，无需再连
        except RuntimeError:
            # 竞态：quit/setParent 期间对象被销毁；清理悬垂引用
            try:
                if thread in _orphan_threads:
                    _orphan_threads.remove(thread)
            except RuntimeError:
                pass

    def _cleanup_worker(self):
        """安全清理任务 worker/thread（安装/更新/启用/禁用/卸载）"""
        thread = self._worker_thread
        self._worker_thread = None
        self._worker = None
        self._orphan_worker_thread(thread)

    def _cleanup_fetch_worker(self):
        """安全清理市场拉取 worker/thread（与任务 worker 相互独立）"""
        thread = self._fetch_thread
        self._fetch_thread = None
        self._fetch_worker = None
        self._orphan_worker_thread(thread)

    def _release_orphan(self, thread):
        """孤儿线程 finished 后从模块级列表移除（deleteLater 由原连接负责）"""
        try:
            if thread in _orphan_threads:
                _orphan_threads.remove(thread)
        except RuntimeError:
            pass

    def __del__(self):
        """GC 析构钩子：先剥离活跃线程再销毁卡片

        卡片可能因 Python 引用消失被 GC 直接销毁（非 deleteLater 路径），
        此时若不先剥离 running 的 worker 线程，线程随卡片析构会被 Qt abort。
        """
        try:
            self._cleanup_worker()
        except Exception:
            pass
        try:
            self._cleanup_fetch_worker()
        except Exception:
            pass
        try:
            self._cleanup_check_worker()
        except Exception:
            pass
        try:
            self._cleanup_dl_fetch_worker()
        except Exception:
            pass
        try:
            self._cleanup_rs_worker()
        except Exception:
            pass

    def deleteLater(self):
        self._cleanup_worker()
        self._cleanup_fetch_worker()
        self._cleanup_check_worker()
        self._cleanup_dl_fetch_worker()
        self._cleanup_rs_worker()
        # 销毁前移除挂在顶层窗口上的事件过滤器（win 非 None 且非 self）
        try:
            win = self.window()
            if win is not None and win is not self:
                win.removeEventFilter(self)
        except RuntimeError:
            pass
        super().deleteLater()
