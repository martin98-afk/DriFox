# -*- coding: utf-8 -*-
"""
轻量级 hover tooltip — 自绘主题色气泡，定位在目标正上方。

完全绕过 Qt 原生 QToolTip / qfluentwidgets ToolTip 的样式表/调色板体系，
颜色由 DriFox 主题 YAML 直接控制，亮/暗主题下表现一致。

系统集成：
  - 模块加载时自动 monkey-patch QWidget.setToolTip，任意 widget.setToolTip("xxx")
    调用都会自动安装自绘 hover tooltip，无需额外步骤。
  - 直接调用 QToolTip.showText() 的代码不受影响（如图表插件），仍显示原生 tooltip。
  - 如需要为尚无 toolTip 的 widget 安装，可显式调用 install_hover_tooltip()。

用法：
    from app.widgets.simple_hover_tooltip import install_hover_tooltip

    btn = TransparentToolButton(...)
    install_hover_tooltip(btn, "发送消息")

    # 主题切换时：
    from app.widgets.simple_hover_tooltip import refresh_all_tooltips
    refresh_all_tooltips()
"""

from typing import Optional

from PyQt5.QtCore import QObject, QPoint, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PyQt5.QtWidgets import QApplication, QWidget

# ── 泄漏修复（6a）：_filters 缓存改弱值字典 ──
# filter 实例由 widget.installEventFilter 以 parent 链持有（widget 销毁即释放），
# 模块级 dict 强引用会让 filter 在 widget 销毁后仍残留 → 窗口对象树无法回收。
# WeakValueDictionary：值（filter）被回收时条目自动消失，id 复用自然重新安装。
import weakref


# ── 自动拦截所有 QWidget.setToolTip，统一接入自绘 tooltip ──
# 只要某处调用了 setToolTip("xxx")，自动为该 widget 安装 _HoverTooltipFilter。
# 使自定义 tooltip 覆盖所有系统内部 widget，无需逐个改调用处。

_original_setToolTip = QWidget.setToolTip
_patching_setToolTip: bool = False
_SKIP_TOOLTIP_TYPES = None


def _get_skip_tooltip_types():
    """懒加载跳过类型，避免模块加载时 QWidget 子类还未准备就绪。"""
    global _SKIP_TOOLTIP_TYPES
    if _SKIP_TOOLTIP_TYPES is None:
        from PyQt5.QtWidgets import QAbstractScrollArea, QLineEdit, QTextEdit

        _SKIP_TOOLTIP_TYPES = (QAbstractScrollArea, QLineEdit, QTextEdit)
    return _SKIP_TOOLTIP_TYPES


def _patched_setToolTip(self, text):
    """Monkey-patch: setToolTip 时自动安装自定义 hover tooltip。"""
    global _patching_setToolTip
    _original_setToolTip(self, text)
    if _patching_setToolTip:
        return
    if text and id(self) not in _filters:
        if isinstance(self, _get_skip_tooltip_types()):
            return
        _patching_setToolTip = True
        try:
            install_hover_tooltip(self)
        finally:
            _patching_setToolTip = False


QWidget.setToolTip = _patched_setToolTip


# ── helpers ─────────────────────────────────────────────


def _hex_to_qcolor(value: str, fallback: str = "#212126") -> QColor:
    """将颜色字符串 (#rrggbb / #aarrggbb / rgba() / rgb()) 解析为 QColor。"""
    import re

    s = str(value or "").strip()
    try:
        if s.startswith("#"):
            return QColor(s)
        # rgba(255, 255, 255, 252) / rgb(22, 30, 45)
        m = re.match(r"rgba?\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", s)
        if m:
            r, g, b = int(float(m.group(1))), int(float(m.group(2))), int(float(m.group(3)))
            a_raw = m.group(4)
            if a_raw is not None:
                av = float(a_raw)
                a = int(round(av * 255)) if av <= 1 else int(round(av))
            else:
                a = 255
            return QColor(
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
                max(0, min(255, a)),
            )
        return QColor(s)
    except Exception:
        return QColor(fallback)


def _get_tooltip_theme() -> dict:
    """从当前主题读取 tooltip 颜色。"""
    try:
        from app.utils.design_tokens import current_theme

        theme = current_theme()
    except Exception:
        theme = {}
    return {
        "bg": theme.get("card_bg_solid", "rgba(30, 30, 32, 250)"),
        "tc": theme.get("text_primary", "#ffffff"),
        "border": theme.get("border", "#3d3d3d"),
    }


def _get_tooltip_font() -> QFont:
    """获取 tooltip 字体（跟随用户设置）。"""
    try:
        from app.utils.design_tokens import _get_global_font, scale_font_size

        family = _get_global_font()
        size = scale_font_size(11)
    except Exception:
        family = "Microsoft YaHei"
        size = 13
    f = QFont(family, size)
    return f


