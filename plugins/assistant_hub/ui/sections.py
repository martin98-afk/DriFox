# -*- coding: utf-8 -*-
"""sections.py — 助手中心单列分区组件（对齐 openhanako AgentTab 分区结构）

分区：ProfileSection（名称/模型）→ AboutSection（人格切换，只读）→
MemorySection（全局/项目/日志）→ ExperienceSection（经验）。

视觉基调（对齐原版纸张风）：细边框卡 + 12px 圆角 + 小字 hint + 大量留白；
去 emoji，统一 FluentIcon / 文字标签。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, LineEdit, MaskDialogBase, SwitchButton

from app.plugins.sdk import Colors, font_size_css, scale_font_size
from app.plugins.sdk import get_font_family_css


from .assistant_avatar import RoundAvatar

# ── 基础样式辅助 ─────────────────────────────────────


def _caps(cap_id):
    """取某能力（无 ctx 场景统一走 sdk 的 caps_for，host 由宿主解析）。"""
    from app.plugins.sdk import caps_for

    return caps_for(cap_id)


def _elide_lines(text: str, fm, width: int, max_lines: int = 2) -> str:
    """按像素宽度把 text 折行到 max_lines 行，超出部分末行截断加 …。

    中文无空格需逐字累积；QLabel 原生无多行省略，chips 卡描述用。
    """
    if not text:
        return ""
    lines: List[str] = []
    cur = ""
    for ch in text:
        if fm.horizontalAdvance(cur + ch) <= width:
            cur += ch
            continue
        lines.append(cur)
        if len(lines) == max_lines:
            lines[-1] = lines[-1][:-1] + "…"
            return "\n".join(lines)
        cur = ch
    if cur:
        lines.append(cur)
    return "\n".join(lines[:max_lines])


def _hint(text: str, size: int = 10) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
        f"{get_font_family_css()} {font_size_css(size)};"
    )
    return lbl


def _title_label(text: str, size: int = 12) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
        f"{get_font_family_css()} {font_size_css(size)}; font-weight: 600;"
    )
    return lbl


def _card_frame(parent=None) -> QFrame:
    """卡片容器。⚠ 样式用 #objectName 限定：QLabel 是 QFrame 子类，
    裸 QFrame 选择器会污染所有子标签（边框泄漏）。"""
    frame = QFrame(parent)
    frame.setObjectName("hubSectionCard")
    frame.setStyleSheet(
        f"""
        QFrame#hubSectionCard {{
            background: {Colors.CARD_BG.format(alpha=140)};
            border: 1px solid {Colors.BORDER};
            border-radius: 12px;
        }}
        QFrame#hubSectionCard QLabel {{ background: transparent; border: none; }}
    """
    )
    return frame


def _input_style() -> str:
    """中性输入框：不依赖主题 INPUT_*（亮色主题下不发蓝）。"""
    return f"""
        QLineEdit {{
            background: {Colors.CARD_BG.format(alpha=90)};
            border: 1px solid {Colors.BORDER};
            color: {Colors.TEXT_PRIMARY};
            padding: 5px 10px;
            border-radius: 6px;
            {get_font_family_css()} {font_size_css(12)}
        }}
        QLineEdit:focus {{ border-color: {Colors.TEXT_ACCENT}; background: {Colors.CARD_BG.format(alpha=160)}; }}
    """


def _btn_style(danger: bool = False, align_left: bool = False, icon_only: bool = False) -> str:
    color = Colors.ERROR if danger else Colors.TEXT_PRIMARY
    align = "text-align: left; padding-left: 12px;" if align_left else ""
    pad = "padding: 0;" if icon_only else "padding: 4px 16px;"
    return f"""
        QPushButton {{
            color: {color};
            border: 1px solid {Colors.ERROR if danger else Colors.BORDER};
            border-radius: 6px;
            {pad}
            {align}
            background: transparent;
            {get_font_family_css()} {font_size_css(11)}
        }}
        QPushButton:hover {{
            background: {Colors.HOVER_BG};
        }}
        QPushButton:disabled {{ opacity: 0.4; }}
    """


def _svg_icon(svg: str, size: int) -> QIcon:
    """SVG 字符串 → QIcon（QSvgRenderer 直渲，颜色烧进 SVG）。"""
    renderer = QSvgRenderer(svg.encode("utf-8"))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    return QIcon(pm)


def _icon_btn(svg: str, size: int) -> "QPushButton":
    """纯图标按钮（列表行增删，替代字符 +/×）。"""
    btn = QPushButton()
    icon = _svg_icon(svg, size)
    btn.setIcon(icon)
    btn.setIconSize(icon.actualSize(QSize(size, size)))
    return btn


def _del_btn(size: int) -> "QPushButton":
    """列表行删除按钮：垃圾桶图标。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{Colors.ERROR}" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="3 6 5 6 21 6"/>'
        '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
        '<line x1="10" y1="11" x2="10" y2="17"/>'
        '<line x1="14" y1="11" x2="14" y2="17"/>'
        "</svg>"
    )
    return _icon_btn(svg, size)


def _add_btn(size: int) -> "QPushButton":
    """列表行添加按钮：加号图标。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{Colors.TEXT_PRIMARY}" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<line x1="12" y1="5" x2="12" y2="19"/>'
        '<line x1="5" y1="12" x2="19" y2="12"/>'
        "</svg>"
    )
    return _icon_btn(svg, size)


def _sep() -> QFrame:
    """分区内子块的细分隔线：给重排后的四个记忆子块一个轻视觉边界。"""
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {Colors.BORDER}; border: none;")
    return line


class _Section(QFrame):
    """分区卡片：标题行（+右侧 context 槽）+ 内容 VBox。

    样式限定 objectName：防止 QFrame 规则波及子 QLabel/QFrame（边框泄漏）。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("hubSectionCard")
        self.setStyleSheet(
            f"""
            QFrame#hubSectionCard {{
                background: {Colors.CARD_BG.format(alpha=140)};
                border: 1px solid {Colors.BORDER};
                border-radius: 12px;
            }}
            QFrame#hubSectionCard QLabel {{ background: transparent; border: none; }}
        """
        )
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(16, 10, 16, 12)
        self._v.setSpacing(7)
        head = QHBoxLayout()
        head.addWidget(_title_label(title))
        head.addStretch()
        self._context_slot = head  # set_context 时追加控件
        self._v.addLayout(head)

    def set_context(self, w: QWidget) -> None:
        self._context_slot.addWidget(w)

    def body(self) -> QVBoxLayout:
        return self._v

    def _on_chips_host_resized(self) -> None:
        """容器宽度变化后重算卡片流行高（子类实现）。"""
        return

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._on_chips_host_resized()


