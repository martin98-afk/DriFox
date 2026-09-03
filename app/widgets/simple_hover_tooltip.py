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

from PyQt5.QtCore import QObject, QPoint, QRect, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath
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


def _max_tip_text_width() -> int:
    """tooltip 单行文本最大宽度：取当前主窗口宽度，拿不到则兜底屏幕宽 1/3。"""
    w = QApplication.activeWindow()
    if w is not None and w.width() >= 200:
        return w.width() - 24  # 留少量余量，避免 tooltip 贴满窗口宽
    screen = QApplication.primaryScreen()
    if screen is not None:
        return max(240, screen.availableGeometry().width() // 3)
    return 360


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
# 🛡️ 泄漏修复（B7）：防御上限——超过则先 prune 死实例再淘汰最旧实例。
# 用弱引用不可行：tooltip 是顶层 widget（无 parent），_tooltip_instances 是
# 唯一强引用持有者；弱引用会导致 tooltip 创建后立即被 GC → 崩溃/不可见。
# 因此保持强引用 List + 自注销（destroyed 信号）+ 防御上限双保险。
_MAX_TOOLTIP_INSTANCES = 256


def _prune_dead_tooltips():
    """清理注册表中已销毁（sip.isdeleted）的 tooltip 实例。"""
    from PyQt5 import sip

    alive = []
    for tt in _tooltip_instances:
        try:
            if tt is None or sip.isdeleted(tt):
                continue
            alive.append(tt)
        except RuntimeError:
            continue
    _tooltip_instances[:] = alive


def refresh_all_tooltips():
    """主题/字体切换时刷新所有已注册 tooltip 的样式。"""
    _prune_dead_tooltips()
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
        self._wrap_w: Optional[int] = None  # 非空表示文本已超限折行，值为折行宽度

        self._refresh_theme()
        if not transient:
            # 🛡️ 泄漏修复（B7）：注册前先清理死实例，防止注册表只增不减；
            # 超过防御上限时淘汰最旧实例（deleteLater），保证注册表有界。
            _prune_dead_tooltips()
            if len(_tooltip_instances) >= _MAX_TOOLTIP_INSTANCES:
                try:
                    oldest = _tooltip_instances.pop(0)
                    if oldest is not None:
                        oldest.deleteLater()
                except (RuntimeError, IndexError):
                    pass
            _tooltip_instances.append(self)
            # 自注销：destroyed 信号在 deleteLater + sendPostedEvents 后
            # 可靠触发，实例销毁时自动从全局注册表移除。
            self.destroyed.connect(lambda *_: self._unregister_from_global())

    def _unregister_from_global(self):
        """从全局注册表移除自身（destroyed 信号回调）。"""
        try:
            _tooltip_instances.remove(self)
        except ValueError:
            pass

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
        limit = _max_tip_text_width()
        if max_w > limit:
            # 超限：按限宽折行重算尺寸（绘制端用同一折行规则）
            self._wrap_w = limit
            rect = fm.boundingRect(QRect(0, 0, limit, 0), Qt.TextWordWrap, self._text)
            w = limit + self._padding_h * 2
            h = rect.height() + self._padding_v * 2
        else:
            self._wrap_w = None
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

        # 文字（逐行绘制，行间距与 _recalc_size 一致；超限折行走整体 drawText）
        if self._text:
            painter.setPen(self._tc)
            painter.setFont(self._font)
            if self._wrap_w is not None:
                # 折行分支：与 _recalc_size 同宽度同规则，交由 Qt 折行
                text_rect = QRect(self._padding_h, self._padding_v,
                                  self._wrap_w, self.height() - self._padding_v * 2)
                painter.drawText(text_rect,
                                 Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap,
                                 self._text)
                return
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
        # #33 根因修复：parent 改弱引用，断开 filter↔父 引用环（filter 是 parent 子对象，
        # 强持有 parent 会阻止父随标签关闭被回收 → per-tab 泄漏）。所有使用点需 deref + 判空。
        self._parent = weakref.ref(parent)
        self._text = text
        self._tooltip: Optional[SimpleHoverTooltip] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._on_timeout)
        # 🛡️ 残留修复（R1）：显示后启动看护轮询，兜底各类漏发的隐藏事件
        # （窗口失焦/alt-tab、列表滚动导致控件移出光标、合成器下漏发的
        #  Leave 等），最长 ~120ms 内强制收起，杜绝“飘在屏幕上不消失”。
        #  仅在该 filter 自己的 tooltip 显示时运行，开销极小。
        self._guard = QTimer(self)
        self._guard.setSingleShot(False)
        self._guard.setInterval(120)
        self._guard.timeout.connect(self._guard_check)
        parent.installEventFilter(self)
        # 目标销毁时自动清理 tooltip
        # 改用 self.destroyed（filter 自身析构时发出，连接仍有效）而非 parent.destroyed
        # （#29 根因：filter 是 parent 子对象，parent 销毁时先删子对象再 emit destroyed，
        # 连接已断开 → _cleanup 永不触发 → per-tab 泄漏）。self.destroyed 确保 _cleanup 可靠触发。
        self.destroyed.connect(self._cleanup)

    def _get_tooltip(self) -> SimpleHoverTooltip:
        if self._tooltip is None:
            self._tooltip = SimpleHoverTooltip()
        return self._tooltip

    def eventFilter(self, obj, event):
        if obj is not self._parent():
            return False
        t = event.type()
        if t == event.ToolTip:
            return True  # 拦截原生
        elif t in (event.Enter, event.HoverEnter):
            tip = self._parent().toolTip() or ""
            if tip:
                self._text = tip
                self._timer.start()
        elif t in (event.Leave, event.HoverLeave, event.Hide, event.HideToParent):
            # 🛡️ B2 修复：目标随父容器隐藏时 Qt 发 HideToParent（27）而非 Hide（18）。
            # 团队 header 按钮（关闭团队 close_btn 等）在团队关闭时随 header 容器
            # 隐藏，旧分支只捕 Hide/Leave/HoverLeave → tooltip 不隐藏 → 屏幕残留
            # "飘着的 tooltip"。补上 HideToParent 使容器隐藏即收掉 tooltip。
            self._timer.stop()
            self._hide()
        elif t in (event.MouseButtonPress, event.MouseButtonDblClick):
            # 🛡️ B3 修复：点击即收起 tooltip（与 Qt 原生 QToolTip / qfluentwidgets
            # ToolTipFilter 在 MouseButtonPress 时 hideToolTip() 的行为对齐）。
            # 此前漏捕：点击关闭团队按钮时 tooltip 仍显示，随后团队组
            # deleteLater 销毁（close_btn 的 destroyed→_cleanup 隐藏 tooltip）
            # 与用户点击之间存在延迟窗口，若窗口 close 阻塞/事件繁忙/清理
            # 竞态 → tooltip 残留在屏幕上不消失。按下即隐藏彻底消除该窗口期。
            # 注意：此处不 return True（不拦截鼠标事件），按钮点击正常响应。
            self._timer.stop()
            self._hide()
        return False

    def _on_timeout(self):
        # 🛡️ 问题B 修复：显示气泡前校验鼠标是否仍在目标控件内——
        # 按钮 visible 切换时序（团队框 hover 显示按钮）或隐藏态几何错位
        # （DPI 缩放）下，timer 可能已启动但鼠标已不在控件上（或控件已
        # 隐藏），直接显示会出现"飘着的 tooltip"。校验失败则停表放弃。
        p = self._parent()
        if p is None or not p.isVisible():
            self._timer.stop()
            return
        local = p.mapFromGlobal(QCursor.pos())
        if not p.rect().contains(local):
            self._timer.stop()
            return
        tt = self._get_tooltip()
        tt.set_text(self._text)
        tt.show_above(p)
        # 启动看护轮询（显示期间持续校验是否仍需显示）
        self._guard.start()

    def _guard_check(self):
        """显示后兜底看护：任何漏发的隐藏事件（失焦/滚动/几何变化/漏发
        Leave）都在此强制收起 tooltip，避免残留在屏幕上。"""
        tt = self._tooltip
        if tt is None or not tt.isVisible():
            self._guard.stop()
            return
        p = self._parent()
        # 目标已销毁/不可见，或应用已失焦（alt-tab 等）→ 立即收起
        if p is None or not p.isVisible() or QApplication.activeWindow() is None:
            self._hide()
            return
        # 光标已不在目标控件内（滚动/遮挡/几何变化导致）→ 收起
        local = p.mapFromGlobal(QCursor.pos())
        if not p.rect().contains(local):
            self._hide()

    def _hide(self):
        try:
            self._guard.stop()
        except (RuntimeError, AttributeError):
            pass
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
            _filters.pop(id(self._parent()), None)
        except Exception:
            pass
        try:
            self._timer.stop()
        except (RuntimeError, AttributeError):
            pass
        self._hide()
        # 🛡️ 泄漏根因修复（B7）：目标 widget 销毁时 tooltip 同步销毁。
        # 此前只 _hide() 不 deleteLater() → tooltip 永不销毁 → 全局注册表
        # _tooltip_instances 只增不减（QWidget 引用驻留）。
        if self._tooltip is not None:
            try:
                self._tooltip.deleteLater()
            except RuntimeError:
                pass
        # 方案 B 兜底：cleanup 触发即断环，避免 _parent 弱引用 / _tooltip 残留
        self._parent = None
        self._tooltip = None

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
