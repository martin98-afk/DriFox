# -*- coding: utf-8 -*-
"""ShareHistoryCard 浮动卡片 — 浏览分享/导出历史记录

功能：
- 浏览历史分享记录（会话分享 + 项目导出）
- 按时间倒序排列
- 打开本地文件
- 复制上传链接

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 stdlib 完成
"""

import json as json_mod
import os
import re
import subprocess
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import QEvent, QObject, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from loguru import logger
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

from .db import get_records


# ════════════════════════════════════════════════════════════
# 主题色/字体辅助
# ════════════════════════════════════════════════════════════


def _text_color(secondary: bool = False) -> str:
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_font(ctx: dict) -> tuple:
    return ctx.get("font_family", "Microsoft YaHei"), ctx.get("font_size", 14)


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _ctx_border_color(ctx: dict) -> str:
    return ctx.get("colors", {}).get("border", "rgba(128,128,128,0.15)")


# ════════════════════════════════════════════════════════════
# 类型常量
# ════════════════════════════════════════════════════════════

_TYPE_ICONS = {"session": "💬", "project": "📦"}
_TYPE_LABELS = {"session": "会话", "project": "项目"}


# ════════════════════════════════════════════════════════════
# 异步 Worker
# ════════════════════════════════════════════════════════════


class _LoadWorker(QObject):
    """后台加载分享记录"""

    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def run(self):
        try:
            records = get_records(limit=500)
            self.finished.emit(records)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ════════════════════════════════════════════════════════════
# 单条记录行
# ════════════════════════════════════════════════════════════