class _RowItem(QFrame):
    """整行可点的列表条目：标题 + 灰色说明 + 尾部操作。

    层级靠 `primary` 区分：primary（分组行：全局记忆 / 记忆分区 / 项目记忆）标题
    字号大一档、颜色实；子行（分区条目）标题缩进且字号小一档，视觉从属。
    `hide_tail` 让尾部操作默认隐形、悬停才出现（删除这类危险操作不该常驻抢眼）。
    """

    clicked = pyqtSignal()

    def __init__(self, title: str, desc: str = "", primary: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("hubRowItem")
        self.setCursor(Qt.PointingHandCursor)
        self._primary = bool(primary)
        v = QHBoxLayout(self)
        v.setContentsMargins(8 if primary else 24, 5, 8, 5)
        v.setSpacing(8)
        self._title = _title_label(title, 11 if primary else 10)
        if not primary:
            self._title.setStyleSheet(
                f"color: {Colors.TEXT_SECONDARY}; background: transparent; border: none;"
                f"{get_font_family_css()} {font_size_css(10)};"
            )
        v.addWidget(self._title)
        self._desc = _hint(desc, 10)
        self._desc.setWordWrap(False)
        v.addWidget(self._desc, 1)
        self._tail = QHBoxLayout()
        self._tail.setContentsMargins(0, 0, 0, 0)
        self._tail.setSpacing(6)
        v.addLayout(self._tail)
        self._tail_widgets: List[QWidget] = []
        self._hide_tail = False
        self._apply_style(False)

    def add_tail(self, w: QWidget) -> None:
        self._tail.addWidget(w)
        self._tail_widgets.append(w)
        if self._hide_tail:
            w.setVisible(False)

    def hide_tail_until_hover(self) -> None:
        """尾部操作默认隐藏，鼠标进入行内才显示。"""
        self._hide_tail = True
        for w in self._tail_widgets:
            w.setVisible(False)

    def set_desc(self, text: str) -> None:
        self._desc.setText(text)
        self._desc.setVisible(bool(text))

    def _apply_style(self, hover: bool) -> None:
        bg = Colors.HOVER_BG if hover else "transparent"
        self.setStyleSheet(
            f"""
            QFrame#hubRowItem {{
                background: {bg};
                border: none;
                border-radius: 6px;
            }}
            QFrame#hubRowItem QLabel {{ background: transparent; border: none; }}
        """
        )

    def enterEvent(self, e) -> None:  # noqa: N802
        self._apply_style(True)
        if self._hide_tail:
            for w in self._tail_widgets:
                w.setVisible(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._apply_style(False)
        if self._hide_tail:
            for w in self._tail_widgets:
                w.setVisible(False)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.rect().contains(e.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class _SectionChip(QFrame):
    """记忆分区卡：名称 + 介绍（这段介绍即蒸馏时指导模型关注什么）。

    视觉对齐人格卡：圆角边框、hover 高亮边框、点击进编辑。删除按钮在卡片右上角，
    默认隐藏（悬停可见），不与名称抢位。介绍容忍 3 行（多行输入），超长靠 tooltip 补全。
    """

    clicked = pyqtSignal()
    deleteRequested = pyqtSignal()

    W = 168
    H = 104
    INTRO_LINES = 3

    def __init__(self, name: str, intro: str, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionChip")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.W, self.H)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 10, 10)
        v.setSpacing(4)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(4)
        self._name = QLabel(name)
        self._name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(11)}; font-weight: 600;"
        )
        head.addWidget(self._name, 1)
        self._del = _del_btn(12)
        self._del.setFixedSize(20, 20)
        self._del.setToolTip("删除该分区（记忆内容保留，下次蒸馏时重分配）")
        self._del.setStyleSheet(_btn_style(danger=True, icon_only=True))
        self._del.setVisible(False)
        self._del.clicked.connect(self.deleteRequested.emit)
        head.addWidget(self._del, 0, Qt.AlignTop)
        v.addLayout(head)

        # 介绍：按像素折 3 行（多行输入可能带换行，_elide_lines 只折行不认 \n，先压平）
        self._intro = QLabel()
        self._intro.setWordWrap(False)
        fm = QFont(self._intro.font())
        fm.setPixelSize(scale_font_size(9))
        metrics = QFontMetrics(fm)
        flat = " ".join(str(intro or "").split())
        self._intro.setText(_elide_lines(flat, metrics, self.W - 26, self.INTRO_LINES))
        self._intro.setFixedHeight(metrics.lineSpacing() * self.INTRO_LINES)
        self._intro.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._intro.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(9)};"
        )
        v.addWidget(self._intro)
        v.addStretch()
        self.setToolTip(intro or name)
        self._apply_style()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QFrame#sectionChip {{
                background: transparent;
                border: 1.5px solid {Colors.BORDER};
                border-radius: 12px;
            }}
            QFrame#sectionChip:hover {{ border-color: {Colors.TEXT_ACCENT}; }}
            QFrame#sectionChip QLabel {{ background: transparent; border: none; }}
        """
        )

    def enterEvent(self, e) -> None:  # noqa: N802
        self._del.setVisible(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._del.setVisible(False)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.rect().contains(e.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class _NewSectionChip(QFrame):
    """「新增分区」卡：虚线边框 + 加号，与分区卡同尺寸排在同一行。"""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionChip")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(_SectionChip.W, _SectionChip.H)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(2)
        v.setAlignment(Qt.AlignHCenter)
        plus = QLabel("+")
        plus.setAlignment(Qt.AlignCenter)
        plus.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(22)};"
        )
        v.addWidget(plus, 0, Qt.AlignHCenter)
        lbl = QLabel("新增分区")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)}; font-weight: 600;"
        )
        v.addWidget(lbl)
        self.setStyleSheet(
            f"""
            QFrame#sectionChip {{
                background: transparent;
                border: 1.5px dashed {Colors.BORDER};
                border-radius: 12px;
            }}
            QFrame#sectionChip:hover {{ border-color: {Colors.TEXT_ACCENT}; }}
            QFrame#sectionChip QLabel {{ background: transparent; border: none; }}
        """
        )

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.rect().contains(e.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class _MetaBar(QLabel):
    """记忆元信息：单行灰字摘要，点击可调上限。

    追求「一眼扫过、不占注意力」：用量上限健康合并成一行，上限直接内联在用量里
    （全局 1469/4000），不额外占一段。健康异常时整行转琥珀色并带 ⚠。
    摘要文本由调用方组装（记忆区唯一知道数据来源的地方在 assistant_card）。
    """

    limitClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hubMetaBar")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击修改字符上限")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setWordWrap(False)
        self._usage = ""
        self._health = ""
        self._warn = False
        self._refresh()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.limitClicked.emit()
        super().mouseReleaseEvent(e)

    def _refresh(self) -> None:
        parts = [p for p in (self._usage, self._health) if p]
        if self._health and self._warn:
            parts[-1] = "⚠ " + self._health
        self.setText(" · ".join(parts) or "—")
        color = Colors.WARNING if self._warn else Colors.TEXT_MUTED
        self.setStyleSheet(
            f"color: {color}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )

    def set_usage(self, text: str) -> None:
        """用量摘要，如「全局 1469/4000 · 项目 2999/3000 · 日志 6 天」。"""
        self._usage = str(text or "")
        self._refresh()

    def set_health(self, text: str, warn: bool = False) -> None:
        self._health = str(text or "")
        self._warn = bool(warn)
        self._refresh()


# ── 名称 / 模型分区 ──────────────────────────────────────


def _host_window():
    """弹窗 parent：完整主窗口（与 assistant_card 同实现，供分区内的弹窗复用）。

    self.window() 在 full 卡容器内可能返回卡片容器导致 MaskDialog 遮罩
    只盖住容器、定位异常；统一走 ``window.host`` 能力（宿主解析当前窗口），
    取不到时返回 None（调用方自行兜底）。
    """
    try:
        host = _caps("window.host")
        win = host() if host else None
        if win is not None:
            return win
    except Exception:
        pass
    return None


class _CenterFlowLayout(QLayout):
    """流式布局：空间不足自动换行，每行水平居中（chips 卡行用）。"""

    def __init__(self, parent=None, spacing: int = 12):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:  # noqa: N802
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, w, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        """单块最大尺寸，高度交给 heightForWidth。

        不能用「按块累加」：四块并排会被算成四块总高（实测 452 而实际行高 104），
        外层滚动区据此留出巨量空白。
        """
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        avail_w = rect.width() - m.left() - m.right()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line: list = []  # [(item, w, h)]
        line_w = 0
        line_h = 0

        def flush_line() -> None:
            nonlocal y
            if not line:
                return
            start_x = x + max(0, (avail_w - line_w) // 2)
            cx = start_x
            for it, iw, ih in line:
                if not test_only:
                    it.setGeometry(QRect(cx, y, iw, ih))
                cx += iw + self._spacing
            y += line_h + self._spacing

        for item in self._items:
            hint = item.sizeHint()
            iw, ih = hint.width(), hint.height()
            add_w = iw if not line else self._spacing + iw
            if line and line_w + add_w > avail_w:
                flush_line()
                line, line_w, line_h = [], 0, 0
                add_w = iw
            line.append((item, iw, ih))
            line_w += add_w
            line_h = max(line_h, ih)
        flush_line()
        # 最后一行之后不加行间距：总高 = 已消耗 y - 起始 y - 末尾多加的 spacing
        return max(0, (y - self._spacing) - rect.y()) + m.bottom()


# ── 名称 / 模型分区 ──────────────────────────────────────


class ProfileSection(_Section):
    """对他/她的称呼 / 称呼 / 用户头像（对话模型跟随系统当前配置，不单独设置；改动即节流保存）。"""

    saveRequested = pyqtSignal(str, str)  # (name, user_addressing)
    userAvatarPicked = pyqtSignal(bytes, str)  # 用户头像选中（数据, 扩展名）
    userAvatarCleared = pyqtSignal()  # 恢复默认用户头像
    _DEBOUNCE_MS = 600

    def __init__(self, parent=None):
        super().__init__("基本信息", parent)
        self._suspend_autosave = True  # bind() 期间不触发自动保存

        # 表单整体固定宽度、居中（不占满整行；两行 label 左缘对齐）
        FORM_W = 520
        form_holder = QWidget()
        form_holder.setFixedWidth(FORM_W)
        form = QVBoxLayout(form_holder)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)

        row1 = QHBoxLayout()
        lbl1 = _title_label("对他/她的称呼", 11)
        lbl1.setFixedWidth(118)  # 与下行 label 同宽 → 控件左缘对齐（须容纳"对他/她的称呼"7 字符）
        row1.addWidget(lbl1)
        self._name = QLineEdit()
        self._name.setStyleSheet(_input_style())
        self._name.textEdited.connect(self._schedule_autosave)
        row1.addWidget(self._name, 1)
        form.addLayout(row1)

        row_addr = QHBoxLayout()
        lbl_addr = _title_label("对你的称呼", 11)
        lbl_addr.setFixedWidth(118)
        row_addr.addWidget(lbl_addr)
        self._addressing = QLineEdit()
        self._addressing.setStyleSheet(_input_style())
        self._addressing.setPlaceholderText("怎么称呼你，默认跟随系统用户名")
        self._addressing.textEdited.connect(self._schedule_autosave)
        row_addr.addWidget(self._addressing, 1)
        form.addLayout(row_addr)

        # 用户头像：预览 + 选择按钮（选中的头像注入消息身份行，显示在用户消息旁）
        row3 = QHBoxLayout()
        lbl3 = _title_label("你的头像", 11)
        lbl3.setFixedWidth(118)
        row3.addWidget(lbl3)
        self._user_avatar = RoundAvatar(size=40, text="?", color="#7C3AED")
        row3.addWidget(self._user_avatar)
        self._pick_avatar_btn = QPushButton("选择头像")
        self._pick_avatar_btn.setFixedHeight(30)
        self._pick_avatar_btn.setCursor(Qt.PointingHandCursor)
        self._pick_avatar_btn.setStyleSheet(_btn_style())
        self._pick_avatar_btn.clicked.connect(self._pick_avatar)
        row3.addWidget(self._pick_avatar_btn)
        row3.addStretch()
        form.addLayout(row3)

        wrap = QHBoxLayout()
        wrap.addStretch()
        wrap.addWidget(form_holder)
        wrap.addStretch()
        self.body().addLayout(wrap)
        self.body().addWidget(
            _hint(
                "对话模型跟随系统配置（助手不单独设置）。"
                "用户头像显示在消息列表中你的消息旁，每个助手独立设置。改动自动保存。"
            )
        )

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_save)

    def set_user_avatar(self, image_path: Optional[str], name: str = "") -> None:
        """刷新用户头像预览（bind 与保存后由宿主调用；name 用于无图时的首字母色块）。

        ⚠ image_path 可能是 Path 或 str：QPixmap 不认 Path 对象（TypeError），
        统一在此转 str，调用方无须关心。
        """
        if name:
            self._user_avatar.set_text(name)
        self._user_avatar.set_image(str(image_path) if image_path else None)

    def _pick_avatar(self) -> None:
        """弹头像选择器（延迟导入避免 sections ↔ avatar_picker 循环依赖）。"""
        from .avatar_picker import UserAvatarDialog

        dlg = UserAvatarDialog(parent=_host_window() or self.window())
        dlg.picked.connect(self.userAvatarPicked)
        dlg.cleared.connect(self.userAvatarCleared)
        dlg.exec_()

    def _schedule_autosave(self, *_a) -> None:
        if self._suspend_autosave:
            return
        self._debounce.start()

    def bind(self, name: str, addressing: str) -> None:
        self._suspend_autosave = True
        self._name.setText(name)
        self._addressing.setText(addressing)
        self._suspend_autosave = False

    def _emit_save(self) -> None:
        self.saveRequested.emit(self._name.text().strip(), self._addressing.text().strip())


# ── 关于 Ta（人格切换）────────────────────


class _PersonaChip(QFrame):
    """人格选择卡：40px 圆头像 + 名 + 描述 + tag 方角牌（对齐原版 yuan-chip）。"""

    clicked = pyqtSignal()

    def __init__(self, persona: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("personaChip")  # ⚠ 样式选择器依赖，缺失则无边框
        self.persona_id = persona["id"]
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(140, 156)  # 预留全局字体缩放后两行 desc + tag 的空间
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(4)
        v.setAlignment(Qt.AlignHCenter)
        self._avatar = RoundAvatar(
            size=40,
            text=persona.get("name") or persona["id"],
            color="#7C3AED",
            image_path=persona.get("avatar_path") or None,
        )
        v.addWidget(self._avatar, 0, Qt.AlignHCenter)
        self._name = QLabel(persona.get("name") or persona["id"])
        self._name.setAlignment(Qt.AlignCenter)
        self._desc = QLabel()
        self._desc.setAlignment(Qt.AlignCenter)
        self._desc.setWordWrap(True)
        self._tag = QLabel(f"[{persona['tag']}]" if persona.get("tag") else "")
        self._tag.setAlignment(Qt.AlignCenter)
        v.addWidget(self._name)
        v.addWidget(self._desc)
        v.addWidget(self._tag)
        self._apply_style()
        # 描述限制 2 行：按像素宽度手动截断（超出加 …），并钉死高度，
        # 防止长描述挤压/遮挡下方 tag（chips 卡固定 118x150）
        # 字号必须与 desc QSS 实际渲染一致（含全局字体缩放 delta），
        # 否则度量偏小 → QLabel 自动折出第三行被裁/遮挡 tag
        fm = QFont(self._desc.font())
        fm.setPixelSize(scale_font_size(10))
        metrics = QFontMetrics(fm)
        self._desc.setText(_elide_lines(persona.get("description", ""), metrics, 118, 2))
        self._desc.setFixedHeight(metrics.lineSpacing() * 2)
        self._desc.setWordWrap(False)  # 兑底：已手动折行，禁自动折行防溢出

    def set_avatar_image(self, image_path: str) -> None:
        """换人格头像后轻量刷新本 chip 头像。"""
        self._avatar.set_image(image_path or None)

    def _apply_style(self) -> None:
        border = Colors.TEXT_ACCENT if self._selected else Colors.BORDER
        bg = "rgba(245, 158, 11, 0.07)" if self._selected else "transparent"
        self.setStyleSheet(
            f"""
            QFrame#personaChip {{
                background: {bg};
                border: {"2px solid " + Colors.TEXT_ACCENT if self._selected else "1.5px solid " + border};
                border-radius: 12px;
            }}
            QFrame#personaChip:hover {{ border-color: {Colors.TEXT_ACCENT}; }}
            QFrame#personaChip QLabel {{ background: transparent; border: none; }}
        """
        )
        self._name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        self._desc.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};")
        self._tag.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT if self._selected else Colors.TEXT_MUTED};"
            f"{get_font_family_css()} {font_size_css(9)}; letter-spacing: 1px;"
        )

    def set_selected(self, on: bool) -> None:
        if self._selected != on:
            self._selected = on
            self._apply_style()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self.clicked.emit()


class _NewPersonaChip(QFrame):
    """「新建人格」卡：虚线边框 + 加号，点击跳回对话拉起 persona-creator。"""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("personaChip")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(140, 156)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(4)
        v.setAlignment(Qt.AlignHCenter)
        plus = QLabel("+")
        plus.setAlignment(Qt.AlignCenter)
        plus.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(26)};"
        )
        v.addWidget(plus, 0, Qt.AlignHCenter)
        lbl = QLabel("新建人格")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        v.addWidget(lbl)
        hint = QLabel("对话向导创建")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        v.addWidget(hint)
        self._apply_style()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QFrame#personaChip {{
                background: transparent;
                border: 1.5px dashed {Colors.BORDER};
                border-radius: 12px;
            }}
            QFrame#personaChip:hover {{ border-color: {Colors.TEXT_ACCENT}; }}
            QFrame#personaChip QLabel {{ background: transparent; border: none; }}
        """
        )

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self.clicked.emit()


