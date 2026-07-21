# -*- coding: utf-8 -*-
"""
【可复制模板】浮动卡片骨架 — 与 system-cleaner / plugin-manager 等官方卡片视觉完全一致

使用方式：
  1. 复制本文件到 plugins/<your-plugin>/ui/cards.py
  2. 全局替换以下占位符：
     - CardTemplate       → 你的卡片类名（如 MyPluginCard）
     - <CardName>         → 中文卡片名
     - FluentIcon.BOOK    → 合适的图标
     - "卡片标题"         → 显示标题
  3. 实现 _load_data() 返回数据
  4. 实现 _render_data(data) 填充内容布局
  5. 修改 plugins/<your-plugin>/ui/__init__.py 注册

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 stdlib 完成
"""

import re
import traceback
from typing import Any, Callable, Optional

from PyQt5.QtCore import QEvent, QObject, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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


# ════════════════════════════════════════════════════════════
# 主题色/字体辅助函数（所有卡片共享，直接复制）
# ════════════════════════════════════════════════════════════


def _text_color(secondary: bool = False) -> str:
    """fallback 文字颜色（无 context_provider 时使用）"""
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_font(ctx: dict) -> tuple:
    """从上下文提取 font_family 和 font_size"""
    ff = ctx.get("font_family", "Microsoft YaHei")
    fs = ctx.get("font_size", 14)
    return ff, fs


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    """从上下文 colors 中获取文字颜色，无则回退"""
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _ctx_border_color(ctx: dict) -> str:
    """从上下文获取边框颜色"""
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


def _adjust_color(hex_color: str, amount: int) -> str:
    """简单调亮/调暗一个 hex 颜色"""
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


# ════════════════════════════════════════════════════════════
# 异步 Worker
# ════════════════════════════════════════════════════════════


class _Worker(QObject):
    """后台执行阻塞操作，通过信号返回结果"""

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


# ════════════════════════════════════════════════════════════
# 主卡片
# ════════════════════════════════════════════════════════════


