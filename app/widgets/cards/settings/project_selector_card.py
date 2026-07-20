# -*- coding: utf-8 -*-
"""
项目选择卡片内容 - 卡片形式展示所有项目，支持选择、新建、归档
替代原来的 ProjectSelectorPopup 弹窗
"""

import colorsys
import os
import re
import zlib
from pathlib import Path
from typing import Dict

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, TransparentToolButton

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style, scale_font_size
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.widgets.cards.settings.mcp_setting_card import _ElidedLabel


def extract_project_initials(name: str) -> str:
    """从项目名提取最多 2 个字符的缩写

    优先级：
    1. 中文 → 首个汉字
    2. 分隔符（_/-/空格）→ 首段首字母 + 末段首字母
    3. 驼峰/帕斯卡 → 首个词首字母 + 末个词首字母
    4. 其他 → 前 2 个字母大写

    Args:
        name: 项目名

    Returns:
        1-2 个字符的缩写字符串
    """
    if not name:
        return "??"

    # ── 中文：首个汉字 ──
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in name)
    if has_cjk:
        for c in name:
            if "\u4e00" <= c <= "\u9fff":
                return c
        return name[0]

    # ── 分隔符拆分（下划线/中划线/空格）──
    # 取第一个段的第一个字母 + 最后一个段的第一个字母
    for delim in ("_", "-", " "):
        if delim in name:
            parts = [p for p in name.split(delim) if p]
            if len(parts) >= 2:
                return (parts[0][0] + parts[-1][0]).upper()
            # 只有一个段，退化到后续逻辑
            name = parts[0]
            break

    # ── 驼峰/帕斯卡：拆分为词 ──
    # 处理连写缩写：OpenAIChat → Open|AI|Chat
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1|\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1|\2", s)
    words = [w for w in s.split("|") if w]

    if len(words) >= 2:
        # 取首词首字母 + 末词首字母
        return (words[0][0] + words[-1][0]).upper()

    # ── 普通单词：前 2 字母大写 ──
    if len(name) >= 2:
        return name[:2].upper()
    return name.upper()


def get_project_color(name: str, alpha: int = 255) -> str:
    """根据项目名计算固定颜色（HSL 全空间哈希）

    旧实现：仅在 12 色调色板中取色 → 项目超过 ~30 个即发生明显撞色
    （生日悖论），尤其是首字母相同或前缀相近的项目。

    新实现：在 HSL 圆柱上从完整字符串 zlib.crc32 直接采样：
      - H ∈ [0°, 360°)   → 色相覆盖整圈，永不局限于 12 离散点
      - S ∈ [55%, 85%]   → 足够鲜艳，避免灰色
      - L ∈ [50%, 65%]   → 在 #212126 深色背景上清晰；白字仍可读

    采样自同一 CRC32 的不同 bit 段（H 低位、S 中位、L 高位），
    保证首字母相同 / 前缀相近的项目也能得到不同颜色。

    使用 zlib.crc32 替代内置 hash()，避免 Python 的
    进程间随机化种子（PYTHONHASHSEED）导致每次启动颜色不一致。

    Args:
        name: 项目名
        alpha: 透明度 0-255

    Returns:
        RGBA 颜色字符串，如 "rgba(33, 139, 255, 255)"
    """
    crc = zlib.crc32(name.encode("utf-8"))
    # 三个分量取自 CRC32 不同 bit 段，相关性低
    h = crc % 360  # 0-359°  色相
    s = 55 + ((crc >> 8) % 31)  # 55-85%  饱和度
    l = 50 + ((crc >> 16) % 16)  # 50-65%  亮度

    r, g, b = colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)
    return f"rgba({int(round(r * 255))}, {int(round(g * 255))}, {int(round(b * 255))}, {alpha})"


