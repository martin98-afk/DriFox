# -*- coding: utf-8 -*-
"""arc_stack.py — 助手弧形卡片堆叠（复刻 openhanako AgentCardStack 交互）

交互对齐原版 Settings.module.css .agent-card-stack 段：
- 收起态：62px 圆卡沿圆弧扇形叠放（等效原版 transform-origin: center 340px + rotate）
- hover/展开态：0.8s OutCubic 动画展开为直线均布，名字浮现；离开回收
- 选中卡：边框高亮 + 放大 1.06；主助手底部 accent ★ 徽章（主助手即不 @ 时
  的默认身份，无独立「当前」概念）
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
from PyQt5.QtWidgets import QLabel, QWidget

from app.utils.design_tokens import Animations, Colors, Shadows
from app.utils.motion import retarget

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
PAD = 8  # 卡片控件四周绘制余量：选中光环/缩放会画出内容区，无余量被控件边界裁剪
CARD_W = CARD_SIZE + 2 * PAD  # 卡片控件实际宽
CARD_H = CARD_SIZE + NAME_AREA + 2 * PAD  # 卡片控件实际高
ROW_GAP = 12  # 展开态多行换行时的行间距
SIDE_MARGIN = 12  # 展开态整行两侧最小留白（收起态弧宽钳制同源）

# 展开/收起动画时长已收敛到全局 token（Animations.SLOW_MS / ENTER_MS）。
# 旧值 800/600ms 是全局 180–300ms 语言的 3~4 倍，扇形重排显得慢半拍。
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
        self._avatar.move(3 + PAD, 3 + PAD)
        self._avatar.show()
        # 主助手徽章：独立子控件（z 序高于头像，不被遮挡），set_primary 时显示
        self._badge = QLabel("★", self)
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedSize(16, 16)
        self._badge.setStyleSheet(
            f"QLabel {{ background: {Colors.TEXT_ACCENT}; color: #FFFFFF;"
            f"border: 2px solid {Colors.CARD_BG_SOLID}; border-radius: 8px;"
            f"font-size: 9px; font-weight: bold; }}"
        )
        self._badge.hide()
        self.setFixedSize(CARD_W, CARD_H)
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
            if on:
                self._badge.show()
                self._badge.raise_()
            else:
                self._badge.hide()
            self._update_badge_geom()
            self.update()

    def _update_badge_geom(self) -> None:
        """徽章几何：底部中央，随 scale（绕圆心）与 lift 动画同步移动。"""
        size = 16
        cx = CARD_SIZE / 2
        cy = CARD_SIZE / 2 + (CARD_SIZE - 4 - CARD_SIZE / 2) * self._scale - self._lift
        self._badge.setGeometry(PAD + round(cx - size / 2), PAD + round(cy - size / 2), size, size)

    def set_avatar_image(self, image_path: Optional[str]) -> None:
        """换人格头像后轻量刷新单卡（不重建堆叠，保留动画状态）。"""
        self._avatar.set_image(image_path or None)

    def set_hover_lift(self, on: bool) -> None:
        self._animate_lift(LIFT_HOVER if on else 0.0)

    # ── 动画（QVariantAnimation 回调式：valueChanged 同步 avatar 子控件几何，
    #    避免 paintEvent 的 translate/scale 只作用于自绘部分、头像掉队）──
    def _apply_scale(self, v: float) -> None:
        self._scale = v
        # 围绕圆心缩放头像，与 paintEvent 的 scale 变换保持一致。
        # ⚠ 尺寸取偶：奇数尺寸中心落在 x.5，round 后偏离圆心 0.5px（视觉显歪）；
        # ⚠ 必须经 set_avatar_size 同步真实尺寸：setFixedSize 钳制下 setGeometry
        #   只改位置不改大小 → 位置按目标尺寸算、实际尺寸还是旧的 → 中心跑偏
        av = CARD_SIZE - 6
        size = int(round(av * v))
        if size % 2 == 1:
            size -= 1
        cx = cy = CARD_SIZE / 2
        x = PAD + round(cx - size / 2)
        y = PAD + round(cy - size / 2 - round(self._lift))
        self._avatar.set_avatar_size(size)
        self._avatar.setGeometry(x, y, size, size)
        self._update_badge_geom()
        self.update()

    def _apply_lift(self, v: float) -> None:
        self._lift = v
        self._avatar.move(3 + PAD, 3 + PAD - round(v))
        self._update_badge_geom()
        self.update()

    def _scaled_anim(self, attr: str, apply_cb) -> QVariantAnimation:
        """取（或惰性创建）缩放/上浮动画对象：同名属性全程只用一个动画

        ★ 旧实现每次 new 一个且不停旧的：连点/快速 hover 时新旧两条同时
        ``valueChanged`` 写同一个 ``_scale`` / ``_lift`` → 数值打架、非单调抖动。
        """
        key = f"{attr}_anim"
        anim = getattr(self, key, None)
        if anim is None:
            anim = QVariantAnimation(self)
            anim.valueChanged.connect(lambda v: apply_cb(float(v)))
            setattr(self, key, anim)
        return anim

    def _animate_scale(self, target: float) -> None:
        if not retarget(
            self._scaled_anim("scale", self._apply_scale),
            self._scale,
            target,
            duration=Animations.HOVER_MS,
            curve=Animations.EASE_HOVER,
        ):
            self._apply_scale(target)

    def _animate_lift(self, target: float) -> None:
        if not retarget(
            self._scaled_anim("lift", self._apply_lift),
            self._lift,
            target,
            duration=Animations.HOVER_MS,
            curve=Animations.EASE_HOVER,
        ):
            self._apply_lift(target)

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
        # 余量平移：光环/缩放画出内容区也不被控件边界裁剪（历史坑：光环被裁成弓形）
        p.translate(PAD, PAD)
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
        # 选中柔光环：外侧低透明度 accent 圈，柔化实色描边的生硬感
        if self._selected:
            glow = QColor(Colors.TEXT_ACCENT)
            glow.setAlpha(64)
            p.setPen(QPen(glow, 4))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(rect.adjusted(-2.5, -2.5, 2.5, 2.5))
        # 边框：选中 accent，其余灰
        border = QColor(Colors.TEXT_ACCENT) if self._selected else QColor(Colors.BORDER)
        p.setPen(QPen(border, 2))
        # ⚠ QColor 不认 "rgba(...)" 字符串（无效色不报错、绘制成黑），必须经 qcolor_from 解析
        p.setBrush(qcolor_from(Colors.CARD_BG.format(alpha=250)))
        p.drawEllipse(rect)
        # 名字（仅展开态显示，选中时 accent 强调）
        if self._expanded:
            p.setPen(QColor(Colors.TEXT_ACCENT if self._selected else Colors.TEXT_MUTED))
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
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.translate(PAD, PAD)
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

    def set_avatar(self, aid: str, avatar_path: str) -> None:
        """更新单卡头像（人格头像变更后由宿主调用）。"""
        for c in self._cards:
            if c.aid == aid:
                c.set_avatar_image(avatar_path or None)
                break

    def _sync_states(self) -> None:
        for c in self._cards:
            c.set_selected(c.aid == self._selected_aid)
            c.set_primary(c.aid == self._primary_aid)

    def _on_card_clicked(self, aid: str) -> None:
        self._selected_aid = aid
        self._sync_states()
        self.selectionChanged.emit(aid)

    # ── 布局与动画 ──
    def _row_capacity(self) -> int:
        """展开态单行容量：步长固定 SPREAD_STEP 不再压缩间距，放不下自动换行。"""
        avail = self.width() - 2 * SIDE_MARGIN
        if avail < CARD_SIZE:
            return 1
        return max(1, int((avail - CARD_SIZE) // SPREAD_STEP) + 1)

    def _expanded_rows(self) -> int:
        """展开态总行数（助手卡 + 「新建」卡按容量分行）。"""
        m = len(self._cards) + 1
        return max(1, math.ceil(m / self._row_capacity()))

    def _arc_deg_step(self) -> float:
        """收起态相邻卡圆心角：卡片多时钳制总弧宽不超容器（不再横向溢出）。"""
        half = len(self._cards) + 1 - 1
        if half <= 0:
            return ARC_SPREAD_DEG
        half = half / 2
        ratio = (self.width() - CARD_SIZE - 2 * SIDE_MARGIN) / 2 / ARC_RADIUS
        max_deg = ARC_SPREAD_DEG if ratio >= 1 else math.degrees(math.asin(max(0.0, ratio)))
        return min(ARC_SPREAD_DEG, max_deg / half)

    def _positions(self, expanded: bool) -> List[tuple]:
        """每张助手卡的 (x, y) 位置。

        展开态按容器宽自适应均布；「新建」卡作为第 n+1 个元素与助手卡
        共同参与居中（见 _add_position），整行视觉中心 = 容器中心。
        """
        n = len(self._cards)
        if n == 0:
            return []
        total_w = self.width()
        base_y = self.height() - REST_GAP - NAME_AREA - CARD_SIZE
        m = n + 1  # 助手卡 + 「新建」卡，整体对称分布
        if expanded:
            # 步长固定不压缩，放不下自动换行（行内居中，行距 ROW_GAP 向上叠）
            cap = self._row_capacity()
            out = []
            for i in range(n):
                row, idx = divmod(i, cap)
                k = min(cap, m - row * cap)  # 该行卡片数
                row_w = SPREAD_STEP * (k - 1) + CARD_SIZE
                x0 = (total_w - row_w) / 2
                out.append((x0 + idx * SPREAD_STEP, base_y - row * (CARD_SIZE + ROW_GAP)))
            return out
        # 收起态：绕 (cx, base_y + CARD_SIZE/2 + ARC_RADIUS) 旋转 ±deg_step（多卡时钳制）
        cx = total_w / 2 - CARD_SIZE / 2
        origin_y = base_y + CARD_SIZE / 2 + ARC_RADIUS
        deg_step = self._arc_deg_step()
        out = []
        for i in range(n):
            deg = (i - (m - 1) / 2) * deg_step
            rad = math.radians(deg)
            # 旋转 CARD 中心相对 origin 的位置（半径 ARC_RADIUS，垂直向上）
            px = cx + CARD_SIZE / 2 + ARC_RADIUS * math.sin(rad) - CARD_SIZE / 2
            py = origin_y - ARC_RADIUS * math.cos(rad) - CARD_SIZE / 2
            out.append((px, py))
        return out

    def _add_position(self, expanded: bool) -> tuple:
        """「新建」卡位置：与助手卡同一几何体系的第 n+1 个元素（整体居中）。"""
        n = len(self._cards)
        total_w = self.width()
        base_y = self.height() - REST_GAP - NAME_AREA - CARD_SIZE
        m = n + 1
        if expanded:
            cap = self._row_capacity()
            row, idx = divmod(n, cap)
            k = min(cap, m - row * cap)
            row_w = SPREAD_STEP * (k - 1) + CARD_SIZE
            x0 = (total_w - row_w) / 2
            return (x0 + idx * SPREAD_STEP, base_y - row * (CARD_SIZE + ROW_GAP))
        cx = total_w / 2 - CARD_SIZE / 2
        origin_y = base_y + CARD_SIZE / 2 + ARC_RADIUS
        rad = math.radians((n - (m - 1) / 2) * self._arc_deg_step())
        px = cx + ARC_RADIUS * math.sin(rad)
        py = origin_y - ARC_RADIUS * math.cos(rad) - CARD_SIZE / 2
        return (px, py)

    def _relayout(self, animate: bool) -> None:
        # 展开态多行时容器动态增高（收起恢复单行高度），外层布局自动下推滚动区
        rows = self._expanded_rows() if self._expanded else 1
        target_h = CONTAINER_H + (rows - 1) * (CARD_SIZE + ROW_GAP)
        if self.height() != target_h:
            self.setFixedHeight(target_h)
        positions = self._positions(self._expanded)
        base_y = self.height() - REST_GAP - NAME_AREA - CARD_SIZE
        for card in self._cards:
            card.set_expanded(self._expanded)
        add_x = None
        if self._add_card is not None:
            if positions:
                add_x, add_y = self._add_position(self._expanded)
            else:
                # 仅剩「新建」卡（助手被清空）：单独居中
                add_x = self.width() / 2 - CARD_SIZE / 2
                add_y = base_y
        self._anims.stop()
        # ★ 复用同一个 group：旧实现每次 relayout 都 new 一个 group，旧的及其
        # N 条子动画挂在 self 上永不删除 → 每次 hover 泄漏一组动画对象。
        # clear() 会移除并删除上一次的子动画，再重新装填。
        self._anims.clear()
        # 展开/收起时长走全局 token：原 800/600ms 是全局 180–300ms 语言的
        # 3~4 倍，扇形重排会显得"慢半拍"。
        duration = Animations.SLOW_MS if self._expanded else Animations.ENTER_MS
        curve = Animations.EASE_ENTER if self._expanded else Animations.EASE_EXIT
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
            tx, ty = tx - PAD, ty - PAD  # 布局给内容区左上角，控件左上角再退 PAD 余量
            card.raise_()
            if animate and Animations.motion_enabled():
                anim = Anim(card, b"pos")
                anim.setDuration(duration)
                anim.setEasingCurve(QEasingCurve(curve))
                anim.setStartValue(card.pos())
                anim.setEndValue(QPoint(int(tx), int(ty)))
                self._anims.addAnimation(anim)
            else:
                card.move(int(tx), int(ty))
        if self._add_card is not None and add_x is not None:
            if self._expanded:
                self._add_card.raise_()
            else:
                # 收起态：新建卡压在扇形最下层（z 轴正确层级），hover 展开时才抬起
                self._add_card.lower()
            add_x, add_y = add_x - PAD, add_y - PAD
            if animate and Animations.motion_enabled():
                anim = Anim(self._add_card, b"pos")
                anim.setDuration(duration)
                anim.setEasingCurve(QEasingCurve(curve))
                anim.setStartValue(self._add_card.pos())
                anim.setEndValue(QPoint(int(add_x), int(add_y)))
                self._anims.addAnimation(anim)
            else:
                self._add_card.move(int(add_x), int(add_y))
        if self._anims.animationCount():
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