class _RecordItem(QFrame):
    """单条分享记录展示行"""

    def __init__(self, record: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._record = record
        self._setup_ui()

    def _setup_ui(self):
        rec = self._record
        rtype = rec.get("type", "session")
        title = rec.get("title", "未命名")
        fmt = rec.get("format", "")
        created_at = rec.get("created_at", "")
        file_path = rec.get("file_path", "") or ""
        upload_url = rec.get("upload_url", "") or ""
        extra = rec.get("extra_info", {})
        if isinstance(extra, str):
            try:
                extra = json_mod.loads(extra)
            except Exception:
                extra = {}

        self.setObjectName("recordRow")
        self.setStyleSheet("""
            #recordRow {
                background: rgba(128,128,128,0.06);
                border: 1px solid rgba(128,128,128,0.12);
                border-radius: 8px;
            }
            #recordRow:hover {
                background: rgba(128,128,128,0.10);
                border: 1px solid rgba(128,128,128,0.20);
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # ── 第一行：类型图标 + 标题 + 格式标签 ──
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        icon_lb = QLabel(_TYPE_ICONS.get(rtype, "📄"))
        icon_lb.setFixedWidth(24)
        icon_lb.setAlignment(Qt.AlignCenter)
        icon_lb.setStyleSheet("background: transparent;")
        row1.addWidget(icon_lb)

        title_lb = QLabel(title)
        title_lb.setObjectName("recordRowTitle")
        title_lb.setWordWrap(False)
        title_lb.setStyleSheet("font-weight: 600; background: transparent;")
        row1.addWidget(title_lb, 1)

        badge_text = fmt.upper() if fmt else _TYPE_LABELS.get(rtype, "")
        if badge_text:
            badge = QLabel(badge_text)
            badge.setObjectName("recordRowBadge")
            badge.setFixedHeight(20)
            badge.setStyleSheet("""
                QLabel {
                    background: rgba(128,128,128,0.10);
                    border-radius: 4px;
                    padding: 0 8px;
                    font-size: 11px;
                    font-weight: 500;
                }
            """)
            row1.addWidget(badge)

        layout.addLayout(row1)

        # ── 第二行：时间 + 信息 ──
        row2 = QHBoxLayout()
        row2.setSpacing(12)

        time_lb = QLabel(created_at)
        time_lb.setObjectName("recordRowTime")
        time_lb.setStyleSheet("background: transparent;")
        row2.addWidget(time_lb)

        info_parts = []
        if rtype == "session":
            mc = extra.get("msg_count", 0)
            if mc:
                info_parts.append(f"{mc} 轮对话")
            proj = extra.get("project", "")
            if proj:
                info_parts.append(f"📁 {proj}")
        elif rtype == "project":
            sc = extra.get("session_count", 0)
            if sc:
                info_parts.append(f"{sc} 个会话")

        if info_parts:
            info_lb = QLabel(" · ".join(info_parts))
            info_lb.setObjectName("recordRowInfo")
            info_lb.setStyleSheet("background: transparent;")
            row2.addWidget(info_lb)

        row2.addStretch()
        layout.addLayout(row2)

        # ── 第三行：操作按钮 ──
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        row3.addStretch()

        has_file = bool(file_path) and Path(file_path).exists()
        has_url = bool(upload_url)

        if has_file:
            open_btn = QPushButton("📂 打开")
            open_btn.setFixedHeight(26)
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(128,128,128,0.08);
                    border: 1px solid rgba(128,128,128,0.15);
                    border-radius: 4px;
                    padding: 0 10px;
                }
                QPushButton:hover {
                    background: rgba(128,128,128,0.15);
                }
            """)
            open_btn.clicked.connect(lambda: self._open_file(file_path))
            row3.addWidget(open_btn)
        else:
            missing_btn = QPushButton("📂 文件缺失")
            missing_btn.setFixedHeight(26)
            missing_btn.setEnabled(False)
            missing_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(128,128,128,0.04);
                    border: 1px solid rgba(128,128,128,0.08);
                    border-radius: 4px;
                    padding: 0 10px;
                    color: rgba(128,128,128,0.4);
                }
            """)
            row3.addWidget(missing_btn)

        if has_url:
            copy_btn = QPushButton("🔗 复制链接")
            copy_btn.setFixedHeight(26)
            copy_btn.setCursor(Qt.PointingHandCursor)
            copy_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(128,128,128,0.08);
                    border: 1px solid rgba(128,128,128,0.15);
                    border-radius: 4px;
                    padding: 0 10px;
                }
                QPushButton:hover {
                    background: rgba(128,128,128,0.15);
                }
            """)
            copy_btn.clicked.connect(lambda: self._copy_link(upload_url))
            row3.addWidget(copy_btn)

        layout.addLayout(row3)

    def _open_file(self, path: str):
        if not path or not Path(path).exists():
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                folder = str(Path(path).parent)
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            logger.warning(f"[ShareHistory] 打开文件失败: {e}")

    def _copy_link(self, url: str):
        if url:
            QApplication.clipboard().setText(url)


# ════════════════════════════════════════════════════════════
# 主卡片（严格对齐官方卡片视觉语言）
# ════════════════════════════════════════════════════════════


