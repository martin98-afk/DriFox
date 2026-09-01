# -*- coding: utf-8 -*-
"""arc_stack.py — 助手弧形卡片堆叠（复刻 openhanako AgentCardStack 交互）

交互对齐原版 Settings.module.css .agent-card-stack 段：
- 收起态：62px 圆卡沿圆弧扇形叠放（等效原版 transform-origin: center 340px + rotate）
- hover/展开态：0.8s OutCubic 动画展开为直线均布，名字浮现；离开回收
- 选中卡：边框高亮 + 放大 1.06；主助手底部 accent 小圆点
- 末尾「+」虚线圆卡：新建助手
- 卡片下方操作行由宿主（assistant_card）自行布局，本组件只发信号
"""

from __future__ import annotations

import math
from typing import List, Optional

from PyQt5.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QVariantAnimation,
    QPoint,
    QRectF,
    Qt,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from app.utils.design_tokens import Colors, Shadows

from .assistant_avatar import RoundAvatar, qcolor_from

# ── 几何常量（single source of truth，对齐原版 CSS 变量）──
CARD_SIZE = 62  # 圆卡直径
ARC_RADIUS = 340  # 扇形弧半径（原版 transform-origin center 340px）
ARC_SPREAD_DEG = 4.0  # 收起态相邻卡圆心角
SPREAD_STEP = 70  # 展开态相邻卡水平间距
REST_GAP = 14  # 卡片底部留白
NAME_AREA = 22  # 名字行高
ARC_HEADROOM = 32  # 顶部弧度余量（收起态上摆溢出）
LIFT_HOVER = 6  # 单卡悬停上浮
CONTAINER_H = ARC_HEADROOM + CARD_SIZE + REST_GAP + NAME_AREA

_DUR_EXPAND = 800  # 展开动画时长（原版 0.8s ease-out）
_DUR_COLLAPSE = 600
Anim = QPropertyAnimation


class _AgentCard(QWidget):
    """单张卡片：圆形头像 + 名字 + 选中/主助手/悬停态。"""

    clicked = pyqtSignal()

    def __init__(self, aid: str, name: str, color: str, image_path: str, parent=None):
        super().__init__(parent)
        self.aid = aid
        self.name = name
        self._selected = False
        self._primary = False
        self._hover = False
        self._lift = 0.0
        self._scale = 1.0
        self._expanded = False  # 容器展开时才画名字（收起态名字重叠）
        self._avatar = RoundAvatar(
            size=CARD_SIZE - 6, text=name, color=color, image_path=image_path or None, parent=self
        )
        self._avatar.move(3, 3)
        self._avatar.show()
        self.setFixedSize(CARD_SIZE, CARD_SIZE + NAME_AREA)
        self.setCursor(Qt.PointingHandCursor)

    # ── 状态 ──
    def set_selected(self, on: bool) -> None:
        if self._selected != on:
            self._selected = on
            self._animate_scale(1.06 if on else 1.0)
            self.update()

    def set_expanded(self, on: bool) -> None:
        if self._expanded != on:
            self._expanded = on
            self.update()

    def set_primary(self, on: bool) -> None:
        if self._primary != on:
            self._primary = on
            self.update()

    def set_hover_lift(self, on: bool) -> None:
        self._animate_lift(LIFT_HOVER if on else 0.0)

    # ── 动画（QVariantAnimation 回调式：valueChanged 同步 avatar 子控件几何，
    #    避免 paintEvent 的 translate/scale 只作用于自绘部分、头像掉队）──
    def _apply_scale(self, v: float) -> None:
        self._scale = v
        # 围绕圆心缩放头像，与 paintEvent 的 scale 变换保持一致
        av = CARD_SIZE - 6
        size = av * v
        cx = cy = CARD_SIZE / 2
        x = cx - size / 2
        y = cy - size / 2 - round(self._lift)
        self._avatar.setGeometry(round(x), round(y), round(size), round(size))
        self.update()

    def _apply_lift(self, v: float) -> None:
        self._lift = v
        self._avatar.move(3, 3 - round(v))
        self.update()

    def _animate_scale(self, target: float) -> None:
        anim = QVariantAnimation(self)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self._scale)
        anim.setEndValue(target)
        anim.valueChanged.connect(lambda v: self._apply_scale(float(v)))
        anim.finished.connect(anim.deleteLater)
        anim.start()

    def _animate_lift(self, target: float) -> None:
        anim = QVariantAnimation(self)
        anim.setDuration(160)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self._lift)
        anim.setEndValue(target)
        anim.valueChanged.connect(lambda v: self._apply_lift(float(v)))
        anim.finished.connect(anim.deleteLater)
        anim.start()

    # ── 事件 ──
    def enterEvent(self, e):  # noqa: N802
        self._hover = True
        self.set_hover_lift(True)
        self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = False
        self.set_hover_lift(False)
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit()

    # ── 绘制 ──
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 缩放绘制（选中 1.06）
        if self._scale != 1.0:
            p.translate(self.width() / 2, CARD_SIZE / 2)
            p.scale(self._scale, self._scale)
            p.translate(-self.width() / 2, -CARD_SIZE / 2)
        p.translate(0, -self._lift)

        # 底框（原版 2.5px bg-card 描边 + 阴影）
        rect = QRectF(1.5, 1.5, CARD_SIZE - 3, CARD_SIZE - 3)
        shadow = Shadows.CARD if not self._hover else Shadows.FLOATING
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, shadow.get("alpha", 40) if isinstance(shadow, dict) else 40))
        p.drawEllipse(rect.adjusted(0, 2, 0, 2))
        # 边框
        # 选中卡：accent 粗边框（比灰色更醒目）
        if self._selected:
            border = QColor(Colors.TEXT_ACCENT)
        else:
            border = QColor(Colors.BORDER)
        p.setPen(QPen(border, 2.5 if self._selected else 2))
        # ⚠ QColor 不认 "rgba(...)" 字符串（无效色不报错、绘制成黑），必须经 qcolor_from 解析
        p.setBrush(qcolor_from(Colors.CARD_BG.format(alpha=250)))
        p.drawEllipse(rect)
        # 主助手角标（底部 accent 圆点）
        if self._primary:
            p.setPen(QPen(qcolor_from(Colors.CARD_BG_SOLID), 1.5))
            p.setBrush(QColor(Colors.TEXT_ACCENT))
            p.drawEllipse(int(CARD_SIZE / 2 - 4), CARD_SIZE - 8, 8, 8)
        # 名字（仅展开态显示，对齐原版 agent-card-name opacity 切换）
        if self._expanded:
            p.setPen(QColor(Colors.TEXT_MUTED if not self._selected else Colors.TEXT_PRIMARY))
            font = self.font()
            font.setPixelSize(11)
            p.setFont(font)
            p.drawText(QRectF(0, CARD_SIZE + 2, CARD_SIZE, NAME_AREA), Qt.AlignCenter, self.name)
        p.end()