class CardTemplate(QWidget):
    """<CardName> 浮动卡片

    继承此类并实现 _load_data() + _render_data() 即可。
    """

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        self._header_icon: Optional[IconWidget] = None

        # 缓存上下文值（供 _retheme 和动态创建的子控件使用）
        self._cached_tc = "rgba(255,255,255,0.9)"
        self._cached_tcs = "rgba(255,255,255,0.55)"
        self._cached_font_family = "Microsoft YaHei"
        self._cached_font_size = 14

        self._setup_ui()

    # ── 上下文注入 ──────────────────────────────────────

    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）"""
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：刷新主题 + 图标 + 加载数据"""
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._async_refresh()
        self.setVisible(True)

    def _apply_plugin_icon(self):
        """从上下文获取插件图标更新头部图标（所有实际卡片均实现此模式）"""
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
        """从上下文拉取最新主题色 + 字体并刷新全部子控件

        三层字体策略：
        1. self.setFont(QFont) — 级联到无显式 font 的子控件
        2. _retheme() 替换 QSS 中的 color + font-size
        3. FluentLabelBase 直接 setFont — 覆盖内部硬编码字体

        ⚠️ 动态创建子控件后必须调 _retheme()！
        """
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

        # 缓存
        self._cached_tc = tc
        self._cached_tcs = tcs
        self._cached_font_family = font_family
        self._cached_font_size = font_size

        # 第 1 层：QFont 级联
        if font_family:
            self.setFont(QFont(font_family, font_size if font_size else 14))

        # 第 2+3 层
        self._retheme()

        # 分隔线颜色
        try:
            for sep in self.findChildren(QFrame):
                if sep.frameShape() == QFrame.HLine:
                    sep.setStyleSheet(f"background: {border_c}; max-height: 1px;")
        except RuntimeError:
            pass

    def _retheme(self):
        """第 2+3 层字体策略：替换 QSS 颜色字号 + 覆盖 FluentLabelBase"""
        tc = self._cached_tc
        ff = self._cached_font_family
        fs = self._cached_font_size

        for child in self.findChildren(QLabel):
            try:
                # 第 3 层：FluentLabelBase 强制覆盖
                if isinstance(child, FluentLabelBase) and ff:
                    child.setFont(QFont(ff, fs))

                # 第 2 层：替换 QSS 中的 color + font-size
                ss = child.styleSheet()
                if not ss:
                    continue
                new_ss = re.sub(r"color:\s*[^;]+;", f"color: {tc};", ss)
                if fs:
                    new_ss = re.sub(
                        r"font-size:\s*[^;]+;", f"font-size: {fs}px;", new_ss
                    )
                if ff and f"font-family: '{ff}'" not in new_ss:
                    new_ss += f" font-family: '{ff}';"
                child.setStyleSheet(new_ss)
            except RuntimeError:
                pass

    # ── 界面搭建 ────────────────────────────────────────

    def _setup_ui(self):
        """构建卡片界面（子类可重写，但建议只重写 _customize_ui）"""
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(f"{type(self).__name__} {{ background: transparent; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_header(root)

        # 分隔线
        self._sep = QFrame(self)
        self._sep.setFrameShape(QFrame.HLine)
        self._sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(self._sep)

        # 滚动内容区
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

        # 空状态
        self._empty_lb = QLabel("暂无数据", self)
        self._empty_lb.setAlignment(Qt.AlignCenter)
        self._empty_lb.setWordWrap(True)
        self._empty_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; background: transparent; padding: 40px;"
        )
        self._empty_lb.setVisible(False)
        root.addWidget(self._empty_lb, 1)  # stretch=1 填满剩余空间

        self._customize_ui()

    def _build_header(self, root: QVBoxLayout):
        """构建标题栏（子类可重写替换图标/按钮）"""
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        # 图标
        self._header_icon = IconWidget(FluentIcon.BOOK, header)
        self._header_icon.setFixedSize(22, 22)
        hly.addWidget(self._header_icon)

        # 标题
        self._header_title = StrongBodyLabel("卡片标题", header)
        self._header_title.setStyleSheet(
            f"color: {_text_color()}; background: transparent;"
        )
        hly.addWidget(self._header_title)

        # 状态标签
        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
        )
        hly.addWidget(self._status_lb)

        hly.addStretch(1)

        # 刷新按钮
        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新")
        self._refresh_btn.clicked.connect(self._async_refresh)
        hly.addWidget(self._refresh_btn)

        # 关闭按钮
        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

    def _customize_ui(self):
        """子类重写：在内容区添加自定义控件"""
        pass

    # ── 比例高度 ────────────────────────────────────────

    def sizeHint(self):
        """与 SystemCardFrame 一致：返回窗口高度的 85%"""
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

    def _async_refresh(self):
        """异步加载数据（子类重写 _load_data）"""
        self._cleanup_worker()
        self._refresh_btn.setEnabled(False)
        self._set_loading(True)

        self._worker = _Worker(self._load_data)
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_data_loaded)
        self._worker.error.connect(self._on_load_error)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.error.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _load_data(self) -> Any:
        """子类重写：返回需要渲染的数据"""
        return []

    def _on_data_loaded(self, result: Any):
        """数据加载完成（子类重写 _render_data）"""
        self._refresh_btn.setEnabled(True)
        self._set_loading(False)
        self._cleanup_worker()
        self._render_data(result)

    def _on_load_error(self, err: str):
        """加载失败"""
        logger.error(f"[{type(self).__name__}] 加载失败: {err}")
        self._refresh_btn.setEnabled(True)
        self._set_loading(False)
        self._cleanup_worker()

    def _render_data(self, data: Any):
        """子类重写：将数据渲染到 self._content_layout"""
        pass

    def _set_loading(self, loading: bool):
        """设置加载状态"""
        self._status_lb.setText("读取中…" if loading else "")

    def _cleanup_worker(self):
        """清理工作线程"""
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    # ── 生命周期 ────────────────────────────────────────

    def _on_close(self):
        """关闭卡片"""
        self.closed.emit()

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()
