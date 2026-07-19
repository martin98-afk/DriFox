# -*- coding: utf-8 -*-
"""SystemCleanerCard 浮动卡片 — 一键清理 DriFox 缓存垃圾，释放磁盘空间与内存

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 os/shutil/stdlib 完成
- 内存读取通过 psutil 完成
"""

import gc
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QEvent, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger

from .cleaner import _CacheItemRow, _styled_cleaner_confirm, _styled_cleaner_info
from .scanner import (
    CACHE_DEFS,
    _CleanWorker,
    _ScanWorker,
    _drifox_dir,
    _format_size,
    _get_process_memory,
)


# ── 主题色/字体辅助 ──────────────────────────────────────


def _ctx_colors(ctx: dict) -> dict:
    return ctx.get("colors", {})


def _ctx_font(ctx: dict) -> Tuple[str, int]:
    ff = ctx.get("font_family", "Microsoft YaHei")
    fs = ctx.get("font_size", 14)
    return ff, fs


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = _ctx_colors(ctx)
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


# ── 颜色工具 ──────────────────────────────────────────


def _adjust_color(hex_color: str, amount: int) -> str:
    """简单地调亮/调暗一个 hex 颜色"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return hex_color
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        r = max(0, min(255, r + amount))
        g = max(0, min(255, g + amount))
        b = max(0, min(255, b + amount))
        return f"#{r:02x}{g:02x}{b:02x}"
    except ValueError:
        return hex_color


# ══════════════════════════════════════════════════════════
# 主卡片
# ══════════════════════════════════════════════════════════


class SystemCleanerCard(QWidget):
    """系统清理浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[_ScanWorker] = None
        self._clean_thread: Optional[QThread] = None
        self._clean_worker: Optional[_CleanWorker] = None
        self._last_clean_time: Optional[str] = None
        self._last_mem_release_time: Optional[str] = None
        self._cache_rows: Dict[str, _CacheItemRow] = {}
        self._is_cleaning = False
        self._is_releasing_mem = False
        self._header_icon: Optional[IconWidget] = None

        self._ctx_font_family = ""
        self._ctx_font_size = 0
        self._ctx_accent = "#62a0ea"
        self._ctx_danger = "#e34c4c"

        self._setup_ui()
        self._apply_fallback_theme()

    def _apply_fallback_theme(self):
        self.setStyleSheet("""
            SystemCleanerCard {
                background: transparent;
            }
        """)

    # ── 拉模型上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def show_card(self):
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._async_scan()
        self._refresh_memory()
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

    def _get_context(self) -> Optional[dict]:
        if self._context_provider is None:
            return None
        try:
            return self._context_provider()
        except Exception:
            return None

    # ── 主题 ──

    def _apply_latest_theme(self):
        ctx = self._get_context()
        if ctx is None:
            return

        self._ctx_font_family, self._ctx_font_size = _ctx_font(ctx)
        ff = self._ctx_font_family
        fs = self._ctx_font_size

        colors = _ctx_colors(ctx)
        accent = colors.get("accent", "#62a0ea")
        danger = colors.get("danger", "#e34c4c") or colors.get("accent_warm", "#e34c4c")
        is_dark = ctx.get("is_dark", True)
        self._ctx_accent = accent
        self._ctx_danger = danger
        self._ctx_theme_colors = colors

        self._cached_tc = _ctx_text_color(ctx)
        self._cached_tcs = _ctx_text_color(ctx, secondary=True)
        self._cached_font_family = ff
        self._cached_font_size = fs
        self._cached_theme_colors = colors

        tc = self._cached_tc
        tcs = self._cached_tcs
        border_c = colors.get("border", "rgba(128,128,128,0.12)")

        font_qss = f"font-family: '{ff}'; font-size: {fs}px;" if ff else (f"font-size: {fs}px;" if fs else "")

        try:
            if hasattr(self, "_header_title"):
                self._header_title.setStyleSheet(f"color: {tc}; background: transparent; {font_qss}")
        except RuntimeError:
            pass

        try:
            if hasattr(self, "_mem_release_btn"):
                self._mem_release_btn.setStyleSheet(self._mem_release_btn_style(accent, is_dark))
        except RuntimeError:
            pass

        try:
            if hasattr(self, "_mem_label"):
                self._mem_label.setStyleSheet(f"color: {tc}; background: transparent; {font_qss}")
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_mem_value"):
                self._mem_value.setStyleSheet(f"color: {accent}; background: transparent; font-weight: 600; {font_qss}")
        except RuntimeError:
            pass

        for row in self._cache_rows.values():
            row.set_font_ctx(ff, fs)
            row.set_accent_color(accent)
            row.set_dark_mode(is_dark)

        try:
            if hasattr(self, "_clean_cache_btn"):
                self._clean_cache_btn.setStyleSheet(self._clean_cache_btn_style(accent, is_dark))
        except RuntimeError:
            pass

        try:
            if hasattr(self, "_sep"):
                self._sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
        except RuntimeError:
            pass

        try:
            if hasattr(self, "_last_clean_lb"):
                self._last_clean_lb.setStyleSheet(f"color: {tcs}; background: transparent; {font_qss}")
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_count_lb"):
                self._count_lb.setStyleSheet(f"color: {tcs}; background: transparent; {font_qss}")
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_status_lb"):
                self._status_lb.setStyleSheet(f"color: {tcs}; background: transparent; {font_qss}")
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_section_title_lb"):
                self._section_title_lb.setStyleSheet(
                    f"color: {tcs}; background: transparent; "
                    f"font-size: 11px; letter-spacing: 2px; padding: 12px 16px 4px; {font_qss}"
                )
        except RuntimeError:
            pass

    # ── 按钮样式工厂 ──

    def _mem_release_btn_style(self, accent: str, is_dark: bool = True) -> str:
        _text_color = "#ffffff"
        _disabled_text = "rgba(255,255,255,0.5)" if is_dark else "rgba(0,0,0,0.4)"
        _disabled_bg = "rgba(128,128,128,0.3)" if is_dark else "rgba(0,0,0,0.12)"
        return f"""
        QPushButton {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:1,
                stop:0 {accent},
                stop:1 {_adjust_color(accent, -20)}
            );
            color: {_text_color};
            border: none;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            padding: 0 10px;
        }}
        QPushButton:hover {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:1,
                stop:0 {_adjust_color(accent, 10)},
                stop:1 {accent}
            );
        }}
        QPushButton:disabled {{
            background: {_disabled_bg};
            color: {_disabled_text};
        }}
        """

    def _clean_cache_btn_style(self, accent: str, is_dark: bool = True) -> str:
        if is_dark:
            _bg = "rgba(255,255,255,0.08)"
            _color = "rgba(255,255,255,0.85)"
            _hover_bg = "rgba(255,255,255,0.14)"
            _hover_color = "#ffffff"
            _border = "rgba(255,255,255,0.12)"
            _disabled_bg = "rgba(128,128,128,0.15)"
            _disabled_color = "rgba(255,255,255,0.3)"
        else:
            _bg = "rgba(0,0,0,0.06)"
            _color = "rgba(0,0,0,0.85)"
            _hover_bg = "rgba(0,0,0,0.10)"
            _hover_color = "#1a1a1a"
            _border = "rgba(0,0,0,0.12)"
            _disabled_bg = "rgba(0,0,0,0.06)"
            _disabled_color = "rgba(0,0,0,0.3)"
        return f"""
        QPushButton {{
            background: {_bg};
            color: {_color};
            border: 1px solid {_border};
            border-radius: 8px;
            font-size: 13px;
            font-weight: 500;
            padding: 12px 0;
        }}
        QPushButton:hover {{
            background: {_hover_bg};
            border-color: {accent};
            color: {_hover_color};
        }}
        QPushButton:disabled {{
            background: {_disabled_bg};
            color: {_disabled_color};
            border-color: transparent;
        }}
        """

    # ── UI 构建 ──

    def _setup_ui(self):
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("SystemCleanerCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_header(root)

        self._sep = QFrame(self)
        self._sep.setFrameShape(QFrame.HLine)
        self._sep.setStyleSheet("background: rgba(128,128,128,0.12); max-height: 1px;")
        root.addWidget(self._sep)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical {
                width: 6px;
                background: transparent;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.12);
                border-radius: 3px;
                min-height: 30px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        self._build_memory_row()
        self._build_cache_section()
        self._build_footer()

    def _build_header(self, root: QVBoxLayout):
        _dark = isDarkTheme()
        _tc = "rgba(255,255,255,0.9)" if _dark else "rgba(0,0,0,0.85)"
        _tcs = "rgba(255,255,255,0.45)" if _dark else "rgba(0,0,0,0.45)"
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        self._header_icon = IconWidget(FluentIcon.BROOM, header)
        self._header_icon.setFixedSize(22, 22)
        hly.addWidget(self._header_icon)

        self._header_title = StrongBodyLabel("系统清理", header)
        self._header_title.setStyleSheet(f"color: {_tc}; background: transparent;")
        hly.addWidget(self._header_title)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(f"color: {_tcs}; font-size: 12px; background: transparent;")
        hly.addWidget(self._status_lb)

        hly.addStretch(1)

        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._on_refresh)
        hly.addWidget(self._refresh_btn)

        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

    def _build_memory_row(self):
        _dark = isDarkTheme()
        _mem_bg = "rgba(255,255,255,0.04)" if _dark else "rgba(0,0,0,0.04)"
        _mem_tc = "rgba(255,255,255,0.85)" if _dark else "rgba(0,0,0,0.85)"
        container = QWidget(self._content)
        container.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(16, 8, 16, 4)

        mem_bg = QWidget(container)
        mem_bg.setStyleSheet(f"background: {_mem_bg}; border-radius: 8px;")
        mem_layout = QHBoxLayout(mem_bg)
        mem_layout.setContentsMargins(14, 8, 6, 8)
        mem_layout.setSpacing(8)

        mem_icon = QLabel("📊", mem_bg)
        mem_icon.setStyleSheet("background: transparent; font-size: 15px;")
        mem_layout.addWidget(mem_icon)

        self._mem_label = QLabel("DriFox 进程内存", mem_bg)
        self._mem_label.setStyleSheet(f"color: {_mem_tc}; background: transparent; font-size: 13px;")
        mem_layout.addWidget(self._mem_label)

        mem_layout.addStretch(1)

        self._mem_value = QLabel("获取中…", mem_bg)
        self._mem_value.setStyleSheet("color: #62a0ea; background: transparent; font-size: 14px; font-weight: 600;")
        mem_layout.addWidget(self._mem_value)

        self._mem_release_btn = QPushButton("⚡ 释放内存", mem_bg)
        self._mem_release_btn.setCursor(Qt.PointingHandCursor)
        self._mem_release_btn.setFixedSize(100, 32)
        self._mem_release_btn.setStyleSheet(self._mem_release_btn_style(self._ctx_accent, isDarkTheme()))
        self._mem_release_btn.clicked.connect(self._on_memory_release)
        mem_layout.addWidget(self._mem_release_btn)

        layout.addWidget(mem_bg)
        self._content_layout.addWidget(container)

    def _build_cache_section(self):
        container = QWidget(self._content)
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        _init_tcs = "rgba(255,255,255,0.35)" if isDarkTheme() else "rgba(0,0,0,0.35)"
        section_title = QLabel("—— 可清理缓存 ——", container)
        section_title.setStyleSheet(
            f"color: {_init_tcs}; background: transparent; "
            "font-size: 11px; letter-spacing: 2px; padding: 12px 16px 4px;"
        )
        self._section_title_lb = section_title
        layout.addWidget(section_title)

        btn_container = QWidget(container)
        btn_container.setStyleSheet("background: transparent;")
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(16, 4, 16, 4)

        self._clean_cache_btn = QPushButton("🗑️ 一键清理选中缓存", btn_container)
        self._clean_cache_btn.setCursor(Qt.PointingHandCursor)
        self._clean_cache_btn.setMinimumHeight(42)
        self._clean_cache_btn.setStyleSheet(self._clean_cache_btn_style(self._ctx_accent, isDarkTheme()))
        self._clean_cache_btn.clicked.connect(self._on_clean_cache_clicked)
        btn_layout.addWidget(self._clean_cache_btn)

        layout.addWidget(btn_container)

        for cid, icon, label, _rel_path, _dir_mode in CACHE_DEFS:
            row = _CacheItemRow(cid, icon, label, container)
            row.toggled.connect(self._on_row_toggled)
            self._cache_rows[cid] = row
            layout.addWidget(row)

        self._content_layout.addWidget(container)

    def _build_footer(self):
        _dark = isDarkTheme()
        _tcs = "rgba(255,255,255,0.35)" if _dark else "rgba(0,0,0,0.35)"
        _sep = "rgba(255,255,255,0.06)" if _dark else "rgba(0,0,0,0.06)"
        container = QWidget(self._content)
        container.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(16, 8, 16, 12)

        self._last_clean_lb = QLabel("尚未清理过", container)
        self._last_clean_lb.setStyleSheet(f"color: {_tcs}; background: transparent; font-size: 11px;")
        layout.addWidget(self._last_clean_lb)

        layout.addStretch(1)

        self._count_lb = QLabel("共 5 项", container)
        self._count_lb.setStyleSheet(f"color: {_tcs}; background: transparent; font-size: 11px;")
        layout.addWidget(self._count_lb)

        footer_sep = QFrame(self._content)
        footer_sep.setFrameShape(QFrame.HLine)
        footer_sep.setStyleSheet(f"background: {_sep}; max-height: 1px;")
        self._content_layout.addWidget(footer_sep)
        self._content_layout.addWidget(container)

    # ── 比例高度 ──

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

    # ── 事件 ──

    def _on_close(self):
        self._cleanup_scan()
        self._cleanup_clean()
        self.setVisible(False)
        self.closed.emit()

    def _on_refresh(self):
        self._async_scan()
        self._refresh_memory()

    def _on_row_toggled(self):
        self._update_cache_button()

    # ── ⚡ 内存释放 ──

    def _on_memory_release(self):
        if self._is_releasing_mem:
            return

        self._is_releasing_mem = True
        self._mem_release_btn.setEnabled(False)
        self._mem_release_btn.setText("⚡ 释放中…")
        self._set_status("释放内存中…")

        mem_before = _get_process_memory()
        self._release_internal_caches()

        collected = 0
        for _ in range(3):
            collected += gc.collect()

        try:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
            if app:
                app.sendPostedEvents()
        except Exception:
            pass

        collected += gc.collect()
        mem_after = _get_process_memory()
        freed_mem = (mem_before - mem_after) if (mem_before and mem_after) else None

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._last_mem_release_time = now

        if freed_mem is not None and freed_mem > 0:
            self._mem_release_btn.setText(f"✅ 已释放 {_format_size(freed_mem)} + {collected} 个对象")
        elif freed_mem is not None:
            self._mem_release_btn.setText(f"✅ 已回收 {collected} 个对象")
        else:
            self._mem_release_btn.setText(f"✅ 已回收 {collected} 个对象")

        self._refresh_memory()
        self._mem_release_btn.setEnabled(True)
        self._is_releasing_mem = False
        self._set_status("")

        QTimer.singleShot(3000, self._reset_mem_release_btn)

    def _reset_mem_release_btn(self):
        if not self._is_releasing_mem:
            self._mem_release_btn.setText("⚡ 释放内存")

    def _release_internal_caches(self):
        try:
            from PyQt5.QtGui import QPixmapCache

            QPixmapCache.clear()
        except Exception:
            pass

        try:
            import importlib

            importlib.invalidate_caches()
        except Exception:
            pass

    # ── 内存 ──

    def _refresh_memory(self):
        mem = _get_process_memory()
        accent = self._ctx_accent
        if mem is not None:
            self._mem_value.setText(_format_size(mem))
            self._mem_value.setStyleSheet(
                f"color: {accent}; background: transparent; font-size: 14px; font-weight: 600;"
            )
        else:
            self._mem_value.setText("N/A")
            self._mem_value.setStyleSheet("color: rgba(255,255,255,0.3); background: transparent; font-size: 14px;")

    # ── 扫描 ──

    def _async_scan(self):
        drifox = _drifox_dir()
        if drifox is None:
            self._set_status("未找到数据目录")
            return

        self._set_status("扫描中…")
        self._refresh_btn.setEnabled(False)

        self._cleanup_scan()
        w = _ScanWorker(drifox)
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(self._on_scan_done)
        w.error.connect(self._on_scan_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._scan_worker, self._scan_thread = w, t
        t.start()

    def _on_scan_done(self, sizes: Dict[str, int]):
        self._refresh_btn.setEnabled(True)

        total = 0
        checked_count = 0
        for cid, row in self._cache_rows.items():
            sz = sizes.get(cid, 0)
            row.set_size(sz)
            if row.is_checked() and sz > 0:
                total += sz
                checked_count += 1

        self._update_cache_button()
        self._count_lb.setText(f"共 {len(self._cache_rows)} 项")
        self._set_status("")

    def _on_scan_error(self, err: str):
        self._refresh_btn.setEnabled(True)
        self._set_status("扫描失败")
        logger.error(f"[SystemCleaner] 扫描失败: {err}")

    # ── 缓存清理 ──

    def _on_clean_cache_clicked(self):
        if self._is_cleaning:
            return

        selected = []
        total_size = 0
        for cid, _icon, _label, rel_path, dir_mode in CACHE_DEFS:
            row = self._cache_rows.get(cid)
            if row and row.is_checked() and row.get_size() > 0:
                selected.append((cid, _icon, _label, rel_path, dir_mode))
                total_size += row.get_size()

        if not selected:
            _styled_cleaner_info(self.window(), "清理缓存", "没有可清理的缓存项。", color_source=self)
            return

        if total_size == 0:
            _styled_cleaner_info(self.window(), "清理缓存", "选中项已为空，无需清理。", color_source=self)
            return

        names = "、".join(label for _, _, label, _, _ in selected)
        reply = _styled_cleaner_confirm(
            self.window(),
            "确认清理缓存",
            f"将清理以下 {len(selected)} 项缓存，共释放 {_format_size(total_size)} 磁盘空间：\n"
            f"{names}\n此操作不可撤销，确认继续？",
            color_source=self,
        )

        if not reply:
            return

        self._start_cache_clean(selected, total_size)

    def _start_cache_clean(self, selected: list, total_size: int):
        drifox = _drifox_dir()
        if drifox is None:
            return

        self._is_cleaning = True
        self._clean_cache_btn.setEnabled(False)
        self._clean_cache_btn.setText("🗑️ 清理中…")
        self._set_status("清理缓存中…")
        self._refresh_btn.setEnabled(False)

        self._cleanup_clean()
        w = _CleanWorker(drifox, selected)
        t = QThread(self)
        w.moveToThread(t)
        t.started.connect(w.run)
        w.progress.connect(self._on_clean_progress)
        w.finished.connect(self._on_clean_done)
        w.error.connect(self._on_clean_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._clean_worker, self._clean_thread = w, t
        t.start()

        self._clean_selected_count = len(selected)
        self._clean_total_size = total_size

    def _on_clean_progress(self, label: str):
        self._set_status(f"清理 {label}…")

    def _on_clean_done(self, freed: Dict[str, int]):
        self._is_cleaning = False
        self._clean_cache_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)

        total_freed = sum(freed.values())
        self._clean_cache_btn.setText(f"✅ 清理完成，释放 {_format_size(total_freed)}")
        self._set_status("")

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._last_clean_time = now
        self._last_clean_lb.setText(f"📝 上次清理: {now}")

        self._async_scan()
        self._refresh_memory()

        QTimer.singleShot(3000, self._reset_cache_clean_btn)

    def _reset_cache_clean_btn(self):
        if not self._is_cleaning:
            self._update_cache_button()

    def _on_clean_error(self, err: str):
        self._is_cleaning = False
        self._clean_cache_btn.setEnabled(True)
        self._clean_cache_btn.setText("🗑️ 一键清理选中缓存")
        self._refresh_btn.setEnabled(True)
        self._set_status("清理出错")
        logger.error(f"[SystemCleaner] 清理失败: {err}")

    # ── 辅助 ──

    def _update_cache_button(self):
        if self._is_cleaning:
            return

        total_size = sum(row.get_contribution() for row in self._cache_rows.values())

        if total_size > 0:
            self._clean_cache_btn.setText(f"🗑️ 一键清理选中缓存 (共 {_format_size(total_size)})")
            self._clean_cache_btn.setEnabled(True)
        else:
            self._clean_cache_btn.setText("🗑️ 一键清理选中缓存")
            self._clean_cache_btn.setEnabled(False)

    def _set_status(self, text: str):
        try:
            self._status_lb.setText(text)
        except RuntimeError:
            pass

    # ── 清理 ──

    def _cleanup_scan(self):
        if self._scan_thread is not None:
            try:
                self._scan_thread.quit()
                self._scan_thread.wait(500)
            except RuntimeError:
                pass
            self._scan_thread = None
        self._scan_worker = None

    def _cleanup_clean(self):
        if self._clean_thread is not None:
            try:
                self._clean_thread.quit()
                self._clean_thread.wait(500)
            except RuntimeError:
                pass
            self._clean_thread = None
        self._clean_worker = None

    def deleteLater(self):
        self._cleanup_scan()
        self._cleanup_clean()
        super().deleteLater()