class AboutSection(_Section):
    """人格选择（chips，含「纯净助手」）。人格只读，只能切换；新增人格走 persona-creator 技能。"""

    personaChangeRequested = pyqtSignal(str)
    createPersonaRequested = pyqtSignal()

    def __init__(self, personas: List[dict], current_pid: str, parent=None):
        super().__init__("关于 Ta", parent)
        self.body().addWidget(_hint("点卡片切换人格（内置只读）；新增在对话里说「创建新人格」。", 10))

        chips_host = QWidget()
        chips_host.setStyleSheet("background: transparent;")
        chips_row = _CenterFlowLayout(chips_host)
        chips_row.setSpacing(12)
        self._chips_row = chips_row
        self._chips: List[_PersonaChip] = []
        self._personas = personas  # 调用方已包含 none（纯净助手）
        self._current_pid = current_pid
        self.body().addWidget(chips_host)
        self.rebuild_chips(personas)
        self.set_persona(current_pid)

    def set_persona(self, pid: str) -> None:
        """刷新 chips 选中态。"""
        self._current_pid = pid
        for chip in self._chips:
            chip.set_selected(chip.persona_id == pid)

    def rebuild_chips(self, personas: List[dict]) -> None:
        """全量重建人格 chips（人格数据/头像变更后刷新，保留当前选中态）。"""
        self._personas = personas
        while self._chips_row.count():
            item = self._chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._chips = []
        for p in personas:
            chip = _PersonaChip(p)
            chip.clicked.connect(lambda pid=p["id"]: self.personaChangeRequested.emit(pid))
            self._chips_row.addWidget(chip)
            self._chips.append(chip)
        new_chip = _NewPersonaChip()
        new_chip.clicked.connect(self.createPersonaRequested.emit)
        self._chips_row.addWidget(new_chip)
        self.set_persona(self._current_pid)