class ShareHistoryCard(QWidget):
    """分享记录管理浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_LoadWorker] = None
        self._header_icon: Optional[IconWidget] = None

        # 缓存上下文值
        self._cached_tc = "rgba(255,255,255,0.9)"
        self._cached_tcs = "rgba(255,255,255,0.55)"
        self._cached_font_family = "Microsoft YaHei"
        self._cached_font_size = 14

        self._setup_ui()

    # ── 上下文注入 ──────────────────────────────────────

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def show_card(self):
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._load_records()
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

    # ── 主题 ────────────────────────────────────────────

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

        # 第 1 层：QFont 级联
        if font_family:
            self.setFont(QFont(font_family, font_size if font_size else 14))

        # 第 2+3 层
        self._retheme()

        # 分隔线
        try:
            self._sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
        except RuntimeError:
            pass

    def _retheme(self):
        """第 2+3 层字体策略"""
        tc = self._cached_tc
        ff = self._cached_font_family
        fs = self._cached_font_size

        for child in self.findChildren(QLabel):
            try:
                if isinstance(child, FluentLabelBase) and ff:
                    child.setFont(QFont(ff, fs))

                ss = child.styleSheet()
                if not ss:
                    continue
                new_ss = re.sub(r"color:\s*[^;]+;", f"color: {tc};", ss)
                if fs:
                    new_ss = re.sub(r"font-size:\s*[^;]+;", f"font-size: {fs}px;", new_ss)
                if ff and f"font-family: '{ff}'" not in new_ss:
                    new_ss += f" font-family: '{ff}';"
                child.setStyleSheet(new_ss)
            except RuntimeError:
                pass

        # QPushButton 也需要 font
        for child in self.findChildren(QPushButton):
            try:
                cur = child.styleSheet()
                child.setStyleSheet(cur + f" font-size: {max(fs - 2, 11)}px; font-family: '{ff}';")
            except RuntimeError:
                pass

    # ── 界面搭建 ────────────────────────────────────────

    def _setup_ui(self):
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("ShareHistoryCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部 ──
        self._build_header(root)

        # ── 分隔线 ──
        self._sep = QFrame(self)
        self._sep.setFrameShape(QFrame.HLine)
        self._sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(self._sep)

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
            "QScrollBar::add-line:vertical,"
            "QScrollBar::sub-line:vertical { height: 0; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 8, 12, 8)
        self._content_layout.setSpacing(6)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

    def _build_header(self, root: QVBoxLayout):
        """标题栏：图标 + 标题 + 状态 + 刷新 + 关闭"""
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        # 图标
        self._header_icon = IconWidget(FluentIcon.HISTORY, header)
        self._header_icon.setFixedSize(22, 22)
        hly.addWidget(self._header_icon)

        # 标题
        self._header_title = StrongBodyLabel("分享记录", header)
        self._header_title.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(self._header_title)

        # 状态/计数
        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        hly.addWidget(self._status_lb)

        hly.addStretch(1)

        # 刷新按钮
        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._load_records)
        hly.addWidget(self._refresh_btn)

        # 关闭按钮
        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

    # ── 比例高度 ────────────────────────────────────────

    def sizeHint(self):
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
        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    # ── 数据加载 ────────────────────────────────────────

    def _load_records(self):
        """异步加载分享记录"""
        self._cleanup_worker()
        self._refresh_btn.setEnabled(False)
        self._status_lb.setText("读取中…")
        self._show_empty_state()

        self._worker = _LoadWorker()
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_records_loaded)
        self._worker.error.connect(self._on_load_error)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_records_loaded(self, records: List[Dict[str, Any]]):
        self._refresh_btn.setEnabled(True)
        self._status_lb.setText("")
        self._cleanup_worker()
        self._render_records(records)

    def _on_load_error(self, err: str):
        logger.warning(f"[ShareHistory] 加载记录失败: {err}")
        self._refresh_btn.setEnabled(True)
        self._status_lb.setText("")
        self._cleanup_worker()

    def _render_records(self, records: List[Dict[str, Any]]):
        """渲染记录列表"""
        # 清空内容
        self._clear_content()

        if not records:
            self._show_empty_state()
            return

        self._status_lb.setText(f"共 {len(records)} 条")
        for rec in records:
            item = _RecordItem(rec)
            self._content_layout.addWidget(item)

        self._content_layout.addStretch()

        # 刷新主题色 + 字体
        self._retheme()

    def _show_empty_state(self):
        """显示空状态（在 content 中居中显示）"""
        self._clear_content()
        empty_lb = QLabel("暂无分享记录\n分享会话或导出项目后，记录将出现在这里")
        empty_lb.setAlignment(Qt.AlignCenter)
        empty_lb.setWordWrap(True)
        empty_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent; padding: 40px;")
        self._content_layout.addWidget(empty_lb, 1, Qt.AlignCenter)

    def _clear_content(self):
        """清空内容布局"""
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    # ── 生命周期 ────────────────────────────────────────

    def _cleanup_worker(self):
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    def _on_close(self):
        self.closed.emit()

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()