class _AddCard(QWidget):
    """「+」新建卡：虚线圆边框，accent 色。"""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(CARD_SIZE, CARD_SIZE + NAME_AREA)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(2, 2, CARD_SIZE - 4, CARD_SIZE - 4)
        pen = QPen(QColor(Colors.TEXT_ACCENT), 1.5, Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)
        p.setPen(QColor(Colors.TEXT_ACCENT))
        font = self.font()
        font.setPixelSize(22)
        p.setFont(font)
        p.drawText(QRectF(0, 0, CARD_SIZE, CARD_SIZE), Qt.AlignCenter, "+")
        p.setPen(QColor(Colors.TEXT_MUTED))
        font2 = self.font()
        font2.setPixelSize(10)
        p.setFont(font2)
        p.drawText(QRectF(0, CARD_SIZE + 2, CARD_SIZE, NAME_AREA), Qt.AlignCenter, "新建")
        p.end()

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit()


class ArcCardStack(QWidget):
    """弧形卡片堆叠容器。

    信号：
        selectionChanged(str)  — 点击某张卡片
        createRequested()      — 点击「+」
    """

    selectionChanged = pyqtSignal(str)
    createRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: List[_AgentCard] = []
        self._add_card: Optional[_AddCard] = None
        self._selected_aid = ""
        self._primary_aid = ""
        self._expanded = False
        self._anims: QParallelAnimationGroup = QParallelAnimationGroup(self)
        self.setFixedHeight(CONTAINER_H)
        self.setMinimumWidth(240)
        self.setMouseTracking(True)

    # ── 数据 ──
    def set_assistants(self, items: List[dict]) -> None:
        """items: [{id,name,color,avatar_path}]（顺序即排列顺序）"""
        for c in self._cards:
            c.deleteLater()
        self._cards.clear()
        for it in items:
            card = _AgentCard(it["id"], it["name"], it.get("color", "#7C3AED"), it.get("avatar_path", ""), self)
            card.clicked.connect(lambda aid=it["id"]: self._on_card_clicked(aid))
            card.installEventFilter(self)
            card.show()  # ⚠ 父容器已可见后重建的子控件必须显式 show，否则整排"消失"
            self._cards.append(card)
        if self._add_card is None:
            self._add_card = _AddCard(self)
            self._add_card.clicked.connect(self.createRequested.emit)
            self._add_card.show()
        self._selected_aid = items[0]["id"] if items else ""
        self._relayout(animate=False)

    def set_selected(self, aid: str) -> None:
        if self._selected_aid == aid:
            return
        self._selected_aid = aid
        self._sync_states()
        self._relayout(animate=True)

    def set_primary(self, aid: str) -> None:
        self._primary_aid = aid
        self._sync_states()

    def _sync_states(self) -> None:
        for c in self._cards:
            c.set_selected(c.aid == self._selected_aid)
            c.set_primary(c.aid == self._primary_aid)

    def _on_card_clicked(self, aid: str) -> None:
        self._selected_aid = aid
        self._sync_states()
        self.selectionChanged.emit(aid)

    # ── 布局与动画 ──
    def _positions(self, expanded: bool) -> List[tuple]:
        """每张卡的 (x, y) 位置。展开态按容器宽自适应均布（助手多也不会溢出被遮）。"""
        n = len(self._cards)
        if n == 0:
            return []
        total_w = self.width()
        base_y = self.height() - REST_GAP - NAME_AREA - CARD_SIZE
        if expanded:
            # 自适应步长：优先 SPREAD_STEP，超出容器宽则压缩（最小 34 防重叠）
            if n > 1:
                step = min(SPREAD_STEP, max(34.0, (total_w - CARD_SIZE - 24) / (n - 1)))
            else:
                step = 0.0
            spread = step * (n - 1)
            x0 = (total_w - spread) / 2 - CARD_SIZE / 2
            return [(x0 + i * step, base_y) for i in range(n)]
        # 收起态：绕 (cx, base_y + CARD_SIZE/2 + ARC_RADIUS) 旋转 ±ARC_SPREAD_DEG
        cx = total_w / 2 - CARD_SIZE / 2
        origin_y = base_y + CARD_SIZE / 2 + ARC_RADIUS
        out = []
        for i in range(n):
            deg = (i - (n - 1) / 2) * ARC_SPREAD_DEG
            rad = math.radians(deg)
            # 旋转 CARD 中心相对 origin 的位置（半径 ARC_RADIUS，垂直向上）
            px = cx + CARD_SIZE / 2 + ARC_RADIUS * math.sin(rad) - CARD_SIZE / 2
            py = origin_y - ARC_RADIUS * math.cos(rad) - CARD_SIZE / 2
            out.append((px, py))
        return out

    def _expanded_step(self, n: int) -> float:
        """展开态相邻卡间距（自适应容器宽，与 _positions 同口径）。"""
        if n <= 1:
            return 0.0
        return min(SPREAD_STEP, max(34.0, (self.width() - CARD_SIZE - 24) / (n - 1)))

    def _relayout(self, animate: bool) -> None:
        positions = self._positions(self._expanded)
        n = len(self._cards)
        for card in self._cards:
            card.set_expanded(self._expanded)
        add_x = None
        if self._add_card is not None:
            if n > 0 and positions:
                last_x, last_y = positions[-1]
                add_x = last_x + self._expanded_step(n) if self._expanded else last_x + 26
            else:
                add_x = self.width() / 2 - CARD_SIZE / 2
        base_y = self.height() - REST_GAP - NAME_AREA - CARD_SIZE
        self._anims.stop()
        self._anims = QParallelAnimationGroup(self)
        # z 序：从右往左 raise → 左侧盖右侧；选中卡最后 raise（最顶层）
        ordered = list(reversed(list(enumerate(self._cards))))
        if self._selected_aid:
            ordered = [(i, c) for i, c in ordered if c.aid != self._selected_aid]
            for i, c in enumerate(self._cards):
                if c.aid == self._selected_aid:
                    ordered.append((i, c))
                    break
        for i, card in ordered:
            if i >= len(positions):
                continue
            tx, ty = positions[i]
            card.raise_()
            if animate:
                anim = Anim(card, b"pos")
                anim.setDuration(_DUR_EXPAND if self._expanded else _DUR_COLLAPSE)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.setStartValue(card.pos())
                anim.setEndValue(QPoint(int(tx), int(ty)))
                self._anims.addAnimation(anim)
            else:
                card.move(int(tx), int(ty))
        if self._add_card is not None and add_x is not None:
            self._add_card.raise_()
            if animate:
                anim = Anim(self._add_card, b"pos")
                anim.setDuration(_DUR_EXPAND if self._expanded else _DUR_COLLAPSE)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.setStartValue(self._add_card.pos())
                anim.setEndValue(QPoint(int(add_x), int(base_y)))
                self._anims.addAnimation(anim)
            else:
                self._add_card.move(int(add_x), int(base_y))
        self._anims.start()

    # ── 事件 ──
    def enterEvent(self, e):  # noqa: N802
        self._expanded = True
        self._relayout(animate=True)

    def leaveEvent(self, e):  # noqa: N802
        self._expanded = False
        self._relayout(animate=True)

    def resizeEvent(self, e):  # noqa: N802
        self._relayout(animate=False)


if __name__ == "__main__":  # 预览入口
    import sys

    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    Colors.refresh()
    w = QWidget()
    w.setWindowTitle("ArcCardStack 预览")
    w.setStyleSheet(f"background: {Colors.CONTENT_BG};")
    stack = ArcCardStack(w)
    stack.resize(500, CONTAINER_H)
    stack.move(30, 30)
    stack.set_assistants(
        [
            {"id": "a", "name": "小狐", "color": "#7C3AED", "avatar_path": ""},
            {"id": "b", "name": "hanako", "color": "#DB2777", "avatar_path": ""},
            {"id": "c", "name": "build", "color": "#0284C7", "avatar_path": ""},
        ]
    )
    stack.set_primary("a")
    w.resize(560, 240)
    w.show()
    sys.exit(app.exec_())
