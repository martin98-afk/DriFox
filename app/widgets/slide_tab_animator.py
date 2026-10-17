# -*- coding: utf-8 -*-
"""SlideTabAnimator — 给 QStackedWidget 补上「长条模型」方向性滑动切页。

## 为什么是两段式

QStackedWidget 同一时刻只显示一页（hidden→visible 传播），无法像并排页那样
做 push 滑动。这里用两段式达成等价观感：

1. 段1：整个 stack 带着**旧页画面**向出场侧滑出画外（旧内容真实跟随滑动）；
2. 段间（完全出画）：``setCurrentIndex`` 换页 + 可选回调（新页首次构建 /
   数据刷新等重活都在画外做，阻塞被掩蔽）；
3. 段2：stack 瞬移到进场侧，带着**新页画面**滑入归位。

## 时长与曲线

两段式是双份成本：两段都取 ``SLOW_MS``(300) 意味着一次切页吃掉 600ms，
且同用 OutCubic 使每段末尾各留一段长尾，观感是「滑一半顿一下再滑一半」。
改按方向语义：段1 用 ``EXIT_MS`` + InCubic（加速带出，离场无需减速收尾），
段2 用 ``ENTER_MS`` + OutCubic（减速进场，让眼睛跟上新页），两段接成一次
连续位移。

## 几何管理

动画期间必须把 stack 从宿主布局里摘除并手动 ``setGeometry``/``move``：
留在布局里时，布局管理器每次 activate 都会把子项几何拉回原位，滑动失效。
收尾按摘除前的布局位置放回（``insertWidget`` 保持原有次序）。

## 复用点

- ``app/widgets/workbench_panel.py``（右侧工作台页签）
- ``app/widgets/cards/card_stack_container.py``（停靠区堆叠卡 Pivot）
"""

from typing import Callable, Optional

from PySide6.QtCore import QObject, QRect, QVariantAnimation
from PySide6.QtWidgets import QLayout, QStackedWidget

from app.utils.design_tokens import Animations


