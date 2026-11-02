# -*- coding: utf-8 -*-
"""SystemCleanerCard 浮动卡片 — 一键清理 DriFox 缓存垃圾，释放磁盘空间与内存

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 os/shutil/stdlib 完成
- 内存读取通过 psutil 完成
"""

from datetime import datetime
import threading
from typing import Callable, Dict, Optional, Tuple

from PyQt5.QtCore import (
    QEvent,
    QEasingCurve,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt5.QtGui import QBrush, QColor, QConicalGradient, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    SmoothScrollDelegate,
    StrongBodyLabel,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger

from .cleaner import _CacheItemRow, _on_accent_text, _styled_cleaner_confirm, _styled_cleaner_info
from .icons import svg_pixmap
from .scanner import (
    CACHE_DEFS,
    _CleanWorker,
    _ScanWorker,
    _drifox_dir,
    _format_size,
    _get_process_memory,
    _kill_qwebengine_processes,
    _release_memory_deep_core,
    _release_memory_light,
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
# 装饰性动态控件（环形仪表 / 旋转指示器）
# ══════════════════════════════════════════════════════════


class _GaugeRing(QWidget):
    """装饰性环形仪表：打开卡片时渐变弧从 0° 扫到 270°，之后静止（无常驻动画，不耗 CPU）"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._progress = 0.0
        self._color = QColor("#62a0ea")
        self._icon_pm: Optional[QPixmap] = None
        self._anim: Optional[QVariantAnimation] = None
        self.setFixedSize(76, 76)

    def set_color(self, color: str):
        """主题切换时更新弧色与中央图标"""
        c = QColor(color)
        if c.isValid():
            self._color = c
        self._render_icon()
        self.update()

    def sweep(self):
        """启动扫掠动画（show 时调用一次；重复调用会重启）"""
        if self._anim is not None:
            self._anim.stop()
            self._anim.deleteLater()
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(900)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(self._on_anim_frame)
        anim.finished.connect(anim.deleteLater)
        self._anim = anim
        anim.start()

    def _on_anim_frame(self, v):
        self._progress = float(v)
        self.update()

    def _render_icon(self):
        self._icon_pm = svg_pixmap("activity", self._color.name(), size=20, dpr=self.devicePixelRatioF())
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(7, 7, -7, -7)
        pen_w = 5.0
        # 背景环：270° 固定弧，缺口朝右下
        p.setPen(QPen(QColor(128, 128, 128, 46), pen_w, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 45 * 16, 270 * 16)
        if self._progress > 0.001:
            # 前景弧：accent 锥形渐变，与背景同起点顺时针扫掠
            grad = QConicalGradient(rect.center(), 45)
            grad.setColorAt(0.0, self._color.lighter(125))
            grad.setColorAt(1.0, self._color.darker(125))
            p.setPen(QPen(QBrush(grad), pen_w, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 45 * 16, -int(270 * 16 * self._progress))
        # 中央 activity 图标
        if self._icon_pm is not None and not self._icon_pm.isNull():
            dpr = self._icon_pm.devicePixelRatio() or 1.0
            x = int((self.width() - self._icon_pm.width() / dpr) / 2)
            y = int((self.height() - self._icon_pm.height() / dpr) / 2)
            p.drawPixmap(x, y, self._icon_pm)


class _Spinner(QWidget):
    """旋转弧指示器：仅在扫描/清理进行中转动（QTimer 驱动，结束后停止）"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._angle = 0
        self._color = QColor("#62a0ea")
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(14, 14)
        self.hide()

    def set_color(self, color: str):
        c = QColor(color)
        if c.isValid():
            self._color = c
            self.update()

    def start(self):
        self.show()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 14) % 360
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(self._color, 2.0, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, (self._angle + 180) * 16, 100 * 16)


# 内存大号数字统一样式（构建期与主题刷新共用，避免两处漂移）
_MEM_VALUE_QSS = (
    "color: {accent}; background: transparent; font-size: 24px; font-weight: 700; letter-spacing: 0.5px; {font_qss}"
)


# ══════════════════════════════════════════════════════════
# 主卡片
# ══════════════════════════════════════════════════════════


class SystemCleanerCard(QWidget):
    """系统清理浮动卡片"""

    closed = pyqtSignal()
    # 内存释放收尾信号：拍3 后台线程完成时 emit，跨线程自动 queued 回主线程。
    # 后台线程没有 Qt 事件循环，不能在那里调 QTimer.singleShot（回调永不执行）。
    _mem_release_finished = pyqtSignal(object, int, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[_ScanWorker] = None
        self._clean_thread: Optional[QThread] = None
        self._clean_worker: Optional[_CleanWorker] = None
        self._is_scanning = False
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
        self._ctx_is_dark = isDarkTheme()

        self._mem_release_finished.connect(self._finish_memory_release)
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
        if hasattr(self, "_gauge"):
            self._gauge.sweep()  # 开场仪表扫掠动效
        self.setVisible(True)
        # [DEBUG-reopen] 临时诊断：show_card 尾部执行确认 + 状态位指纹
        try:
            logger.warning(
                f"[DEBUG-reopen] card.show_card done hidden={self.isHidden()} "
                f"vis={self.isVisible()} parent={type(self.parent()).__name__} "
                f"parent_vis={self.parent().isVisible() if self.parent() else None}"
            )
        except Exception:
            pass

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
            # [theme-probe] 临时诊断：provider 缺失/抛错导致整次刷新早退
            logger.warning("[theme-probe] apply_latest ctx=None（provider 缺失或抛错）")
            return
        logger.warning(
            f"[theme-probe] apply_latest colors={len(_ctx_colors(ctx))} "
            f"dark={ctx.get('is_dark')} font={ctx.get('font_family')}"
        )

        self._ctx_font_family, self._ctx_font_size = _ctx_font(ctx)
        ff = self._ctx_font_family
        fs = self._ctx_font_size

        colors = _ctx_colors(ctx)
        accent = colors.get("accent", "#62a0ea")
        danger = colors.get("danger", "#e34c4c") or colors.get("accent_warm", "#e34c4c")
        is_dark = ctx.get("is_dark", True)
        self._ctx_accent = accent
        self._ctx_danger = danger
        self._ctx_is_dark = is_dark
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
                self._mem_release_btn.setStyleSheet(self._mem_release_btn_style(accent, fs, is_dark))
        except RuntimeError:
            pass

        try:
            if hasattr(self, "_mem_label"):
                self._mem_label.setStyleSheet(
                    f"color: {tcs}; background: transparent; font-size: {max(11, fs - 3)}px; {font_qss}"
                )
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_mem_value"):
                self._mem_value.setStyleSheet(_MEM_VALUE_QSS.format(accent=accent, font_qss=font_qss))
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_gauge"):
                self._gauge.set_color(accent)
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_spinner"):
                self._spinner.set_color(accent)
        except RuntimeError:
            pass
        self._apply_hero_style()

        for row in self._cache_rows.values():
            row.set_font_ctx(ff, fs)
            row.set_accent_color(accent)
            row.set_dark_mode(is_dark)

        try:
            if hasattr(self, "_clean_cache_btn"):
                self._clean_cache_btn.setStyleSheet(self._clean_cache_btn_style(accent, fs, is_dark))
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
                    f"font-size: {max(10, fs - 3)}px; letter-spacing: 2px; padding: 12px 16px 4px; {font_qss}"
                )
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_last_clean_lb"):
                self._last_clean_lb.setStyleSheet(
                    f"color: {tcs}; background: transparent; font-size: {max(10, fs - 3)}px; {font_qss}"
                )
        except RuntimeError:
            pass
        try:
            if hasattr(self, "_count_lb"):
                self._count_lb.setStyleSheet(
                    f"color: {tcs}; background: transparent; font-size: {max(10, fs - 3)}px; {font_qss}"
                )
        except RuntimeError:
            pass

    # ── 按钮样式工厂 ──

    def _mem_release_btn_style(self, accent: str, fs: int, is_dark: bool = True) -> str:
        _disabled_text = "rgba(255,255,255,0.5)" if is_dark else "rgba(0,0,0,0.4)"
        _disabled_bg = "rgba(128,128,128,0.3)" if is_dark else "rgba(0,0,0,0.12)"
        hi = _adjust_color(accent, 24)
        lo = _adjust_color(accent, -28)
        _fg = _on_accent_text(accent)
        return f"""
        QPushButton {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:1,
                stop:0 {_adjust_color(accent, 12)},
                stop:0.55 {accent},
                stop:1 {lo}
            );
            color: {_fg};
            border: none;
            border-radius: 17px;
            font-size: {max(10, fs - 2)}px;
            font-weight: 600;
            padding: 0 16px;
        }}
        QPushButton:hover {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:1,
                stop:0 {hi},
                stop:0.55 {_adjust_color(accent, 12)},
                stop:1 {accent}
            );
        }}
        QPushButton:pressed {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:1,
                stop:0 {accent},
                stop:0.55 {lo},
                stop:1 {_adjust_color(lo, -12)}
            );
        }}
        QPushButton:disabled {{
            background: {_disabled_bg};
            color: {_disabled_text};
        }}
        """

    def _clean_cache_btn_style(self, accent: str, fs: int, is_dark: bool = True) -> str:
        _disabled_bg = "rgba(128,128,128,0.22)" if is_dark else "rgba(0,0,0,0.08)"
        _disabled_color = "rgba(255,255,255,0.35)" if is_dark else "rgba(0,0,0,0.3)"
        hi = _adjust_color(accent, 20)
        lo = _adjust_color(accent, -26)
        _fg = _on_accent_text(accent)
        return f"""
        QPushButton {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {hi}, stop:0.5 {accent}, stop:1 {lo});
            color: {_fg};
            border: none;
            border-radius: 12px;
            font-size: {max(11, fs - 1)}px;
            font-weight: 600;
            letter-spacing: 1px;
            padding: 13px 0;
        }}
        QPushButton:hover {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {_adjust_color(hi, 12)}, stop:0.5 {hi}, stop:1 {accent});
        }}
        QPushButton:pressed {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {accent}, stop:0.5 {lo}, stop:1 {_adjust_color(lo, -14)});
        }}
        QPushButton:disabled {{
            background: {_disabled_bg};
            color: {_disabled_color};
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
        # 两端淡出的渐变分隔线（比硬直线更轻盈现代）
        self._sep.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            " stop:0 rgba(128,128,128,0), stop:0.5 rgba(128,128,128,0.28), stop:1 rgba(128,128,128,0));"
            " max-height: 1px; border: none;"
        )
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
                background: rgba(128,128,128,0.25);
                border-radius: 3px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(128,128,128,0.45);
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
        # 背景透明（用户要求）：不刷等价实底，放弃滚动 blit 优化。
        # 本卡片仅 8 行缓存项，滚动量极小，全视口重绘代价可接受。
        SmoothScrollDelegate(self._scroll)  # 平滑滚动（与主程序同款引擎）
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

        self._spinner = _Spinner(header)
        self._spinner.set_color(self._ctx_accent)
        hly.addWidget(self._spinner)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(f"color: {_tcs}; font-size: 12px; background: transparent;")
        hly.addWidget(self._status_lb)

        hly.addStretch(1)

        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, header)
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
        """内存英雄卡：环形仪表 + 大号数字 + 渐变胶囊按钮，形成卡片视觉锚点"""
        fs = self._ctx_font_size or 14
        wrap = QWidget(self._content)
        wrap.setStyleSheet("background: transparent;")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(16, 12, 16, 4)

        hero = QFrame(wrap)
        hero.setObjectName("memHero")
        self._hero = hero
        self._apply_hero_style()
        hly = QHBoxLayout(hero)
        hly.setContentsMargins(14, 12, 14, 12)
        hly.setSpacing(14)

        self._gauge = _GaugeRing(hero)
        self._gauge.set_color(self._ctx_accent)
        hly.addWidget(self._gauge)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        init_tcs = "rgba(255,255,255,0.55)" if isDarkTheme() else "rgba(0,0,0,0.5)"
        self._mem_label = QLabel("DriFox 进程内存", hero)
        self._mem_label.setStyleSheet(f"color: {init_tcs}; background: transparent; font-size: {max(11, fs - 3)}px;")
        text_col.addWidget(self._mem_label)
        self._mem_value = QLabel("获取中…", hero)
        self._mem_value.setStyleSheet(_MEM_VALUE_QSS.format(accent=self._ctx_accent, font_qss=""))
        text_col.addWidget(self._mem_value)
        hly.addLayout(text_col)

        hly.addStretch(1)

        self._mem_release_btn = TransparentPushButton("释放内存", hero)
        self._mem_release_btn.setCursor(Qt.PointingHandCursor)
        self._mem_release_btn.setFixedHeight(34)
        self._mem_release_btn.setMinimumWidth(112)
        self._mem_release_btn.setStyleSheet(self._mem_release_btn_style(self._ctx_accent, fs, isDarkTheme()))
        self._mem_release_btn.clicked.connect(self._on_memory_release)
        hly.addWidget(self._mem_release_btn, 0, Qt.AlignVCenter)

        wl.addWidget(hero)
        self._content_layout.addWidget(wrap)

    def _apply_hero_style(self):
        """英雄卡玻璃质感：半透明底 + 细描边 + 大圆角（跟随主题）"""
        if not hasattr(self, "_hero"):
            return
        if self._ctx_is_dark:
            bg, border = "rgba(255,255,255,0.045)", "rgba(255,255,255,0.09)"
        else:
            bg, border = "rgba(0,0,0,0.035)", "rgba(0,0,0,0.10)"
        self._hero.setStyleSheet(
            f"QFrame#memHero {{ background: {bg}; border: 1px solid {border}; border-radius: 14px; }}"
        )

    def _build_cache_section(self):
        container = QWidget(self._content)
        container.setStyleSheet("background: transparent;")
        fs = self._ctx_font_size or 14
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        _init_tcs = "rgba(255,255,255,0.35)" if isDarkTheme() else "rgba(0,0,0,0.35)"
        self._section_title_lb = QLabel("可清理项", container)
        self._section_title_lb.setStyleSheet(
            f"color: {_init_tcs}; background: transparent; "
            "font-size: 11px; letter-spacing: 2px; padding: 12px 16px 4px;"
        )
        layout.addWidget(self._section_title_lb)

        btn_container = QWidget(container)
        btn_container.setStyleSheet("background: transparent;")
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(16, 4, 16, 4)

        self._clean_cache_btn = TransparentPushButton("一键清理选中缓存", btn_container)
        self._clean_cache_btn.setCursor(Qt.PointingHandCursor)
        self._clean_cache_btn.setMinimumHeight(46)
        self._clean_cache_btn.setStyleSheet(self._clean_cache_btn_style(self._ctx_accent, fs, isDarkTheme()))
        self._clean_cache_btn.clicked.connect(self._on_clean_cache_clicked)
        btn_layout.addWidget(self._clean_cache_btn)

        layout.addWidget(btn_container)

        # 行卡片独立容器：行间距 4px，与主按钮左右对齐（16px 边距）
        rows_wrap = QWidget(container)
        rows_wrap.setStyleSheet("background: transparent;")
        rows_lay = QVBoxLayout(rows_wrap)
        rows_lay.setContentsMargins(16, 4, 16, 4)
        rows_lay.setSpacing(4)

        for item in CACHE_DEFS:
            row = _CacheItemRow(
                item.cid, item.icon, item.label, rows_wrap, tip=item.tip, default_checked=item.default_checked
            )
            row.toggled.connect(self._on_row_toggled)
            self._cache_rows[item.cid] = row
            rows_lay.addWidget(row)

        layout.addWidget(rows_wrap)

        self._content_layout.addWidget(container)

    def _build_footer(self):
        _dark = isDarkTheme()
        _tcs = "rgba(255,255,255,0.35)" if _dark else "rgba(0,0,0,0.35)"
        container = QWidget(self._content)
        container.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(16, 8, 16, 12)

        self._last_clean_lb = QLabel("尚未清理过", container)
        self._last_clean_lb.setStyleSheet(f"color: {_tcs}; background: transparent; font-size: 11px;")
        layout.addWidget(self._last_clean_lb)

        layout.addStretch(1)

        self._count_lb = QLabel(f"共 {len(CACHE_DEFS)} 项", container)
        self._count_lb.setStyleSheet(f"color: {_tcs}; background: transparent; font-size: 11px;")
        layout.addWidget(self._count_lb)

        footer_sep = QFrame(self._content)
        footer_sep.setFrameShape(QFrame.HLine)
        footer_sep.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            " stop:0 rgba(128,128,128,0), stop:0.5 rgba(128,128,128,0.28), stop:1 rgba(128,128,128,0));"
            " max-height: 1px; border: none;"
        )
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
        self._async_scan(force=True)
        self._refresh_memory()

    def _on_row_toggled(self):
        self._update_cache_button()

    # ── 内存释放 ──

    def _on_memory_release(self):
        """深度释放进程内存并归还给操作系统。

        分拍执行（2026-09-11）：整个释放链含全代 GC、工作集修剪、终止 WebEngine
        子进程等高危原生操作。原先同步塞在鼠标事件里，「GC 连带析构 QObject」「堆
        修剪遍历」会与 Chromium 后台线程在途回调（console 上报等）撞在同一拍，是
        crash_*.log 中 scanner.py:317/328 处 9 次 access violation 闪退的根因
        （其中一次直接崩在 message_card 的 javaScriptConsoleMessage 回调里）。
        现拆成两拍：轻清理 → 事件循环排干 120ms（在途回调、deleteLater 落地）→
        再执行 GC/堆归还/杀进程，既缩小竞态窗口，也避免按钮点击期间 UI 冻结数秒。
        """
        if self._is_releasing_mem:
            return

        self._is_releasing_mem = True
        self._mem_release_btn.setEnabled(False)
        self._mem_release_btn.setText("释放中…")
        self._set_status("深度清理内存 + WebEngine 进程中…")

        mem_before = _get_process_memory()
        _release_memory_light()

        # ── 分三拍（2026-10-31 运行日志驱动）──
        # 拍1（本帧）：轻清理。
        # 拍2（+120ms 主线程）：GC 全代 + 工作集修剪。留主线程是因为 gc.collect(2)
        #   回收期间暂停全部线程，换到后台只是把停顿挪个位置，主线程照样卡；
        #   而它确实需要「事件循环已排干在途回调」这个前置（既有崩溃修复）。
        # 拍3（后台线程）：`_kill_qwebengine_processes` 的 psutil.process_iter
        #   全系统遍历。这是真实的阻塞式 IO（逐个 OpenProcess 取 pid/name/ppid），
        #   实测主线程阻塞 501ms，栈顶落在 psutil._pswindows.ppid。它只碰 OS
        #   进程句柄，不碰任何 QObject，放后台是安全的。
        # 收尾回主线程，所有 UI 写操作 + sip 活性硬判都在那里。
        def _deep_core_on_main():
            try:
                collected = _release_memory_deep_core()
            except Exception:  # noqa: BLE001 — 主线程阶段失败不能吞掉按钮复位
                collected = 0

            def _kill_in_background():
                try:
                    killed = _kill_qwebengine_processes()
                except Exception:  # noqa: BLE001
                    killed = 0
                try:
                    # 跨线程回主线程只能走信号（auto queued connection）。
                    # b47875df 曾在此调 QTimer.singleShot(0, ...)：daemon 线程无
                    # 事件循环，回调永不执行，按钮永久卡「释放中…」。
                    self._mem_release_finished.emit(mem_before, collected, killed)
                except RuntimeError:
                    pass  # 卡片已销毁，无需收尾

            threading.Thread(
                target=_kill_in_background,
                daemon=True,
                name="cleaner-kill-webengine",
            ).start()

        QTimer.singleShot(120, _deep_core_on_main)

    def _finish_memory_release(self, mem_before, collected, killed_procs):
        """深度释放完成后的主线程收尾（按钮文案 / 状态 / 刷新）。

        单独成方法是为了让 QTimer 回调里只有一个方法调用：卡片在后台线程
        工作期间被销毁时，lambda 内的属性访问会在调用时立刻抛 RuntimeError，
        由 sip 活性检查提前拦截。
        """
        # 活性判定用 sip.isdeleted 硬判：已析构 wrapper 上访问 Python 侧实例
        # 属性不会抛异常（项目既有坑），靠属性访问做存在性检查会静默漏过。
        try:
            from PyQt5 import sip as _sip

            if _sip.isdeleted(self) or _sip.isdeleted(self._mem_release_btn):
                return
        except Exception:
            return
        try:
            mem_after = _get_process_memory()

            freed_mem = (mem_before - mem_after) if (mem_before and mem_after) else None

            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            self._last_mem_release_time = now

            parts = []
            if freed_mem is not None and freed_mem > 0:
                parts.append(f"释放 {_format_size(freed_mem)}")
            if collected:
                parts.append(f"回收 {collected} 对象")
            if killed_procs:
                parts.append(f"结束 {killed_procs} 个 WebEngine 进程")
            self._mem_release_btn.setText(f"{' · '.join(parts) if parts else '清理完成'}")

            self._refresh_memory()
            self._mem_release_btn.setEnabled(True)
            self._is_releasing_mem = False
            self._set_status("")

            QTimer.singleShot(3000, self._reset_mem_release_btn)
        except RuntimeError:
            return  # 控件在收尾途中被销毁

    def _reset_mem_release_btn(self):
        if not self._is_releasing_mem:
            self._mem_release_btn.setText("释放内存")

    # ── 内存 ──

    def _refresh_memory(self):
        mem = _get_process_memory()
        accent = self._ctx_accent
        self._mem_value.setStyleSheet(_MEM_VALUE_QSS.format(accent=accent, font_qss=""))
        if mem is None:
            self._mem_value.setText("N/A")
            return
        self._animate_mem_value(mem)

    def _animate_mem_value(self, target: int):
        """内存数字缓动：从旧值滚到新值（首次从 0 起），与仪表扫掠呼应"""
        old = getattr(self, "_mem_display_bytes", None)
        self._mem_display_bytes = target
        if old is None or old == target:
            self._mem_value.setText(_format_size(target))
            return
        anim = QVariantAnimation(self)  # parent=self：卡片销毁时动画一并销毁
        anim.setStartValue(float(old))
        anim.setEndValue(float(target))
        anim.setDuration(450)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(self._on_mem_anim_frame)
        anim.finished.connect(anim.deleteLater)
        anim.start()

    def _on_mem_anim_frame(self, v):
        try:
            self._mem_value.setText(_format_size(int(v)))
        except RuntimeError:
            pass  # 动画途中卡片被销毁

    # ── 扫描 ──

    def _async_scan(self, force: bool = False):
        drifox = _drifox_dir()
        if drifox is None:
            self._set_status("未找到数据目录")
            return

        # 去抖：扫描未完成时不重复启动（show_card 每次都调）；
        # force=True（手动刷新/清理后重扫）则取消旧扫描立即重扫。
        # 旧扫描取消等待实测 <50ms（目录边界生效），不卡 UI。
        if self._is_scanning and not force:
            return

        self._set_status("扫描中…")
        self._refresh_btn.setEnabled(False)

        self._cleanup_scan()
        w = _ScanWorker(drifox)
        t = QThread()  # 无 parent：卡片销毁不析构运行中线程，生命周期由 finished→deleteLater 链接管
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(self._on_scan_done)
        w.error.connect(self._on_scan_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        # worker 销毁挂线程退出信号：正常完成与取消返回两条路径都会走 t.finished，
        # 避免被取消的 worker 无人 deleteLater → 跨线程析构崩溃
        t.finished.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._scan_worker, self._scan_thread = w, t
        self._is_scanning = True
        t.start()

    def _on_scan_done(self, sizes: Dict[str, int]):
        self._is_scanning = False
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
        self._is_scanning = False
        self._refresh_btn.setEnabled(True)
        self._set_status("扫描失败")
        logger.error(f"[SystemCleaner] 扫描失败: {err}")

    # ── 缓存清理 ──

    def _on_clean_cache_clicked(self):
        if self._is_cleaning:
            return

        selected = []
        total_size = 0
        for item in CACHE_DEFS:
            row = self._cache_rows.get(item.cid)
            if row and row.is_checked() and row.get_size() > 0:
                selected.append(item)
                total_size += row.get_size()

        if not selected:
            _styled_cleaner_info(self.window(), "清理缓存", "没有可清理的缓存项。", color_source=self)
            return

        if total_size == 0:
            _styled_cleaner_info(self.window(), "清理缓存", "选中项已为空，无需清理。", color_source=self)
            return

        names = "、".join(item.label for item in selected)
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
        self._clean_cache_btn.setText("清理中…")
        self._set_status("清理缓存中…")
        self._refresh_btn.setEnabled(False)

        self._cleanup_clean()
        w = _CleanWorker(drifox, selected)
        t = QThread()  # 无 parent：同 _async_scan
        w.moveToThread(t)
        t.started.connect(w.run)
        w.progress.connect(self._on_clean_progress)
        w.finished.connect(self._on_clean_done)
        w.error.connect(self._on_clean_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        t.finished.connect(w.deleteLater)
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
        self._clean_cache_btn.setText(f"清理完成，释放 {_format_size(total_freed)}")
        self._set_status("")

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._last_clean_time = now
        self._last_clean_lb.setText(f"上次清理: {now}")

        self._async_scan(force=True)
        self._refresh_memory()

        QTimer.singleShot(3000, self._reset_cache_clean_btn)

    def _reset_cache_clean_btn(self):
        if not self._is_cleaning:
            self._update_cache_button()

    def _on_clean_error(self, err: str):
        self._is_cleaning = False
        self._clean_cache_btn.setEnabled(True)
        self._clean_cache_btn.setText("一键清理选中缓存")
        self._refresh_btn.setEnabled(True)
        self._set_status("清理出错")
        logger.error(f"[SystemCleaner] 清理失败: {err}")

    # ── 辅助 ──

    def _update_cache_button(self):
        if self._is_cleaning:
            return

        total_size = sum(row.get_contribution() for row in self._cache_rows.values())

        if total_size > 0:
            self._clean_cache_btn.setText(f"一键清理 · {_format_size(total_size)}")
            self._clean_cache_btn.setEnabled(True)
        else:
            self._clean_cache_btn.setText("勾选下方要清理的缓存项")
            self._clean_cache_btn.setEnabled(False)

    def _set_status(self, text: str):
        try:
            self._status_lb.setText(text)
            # 进行中的任务都以「…」结尾，此时转 spinner；错误/提示类停转
            if text.endswith("…"):
                self._spinner.start()
            else:
                self._spinner.stop()
        except RuntimeError:
            pass

    # ── 清理 ──

    def _cleanup_scan(self):
        # 协作式取消后旧 worker 在下一个目录边界快速退出；
        # 必须等线程真正结束才能丢引用——提前丢会让 GC 从主线程
        # 析构仍属于工作线程的 worker（跨线程析构 → 0xC0000005 崩溃）。
        if self._scan_worker is not None:
            try:
                self._scan_worker.cancel()
            except RuntimeError:
                pass
        if self._scan_thread is not None:
            try:
                self._scan_thread.quit()
                self._scan_thread.wait(1000)
            except RuntimeError:
                pass
        self._scan_thread = None
        self._scan_worker = None

    def _cleanup_clean(self):
        # 清理 worker 不可中断（删一半会留垃圾）。同 _cleanup_scan：
        # 等线程真正结束再丢引用，避免跨线程析构；清理通常秒级完成，
        # wait 超时只发生在超大缓存清理中关卡片时，此时放弃等待但引用
        # 语义仍不安全——因此仅在 wait 成功后才清 worker 引用。
        if self._clean_thread is not None:
            try:
                self._clean_thread.quit()
                finished = self._clean_thread.wait(3000)
            except RuntimeError:
                finished = True
            if finished:
                self._clean_thread = None
                self._clean_worker = None
        else:
            self._clean_worker = None

    def deleteLater(self):
        self._cleanup_scan()
        self._cleanup_clean()
        super().deleteLater()
