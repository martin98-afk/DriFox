# -*- coding: utf-8 -*-
"""统一动效工具 —— 全项目动画的**唯一实现入口**

## 为什么需要这一层

改造前全项目 42 处动画各自手写 ``QPropertyAnimation``，重复踩同一批坑：

| 坑 | 表现 | 本模块怎么兜 |
|---|---|---|
| 反向跳变 | 动画进行中再触发 → 先跳到终值再动 | ``retarget()`` 一律从**当前实测值**起步 |
| 新旧打架 | 新建动画但不 stop 旧的 → 两个动画同写一属性，抖动 | ``retarget()`` 复用同一个动画对象 |
| 无障碍缺失 | 42 处仅 6 处检查「减少动态效果」 | 本模块所有入口统一检查 |
| 终态残留 | ``setFixedHeight`` 后只放开 max 不开 min → 永久卡死 | ``HeightAnimator`` 结束成对恢复 min/max |
| 亚像素抖动 | <1px 的变化也走 200ms 动画 | ``retarget()`` 低于阈值直接置终值 |

用法约定：**控件上只存一个动画对象，反复 retarget，不要每次 new。**

## 时长/曲线怎么选

见 ``design_tokens.Animations``。要点：

- 展开/进入 ``ENTER_MS`` + ``EASE_OUT``；收起/退出 ``EXIT_MS`` + ``EASE_IN``
- hover ``HOVER_MS`` + ``EASE_HOVER``（不是 ``EASE_IN_OUT``，两头慢会发钝）
- 箭头/指示器旋转必须与被指示内容的时长**一致**，否则出现
  「箭头转完了内容还在长」的割裂

## 循环动画

``LoopTimer`` 是常驻循环动画（呼吸、扫描、旋转、帧动画）的唯一驱动：
不可见 / 系统减少动效 / 自定义门控为假时**跳过回调**，不白烧 CPU。
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import QEasingCurve, QObject, QPropertyAnimation, QTimer, QVariantAnimation, pyqtProperty, pyqtSignal

from app.utils.design_tokens import Animations

__all__ = [
    "Animations",
    "HeightAnimator",
    "LoopTimer",
    "cross_fade",
    "fade_widget",
    "retarget",
]

# 小于该差值认为「已经到位」，直接置终值不启动动画。
# 目的：避免亚像素变化触发一次完整动画（视觉上是无意义的抖动）。
_EPSILON = 1.0


def retarget(
    anim: QVariantAnimation,
    current: float,
    target: float,
    *,
    duration: Optional[int] = None,
    curve: Optional[QEasingCurve.Type] = None,
    on_finished: Optional[Callable[[], None]] = None,
) -> bool:
    """把动画重定向到新目标：**从当前实测值起步**，全程只用一个动画对象

    这是全项目动画的统一启动方式。它同时解决三件事：

    1. **续接**：``current`` 由调用方从控件实测（``widget.height()`` /
       ``effect.opacity()`` / ``self._angle``），而不是读动画的内部值 ——
       后者在 stop 后会跳到终值，正是「反向先跳一下」的来源。
    2. **互斥**：复用传入的 ``anim``，先 ``stop()`` 旧的再设新值，不存在
       两个动画同时写同一属性的情况。
    3. **无障碍**：系统「减少动态效果」或差值过小 → 不启动动画，返回
       ``False``，由调用方把属性直接置为终值（保留状态反馈，去掉运动过程）。

    Args:
        anim: 复用的动画对象（构造时已挂 parent）
        current: 当前实际值（务必实测，不要用 ``anim.currentValue()``）
        target: 目标值
        duration: 时长（ms），None → ``Animations.ENTER_MS``
        curve: 缓动曲线，None → ``Animations.EASE_OUT``
        on_finished: 结束回调（重定向前会先断开上一次的回调，避免叠加）

    Returns:
        True = 已启动动画；False = 无需动画，调用方应直接置终值
    """
    d = Animations.ENTER_MS if duration is None else duration
    if not Animations.motion_enabled() or d <= 0 or abs(float(target) - float(current)) < _EPSILON:
        return False
    try:
        # ★ finished 必须无条件先断开：上一次重定向挂的回调（典型是「收起完
        # 成后回收/reparent」）若留着，本次打断后会误触发，把还在用的控件
        # 提前收走。需要长期连接的场景不要走 retarget。
        try:
            anim.finished.disconnect()
        except (TypeError, RuntimeError):
            pass
        if on_finished is not None:
            anim.finished.connect(on_finished)
        anim.stop()
        anim.setDuration(int(d))
        anim.setEasingCurve(QEasingCurve(Animations.EASE_OUT if curve is None else curve))
        anim.setStartValue(float(current))
        anim.setEndValue(float(target))
        anim.start()
    except RuntimeError:
        return False  # 宿主已销毁
    return True


class _HeightDriver(QObject):
    """高度动画驱动：逐帧 ``setFixedHeight``（不用 maximumHeight）

    ``maximumHeight`` 只是给父布局一个上限，控件实际高度仍由布局施舍；控件
    脱离布局或带最小高度约束的子控件时动画等于没跑（或每帧重排 → 双向收缩）。
    用中间对象驱动 + ``setFixedHeight``，高度完全由动画掌控。
    """

    def __init__(self, widget):
        super().__init__(widget)
        self._w = widget
        self._value = 0

    def _get(self) -> int:
        return self._value

    def _set(self, value) -> None:
        self._value = int(value)
        try:
            self._w.setFixedHeight(self._value)
        except RuntimeError:
            pass  # 控件已销毁

    value = pyqtProperty(int, _get, _set)


class HeightAnimator:
    """高度展开/折叠动画：复用驱动 + 续接 + 终态自动放开约束

    典型用法（展开/收起同一个区域）::

        animator = HeightAnimator(widget)
        if not animator.animate_to(target, on_finished=self._on_done):
            widget.setFixedHeight(target)  # 返回 False 时自行定格
            self._on_done()

    动画期间走 ``setFixedHeight``（min/max 同时收紧）；结束时 **min 与 max
    成对恢复** —— 只放开 max 会让控件永久卡死在动画末值高度。
    """

    #: Qt 的「不限制」上界
    UNLIMITED = 16777215

    def __init__(self, widget):
        self._w = widget
        self._driver = _HeightDriver(widget)
        self._anim = QPropertyAnimation(self._driver, b"value", widget)

    @property
    def running(self) -> bool:
        return self._anim.state() == QPropertyAnimation.Running

    def animate_to(
        self,
        target: int,
        *,
        duration: Optional[int] = None,
        curve: Optional[QEasingCurve.Type] = None,
        on_finished: Optional[Callable[[], None]] = None,
        expanded: bool = True,
    ) -> bool:
        """把高度动画到 ``target``

        Args:
            target: 目标高度（px）
            duration: 时长，None → 展开 ``ENTER_MS`` / 收起 ``EXIT_MS``
            curve: 曲线，None → 展开 ``EASE_OUT`` / 收起 ``EASE_IN``
            on_finished: 结束回调（在 min/max 恢复**之后**调用）
            expanded: 方向（决定默认时长/曲线）

        Returns:
            True = 已在动画中；False = 未启动，调用方需自行 ``setFixedHeight``
        """
        widget = self._w
        try:
            current = widget.height()
        except RuntimeError:
            return False

        if duration is None:
            duration = Animations.ENTER_MS if expanded else Animations.EXIT_MS
        if curve is None:
            curve = Animations.EASE_OUT if expanded else Animations.EASE_IN

        def _finish() -> None:
            self.release()
            if on_finished is not None:
                on_finished()

        if not retarget(self._anim, current, int(target), duration=duration, curve=curve, on_finished=_finish):
            return False
        return True

    def release(self) -> None:
        """放开动画期间收紧的高度约束（min 与 max 必须成对恢复）"""
        try:
            self._w.setMinimumHeight(0)
            self._w.setMaximumHeight(self.UNLIMITED)
        except RuntimeError:
            pass

    def stop(self) -> None:
        """停止动画（**不**自动置终值：续接场景下起点由调用方实测）"""
        try:
            self._anim.stop()
        except RuntimeError:
            pass


def fade_widget(
    widget,
    target: float,
    *,
    duration: Optional[int] = None,
    on_finished: Optional[Callable[[], None]] = None,
) -> bool:
    """淡入/淡出到 ``target``（0.0–1.0），复用同一个 opacity effect

    结束回调里自行决定是否 ``hide()``：淡出必须等动画跑完再隐藏，否则
    末帧「内容还在就突然消失」。

    Returns:
        True = 已在动画中；False = 未启动，调用方需自行写 opacity
    """
    from PyQt5.QtWidgets import QGraphicsOpacityEffect

    effect = getattr(widget, "_fade_effect", None)
    if effect is None:
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        widget._fade_effect = effect
        widget._fade_anim = QPropertyAnimation(effect, b"opacity", widget)
    anim = widget._fade_anim
    current = float(effect.opacity())
    if duration is None:
        duration = Animations.ENTER_MS if target > current else Animations.EXIT_MS

    def _finish() -> None:
        if on_finished is not None:
            on_finished()

    if not retarget(anim, current, float(target), duration=duration, on_finished=_finish):
        effect.setOpacity(float(target))
        if on_finished is not None:
            on_finished()
        return False
    return True


def cross_fade(
    out_widget,
    in_widget,
    *,
    duration: Optional[int] = None,
    on_finished: Optional[Callable[[], None]] = None,
) -> bool:
    """两个互斥区域的交叉淡入淡出（同时反向，避免「一个没了另一个才来」的空窗）

    Returns:
        True = 已启动；False = 未启动（已直切终态）
    """
    d = Animations.NORMAL_MS if duration is None else duration
    if not Animations.motion_enabled():
        out_widget.setVisible(False)
        in_widget.setVisible(True)
        if on_finished is not None:
            on_finished()
        return False
    out_widget.setVisible(True)
    in_widget.setVisible(True)
    in_widget.raise_()
    ok_out = fade_widget(out_widget, 0.0, duration=d)
    ok_in = fade_widget(in_widget, 1.0, duration=d)
    if not (ok_out or ok_in):
        out_widget.setVisible(False)
        if on_finished is not None:
            on_finished()
        return False
    if on_finished is not None:
        QTimer.singleShot(d + 20, on_finished)
    return True


class LoopTimer(QObject):
    """常驻循环动画的统一驱动：不可见 / 减少动效 / 门控为假时跳过回调

    改造前项目里有多个「永不停歇」的循环动画（桌面宠物帧循环、按钮呼吸、
    子代理旋转图标…），窗口最小化、控件隐藏、或系统开启「减少动态效果」时
    仍在跑 → 白烧 CPU，流式输出期间还会跟渲染抢主线程。

    ``LoopTimer`` 在**每一拍**检查门控，不通过就直接跳过回调（定时器本身的
    开销可忽略）。默认门控 = 宿主可见 + 系统未开启减少动效。

    用法::

        self._clock = LoopTimer(self, 60, self._on_tick)
        self._clock.start()
    """

    def __init__(
        self,
        parent: QObject,
        interval_ms: int,
        on_tick: Callable[[], None],
        *,
        gate: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(parent)
        self._on_tick = on_tick
        self._gate = gate
        self._host = parent
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(interval_ms)))
        self._timer.timeout.connect(self._handle_timeout)

    @property
    def timer(self) -> QTimer:
        """底层 QTimer（需要在别处 stop/setInterval 时用）"""
        return self._timer

    @property
    def active(self) -> bool:
        """当前是否满足运行条件（可见 + 未减少动效 + 自定义门控）"""
        if not Animations.motion_enabled():
            return False
        host = self._host
        visible = getattr(host, "isVisible", None)
        if visible is not None and callable(visible):
            try:
                if not visible():
                    return False
            except RuntimeError:
                return False
        if self._gate is not None:
            try:
                if not self._gate():
                    return False
            except RuntimeError:
                return False
        return True

    def start(self, interval_ms: Optional[int] = None) -> None:
        if interval_ms is not None:
            self._timer.setInterval(max(1, int(interval_ms)))
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _handle_timeout(self) -> None:
        if not self.active:
            return
        try:
            self._on_tick()
        except RuntimeError:
            self._timer.stop()  # 宿主已销毁