# ── 项目注入分区 ────────────────────────────────────────


class ProjectSection(_Section):
    """项目注入开关：项目笔记（AGENTS.md）+ 项目上下文（根目录/git 状态）。

    开关只控制主智能体的注入；子智能体始终注入（干活需要项目上下文）。
    """

    toggleNotes = pyqtSignal(bool)
    toggleContext = pyqtSignal(bool)
    toggleSkills = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__("项目", parent)
        self.body().addWidget(_hint("注入到系统提示词的项目信息，子智能体不受开关影响。"))

        # 项目笔记
        notes_row = QHBoxLayout()
        notes_row.addWidget(_title_label("项目笔记", 11))
        notes_row.addStretch()
        notes_lbl = QLabel("注入")
        notes_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        notes_row.addWidget(notes_lbl)
        self._notes_switch = SwitchButton()
        self._notes_switch.setOnText("开")
        self._notes_switch.setOffText("关")
        self._notes_switch.checkedChanged.connect(self.toggleNotes.emit)
        notes_row.addWidget(self._notes_switch)
        self.body().addLayout(notes_row)
        self.body().addWidget(_hint("AGENTS.md 项目操作手册与约束，写入 {项目根目录}/AGENTS.md。"))

        self.body().addWidget(_sep())

        # 项目上下文
        ctx_row = QHBoxLayout()
        ctx_row.addWidget(_title_label("项目上下文", 11))
        ctx_row.addStretch()
        ctx_lbl = QLabel("注入")
        ctx_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        ctx_row.addWidget(ctx_lbl)
        self._context_switch = SwitchButton()
        self._context_switch.setOnText("开")
        self._context_switch.setOffText("关")
        self._context_switch.checkedChanged.connect(self.toggleContext.emit)
        ctx_row.addWidget(self._context_switch)
        self.body().addLayout(ctx_row)
        self.body().addWidget(_hint("项目根目录路径规则 + git 分支/工作树状态。"))

        self.body().addWidget(_sep())

        # 项目技能
        skills_row = QHBoxLayout()
        skills_row.addWidget(_title_label("项目技能", 11))
        skills_row.addStretch()
        skills_lbl = QLabel("注入")
        skills_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        skills_row.addWidget(skills_lbl)
        self._skills_switch = SwitchButton()
        self._skills_switch.setOnText("开")
        self._skills_switch.setOffText("关")
        self._skills_switch.checkedChanged.connect(self.toggleSkills.emit)
        skills_row.addWidget(self._skills_switch)
        self.body().addLayout(skills_row)
        self.body().addWidget(_hint("{项目根}/.drifox/skills/ 下的项目内技能，自动进技能段，可被 /技能名 加载。"))

    def set_notes(self, on: bool) -> None:
        self._notes_switch.setChecked(on)

    def set_context_enabled(self, on: bool) -> None:
        self._context_switch.setChecked(on)

    def set_skills(self, on: bool) -> None:
        self._skills_switch.setChecked(on)


# ── 人工提示分区 ────────────────────────────────────────