# ── 全局注册表（用于主题切换时刷新） ───────────────────

_tooltip_instances: list = []  # weak refs would be better, but simple list for now


def refresh_all_tooltips():
    """主题/字体切换时刷新所有已注册 tooltip 的样式。"""
    for tt in _tooltip_instances:
        try:
            tt._refresh_theme()
        except Exception:
            pass


# ── Tooltip 控件 ────────────────────────────────────────


class SimpleHoverTooltip(QWidget):
    """轻量级悬浮 tooltip 气泡。

    - 自绘圆角实底 + 边框
    - 主题色由 current_theme() 驱动
    - 显示在目标 widget 正上方（水平居中）
    """

    _gap: int = 4  # 与目标控件的间距

    def __init__(self, parent=None, transient=False):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._text: str = ""
        self._bg: QColor = QColor(33, 33, 38, 250)
        self._tc: QColor = QColor("#ffffff")
        self._border: QColor = QColor("#3d3d3d")
        self._font: QFont = _get_tooltip_font()
        self._padding_h: int = 8
        self._padding_v: int = 4
        self._border_radius: int = 6

        self._refresh_theme()
        if not transient:
            _tooltip_instances.append(self)

    def _refresh_theme(self):
        """从当前主题刷新颜色。"""
        t = _get_tooltip_theme()
        self._bg = _hex_to_qcolor(t["bg"])
        self._tc = _hex_to_qcolor(t["tc"])
        self._border = _hex_to_qcolor(t["border"])
        self._font = _get_tooltip_font()
        if self._text:
            self._recalc_size()

    def set_text(self, text: str):
        """设置显示文本并重算尺寸。"""
        self._text = text
        self._recalc_size()
        self.update()

    def _recalc_size(self):
        """根据文本（支持多行 \\n） + padding 计算 widget 尺寸。"""
        fm = QFontMetrics(self._font)
        lines = self._text.split("\n") if self._text else [""]
        max_w = max((fm.width(line) for line in lines), default=0)
        line_h = fm.lineSpacing()  # 含行间距，多行不挤
        w = max_w + self._padding_h * 2
        h = line_h * len(lines) + self._padding_v * 2
        self.setFixedSize(max(w, 20), max(h, 20))

    def show_above(self, target: QWidget):
        """将 tooltip 定位到 target 正上方并显示。"""
        if not self._text:
            return
        # 定位：水平居中于 target，垂直在 target 上方
        target_global = target.mapToGlobal(QPoint(0, 0))
        cx = target_global.x() + target.width() // 2
        tx = cx - self.width() // 2
        ty = target_global.y() - self.height() - self._gap

        # 屏幕边界约束
        screen = self.screen() if self.screen() else QApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            if tx < sg.left():
                tx = sg.left() + 2
            if tx + self.width() > sg.right():
                tx = sg.right() - self.width() - 2
            if ty < sg.top():
                ty = target_global.y() + target.height() + self._gap  # 放下面

        # 预创建原生窗口句柄，再 move，避免 hide→show 时闪一帧
        self.winId()
        self.move(tx, ty)
        self.show()

    def hide_tip(self):
        """隐藏 tooltip。"""
        try:
            self.hide()
        except RuntimeError:
            pass

    # ── 自绘 ─────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        r = self._border_radius

        # 背景
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)
        painter.setBrush(self._bg)
        painter.drawPath(path)

        # 边框
        painter.setPen(self._border)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, r, r)

        # 文字（逐行绘制，行间距与 _recalc_size 一致）
        if self._text:
            painter.setPen(self._tc)
            painter.setFont(self._font)
            lines = self._text.split("\n")
            fm = QFontMetrics(self._font)
            line_h = fm.lineSpacing()
            y = self._padding_v + fm.ascent()
            for line in lines:
                painter.drawText(self._padding_h, y, line)
                y += line_h


# ── Hover 事件过滤器 ────────────────────────────────────