class SlideTabAnimator(QObject):
    """QStackedWidget 的方向性两段式滑动切页控制器（每栈一个实例）。"""

    # 两段各自的时长：离场快（不拖沓）、进场稍长（看清新页），合计 ~390ms
    SLIDE_OUT_MS = Animations.EXIT_MS
    SLIDE_IN_MS = Animations.ENTER_MS

    def __init__(self, stack: QStackedWidget, layout: QLayout, parent: Optional[QObject] = None):
        super().__init__(parent if parent is not None else stack)
        self._stack = stack
        self._layout = layout
        self._anim: Optional[QVariantAnimation] = None
        self._geo = QRect()
        self._layout_index = -1
        self._target = -1
        self._dir = 0
        self._phase = ""  # "out" 滑出 / "in" 滑入
        self._on_switched: Optional[Callable[[], None]] = None

    # ── 状态 ──

    def is_running(self) -> bool:
        """是否有滑动在途（调用方可据此避免叠加动画）"""
        return self._anim is not None and self._anim.state() == QVariantAnimation.Running

    # ── 切换 ──

    def switch(self, index: int, direction: int, on_switched: Optional[Callable[[], None]] = None) -> bool:
        """尝试以滑动方式切到 ``index``。

        Args:
            index: 目标页索引（越界或等于当前页时为无操作）
            direction: +1 = 新页在右（视口右移，新页从右侧进场）/ -1 反之
            on_switched: 段间回调（换页后、滑入前执行；重活放这里）

        Returns:
            True 表示已接手切换（调用方**不要**再自行 setCurrentIndex）；
            False 表示条件不满足（无方向 / 系统要求减少动态效果 / 无操作），
            调用方按原逻辑直切。
        """
        stack = self._stack
        if not (0 <= index < stack.count()) or index == stack.currentIndex():
            return False
        if direction == 0 or not Animations.motion_enabled():
            return False
        if self.is_running():
            self._finish(interrupted=True)
        self._target = index
        self._dir = 1 if direction > 0 else -1
        self._on_switched = on_switched
        self._phase = "out"
        self._detach()
        self._start_anim()
        return True

    def finish_now(self) -> None:
        """立即收敛到终态（外部打断：面板隐藏 / 宽度动画 / 页面被移除等）"""
        if self._anim is not None:
            self._finish(interrupted=True)

    # ── 内部 ──

    def _detach(self) -> None:
        """从布局摘除并锁定当前几何，动画期间由本类手动摆放"""
        stack = self._stack
        self._geo = stack.geometry()
        self._layout_index = self._layout.indexOf(stack)
        self._layout.removeWidget(stack)
        stack.setGeometry(self._geo)
        stack.raise_()

    def _reattach(self) -> None:
        """按摘除前的布局次序放回"""
        stack = self._stack
        idx = self._layout_index
        if idx >= 0 and idx <= self._layout.count():
            self._layout.insertWidget(idx, stack)
        else:
            self._layout.addWidget(stack)
        self._layout_index = -1
        stack.setGeometry(self._geo)

    def _start_anim(self) -> None:
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        if self._phase == "in":
            anim.setDuration(self.SLIDE_IN_MS)
            anim.setEasingCurve(Animations.EASE_ENTER)
        else:
            anim.setDuration(self.SLIDE_OUT_MS)
            # 离场用 InCubic 加速带出：OutCubic 的减速长尾落在画外，纯属白等
            anim.setEasingCurve(Animations.EASE_IN)
        anim.valueChanged.connect(self._on_tick)
        anim.finished.connect(self._on_phase_done)
        self._anim = anim
        anim.start()

    def _on_tick(self, t: float) -> None:
        w = self._geo.width()
        if self._phase == "out":
            # 旧页画面向出场侧滑出
            self._stack.move(self._geo.x() - round(w * self._dir * t), self._geo.y())
        else:
            # 新页从进场侧滑入
            self._stack.move(self._geo.x() + round(w * self._dir * (1.0 - t)), self._geo.y())

    def _on_phase_done(self) -> None:
        if self._phase == "out":
            self._swap_page()
            if not self.isVisible_stack():
                # 面板在槽间被隐藏/销毁：不再做滑入，直接收敛
                self._finish(interrupted=True)
                return
            self._phase = "in"
            self._stack.move(self._geo.x() + self._geo.width() * self._dir, self._geo.y())
            self._start_anim()
            return
        self._finish(interrupted=False)

    def isVisible_stack(self) -> bool:
        """栈是否仍可安全动画（探活 + 可见性）；面板隐藏时补间无意义"""
        try:
            return self._stack.isVisible()
        except RuntimeError:
            return False

    def _swap_page(self) -> None:
        """段间：画外换页并执行回调（新页构建 / 刷新阻塞被掩蔽）"""
        cb = self._on_switched
        self._on_switched = None
        try:
            self._stack.setCurrentIndex(self._target)
        except RuntimeError:
            return
        if cb is not None:
            cb()

    def _finish(self, interrupted: bool) -> None:
        """收尾（幂等）：停动画、必要时补换页、按原布局位置放回并归位"""
        anim = self._anim
        if anim is not None:
            try:
                anim.finished.disconnect(self._on_phase_done)
                anim.valueChanged.disconnect(self._on_tick)
            except (TypeError, RuntimeError):
                pass
            anim.stop()
        self._anim = None
        # 先取出待用状态再清空，避免补换页回调读到已归零的 target
        need_swap = self._phase == "out" and self._target >= 0
        target = self._target
        cb = self._on_switched
        self._phase = ""
        self._on_switched = None
        self._target = -1
        self._dir = 0
        if need_swap:
            # 段1 被打断：仍完成换页，杜绝「高亮/信号已切、内容未切」的状态漂移
            try:
                if 0 <= target < self._stack.count():
                    self._stack.setCurrentIndex(target)
                    if cb is not None:
                        cb()
            except RuntimeError:
                pass
        try:
            self._reattach()
        except RuntimeError:
            pass