class PinnedSection(_Section):
    """人工提示：人工添加，无自动记忆风险。

    独立于记忆分区（MemorySection）：不受记忆开关控制，始终可见可编辑、始终注入。
    数据源与原置顶记忆同（pinned.md）。
    """

    pinAddRequested = pyqtSignal(str)
    pinEdited = pyqtSignal(str, str)  # (pin_id, 新内容)
    pinDeleteRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("人工提示", parent)
        self.body().addWidget(_hint("人工添加的明确要求，始终注入，不受记忆开关影响。一条一条管理，可增删改。"))
        self._pin_list = QVBoxLayout()
        self._pin_list.setSpacing(3)
        self.body().addLayout(self._pin_list)
        add_row = QHBoxLayout()
        self._pin_input = QLineEdit()
        self._pin_input.setPlaceholderText("添加一条人工提示...")
        self._pin_input.setStyleSheet(_input_style())
        self._pin_input.returnPressed.connect(self._emit_add_pin)
        add_row.addWidget(self._pin_input, 1)
        add_btn = _add_btn(14)
        add_btn.setFixedSize(30, 28)
        add_btn.setStyleSheet(_btn_style(icon_only=True))
        add_btn.clicked.connect(self._emit_add_pin)
        add_row.addWidget(add_btn)
        self.body().addLayout(add_row)

    def _emit_add_pin(self) -> None:
        text = self._pin_input.text().strip()
        if not text:
            return
        self._pin_input.clear()
        self.pinAddRequested.emit(text)

    def reload_pins(self, items: List[Tuple[str, str]]) -> None:
        """重建列表：一条一行，行内绑定 pin_id（编辑/删除按 id 上报）。

        UI 永远以盘上数据重绘（宿主写盘后回调本方法），增删改即时可见。
        """
        while self._pin_list.count():
            item = self._pin_list.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for pid, content in items:
            row = QFrame()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(content)
            edit.setStyleSheet(_input_style())
            edit.editingFinished.connect(lambda pid=pid, e=edit: self.pinEdited.emit(pid, e.text().strip()))
            h.addWidget(edit, 1)
            del_btn = _del_btn(14)
            del_btn.setFixedSize(26, 26)
            del_btn.setToolTip("删除这条提示")
            del_btn.setStyleSheet(_btn_style(danger=True, icon_only=True))
            del_btn.clicked.connect(lambda _checked=False, pid=pid: self.pinDeleteRequested.emit(pid))
            h.addWidget(del_btn)
            self._pin_list.addWidget(row)


# ── 记忆分区 ────────────────────────────────────────────