class _HoverTooltipFilter(QObject):
    """安装在目标 widget 上的事件过滤器：hover 延迟后显示 tooltip。"""

    def __init__(self, parent: QWidget, text: str, delay_ms: int = 400):
        super().__init__(parent)
        self._parent = parent
        self._text = text
        self._tooltip: Optional[SimpleHoverTooltip] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._on_timeout)
        parent.installEventFilter(self)
        # 目标销毁时自动清理 tooltip
        parent.destroyed.connect(self._cleanup)

    def _get_tooltip(self) -> SimpleHoverTooltip:
        if self._tooltip is None:
            self._tooltip = SimpleHoverTooltip()
        return self._tooltip

    def eventFilter(self, obj, event):
        if obj is not self._parent:
            return False
        t = event.type()
        if t == event.ToolTip:
            return True  # 拦截原生
        elif t in (event.Enter, event.HoverEnter):
            tip = self._parent.toolTip() or ""
            if tip:
                self._text = tip
                self._timer.start()
        elif t in (event.Leave, event.HoverLeave, event.Hide):
            self._timer.stop()
            self._hide()
        return False

    def _on_timeout(self):
        tt = self._get_tooltip()
        tt.set_text(self._text)
        tt.show_above(self._parent)

    def _hide(self):
        if self._tooltip:
            try:
                self._tooltip.hide_tip()
            except RuntimeError:
                pass

    def _cleanup(self):
        # 泄漏修复（6a）：目标 widget 销毁（parent.destroyed）时立即移除
        # _filters 缓存条目，不等弱值兜底回收。WeakValueDictionary 弱值
        # 语义保证 _filters 不强引用 filter；此处显式 pop 让条目即时消失，
        # 避免 filter wrapper 被 PyQt 信号连接/event filter 框架层短暂持有时
        # 条目仍残留（id 复用会误判"已安装"）。
        try:
            _filters.pop(id(self._parent), None)
        except Exception:
            pass
        try:
            self._timer.stop()
        except (RuntimeError, AttributeError):
            pass
        self._hide()

    def refresh_theme(self):
        if self._tooltip:
            self._tooltip._refresh_theme()


# 缓存：同一 widget 不要重复安装
# 弱值字典（泄漏修复 6a）：值即 _HoverTooltipFilter 实例，由 widget 的
# installEventFilter 以 parent 链持有；widget 销毁后 filter 被释放、条目
# 自动消失，避免模块级强引用造成窗口对象树残留。
_filters: "weakref.WeakValueDictionary[int, QObject]" = weakref.WeakValueDictionary()


def install_hover_tooltip(widget: QWidget, text: str = "", delay_ms: int = 400):
    """为目标 widget 安装 hover tooltip。

    Args:
        widget: 目标控件
        text: tooltip 文本。留空则读取 widget.toolTip()
        delay_ms: 悬停延迟（毫秒），默认 400
    """
    # 避免重复安装
    if id(widget) in _filters:
        return
    if text:
        widget.setToolTip(text)
        # setToolTip 可能触发 _patched_setToolTip 已安装 filter，此时直接返回
        if id(widget) in _filters:
            return
    f = _HoverTooltipFilter(widget, widget.toolTip() or "", delay_ms)
    _filters[id(widget)] = f
    return f


def batch_install_hover_tooltips(container: QWidget, delay_ms: int = 400):
    """为容器内所有子控件安装 hover tooltip（含尚无 toolTip 的，后续动态赋值也会生效）。

    会递归遍历所有子控件，跳过已知不需要 tooltip 的控件类型。

    Args:
        container: 父容器
        delay_ms: 悬停延迟
    """
    from PyQt5.QtWidgets import QAbstractScrollArea, QLineEdit, QTextEdit

    skipped_types = (QAbstractScrollArea, QLineEdit, QTextEdit)

    for child in container.findChildren(QWidget):
        if id(child) in _filters:
            continue
        if isinstance(child, skipped_types):
            continue
        install_hover_tooltip(child, delay_ms=delay_ms)


def show_immediate_tooltip(
    widget: QWidget,
    text: str,
    pos: Optional[QPoint] = None,
    duration_ms: int = 2500,
) -> SimpleHoverTooltip:
    """在 widget 附近立即显示自绘 tooltip（无悬停延迟），自动消失后清理。

    Args:
        widget: 关联的目标控件
        text: 显示文本
        pos: 屏幕坐标位置（None 则自动定位到 widget 正上方）
        duration_ms: 显示时长 ms，到期自动隐藏并清理（0=不自动隐藏）

    Returns:
        创建的 SimpleHoverTooltip 实例（外部可提前 hide/destroy）
    """
    tip = SimpleHoverTooltip(transient=True)
    tip.set_text(text)
    tip.winId()

    if pos is not None:
        # 定位到鼠标附近：水平居中于 pos，显示在其上方
        tx = pos.x() - tip.width() // 2
        ty = pos.y() - tip.height() - 8
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            tx = max(sg.left() + 2, min(tx, sg.right() - tip.width() - 2))
            if ty < sg.top():
                ty = pos.y() + 20
        tip.move(tx, ty)
    else:
        tip.show_above(widget)

    tip.show()

    if duration_ms > 0:

        def _cleanup():
            tip.hide()
            tip.deleteLater()

        QTimer.singleShot(duration_ms, _cleanup)

    return tip