class _SquareAvatar(QWidget):
    """使用 QPainter 绘制的方形项目头像 — flat design squircle 风格

    纯色圆角矩形 + 1-2 个白色缩写字母，用于项目卡片列表和面包屑标签。
    Qt QSS 在小尺寸（24×24）上同时渲染 border + border-radius 时存在
    抗锯齿走样问题。本类用 QPainter 精确绘制，保证像素完美。
    """

    def __init__(self, text: str, color: str, parent=None, size: int = 24):
        super().__init__(parent)
        self._text = text if text else "?"
        # get_project_color() 返回 "rgba(r,g,b,a)" 格式，
        # QColor(string) 不解析此 CSS 格式，需拆解为数值构造
        self._color = self._parse_rgba(color)
        self._size = size
        self.setFixedSize(size, size)

    @staticmethod
    def _parse_rgba(rgba_str: str) -> QColor:
        """解析 "rgba(r,g,b,a)" 字符串为 QColor，失败时返回灰色"""
        if rgba_str.startswith("#"):
            return QColor(rgba_str)
        try:
            import re

            m = re.match(r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?\s*\)", rgba_str)
            if m:
                r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
                a = int(m.group(4)) if m.group(4) else 255
                return QColor(r, g, b, a)
        except Exception:
            pass
        return QColor(128, 128, 128)  # fallback 灰色

    def set_project(self, name: str, color: str):
        """更新项目名和颜色（用于面包屑等动态场景）"""
        self._text = extract_project_initials(name)
        self._color = self._parse_rgba(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        # 微妙圆角（约 5px，like VS Code squircle）
        corner_radius = 5

        # 纯色填充背景（无边框，flat design）
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(rect, corner_radius, corner_radius)

        # 居中白字，使用系统配置字体
        painter.setPen(Qt.white)
        font = get_unified_font()
        # 单字符（中文）字号稍大，双字符略小
        if len(self._text) <= 1:
            font.setPixelSize(scale_font_size(self._size * 14 // 24))
        else:
            font.setPixelSize(scale_font_size(self._size * 14 // 24))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._text)

    # ── 立即显示 tooltip ─────────────────────────────────────
    # 默认 Qt tooltip 延迟约 700ms，用户体验偏慢。
    # 这里重写 enter/leave，直接调用 QToolTip.showText 立即显示，
    # 鼠标移出立即隐藏，体感与按钮 hover 行为一致。
    def enterEvent(self, event):
        tip = self.toolTip()
        if tip:
            QToolTip.showText(self.mapToGlobal(self.rect().center()), tip, self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)


class ProjectItem(QWidget):
    """单个项目项 - 卡片内项目选择列表项"""

    clicked = pyqtSignal(str)
    archiveClicked = pyqtSignal(str)
    exportClicked = pyqtSignal(str)  # 导出项目压缩包
    openFolderClicked = pyqtSignal(str, str)  # project_name, root_dir

    # 单行高度（无根目录）；有根目录时切换为 _DOUBLE_LINE_HEIGHT
    _SINGLE_LINE_HEIGHT = 30
    _DOUBLE_LINE_HEIGHT = 44

    def __init__(self, name: str, is_current: bool = False, parent=None):
        super().__init__(parent)
        self._name = name
        self._is_current = is_current
        self._session_count = 0
        self._worktree_count = 0
        self._project_color = get_project_color(name)
        self._root_dir = ""
        self.setFixedHeight(self._SINGLE_LINE_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        # 上下边距 0、单行 30px：紧凑布局，让项目之间视觉密度更高
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(6)

        # 当前项目指示（放在最前，icon 之前）
        if self._is_current:
            self._check_label = QLabel("✓", self)
            self._check_label.setStyleSheet(f"color: {Colors.BORDER_ACCENT}; font-size: {scale_font_size(14)}px;")
            self._check_label.setAlignment(Qt.AlignVCenter)
            layout.addWidget(self._check_label)

        # 项目彩色方形标识（缩写字母 + 项目专属色）
        # 使用 QPainter 绘制的 _SquareAvatar，flat design 风格
        initials = extract_project_initials(self._name)
        self._avatar_label = _SquareAvatar(initials, self._project_color, self)
        layout.addWidget(self._avatar_label)

        # 中间：项目名 + 根目录（垂直布局）
        text_vbox = QVBoxLayout()
        text_vbox.setContentsMargins(0, 0, 0, 0)
        text_vbox.setSpacing(0)
        text_vbox.setAlignment(Qt.AlignVCenter)

        # 项目名
        self._name_label = QLabel(self._name, self)
        self._apply_name_style()
        text_vbox.addWidget(self._name_label)

        # 项目根目录（默认隐藏：未设置时由 set_root_dir 保持隐藏）
        # 使用 _ElidedLabel 根据可用宽度自动省略长路径
        self._root_dir_label = _ElidedLabel("", self)
        self._root_dir_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};")
        self._root_dir_label.hide()
        text_vbox.addWidget(self._root_dir_label)

        layout.addLayout(text_vbox, 1)

        # 元数据（会话数 · 工作目录数），灰色小字
        self._meta_label = QLabel("", self)
        self._meta_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};")
        self._meta_label.setAlignment(Qt.AlignVCenter)
        layout.addWidget(self._meta_label)

        # 打开根目录按钮（默认隐藏，有根目录且 hover 时显示）
        self._open_folder_btn = TransparentToolButton(get_icon("根目录"), self)
        self._open_folder_btn.setFixedSize(24, 24)
        self._open_folder_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                font-size: {scale_font_size(12)}px;
            }}
            QToolButton:hover {{
                background: rgba(255, 255, 255, 50);
                border-radius: 4px;
            }}
        """)
        self._open_folder_btn.clicked.connect(self._emit_open_folder)
        self._open_folder_btn.setToolTip("打开项目根目录")
        self._open_folder_btn.hide()
        layout.addWidget(self._open_folder_btn)

        # 导出按钮（默认隐藏）
        self._export_btn = TransparentToolButton(FluentIcon.SHARE, self)
        self._export_btn.setFixedSize(24, 24)
        self._export_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                font-size: {scale_font_size(12)}px;
            }}
            QToolButton:hover {{
                background: rgba(255, 255, 255, 50);
                border-radius: 4px;
            }}
        """)
        self._export_btn.clicked.connect(self._emit_export)
        self._export_btn.setToolTip("导出项目压缩包（含会话+Git文件）")
        self._export_btn.hide()
        layout.addWidget(self._export_btn)

        # 归档按钮（默认隐藏）
        self._archive_btn = TransparentToolButton(get_icon("归档"), self)
        self._archive_btn.setFixedSize(24, 24)
        self._archive_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                font-size: {scale_font_size(12)}px;
            }}
            QToolButton:hover {{
                background: rgba(255, 255, 255, 50);
                border-radius: 4px;
            }}
        """)
        self._archive_btn.clicked.connect(self._emit_archive)
        self._archive_btn.setToolTip("归档此项目")
        self._archive_btn.hide()
        layout.addWidget(self._archive_btn)

    def _apply_name_style(self):
        if self._is_current:
            self._name_label.setStyleSheet(
                f"color: {self._project_color}; font-weight: bold; {get_font_family_css()} {font_size_css(13)};"
            )
        else:
            # 非当前项目用半透明版本
            semi_color = get_project_color(self._name, alpha=160)
            self._name_label.setStyleSheet(f"color: {semi_color}; {get_font_family_css()} {font_size_css(13)};")

    def _emit_export(self):
        self.exportClicked.emit(self._name)

    def _emit_archive(self):
        self.archiveClicked.emit(self._name)

    def _emit_open_folder(self):
        if self._root_dir:
            self.openFolderClicked.emit(self._name, self._root_dir)

    def mousePressEvent(self, event):
        self.clicked.emit(self._name)
        super().mousePressEvent(event)

    def set_meta(self, session_count: int, worktree_count: int):
        """设置项目元数据（会话数、工作目录数）"""
        self._session_count = session_count
        self._worktree_count = worktree_count
        parts = []
        if session_count > 0:
            parts.append(f"{session_count}会话")
        if worktree_count > 0:
            parts.append(f"{worktree_count}工作目录")
        Colors.refresh()
        self._meta_label.setText(" · ".join(parts) if parts else "")
        self._meta_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};")

    def set_root_dir(self, root_dir: str):
        """设置项目根目录路径（空字符串/None 则隐藏根目录行）"""
        self._root_dir = root_dir or ""
        if not root_dir:
            # 切换回单行高度，与未设置根目录的项目保持紧凑
            self._root_dir_label.hide()
            self.setFixedHeight(self._SINGLE_LINE_HEIGHT)
            return
        Colors.refresh()
        self._root_dir_label.setText(root_dir)
        self._root_dir_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};")
        self._root_dir_label.setToolTip(root_dir)  # tooltip 展示完整路径
        self._root_dir_label.show()
        self.setFixedHeight(self._DOUBLE_LINE_HEIGHT)

    def enterEvent(self, event):
        # hover 时：整行加半透明背景 + 更亮的项目颜色 + 元数据提亮
        Colors.refresh()
        self.setStyleSheet(f"""
            ProjectItem {{
                background: {Colors.HOVER_BG};
                border-radius: 6px;
                border: none;
            }}
        """)
        hover_color = get_project_color(self._name, alpha=240)
        self._name_label.setStyleSheet(
            f"color: {hover_color}; font-weight: bold; {get_font_family_css()} {font_size_css(13)};"
        )
        self._meta_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(10)};")
        self._export_btn.show()
        self._archive_btn.show()
        if self._root_dir:
            self._open_folder_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet("")
        self._apply_name_style()
        Colors.refresh()
        self._meta_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};")
        self._export_btn.hide()
        self._archive_btn.hide()
        self._open_folder_btn.hide()
        super().leaveEvent(event)


class ProjectSelectorCardContent(QWidget):
    """项目选择卡片内容"""

    projectSelected = pyqtSignal(str)
    newProjectCreated = pyqtSignal(str)
    archiveProject = pyqtSignal(str)
    exportProject = pyqtSignal(str)  # 导出项目压缩包
    importProjectRequested = pyqtSignal()  # 导入项目压缩包（按钮触发）
    projectFileDropped = pyqtSignal(str)  # 拖拽 .drifox_project 文件路径
    openFolderRequested = pyqtSignal(str, str)  # project_name, root_dir
    folderDropped = pyqtSignal(str)  # 拖拽文件夹路径

    def __init__(self, parent=None):
        super().__init__(parent)
        self._projects: list = []
        self._current_project: str = ""
        self._meta_map: Dict[str, Dict[str, int]] = {}
        self._root_dir_map: Dict[str, str] = {}
        self._project_items: list = []  # 存储 ProjectItem 实例，用于过滤
        self._filter_text: str = ""
        self._setup_ui()
        # 启用拖拽
        self.setAcceptDrops(True)

    def _setup_ui(self):
        Colors.refresh()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # ── 项目列表滚动区域 ──
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self._scroll_area.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(8)}
        """)

        self._content_widget = QWidget()
        self._content_widget.setStyleSheet("background: transparent;")
        self._content_widget.setAcceptDrops(True)
        # 内容 widget 也接受拖拽，扩大拖拽热区
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        # 项目之间用 1px 细缝：避免 0 完全相连导致看不出分隔，又比 2px 紧凑
        self._content_layout.setSpacing(1)

        self._scroll_area.setWidget(self._content_widget)
        self._scroll_area.setMinimumHeight(40)
        # scroll_area 本身也启用拖拽（用户可能拖到空白区）
        self._scroll_area.setAcceptDrops(True)
        self._content_widget.setAcceptDrops(True)
        layout.addWidget(self._scroll_area, 1)

    # ── 拖拽支持（文件夹 / .drifox_project 文件） ──

    def _is_drifox_project(self, path: str) -> bool:
        return path.endswith(".drifox_project") or path.endswith(".zip")

    def dragEnterEvent(self, event):
        """判断拖入内容是否为文件夹或 .drifox_project 文件"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if os.path.isdir(path) or self._is_drifox_project(path):
                        event.acceptProposedAction()
                        return
        event.ignore()

    def dragMoveEvent(self, event):
        """拖拽移动时保持接受状态"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if os.path.isdir(path) or self._is_drifox_project(path):
                        event.acceptProposedAction()
                        return
        event.ignore()

    def dropEvent(self, event):
        """放下文件夹或 .drifox_project 文件时发射信号"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if self._is_drifox_project(path):
                        self.projectFileDropped.emit(path)
                        event.acceptProposedAction()
                        return
                    if os.path.isdir(path):
                        self.folderDropped.emit(path)
                        event.acceptProposedAction()
                        return
        event.ignore()

    # ── 拖拽视觉反馈 ──
    def _show_drop_indicator(self, visible: bool):
        """拖拽悬停时的视觉反馈（可在此添加背景色等效果）"""
        if visible:
            self._scroll_area.setStyleSheet(
                self._scroll_area.styleSheet()
                + """
                QScrollArea { background: rgba(255, 255, 255, 30); border: 2px dashed #4a9eff; }
            """
            )
        else:
            self.refresh_style()

    def refresh_style(self):
        """刷新主题样式"""
        Colors.refresh()

    # ── 公有方法 ──────────────────────────────────────

    def set_projects_data(
        self,
        projects: list,
        current_project: str,
        meta_map: Dict[str, Dict[str, int]] = None,
        root_dir_map: Dict[str, str] = None,
    ):
        """设置项目列表数据

        Args:
            projects: 项目名列表
            current_project: 当前项目名
            meta_map: {project: {"sessions": int, "worktrees": int}} 可选元数据
            root_dir_map: {project: root_dir_path} 可选根目录映射
        """
        self._projects = list(projects)
        self._current_project = current_project
        self._meta_map = meta_map or {}
        self._root_dir_map = root_dir_map or {}
        self._refresh_project_list()
        # 设置完后重新应用当前过滤
        if self._filter_text:
            self.set_filter(self._filter_text)

    def set_filter(self, text: str):
        """根据文本过滤项目列表，空文本/无匹配时显示全部"""
        self._filter_text = text
        keyword = text.strip().lower() if text.strip() else ""

        for item in self._project_items:
            if not keyword:
                item.setVisible(True)
            else:
                item.setVisible(keyword in item._name.lower())

    def _refresh_project_list(self):
        """刷新项目列表"""
        # 清空现有项
        while self._content_layout.count():
            child = self._content_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._project_items.clear()

        # 添加项目
        for proj_name in self._projects:
            is_current = proj_name == self._current_project
            item = ProjectItem(proj_name, is_current, self)
            # 设置元数据
            meta = self._meta_map.get(proj_name, {})
            item.set_meta(
                session_count=meta.get("sessions", 0),
                worktree_count=meta.get("worktrees", 0),
            )
            # 设置根目录（空字符串时 ProjectItem 内部隐藏该行）
            item.set_root_dir(self._root_dir_map.get(proj_name, ""))
            item.clicked.connect(self._on_project_item_clicked)
            item.archiveClicked.connect(self._on_archive_clicked)
            item.exportClicked.connect(self._on_export_clicked)
            item.openFolderClicked.connect(self._on_open_folder_clicked)
            self._content_layout.addWidget(item)
            self._project_items.append(item)

        self._content_layout.addStretch(1)

    def _on_project_item_clicked(self, name: str):
        """项目被点击"""
        self.projectSelected.emit(name)

    def _on_open_folder_clicked(self, project_name: str, root_dir: str):
        """打开项目根目录按钮被点击"""
        self.openFolderRequested.emit(project_name, root_dir)

    def _on_export_clicked(self, project_name: str):
        """导出按钮被点击"""
        self.exportProject.emit(project_name)

    def _on_archive_clicked(self, project_name: str):
        """归档按钮被点击"""
        from app.widgets.common_dialogs import ConfirmDialog

        _confirmed: list[bool] = [False]

        def _on_archive_confirm():
            _confirmed[0] = True

        _dialog = ConfirmDialog(
            title="归档确认",
            content=f"确定归档项目「{project_name}」吗？\n归档后该项目的所有会话将移动到归档区。",
            confirm_text="归档",
            cancel_text="取消",
            parent=self.window(),
        )
        _dialog.confirmed.connect(_on_archive_confirm)
        _dialog.exec_()
        if _confirmed[0]:
            self.archiveProject.emit(project_name)