class MemoryLimitDialog(MaskDialogBase):
    """记忆上限编辑弹窗：全局/项目两个数字输入，确认发 confirmed(int, int)。"""

    confirmed = pyqtSignal(int, int)

    _INPUT_QSS = """
        LineEdit {{
            background: {bg};
            border: 1px solid {border};
            color: {text};
            padding: 4px 10px;
            border-radius: 6px;
            {font}
        }}
        LineEdit:focus {{ border-color: {accent}; }}
    """

    def __init__(self, global_chars: int, project_chars: int, parent=None):
        super().__init__(parent)
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))
        self.widget.setObjectName("memoryLimitDialog")
        self.widget.setStyleSheet(
            f"""
            #memoryLimitDialog {{
                background: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """
        )
        v = QVBoxLayout(self.widget)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)

        title = BodyLabel("记忆上限", self.widget)
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(14)}; font-weight: 600;"
        )
        v.addWidget(title)
        hint = QLabel("单文件字符上限，超限写入被拒", self.widget)
        hint.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}")
        v.addWidget(hint)

        input_qss = self._INPUT_QSS.format(
            bg=Colors.INPUT_BG_START,
            border=Colors.INPUT_BORDER,
            text=Colors.INPUT_TEXT,
            accent=Colors.INFO,
            font=f"{get_font_family_css()} {font_size_css(13)}",
        )

        def _num_row(label: str, value: int) -> LineEdit:
            row = QHBoxLayout()
            lab = QLabel(label, self.widget)
            lab.setFixedWidth(72)
            lab.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}")
            row.addWidget(lab)
            edit = LineEdit(self.widget)
            edit.setText(str(int(value)))
            edit.setFixedHeight(34)
            edit.setClearButtonEnabled(True)
            edit.setStyleSheet(input_qss)
            row.addWidget(edit, 1)
            v.addLayout(row)
            return edit

        self._global_edit = _num_row("全局记忆", global_chars)
        self._project_edit = _num_row("项目记忆", project_chars)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("取消", self.widget)
        cancel.setFixedHeight(32)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent;
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 4px 26px;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QPushButton:hover {{ background: {Colors.HOVER_BG}; border-color: {Colors.TEXT_ACCENT}; }}
        """
        )
        cancel.clicked.connect(self.close)
        confirm = QPushButton("确定", self.widget)
        confirm.setFixedHeight(32)
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.setDefault(True)
        confirm.setStyleSheet(
            f"""
            QPushButton {{
                background: {Colors.TEXT_ACCENT};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 4px 26px;
                {get_font_family_css()} {font_size_css(12)}
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {Colors.TEXT_ACCENT}; }}
        """
        )
        confirm.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(confirm)
        v.addLayout(btns)

        self.widget.setFixedSize(380, self.widget.sizeHint().height() + 40)

    def _accept(self) -> None:
        def _parse(edit, fallback: int) -> int:
            try:
                val = int(edit.text().strip())
            except TypeError, ValueError:
                return fallback
            return val if 100 <= val <= 1000000 else fallback

        g = _parse(self._global_edit, 4000)
        p = _parse(self._project_edit, 3000)
        self.confirmed.emit(g, p)
        self.accept()


class SectionEditDialog(MaskDialogBase):
    """记忆分区编辑弹窗：名称 + 介绍，确认发 confirmed(name, intro)。

    介绍不是装饰文字：它会被注入蒸馏指令，指导模型整理时往这个分区关注什么内容。
    """

    confirmed = pyqtSignal(str, str)

    _INPUT_QSS = MemoryLimitDialog._INPUT_QSS

    def __init__(self, name: str = "", hint: str = "", title: str = "新增分区", parent=None):
        super().__init__(parent)
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))
        self.widget.setObjectName("sectionEditDialog")
        self.widget.setStyleSheet(
            f"""
            #sectionEditDialog {{
                background: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """
        )
        v = QVBoxLayout(self.widget)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)

        head = BodyLabel(title, self.widget)
        head.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(14)}; font-weight: 600;"
        )
        v.addWidget(head)

        input_qss = self._INPUT_QSS.format(
            bg=Colors.INPUT_BG_START,
            border=Colors.INPUT_BORDER,
            text=Colors.INPUT_TEXT,
            accent=Colors.INFO,
            font=f"{get_font_family_css()} {font_size_css(13)}",
        )

        def _field(label: str, value: str, placeholder: str) -> LineEdit:
            lab = QLabel(label, self.widget)
            lab.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}")
            v.addWidget(lab)
            edit = LineEdit(self.widget)
            edit.setText(value)
            edit.setPlaceholderText(placeholder)
            edit.setFixedHeight(34)
            edit.setStyleSheet(input_qss)
            v.addWidget(edit)
            return edit

        self._name_edit = _field("分区名", name, "如：工程方法论")

        # 介绍：多行输入（这段会注入蒸馏指令，需要能写长）
        intro_lab = QLabel("介绍", self.widget)
        intro_lab.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}"
        )
        v.addWidget(intro_lab)
        self._hint_edit = QPlainTextEdit(self.widget)
        self._hint_edit.setPlainText(hint)
        self._hint_edit.setPlaceholderText(
            "这段会作为蒸馏指令读给模型，指导它整理时关注什么。\n"
            "建议写：该收什么内容；明确不收什么（防止相邻分区互相混入）。"
        )
        self._hint_edit.setFixedHeight(96)
        self._hint_edit.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background: {Colors.INPUT_BG_START};
                border: 1px solid {Colors.INPUT_BORDER};
                border-radius: 6px;
                color: {Colors.INPUT_TEXT};
                padding: 6px 8px;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QPlainTextEdit:focus {{ border-color: {Colors.INFO}; }}
        """
        )
        v.addWidget(self._hint_edit)

        err = QLabel("", self.widget)
        err.setWordWrap(True)
        err.setVisible(False)
        err.setStyleSheet(f"color: {Colors.ERROR}; {get_font_family_css()} {font_size_css(11)}")
        self._err = err
        v.addWidget(err)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("取消", self.widget)
        cancel.setFixedHeight(32)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent;
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 4px 26px;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QPushButton:hover {{ background: {Colors.HOVER_BG}; border-color: {Colors.TEXT_ACCENT}; }}
        """
        )
        cancel.clicked.connect(self.close)
        confirm = QPushButton("确定", self.widget)
        confirm.setFixedHeight(32)
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.setDefault(True)
        confirm.setStyleSheet(
            f"""
            QPushButton {{
                background: {Colors.TEXT_ACCENT};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 4px 26px;
                {get_font_family_css()} {font_size_css(12)}
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {Colors.TEXT_ACCENT}; }}
        """
        )
        confirm.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(confirm)
        v.addLayout(btns)

        self.widget.setFixedSize(460, self.widget.sizeHint().height() + 40)

    def _accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._err.setText("分区名不能为空")
            self._err.setVisible(True)
            return
        if "\n" in name or name.startswith("#"):
            self._err.setText("分区名不能含换行，也不能以 # 开头")
            self._err.setVisible(True)
            return
        self.confirmed.emit(name, self._hint_edit.toPlainText().strip())
        self.accept()


class MemorySection(_Section):
    """记忆分区：开关 + 状态 + 全局记忆/项目记忆/工作日志查看 + 清除。

    记忆体系照抄 WorkBuddy：全局 MEMORY.md（跨项目）+ 项目 .drifox/memory/（笔记+日志），
    模型经 memory_read/memory_write 自主读写；UI 只做查看与清理。
    """

    toggleMemory = pyqtSignal(bool)
    viewGlobal = pyqtSignal()
    viewProject = pyqtSignal()
    viewDaily = pyqtSignal()
    clearAll = pyqtSignal()
    limitsChanged = pyqtSignal(int, int)
    restoreRequested = pyqtSignal()
    migrateRequested = pyqtSignal(str)
    dismissDriftRequested = pyqtSignal(str)
    sectionsChanged = pyqtSignal(list)
    seedSectionsRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("记忆", parent)
        self._memory_switch = SwitchButton()
        self._memory_switch.setOnText("开")
        self._memory_switch.setOffText("关")
        self._memory_switch.checkedChanged.connect(self.toggleMemory.emit)
        self.set_context(self._memory_switch)

        # 记忆字符上限（set_limits 回填；弹窗编辑用）
        self._global_chars = 4000
        self._project_chars = 3000
        self._sections: List[Dict[str, str]] = []

        # ── 顶部元信息：单行灰字摘要，点它改上限 ──
        self._meta = _MetaBar()
        self._meta.limitClicked.connect(self._edit_limits)
        self.body().addWidget(self._meta)
        self.body().addWidget(_sep())

        # 漂移提示行：检测到旧工作目录记忆时显示（迁移/忽略）
        drift_row = QHBoxLayout()
        drift_row.setContentsMargins(0, 0, 0, 0)
        self._drift_label = _hint("")
        drift_row.addWidget(self._drift_label, 1)
        mig_btn = QPushButton("迁移到此目录")
        mig_btn.setStyleSheet(_btn_style())
        mig_btn.clicked.connect(lambda: self.migrateRequested.emit(self._drift_old))
        drift_row.addWidget(mig_btn)
        dis_btn = QPushButton("忽略")
        dis_btn.setStyleSheet(_btn_style())
        dis_btn.clicked.connect(lambda: self.dismissDriftRequested.emit(self._drift_old))
        drift_row.addWidget(dis_btn)
        self._drift_row = QWidget()
        self._drift_row.setLayout(drift_row)
        self._drift_row.setVisible(False)
        self.body().addWidget(self._drift_row)
        self._drift_old = ""

        self._body_wrap = QWidget()
        self._wrap_v = QVBoxLayout(self._body_wrap)
        self._wrap_v.setContentsMargins(0, 0, 0, 0)
        self._wrap_v.setSpacing(2)

        # ── 全局记忆组：标题行 + 可点的分区行 ──
        self._global_row = _RowItem("全局记忆", "跨项目 MEMORY.md")
        self._global_row.clicked.connect(self.viewGlobal.emit)
        self._restore_btn = QPushButton("恢复")
        self._restore_btn.setStyleSheet(_btn_style())
        self._restore_btn.setToolTip("从快照恢复记忆（每次覆盖写/清除前自动存快照）")
        self._restore_btn.clicked.connect(self.restoreRequested.emit)
        self._restore_btn.setVisible(False)
        self._global_row.add_tail(self._restore_btn)
        self._wrap_v.addWidget(self._global_row)

        # 记忆分区：标题行 + 卡片流（紧贴全局记忆，卡片在标题行下面）
        self._sec_head = _RowItem("记忆分区", "指导蒸馏时按区归类")
        self._sec_head.clicked.connect(self._on_add_section)
        self._wrap_v.addWidget(self._sec_head)

        self._chips_host = QWidget()
        self._chips_host.setStyleSheet("background: transparent;")
        # 流式布局的高度依赖宽度：容器必须让布局走 heightForWidth，
        # 否则 sizeHint 用最小块尺寸拼出的值（远大于实际行高）会挤压外层。
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        sp.setHeightForWidth(True)
        self._chips_host.setSizePolicy(sp)
        self._chips_row = _CenterFlowLayout(self._chips_host)
        self._chips_row.setSpacing(10)
        self._wrap_v.addWidget(self._chips_host)

        # 空分区提示：只在用户把分区全删光时出现（默认已落盘，不再显示初始化引导）
        self._sections_empty_row = QWidget()
        _se = QHBoxLayout(self._sections_empty_row)
        _se.setContentsMargins(8, 2, 0, 2)
        _se.addWidget(_hint("未定义分区，全局记忆为自由格式。"))
        init_btn = QPushButton("用默认分区")
        init_btn.setStyleSheet(_btn_style())
        init_btn.clicked.connect(self._on_seed_sections)
        _se.addWidget(init_btn)
        _se.addStretch()
        self._sections_empty_row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._wrap_v.addWidget(self._sections_empty_row)
        self._sections_empty_row.hide()  # 靠 hide() 让布局项失效；setVisible 在未显示窗口时不生效

        # ── 项目记忆组：全局与项目之间必须有分隔线 ──
        self._wrap_v.addSpacing(4)
        self._wrap_v.addWidget(_sep())
        self._wrap_v.addSpacing(4)

        self._project_row = _RowItem("项目记忆", "本项目 .drifox/memory")
        self._project_row.clicked.connect(self.viewProject.emit)
        self._wrap_v.addWidget(self._project_row)

        self._daily_row = _RowItem("工作日志", "按天记录的详细过程")
        self._daily_row.clicked.connect(self.viewDaily.emit)
        self._wrap_v.addWidget(self._daily_row)

        self._wrap_v.addSpacing(4)
        self._wrap_v.addWidget(_sep())

        # 清除记忆
        clear_row = QHBoxLayout()
        clear_row.setContentsMargins(0, 4, 0, 0)
        clear_row.addStretch()
        clear_btn = QPushButton("清除记忆")
        clear_btn.setStyleSheet(_btn_style(danger=True))
        clear_btn.clicked.connect(self.clearAll.emit)
        clear_row.addWidget(clear_btn)
        self._wrap_v.addLayout(clear_row)

        self.body().addWidget(self._body_wrap)

    def set_memory_enabled(self, on: bool) -> None:
        self._memory_switch.setChecked(on)
        self.setEnabled_all(on)

    def set_limits(self, global_chars: int, project_chars: int) -> None:
        """回填上限（元信息摘要由 card 侧 set_status 统一刷新）。"""
        self._global_chars = int(global_chars) if global_chars and int(global_chars) > 0 else 4000
        self._project_chars = int(project_chars) if project_chars and int(project_chars) > 0 else 3000

    def _edit_limits(self) -> None:
        """弹上限编辑窗，确认后发 limitsChanged。"""
        dlg = MemoryLimitDialog(self._global_chars, self._project_chars, parent=self.window())
        dlg.confirmed.connect(self.limitsChanged.emit)
        dlg.exec()

    def setEnabled_all(self, on: bool) -> None:  # noqa: N802
        """记忆开关关闭时灰置记忆内容（开关与状态条保持可见）。"""
        self._body_wrap.setVisible(bool(on))

    def set_status(self, text: str) -> None:
        self._meta.set_usage(text)

    # ── 记忆分区（全局记忆归类骨架）──

    def set_sections(self, sections: List[Dict[str, str]]) -> None:
        """重建分区卡片流。空列表 → 显示「未定义分区」提示（用户手动删光时才出现）。

        卡片介绍用 detail（完整归类说明）：这段文字就是蒸馏时指导模型关注什么的内容，
        不是给人看的短标签。短 hint 仅作卡片无 detail 时的兜底。
        """
        self._sections = [dict(s) for s in (sections or [])]
        while self._chips_row.count():
            item = self._chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._sections_empty_row.setHidden(bool(self._sections))
        if self._sections:
            # 隐藏项的 geometry 会滞留旧值（实测 480px），配合外层 adjustSize 会撑出空白
            self._sections_empty_row.setFixedHeight(0)
        else:
            self._sections_empty_row.setMinimumHeight(0)
            self._sections_empty_row.setMaximumHeight(16777215)
        for idx, sec in enumerate(self._sections):
            name = str(sec.get("name") or "")
            intro = str(sec.get("detail") or sec.get("hint") or "")
            chip = _SectionChip(name, intro)
            chip.clicked.connect(lambda i=idx: self._edit_section(i))
            chip.deleteRequested.connect(lambda i=idx: self._delete_section(i))
            self._chips_row.addWidget(chip)
        new_chip = _NewSectionChip()
        new_chip.clicked.connect(self._on_add_section)
        self._chips_row.addWidget(new_chip)
        self._sec_head.set_desc("" if not self._sections else f"{len(self._sections)} 个 · 指导蒸馏关注点")
        # 显隐切换后布局项高度会滞留旧值（实测 empty_row 隐藏后仍占 480px），
        # invalidate 强制父链重算；notify 让外层滚动区同步收缩。
        self._wrap_v.invalidate()
        self._chips_row.invalidate()
        self._sync_chips_height()

    def _sync_chips_height(self) -> None:
        """按当前宽度重算卡片流行高并钉住容器高度。

        流式布局的 sizeHint 是单块尺寸，实际行高只能靠 heightForWidth；
        不钉高度会让外层按单块 hint 预留空间，卡片行被压扁或留白。
        """
        w = self._chips_host.width()
        if w <= 1:
            w = max(1, self._chips_host.sizeHint().width())
        rows_h = self._chips_row.heightForWidth(w)
        if rows_h > 0 and rows_h != self._chips_host.height():
            # 不用 setFixedHeight：那会把高度锁死，后续宽度变化时 heightForWidth
            # 算出的新行高无法生效（一开始宽度为 0，被钉成单行后永远只显一行）。
            self._chips_host.setMinimumHeight(rows_h)
            self._chips_host.setMaximumHeight(rows_h)
            self._chips_host.updateGeometry()

    def _on_chips_host_resized(self) -> None:
        self._sync_chips_height()

    def _commit_sections(self, sections: List[Dict[str, str]]) -> None:
        self.set_sections(sections)
        self.sectionsChanged.emit([dict(s) for s in sections])

    def _on_add_section(self) -> None:
        dlg = SectionEditDialog(title="新增分区", parent=self.window())
        dlg.confirmed.connect(
            lambda n, h: self._commit_sections([*(self._sections), {"name": n, "hint": h, "detail": h}])
        )
        dlg.exec()

    def _edit_section(self, idx: int) -> None:
        if not (0 <= idx < len(self._sections)):
            return
        cur = self._sections[idx]
        cur_detail = str(cur.get("detail") or cur.get("hint") or "")
        dlg = SectionEditDialog(str(cur.get("name") or ""), cur_detail, "编辑分区", self.window())
        dlg.confirmed.connect(lambda n, h, i=idx: self._replace_section(i, n, h))
        dlg.exec()

    def _replace_section(self, idx: int, name: str, hint: str) -> None:
        names = {str(s.get("name") or "") for i, s in enumerate(self._sections) if i != idx}
        if name in names:
            self._sec_head.set_desc(f"分区「{name}」已存在")
            return
        items = [dict(s) for s in self._sections]
        # 弹窗输入的是完整归类说明：detail 存全文，hint 取首句做行内短标签
        short = hint.split("。", 1)[0].strip() or hint
        items[idx] = {"name": name, "hint": short, "detail": hint}
        self._commit_sections(items)

    def _delete_section(self, idx: int) -> None:
        if not (0 <= idx < len(self._sections)):
            return
        items = [dict(s) for s in self._sections]
        items.pop(idx)
        self._commit_sections(items)

    def _on_seed_sections(self) -> None:
        """恢复默认分区：UI 不持有默认值，发信号让 card 取 store 的唯一定义。"""
        self.seedSectionsRequested.emit()

    def set_health(self, health: Optional[Dict[str, Any]]) -> None:
        """渲染健康摘要与漂移提示（health=None/空 → 元信息条只显示用量）。"""
        if not health:
            self._meta.set_health("")
            self._restore_btn.setVisible(False)
            self._drift_row.setVisible(False)
            return
        warn: List[str] = []
        info: List[str] = []
        stale = int(health.get("stale_days") or 0)
        if stale >= 30:
            warn.append(f"{stale} 天未蒸馏")
        usage = health.get("usage") or {}
        over = health.get("over_limit") or {}
        g = usage.get("global") or [0, 0]
        p = usage.get("project") or [0, 0]
        if over.get("global"):
            warn.append(f"全局 {g[0]}/{g[1]} 超限")
        if over.get("project"):
            warn.append(f"项目 {p[0]}/{p[1]} 超限")
        info.append(f"上次蒸馏 {str(health.get('last_distill') or '无')}")
        snaps = health.get("snapshot_counts") or {}
        snap_n = int(snaps.get("global") or 0) + int(snaps.get("project") or 0)
        if warn:
            text = "⚠ " + " · ".join(warn)
        else:
            text = f"正常 · {info[0]}"
        self._meta.set_health(text, warn=bool(warn))
        # 恢复按钮：有快照才有意义
        self._restore_btn.setVisible(snap_n > 0)
        self._restore_btn.setToolTip(f"从快照恢复记忆（共 {snap_n} 份；每次覆盖写/清除前自动存）")
        drift = health.get("drift") or {}
        if drift.get("drifted"):
            self._drift_old = str(drift.get("old_workdir") or "")
            self._drift_label.setText(
                f"检测到旧工作目录的项目记忆（{int(drift.get('old_chars') or 0)} 字）：{self._drift_old}"
            )
            self._drift_row.setVisible(True)
        else:
            self._drift_row.setVisible(False)


# ── 经验分区 ────────────────────────────────────────────


class ExperienceSection(_Section):
    """经验：开关 + 分类卡片（查看/删除）+ 压缩整理（走对话提示词）。

    分类展示与记忆分区共用卡片形态（_SectionChip）：同一插件内同类信息用同一种载体，
    避免一处卡片一处列表的视觉割裂。
    """

    toggleExperience = pyqtSignal(bool)
    viewCategory = pyqtSignal(str)
    deleteCategoryRequested = pyqtSignal(str)
    consolidateRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("经验", parent)
        self._exp_switch = SwitchButton()
        self._exp_switch.setOnText("开")
        self._exp_switch.setOffText("关")
        self._exp_switch.checkedChanged.connect(self.toggleExperience.emit)
        self.set_context(self._exp_switch)

        self._body_wrap = QWidget()
        self._wrap_v = QVBoxLayout(self._body_wrap)
        self._wrap_v.setContentsMargins(0, 0, 0, 0)
        self._wrap_v.setSpacing(6)
        top_row = QHBoxLayout()
        self._hint = _hint("默认关闭。开启后助手可自主回忆/记录工作经验。")
        top_row.addWidget(self._hint, 1)
        cons_btn = QPushButton("压缩整理")
        cons_btn.setStyleSheet(_btn_style())
        cons_btn.setToolTip("向对话输入整理提示词：由模型读全库、合并去重、重新归类")
        cons_btn.clicked.connect(self.consolidateRequested.emit)
        top_row.addWidget(cons_btn)
        self._wrap_v.addLayout(top_row)

        self._chips_host = QWidget()
        self._chips_host.setStyleSheet("background: transparent;")
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        sp.setHeightForWidth(True)
        self._chips_host.setSizePolicy(sp)
        self._chips_row = _CenterFlowLayout(self._chips_host)
        self._chips_row.setSpacing(10)
        self._wrap_v.addWidget(self._chips_host)

        self._empty = _hint("（暂无经验分类）")
        self._wrap_v.addWidget(self._empty)
        self.body().addWidget(self._body_wrap)

    def set_enabled(self, on: bool) -> None:
        self._exp_switch.setChecked(on)
        # 与记忆一致：关闭后内容全部隐藏（含提示/按钮/分类列表），只留开关
        self._body_wrap.setVisible(bool(on))
        if on:
            self._hint.setText("已启用：助手可在对话中自主回忆/记录经验（recall/record_experience 工具）。")

    def reload_categories(self, docs: List[Dict[str, Any]]) -> None:
        while self._chips_row.count():
            item = self._chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._empty.setVisible(not docs)
        for doc in docs:
            cat = str(doc.get("category") or "")
            chip = _SectionChip(cat, f"{int(doc.get('count') or 0)} 条经验 · 点击查看与编辑")
            chip.clicked.connect(lambda c=cat: self.viewCategory.emit(c))
            chip.deleteRequested.connect(lambda c=cat: self.deleteCategoryRequested.emit(c))
            self._chips_row.addWidget(chip)
        self._sync_chips_height()

    def _sync_chips_height(self) -> None:
        """按当前宽度钉住卡片流行高（同记忆分区，理由见 MemorySection._sync_chips_height）。"""
        w = self._chips_host.width()
        if w <= 1:
            w = max(1, self._chips_host.sizeHint().width())
        rows_h = self._chips_row.heightForWidth(w)
        if rows_h > 0 and rows_h != self._chips_host.height():
            self._chips_host.setMinimumHeight(rows_h)
            self._chips_host.setMaximumHeight(rows_h)
            self._chips_host.updateGeometry()

    def _on_chips_host_resized(self) -> None:
        self._sync_chips_height()


# ── 预置工具分区 ────────────────────────────────────────

# 档位定义（key 与 Assistant.tool_access / schema_filter 对齐）：控制初始注入的
# 工具 schema，不影响可用性（未注入的可经 tool_search 搜索后照常调用）
_TOOL_MODES = (
    ("full", "全量", "初始注入全部工具", "[FULL]"),
    ("readonly", "只读", "初始只注入安全类工具", "[READ-ONLY]"),
    ("minimal", "极简", "bash+write+edit，其余不可用", "[MINIMAL]"),
    ("search", "仅搜索", "仅搜索+中转执行，其余搜索后调用", "[SEARCH]"),
    ("none", "无工具", "不注入任何工具", "[NONE]"),
)


class _ToolModeChip(QFrame):
    """单档卡片：视觉对齐人格 chips（名 + 两行描述 + tag 方角牌，选中 accent 边框）。"""

    clicked = pyqtSignal()

    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        self.setObjectName("toolModeChip")  # ⚠ 样式选择器依赖
        self.mode = mode
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(150, 112)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(5)
        name, desc, tag = next((n, d, t) for k, n, d, t in _TOOL_MODES if k == mode)
        self._name = QLabel(name)
        self._name.setAlignment(Qt.AlignCenter)
        v.addWidget(self._name)
        self._desc = QLabel()
        self._desc.setAlignment(Qt.AlignCenter)
        # 描述限制 2 行：按像素宽度手动截断（超出加 …）并钉死高度，防遮挡 tag
        # （手法与 _PersonaChip 一致；字号须与 QSS 实际渲染一致，含全局字体缩放 delta）
        fm = QFont(self._desc.font())
        fm.setPixelSize(scale_font_size(10))
        metrics = QFontMetrics(fm)
        self._desc.setText(_elide_lines(desc, metrics, 124, 2))
        self._desc.setFixedHeight(metrics.lineSpacing() * 2)
        self._desc.setWordWrap(False)
        v.addWidget(self._desc)
        v.addStretch()
        self._tag = QLabel(tag)
        self._tag.setAlignment(Qt.AlignCenter)
        v.addWidget(self._tag)
        self._apply_style()

    def _apply_style(self) -> None:
        border = Colors.TEXT_ACCENT if self._selected else Colors.BORDER
        bg = "rgba(245, 158, 11, 0.07)" if self._selected else "transparent"
        self.setStyleSheet(
            f"""
            QFrame#toolModeChip {{
                background: {bg};
                border: {"2px solid " + Colors.TEXT_ACCENT if self._selected else "1.5px solid " + border};
                border-radius: 12px;
            }}
            QFrame#toolModeChip:hover {{ border-color: {Colors.TEXT_ACCENT}; }}
            QFrame#toolModeChip QLabel {{ background: transparent; border: none; }}
        """
        )
        self._name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        self._desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        self._tag.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT if self._selected else Colors.TEXT_MUTED};"
            f"background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(9)}; letter-spacing: 1px;"
        )

    def set_selected(self, on: bool) -> None:
        if self._selected != on:
            self._selected = on
            self._apply_style()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self.clicked.emit()


class ToolAccessSection(_Section):
    """预置工具：三档 chips 卡快捷切换（控制初始注入的工具 schema，不影响可用性）。"""

    modeChangeRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("预置工具", parent)
        self.body().addWidget(
            _hint(
                "点卡片切换初始注入的工具范围，下一条消息生效；未注入的工具并非都能找回：含工具搜索的档位可经 tool_search 搜索、tool_execute 中转调用，极简/无工具档则不可。",
                10,
            )
        )
        self._mode = "full"
        self._chips: Dict[str, _ToolModeChip] = {}
        chips_host = QWidget()
        chips_host.setStyleSheet("background: transparent;")
        row = _CenterFlowLayout(chips_host)
        row.setSpacing(12)
        for key, _n, _d, _t in _TOOL_MODES:
            chip = _ToolModeChip(key)
            chip.clicked.connect(lambda k=key: self.modeChangeRequested.emit(k))
            row.addWidget(chip)
            self._chips[key] = chip
        self.body().addWidget(chips_host)
        self.set_mode("full")

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in self._chips else "full"
        for key, chip in self._chips.items():
            chip.set_selected(key == self._mode)
