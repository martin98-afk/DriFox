# -*- coding: utf-8 -*-
"""工作区树 — TabPanel「树模式」的渲染容器

层次：项目 → 工作树 → 会话（历史会话行 / 已打开的对话页行）。

⚠️ 团队不在此渲染：团队框只在列表模式出现（用户明确要求「团队只支持列表」），
树模式下团队成员会话直接平铺到所属工作树下，行为与普通对话页一致。

职责边界
--------
本模块只负责「渲染 + 交互 + 折叠态」，刻意不认识 TabItem、不认识 HistoryManager、
不 import tab_panel（避免循环导入）。调用方（TabPanel）负责把数据编排成
:class:`TreeNodeSpec` 列表，需要嵌入的外部 widget（已打开的对话页 / 团队框）通过
``spec.widget`` 挂进来，由本模块统一插入布局。

约束与坑
--------
- ⚠️ **脱离布局的子 widget 不会自动隐藏**：``QLayout.takeAt()`` 只是把布局项摘掉，
  widget 仍保留原几何并继续绘制，会形成「残影」浮在原地。折叠父节点后子节点不再
  入布局，必须逐个 ``hide()``——rebuild() 里的 _orphan 处理就是干这个的。
- 侧栏宽度可能只有 ~250px，所有文本走 ``_ElidedLabel``（中间省略 + tooltip）。
- 主题 YAML 里大量 rgba() 字符串，``QColor("rgba(...)")`` 解析失败会静默变黑，
  统一走 :func:`_parse_rgba`。
- 子节点只在父节点**展开时**才构建（懒加载），避免一次性创建上百行拖慢首帧；
  行/头按 key 缓存复用，重建只增删差异部分。
- 会话行不带图标：项目根节点已经用项目 icon 表达了归属，再叠一层图标反而噪声。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PyQt5.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QPainter, QPainterPath
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import TransparentToolButton, isDarkTheme

from app.utils.design_tokens import Colors, font_size_css, scale_icon_size
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.utils import icons_light_rc as _icons_light_rc  # noqa: F401
from app.widgets.elided_label import _ElidedLabel

# 节点种类
KIND_PROJECT = "project"  # 项目头
KIND_WORKTREE = "worktree"  # 工作树头
KIND_TEAM = "team"  # 团队头
KIND_SESSION = "session"  # 历史会话行
KIND_WIDGET = "widget"  # 外部 widget（已打开的对话页 / 团队框）

_HEADER_KINDS = (KIND_PROJECT, KIND_WORKTREE, KIND_TEAM)

_ROW_HEIGHT = 28
_HEADER_HEIGHT = 28

# hover 背景（模块级常量，避免 paintEvent 里反复构造）
_HOVER_DARK = QColor(99, 102, 241, 32)
_HOVER_LIGHT = QColor(120, 130, 160, 30)


def _parse_rgba(rgba_str: str) -> QColor:
    """解析 'rgba(r,g,b,a)' / '#rrggbb' / 颜色名 → QColor。

    Qt 的 QColor 构造函数不认 CSS 的 rgba() 语法，会静默返回黑色；主题色里
    大量使用 rgba() 字符串，必须自己拆。alpha 语义混用（0-255 与 0.0-1.0），
    按是否含小数点判定。
    """
    try:
        text = (rgba_str or "").strip()
        if text.startswith("rgba(") or text.startswith("rgb("):
            parts = text.strip("rgba() ").split(",")
            r, g, b = int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
            if len(parts) > 3:
                a_raw = parts[3].strip()
                a = int(float(a_raw) * 255) if float(a_raw) <= 1 else int(float(a_raw))
            else:
                a = 255
            return QColor(r, g, b, a)
    except Exception:
        pass
    return QColor(rgba_str)


def _short_time(value: str) -> str:
    """'2026-08-30 19:06:52' → '08-30 19:06'；异常输入原样返回"""
    text = (value or "").strip()
    if len(text) >= 16:
        return text[5:16]
    return text


@dataclass
class TreeNodeSpec:
    """树的一个节点描述（由调用方编排，本模块只做渲染）"""

    key: str  # 稳定唯一 key（用于 widget 缓存与折叠态持久化）
    kind: str  # KIND_*
    title: str
    indent: int = 0  # 缩进像素（父级决定）
    icon: str = ""  # get_icon() 图标名（外部 widget 节点忽略）
    count: int = 0  # 右侧计数徽标（0 = 不显示）
    active_count: int = 0  # 该节点下「已激活」的对话页数（>0 显示胶囊）
    project: str = ""  # 归属项目（新建会话用）
    worktree: str = ""  # 归属工作树路径（"" = 主仓库）
    tooltip: str = ""
    expanded_by_default: bool = False
    bold: bool = False  # 标题加粗（项目头用）
    record: Optional[dict] = None  # 会话记录（KIND_SESSION）
    widget: Optional[QWidget] = field(default=None, repr=False)  # KIND_WIDGET 的外部 widget
    # ── 项目头像（与 TabItem 的项目 icon 同一套视觉）：非空时优先于 icon 渲染 ──
    initials: str = ""  # 缩写（1-2 字符）
    color: str = ""  # 色块颜色（rgba 字符串）
    # ── 头行右侧快捷按钮：((图标名, tooltip, 无参回调), ...) ──
    # 用途：项目根行的「管理工作树」等一键入口。hover 才显，与「新建」按钮一致。
    actions: tuple = ()

    @property
    def avatar_sig(self) -> tuple:
        """头像签名（用于判断是否需要重画色块）"""
        return (self.initials, self.color)

    @property
    def actions_sig(self) -> tuple:
        """快捷按钮签名：只比 (图标, tooltip)，回调允许换对象而不重建按钮"""
        return tuple((str(a[0]), str(a[1])) for a in (self.actions or ()))


class _ProjectBadge(QWidget):
    """项目头像：圆角色块 + 白色缩写

    与 TabItem 的项目 icon（``_TabProjectIcon``）保持同一套视觉，只是尺寸更小。
    直接 QPainter 绘制，Qt 自动处理 devicePixelRatio，无需中间 QPixmap。
    """

    def __init__(self, parent=None, size: int = 14):
        super().__init__(parent)
        self._size = size
        self._initials = ""
        self._color = QColor(128, 128, 128)
        self.setFixedSize(size, size)
        self.setVisible(False)

    def set_project(self, initials: str, color_rgba: str):
        self._initials = (initials or "").strip() or "?"
        self._color = _parse_rgba(color_rgba)
        self.update()

    def set_px(self, px: int):
        if px == self._size:
            return
        self._size = px
        self.setFixedSize(px, px)
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt 约定
        if not self._initials:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(QRectF(self.rect()), 4, 4)
        painter.setPen(Qt.white)
        font = get_unified_font()
        font.setPixelSize(max(7, int(self._size * 0.58)))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, self._initials)


class _TreeArrow(QWidget):
    """折叠指示箭头：0° 指向右（折叠），90° 指向下（展开），140ms 缓动旋转"""

    def __init__(self, parent=None, size: int = 12):
        super().__init__(parent)
        self._angle = 90.0
        self.setFixedSize(size, size)
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)

    def get_angle(self) -> float:
        return self._angle

    def set_angle(self, value: float):
        self._angle = float(value)
        self.update()

    angle = pyqtProperty(float, get_angle, set_angle)

    def set_expanded(self, expanded: bool, animate: bool = True):
        """收敛式设置：动画进行中绝不重启，否则缓动曲线被反复重置、进度到不了终点"""
        target = 90.0 if expanded else 0.0
        if self._anim.state() == QPropertyAnimation.Running:
            return
        if abs(self._angle - target) < 0.5:
            return
        if not animate:
            self._anim.stop()
            self._angle = target
            self.update()
            return
        self._anim.setStartValue(self._angle)
        self._anim.setEndValue(target)
        self._anim.start()

    def paintEvent(self, event):  # noqa: N802 - Qt 约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(_parse_rgba(Colors.TEXT_MUTED))
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(self._angle)
        s = self.width() * 0.30
        path = QPainterPath()
        path.moveTo(-s * 0.45, -s * 0.75)
        path.lineTo(s * 0.55, 0.0)
        path.lineTo(-s * 0.45, s * 0.75)
        path.closeSubpath()
        painter.drawPath(path)


class _NodeHeader(QFrame):
    """项目 / 工作树 / 团队 的折叠头

    组成：箭头 + 图标 + 标题（省略）+ 计数 + 「新建」按钮（hover 显示）。
    """

    toggled = pyqtSignal(str)  # key
    newRequested = pyqtSignal(str, str)  # project, worktree_path

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key = ""
        self._hovered = False
        self._expanded = True
        self._indent = 0
        self._compact = False
        self._icon_name = ""
        self._project = ""
        self._worktree = ""
        self._spec_bold = False
        self._active_count = 0
        self._icon_px = scale_icon_size(14)
        # ── 项目头像色块（initials 非空时顶替 _icon_label）──
        self._badge = _ProjectBadge(self, size=self._icon_px)
        self._initials = ""
        self._color = ""
        # ── 头行右侧快捷按钮（项目根的「管理工作树」等入口）──
        self._actions: tuple = ()
        self._actions_sig: tuple = ()
        self._action_btns: List[QWidget] = []

        self.setFixedHeight(_HEADER_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 4, 0)
        layout.setSpacing(4)

        self._arrow = _TreeArrow(self, size=12)
        layout.addWidget(self._arrow)

        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(self._icon_px, self._icon_px)
        self._icon_label.setStyleSheet("background: transparent;")
        layout.addWidget(self._icon_label)
        layout.addWidget(self._badge)

        self._title = _ElidedLabel("", self)
        self._title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self._title, 1)

        # 「已激活 N」胶囊：折叠时也能看到还有几个会话占着后端
        self._active_label = QLabel("", self)
        self._active_label.setStyleSheet("background: transparent;")
        self._active_label.setToolTip("已激活的对话页（每个占用一个后端）")
        self._active_label.setVisible(False)
        layout.addWidget(self._active_label)

        self._count_label = QLabel("", self)
        self._count_label.setStyleSheet("background: transparent;")
        layout.addWidget(self._count_label)

        self._new_btn = TransparentToolButton(self)
        self._new_btn.setIcon(FIF.ADD)
        self._new_btn.setFixedSize(20, 20)
        self._new_btn.setIconSize(self._icon_label.size())
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setToolTip("在此新建对话页")
        self._new_btn.setVisible(False)
        self._new_btn.setAttribute(Qt.WA_NoMousePropagation, True)
        self._new_btn.clicked.connect(lambda *_a: self.newRequested.emit(self._project, self._worktree))
        layout.addWidget(self._new_btn)

        self._apply_appearance()

    # ── 数据绑定 ──────────────────────────────────────────────────
    def apply_spec(self, spec: "TreeNodeSpec"):
        self._key = spec.key
        self._project = spec.project
        self._worktree = spec.worktree
        if spec.indent != self._indent:
            self._indent = spec.indent
            self.layout().setContentsMargins(6 + spec.indent, 0, 4, 0)
        if self._title.text() != spec.title:
            self._title.setText(spec.title)
        tip = spec.tooltip or spec.title
        self.setToolTip(tip)
        self._title.setToolTip(tip)
        count_text = str(spec.count) if spec.count > 0 else ""
        if self._count_label.text() != count_text:
            self._count_label.setText(count_text)
        self._active_count = int(spec.active_count or 0)
        active_text = f"{self._active_count} 激活" if self._active_count > 0 else ""
        if self._active_label.text() != active_text:
            self._active_label.setText(active_text)
        self._sync_badges()
        if spec.icon != self._icon_name:
            self._icon_name = spec.icon
        self._new_btn.setToolTip(f"在「{spec.title}」下新建对话页")
        self._spec_bold = spec.bold
        self._apply_avatar(spec)
        self._apply_actions(spec)
        self._apply_appearance()

    def _apply_avatar(self, spec: "TreeNodeSpec"):
        """项目头像：initials 非空时用它画色块并顶掉 get_icon 图标"""
        if spec.avatar_sig == (self._initials, self._color):
            return
        self._initials, self._color = spec.avatar_sig
        if self._initials:
            self._badge.set_px(self._icon_px)
            self._badge.set_project(self._initials, self._color)

    def _apply_actions(self, spec: "TreeNodeSpec"):
        """同步头行右侧的快捷按钮

        签名只比 (图标, tooltip)：回调每次重建都可能换对象（lambda 闭包捕获新的
        project），若把回调也算进签名会每次重建都销毁重建按钮 —— 既浪费又会在
        hover 时闪。这里只更新闭包引用。
        """
        sig = spec.actions_sig
        self._actions = tuple(spec.actions or ())
        if sig != self._actions_sig:
            self._actions_sig = sig
            for btn in self._action_btns:
                self.layout().removeWidget(btn)
                btn.hide()
                btn.deleteLater()
            self._action_btns = []
            lay = self.layout()
            for name, tip, cb in self._actions:
                btn = TransparentToolButton(self)
                btn.setIcon(get_icon(name))
                btn.setFixedSize(20, 20)
                btn.setIconSize(self._icon_label.size())
                btn.setCursor(Qt.PointingHandCursor)
                btn.setToolTip(tip)
                btn.setVisible(False)
                # ⚠️ 不设这个，点击会同时冒泡到头行的 mousePressEvent → 折叠/展开
                btn.setAttribute(Qt.WA_NoMousePropagation, True)
                btn.clicked.connect(cb)
                lay.insertWidget(lay.indexOf(self._new_btn), btn)
                self._action_btns.append(btn)
        else:
            # 签名未变：只换掉回调（按钮对象复用，避免 hover 闪烁）
            for btn, (_n, _t, cb) in zip(self._action_btns, self._actions):
                try:
                    btn.clicked.disconnect()
                except Exception:
                    pass
                btn.clicked.connect(cb)

    def _sync_badges(self):
        """计数徽标 / 已激活胶囊的可见性：有文本 且 非紧凑态才显示

        只setText不setVisible 会让胶囊永远不出现；而 set_compact(False) 无条件
        setVisible(True) 又会在文本为空时渲染出空药丸。统一收敛到这里。
        """
        shown = not self._compact
        self._count_label.setVisible(bool(self._count_label.text()) and shown)
        self._active_label.setVisible(bool(self._active_label.text()) and shown)

    # ── 状态 ─────────────────────────────────────────────────────
    def set_expanded(self, expanded: bool, animate: bool = True):
        self._expanded = expanded
        self._arrow.set_expanded(expanded, animate=animate)

    def set_compact(self, compact: bool):
        """紧凑态：只留图标（侧栏窄条），隐藏文字与按钮"""
        if self._compact == compact:
            return
        self._compact = compact
        self._title.setVisible(not compact)
        self._sync_badges()
        self._arrow.setVisible(not compact)
        if compact:
            self._new_btn.setVisible(False)
            for btn in self._action_btns:
                btn.setVisible(False)
        self._apply_appearance()

    # ── 样式 ─────────────────────────────────────────────────────
    def _apply_appearance(self):
        Colors.refresh()
        self.setStyleSheet("QFrame { background: transparent; border: none; }")
        self._title.setFont(get_unified_font(12))
        self._title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)};"
            + (" font-weight: bold;" if self._spec_bold else "")
        )
        self._active_label.setFont(get_unified_font(10))
        self._active_label.setStyleSheet(
            f"color: white; background: {Colors.INFO}; border-radius: 7px; "
            f"padding: 0px 6px; {get_font_family_css()} {font_size_css(10)};"
        )
        self._count_label.setFont(get_unified_font(10))
        self._count_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: {Colors.HOVER_BG}; "
            f"border-radius: 7px; padding: 0px 6px; "
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        px = scale_icon_size(14)
        if px != self._icon_px:
            self._icon_px = px
            self._icon_label.setFixedSize(px, px)
            self._badge.set_px(px)
        # 项目头像优先：有色块就不画通用图标
        if self._initials:
            self._badge.setVisible(True)
            self._icon_label.setVisible(False)
        elif self._icon_name:
            self._icon_label.setPixmap(get_icon(self._icon_name).pixmap(self._icon_label.size()))
            self._icon_label.setVisible(True)
            self._badge.setVisible(False)
        else:
            self._icon_label.setVisible(False)
            self._badge.setVisible(False)
        self._new_btn.setIconSize(self._icon_label.size())
        for btn in self._action_btns:
            btn.setIconSize(self._icon_label.size())

    def refresh_style(self):
        self._apply_appearance()

    # ── 交互 ─────────────────────────────────────────────────────
    def enterEvent(self, event):  # noqa: N802 - Qt 约定
        self._hovered = True
        if not self._compact:
            self._new_btn.setVisible(True)
            for btn in self._action_btns:
                btn.setVisible(True)
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt 约定
        self._hovered = False
        self._new_btn.setVisible(False)
        for btn in self._action_btns:
            btn.setVisible(False)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 - Qt 约定
        if event.button() == Qt.LeftButton:
            self.toggled.emit(self._key)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):  # noqa: N802 - Qt 约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self._hovered:
            path = QPainterPath()
            path.addRoundedRect(QRectF(2, 2, self.width() - 4, self.height() - 4), 6, 6)
            painter.fillPath(path, _HOVER_DARK if isDarkTheme() else _HOVER_LIGHT)
        super().paintEvent(event)


class _SessionRow(QFrame):
    """历史会话行：标题 + 时间；点击请求打开

    不画图标：项目根节点已经用项目 icon 表达了归属，会话行再叠一层图标纯噪声
    （用户明确要求）。层级只靠缩进 + 字号/颜色区分。
    """

    openRequested = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hovered = False
        self._indent = 0
        self._record: dict = {}
        self._compact = False

        self.setFixedHeight(_ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)

        self._title = _ElidedLabel("", self)
        self._title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self._title, 1)

        self._meta = QLabel("", self)
        self._meta.setStyleSheet("background: transparent;")
        layout.addWidget(self._meta)

        self._apply_appearance()

    def apply_spec(self, spec: "TreeNodeSpec"):
        record = spec.record or {}
        self._record = record
        title = spec.title or "未命名会话"
        if self._title.text() != title:
            self._title.setText(title)
        tip = spec.tooltip or title
        self.setToolTip(tip)
        self._title.setToolTip(tip)
        meta = _short_time(str(record.get("last_time", "")))
        if self._meta.text() != meta:
            self._meta.setText(meta)
        if spec.indent != self._indent:
            self._indent = spec.indent
            self.layout().setContentsMargins(6 + spec.indent, 0, 6, 0)
        self._apply_appearance()

    def set_compact(self, compact: bool):
        if self._compact == compact:
            return
        self._compact = compact
        self._title.setVisible(not compact)
        self._meta.setVisible(not compact)

    def _apply_appearance(self):
        Colors.refresh()
        self.setStyleSheet("QFrame { background: transparent; border: none; }")
        self._title.setFont(get_unified_font(12))
        self._title.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)};"
        )
        self._meta.setFont(get_unified_font(10))
        self._meta.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)};"
        )

    def refresh_style(self):
        self._apply_appearance()

    def enterEvent(self, event):  # noqa: N802 - Qt 约定
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt 约定
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 - Qt 约定
        if event.button() == Qt.LeftButton and self._record:
            self.openRequested.emit(self._record)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):  # noqa: N802 - Qt 约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self._hovered:
            path = QPainterPath()
            path.addRoundedRect(QRectF(2, 2, self.width() - 4, self.height() - 4), 6, 6)
            painter.fillPath(path, _HOVER_DARK if isDarkTheme() else _HOVER_LIGHT)
        super().paintEvent(event)


class WorkspaceTree(QWidget):
    """工作区树容器

    调用方用 :meth:`rebuild` 传入节点描述列表；本模块负责渲染、折叠、主题刷新。

    信号：
        newSessionRequested(project, worktree_path) —— 节点头「+」点击
        openSessionRequested(record) —— 历史会话行点击
        expansionChanged(state) —— 折叠态变化（外部持久化）
    """

    newSessionRequested = pyqtSignal(str, str)
    openSessionRequested = pyqtSignal(dict)
    expansionChanged = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(2, 0, 2, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch()

        self._headers: Dict[str, _NodeHeader] = {}
        self._rows: Dict[str, _SessionRow] = {}
        self._expanded: Dict[str, bool] = {}
        self._compact = False
        self._specs: List[TreeNodeSpec] = []

    # ── 折叠态持久化 ──────────────────────────────────────────────
    def set_expansion_state(self, state: dict):
        if isinstance(state, dict):
            self._expanded = {str(k): bool(v) for k, v in state.items()}

    def expansion_state(self) -> dict:
        return dict(self._expanded)

    def set_compact(self, compact: bool):
        if self._compact == compact:
            return
        self._compact = compact
        for h in self._headers.values():
            h.set_compact(compact)
        for r in self._rows.values():
            r.set_compact(compact)
            # 紧凑态（46px 窄条）放不下会话行
            r.setVisible(not compact)

    # ── 构建 ─────────────────────────────────────────────────────
    def _layout_widgets(self) -> List[QWidget]:
        out: List[QWidget] = []
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            w = item.widget() if item is not None else None
            if w is not None and w not in out:
                out.append(w)
        return out

    def rebuild(self, specs: List[TreeNodeSpec]):
        """按节点描述重建树。

        节点必须按「父在前、子在后且缩进递增」的顺序给出；遇到折叠的父节点，
        其后的所有更深层节点会被整体跳过（懒构建，不创建 widget）。

        ⚠️ 被跳过的 widget（尤其是外部挂进来的已打开对话页）此前已经入过布局，
        takeAt 只是摘掉布局项、不会隐藏它 —— 必须显式 hide，否则会以旧几何
        继续绘制，形成「残影」。
        """
        self._specs = list(specs or [])
        previous = self._layout_widgets()

        # 取出末尾 stretch（不假设它恒在最末，扫描找到第一个 spacer）
        stretch_item = None
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            if item is not None and item.widget() is None:
                stretch_item = self._layout.takeAt(i)
                break
        # 清空布局（widget 不销毁，仅脱绑；key 缓存保证复用）
        while self._layout.count() > 0:
            self._layout.takeAt(0)

        used_headers: set = set()
        used_rows: set = set()

        skip_above_indent = None  # 非 None 时：跳过所有缩进 > 该值的节点
        for spec in self._specs:
            if skip_above_indent is not None:
                if spec.indent <= skip_above_indent:
                    skip_above_indent = None
                else:
                    continue

            if spec.kind in _HEADER_KINDS:
                header = self._headers.get(spec.key)
                if header is None:
                    header = _NodeHeader(self)
                    header.toggled.connect(self._on_header_toggled)
                    header.newRequested.connect(self.newSessionRequested)
                    self._headers[spec.key] = header
                header.apply_spec(spec)
                header.set_compact(self._compact)
                expanded = self._expanded.get(spec.key, spec.expanded_by_default)
                header.set_expanded(expanded)
                if not expanded:
                    skip_above_indent = spec.indent
                self._layout.addWidget(header)
                header.setVisible(True)
                used_headers.add(spec.key)
            elif spec.kind == KIND_SESSION:
                if self._compact:
                    continue  # 紧凑态不渲染会话行
                row = self._rows.get(spec.key)
                if row is None:
                    row = _SessionRow(self)
                    row.openRequested.connect(self.openSessionRequested)
                    self._rows[spec.key] = row
                row.apply_spec(spec)
                row.set_compact(False)
                self._layout.addWidget(row)
                row.setVisible(True)
                used_rows.add(spec.key)
            else:
                widget = spec.widget
                if widget is None:
                    continue
                if widget.parent() is not self:
                    widget.setParent(self)
                self._layout.addWidget(widget)
                widget.setVisible(True)

        # 清理不再使用的缓存 widget
        for key in [k for k in self._headers if k not in used_headers]:
            w = self._headers.pop(key)
            w.hide()
            w.deleteLater()
        for key in [k for k in self._rows if k not in used_rows]:
            w = self._rows.pop(key)
            w.hide()
            w.deleteLater()

        if stretch_item is not None:
            self._layout.addItem(stretch_item)
        else:
            self._layout.addStretch()

        # ── 残影防线：本轮没进布局的旧 widget 一律隐藏 ──
        current = {id(w) for w in self._layout_widgets()}
        for w in previous:
            if id(w) not in current:
                w.hide()

    # ── 交互 ─────────────────────────────────────────────────────
    def _on_header_toggled(self, key: str):
        # ⚠️ 必须按「当前生效状态」取反，不能用 _expanded.get(key, False)：
        # expanded_by_default=True 的节点还没写进 _expanded，取默认 False
        # 再取反会得到 True —— 首次点击展开态节点反而保持展开（点不动）。
        header = self._headers.get(key)
        current = header._expanded if header is not None else self._expanded.get(key, False)
        expanded = not current
        self._expanded[key] = expanded
        header = self._headers.get(key)
        if header is not None:
            header.set_expanded(expanded)
        self.expansionChanged.emit(self.expansion_state())
        self.rebuild(self._specs)

    # ── 外部 widget 管理 ─────────────────────────────────────────
    def detach_widget(self, widget):
        """把外部 widget 从树布局中脱绑（不销毁，所有权仍归调用方）"""
        if widget is None:
            return
        self._layout.removeWidget(widget)

    # ── 主题 ─────────────────────────────────────────────────────
    def refresh_style(self):
        for h in self._headers.values():
            h.refresh_style()
        for r in self._rows.values():
            r.refresh_style()

    def clear(self):
        """清空所有内部缓存（模式切换/重建宿主时调用）"""
        self.rebuild([])
