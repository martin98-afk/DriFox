# -*- coding: utf-8 -*-
"""滚动区滚轮接管：关掉 qfw 平滑动画，保留连续滚轮加速，一次到位。

## 背景

qfw 的 SmoothScroll 每格滚轮要跑 400ms / 60fps 的逐帧 setValue，每帧都把视口
重画一遍。离屏实测服务商编辑卡：每滚动步 61 次 paint、4.6 倍视口面积，而同结构
纯 QScrollArea 只有 7 次 / 0.23 倍。

关平滑（``SmoothMode.NO_SMOOTH``）能把重绘降下来（滚轮期 paint 1791→488），但
qfw 引擎里的两级放大也一并失效：基础 1.5 倍 ``stepRatio``，以及 500ms 窗口内
最多 +100% 的连续滚轮加速。本模块把这两级放大搬到「一次 setValue」上：加速手感
保留，重绘仍是每格一次。

## 用法

    from app.utils.scroll_tuning import install_fast_wheel
    install_fast_wheel(self.scroll_area)   # 幂等，重复调用无副作用

兼容 qfw ``ScrollArea`` / ``SingleDirectionScrollArea`` 与原生 ``QScrollArea``
（原生本来就没有平滑动画，只装接管器）。

## 边界行为

滚到顶/底时返回 False 让事件继续冒泡，不吞掉外层容器（卡片套卡片、WebView 内外
路由、消息卡把滚轮转给聊天区）已有的滚轮链路。

## 触控板（pixelDelta）

Qt5 的 ``QAbstractSlider::wheelEvent`` 只认 ``angleDelta``，按
``singleStep × wheelScrollLines`` 做「累积到 120 才走一步」的量化，
触控板的连续小 delta 与 ``pixelDelta`` 因此被整段丢掉（离屏实测：
``pixelDelta=-10`` 连发 6 次，滚动条只走 1px；手指滑 60px、内容走 1px）。
本模块对带 ``pixelDelta`` 的事件一律按像素位移 1:1 ``setValue``，
惯性仍由系统 momentum 事件（同样以 wheel 形式到达）驱动，不自造动画。
"""

from __future__ import annotations

import math
import time
from collections import deque

from PyQt5.QtCore import QElapsedTimer, QEvent, QObject, Qt, QTimer
from PyQt5.QtWidgets import QAbstractScrollArea, QApplication

try:  # qfw 是硬依赖，这里只是让模块可被单独 import 做自检
    from qfluentwidgets import SmoothMode
except Exception:  # pragma: no cover - 仅降级保护
    SmoothMode = None

# qfw 默认放大系数（common/smooth_scroll.py：stepRatio=1.5、acceleration=1）
_DEFAULT_STEP_RATIO = 1.5
_DEFAULT_ACCEL_WINDOW_MS = 500
_DEFAULT_ACCEL_STAMP_CAP = 15
# 短过渡时长：默认 0（一次到位）。
# 实测 120ms 分帧（QTimer 16ms + ease-out + int 取整）在真实窗口上会拖出
# 颜抖：定时器与显示器刷新不同步，一帧可能推进 0 次或 2 次，加上整数
# 步进与既有滚动驱动（吸底/转发）竞争。一次到位虽然瞬变但无颜抖，
# 且重绘量最低（滚轮 8 格：452 次 paint vs 动画 1850）。
_DEFAULT_SMOOTH_MS = 0
_FRAME_INTERVAL_MS = 16
_WHEEL_NOTCH = 120
_INSTALLED_ATTR = "_drifox_fast_wheel_filter"
# 「滚轮输入到达」回调（由滚动区宿主注册，见 set_wheel_input_hook）。
_INPUT_HOOK_ATTR = "_drifox_wheel_input_hook"
# 滚动活跃窗口：滚轮/触控板消费事件后置位，动画类控件据此在滚动期降级重绘。
# 120ms ≈ 两次手势间的最长空档，滚动一停动画立即恢复（相位由各控件自己保存）。
_SCROLL_ACTIVE_MS = 120.0
_scroll_active_until = 0.0


def mark_scroll_activity() -> None:
    """登记一次滚动输入，刷新活跃窗口。"""
    global _scroll_active_until
    _scroll_active_until = time.monotonic() + _SCROLL_ACTIVE_MS / 1000.0


def is_scroll_active() -> bool:
    """是否处于滚动活跃窗口内（供动画类控件跳过装饰性重绘）。"""
    return time.monotonic() < _scroll_active_until


# qfw ScrollArea 的悬浮自绘条属性名（SmoothScrollDelegate 持有）
_QFW_BAR_ATTRS = ("vScrollBar", "hScrollBar")


def _switch_to_native_bar(area: QAbstractScrollArea) -> bool:
    """把 qfw 悬浮自绘滚动条换成原生 QScrollBar，恢复 Qt 的滚动 blit。

    [PERF] 悬浮条是 QScrollArea 的 child，压在 viewport 上方，Qt 因此不敢把
    视口判定为可搬移，每滚一步整视口重绘。离屏实测（同一 60 行内容、48px/步）：
    悬浮条 35.9 次 paint / 1.16 倍视口 / 3.33ms；换成原生条 8.2 次 / 0.23 倍 /
    1.24ms（与纯 QScrollArea 持平）。

    视觉不变：现有 _scroll_style 的透明声明全部保留，blit 不依赖它们
    （实测去掉 QSS 后 paint 与保留时完全一致）。

    Returns:
        True = 已换；False = 不是 qfw 滚动区（原生 QScrollArea 无需处理）。
    """
    dele = getattr(area, "scrollDelagate", None)
    # [#I6] qfw ScrollArea 的自绘条挂在 delegate 上；SingleDirectionScrollArea
    # （聊天区在用）没有 delegate，SmoothScrollBar 直挂 area 自身属性——
    # 只认 delegate 会让聊天区的 native_scrollbar=True 退化成空操作。
    holder = dele if dele is not None else area
    if getattr(holder, "vScrollBar", None) is None and getattr(holder, "hScrollBar", None) is None:
        return False
    from PyQt5.QtWidgets import QAbstractScrollArea as _QASA
    from PyQt5.QtWidgets import QScrollBar

    # ① 隐藏悬浮条（不删：delegate / SmoothScroll 引擎仍会读它，删了会悬空引用）
    for name in _QFW_BAR_ATTRS:
        bar = getattr(holder, name, None)
        if bar is not None:
            try:
                bar.hide()
                bar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            except RuntimeError:
                pass

    # ② 换原生条（delegate 把 setVerticalScrollBarPolicy 换成了自己的实现，
    #    必须用 QAbstractScrollArea 类方法绕开，否则策略仍被强制 AlwaysOff）
    vbar = QScrollBar(Qt.Vertical, area)
    _QASA.setVerticalScrollBar(area, vbar)
    _QASA.setVerticalScrollBarPolicy(area, Qt.ScrollBarAsNeeded)
    try:
        from app.utils.design_tokens import get_unified_scrollbar_style

        vbar.setStyleSheet(get_unified_scrollbar_style(8))
    except Exception:  # noqa: BLE001 - 样式刷不上不影响滚动
        pass

    # ③ 反劫持：SmoothScrollDelegate.__init__ 把实例方法 setVerticalScrollBarPolicy
    #    换成了自己的实现——内部用类方法强制原生条 AlwaysOff，只联动已隐藏的
    #    qfw 悬浮条。不删除的话，调用点随后任何一次 setVerticalScrollBarPolicy
    #    （如各滚动区构造时的 ScrollBarAsNeeded）都会把原生条打回永久隐藏
    #    （2026-11-18 侧边栏自定义插件区滚动条消失的根因，tab 列表同病）。
    #    删实例属性恢复类方法：policy setter 回到原生语义；悬浮条已 hide 且
    #    鼠标穿透，setForceHidden 联动不再有意义。
    if dele is not None:
        for attr in ("setVerticalScrollBarPolicy", "setHorizontalScrollBarPolicy"):
            try:
                delattr(area, attr)
            except AttributeError:
                pass  # 无劫持（理论不可达，防御）
    return True


def _can_step(bar, value_delta: int) -> bool:
    """滚动条能否按 value_delta 再走一步（到顶/到底返回 False，保留外层链路）。"""
    if bar is None or bar.maximum() <= bar.minimum():
        return False
    if value_delta > 0 and bar.value() >= bar.maximum():
        return False
    if value_delta < 0 and bar.value() <= bar.minimum():
        return False
    return True


def scroll_wheel_to(area: QAbstractScrollArea, event) -> bool:
    """把 wheel 事件交给 area 已装的接管器；未装则回退 Qt 原生。

    转发点原先直接调 ``area.wheelEvent(event)``，绕过 viewport 上的
    eventFilter → 触控板像素路径与整格放大全部失效。统一收口到这里。

    Returns:
        True = 已消费（调用方应 accept），False = 未消费。
    """
    flt = getattr(area, _INSTALLED_ATTR, None)
    if isinstance(flt, _FastWheelFilter):
        try:
            return flt.handle_wheel(event)
        except RuntimeError:
            return False
    area.wheelEvent(event)
    return True


def set_wheel_input_hook(area: QAbstractScrollArea, hook) -> None:
    """登记「滚轮输入到达」回调（幂等，与 install_fast_wheel 顺序无关）。

    为什么需要：卡片内滚到边界后，viewer / MessageCard 把 wheel 直接交给
    ``scroll_wheel_to`` → ``handle_wheel``，事件**不经过** viewport 上的
    eventFilter。宿主若只在 eventFilter 里标记「真实滚动输入」，经卡片转发
    的外层滚动会被当成程序滚动（away 永不置位 → 内容更新时视口恒被拽回
    底部）。回调放在接管器上，两条入口（viewport 过滤 / 外部转发）都能覆盖。

    Args:
        area: 已装或待装接管器的滚动区。
        hook: 无参回调，收到滚轮输入时调用（可为 None 取消登记）。
    """
    if area is None:
        return
    try:
        setattr(area, _INPUT_HOOK_ATTR, hook)
        flt = getattr(area, _INSTALLED_ATTR, None)
    except RuntimeError:
        return  # area 已销毁
    if isinstance(flt, _FastWheelFilter):
        flt._input_hook = hook


class _FastWheelFilter(QObject):
    """viewport 级滚轮过滤器：触控板像素直通 + 连续滚轮加速 + 短过渡帧推进。"""

    def __init__(self, area: QAbstractScrollArea, parent=None):
        super().__init__(parent or area)
        self._area = area
        # 宿主注册的输入回调：install_fast_wheel 可能晚于注册（两处构建分支
        # 装配顺序不同），故安装时也要从 area 上回读一次。
        self._input_hook = getattr(area, _INPUT_HOOK_ATTR, None)
        self._stamps: deque = deque()
        self._step_ratio = _DEFAULT_STEP_RATIO
        self._window_ms = _DEFAULT_ACCEL_WINDOW_MS
        self._stamp_cap = _DEFAULT_ACCEL_STAMP_CAP
        # 默认关：连续滚轮时步长会在 96→180px 间递增，一次到位下这种
        # 「每格不一样远」直接可见，观感就是颜。固定步长反而稳。
        self._accel = False
        self._smooth_ms = _DEFAULT_SMOOTH_MS
        # 短过渡状态：计时器只在动画期间创建并运行，空闲时不占 CPU。
        # 懒创建（而非构造即建）：默认 smooth_ms=0 的调用点永远用不到它，
        # 每个滚动区少一个常驻 QTimer；离屏测试里也能避免残留 timer 与
        # qfw 图标绘制同进程时的段错误。
        self._timer: QTimer | None = None
        self._elapsed = QElapsedTimer()
        self._anim_bar = None
        self._anim_target: int | None = None
        self._anim_start = 0
        self._last_set: int | None = None
        # 细分 angleDelta（无 pixelDelta 的平台）的像素折算残差
        self._frac = 0.0

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 回调
        if event.type() != QEvent.Wheel:
            return False
        return self.handle_wheel(event)

    def handle_wheel(self, event) -> bool:
        """消费一个 wheel 事件；True = 已滚动并 accept，False = 放行。

        从 viewport 的 eventFilter 与外部转发点（卡内 viewer 到边界后转外层、
        滚轮守卫）共用：直接调 ``area.wheelEvent()`` 会绕过 eventFilter，
        触控板像素路径就失效了。
        """
        # 任何到达接管器的 wheel 都是真实用户输入（无论下面是否消费成功）。
        # 必须在 setValue 之前调用：宿主靠这个时间窗判定随后的 valueChanged
        # 是用户意图（away 置位）而非程序滚动。
        hook = self._input_hook
        if hook is not None:
            try:
                hook()
            except Exception:  # noqa: BLE001 - 通知失败不得影响滚动本身
                pass
        area = self._area
        try:
            vbar = area.verticalScrollBar()
            hbar = area.horizontalScrollBar()
        except RuntimeError:
            return False  # area 已销毁

        # ① 触控板/高精度指针：pixelDelta 1:1 跟手（正 delta = 内容下移 = value 减）
        pixel = event.pixelDelta()
        if not pixel.isNull():
            py, px = pixel.y(), pixel.x()
            bar = vbar if py else hbar
            move = -(py or px)
            if move and _can_step(bar, move):
                mark_scroll_activity()
                bar.setValue(bar.value() + move)
                event.accept()
                return True
            return False

        dy = event.angleDelta().y()
        dx = event.angleDelta().x()
        delta = dy or dx
        if not delta:
            return False

        # ② 无 pixelDelta 的细分滚轮：按角度折算像素并保留小数残差，
        #    不交给 Qt 的整格累积量化（那正是「滚了没反应」的源头）。
        if abs(delta) % _WHEEL_NOTCH != 0:
            bar = vbar if dy else hbar
            lines = QApplication.wheelScrollLines()
            if lines <= 0:
                lines = 3
            step_px = max(1, bar.singleStep()) * lines
            move = -self._accumulate_angle(delta, step_px)
            if move and _can_step(bar, move):
                mark_scroll_activity()
                bar.setValue(bar.value() + move)
            event.accept()  # 本区接管（含残差不足 1px 的拍），不再外溢
            return True

        bar = vbar if dy else hbar
        if not _can_step(bar, -delta):
            return False

        now = time.monotonic() * 1000.0
        stamps = self._stamps
        stamps.append(now)
        while stamps and now - stamps[0] > self._window_ms:
            stamps.popleft()
        # 与 qfw 同构：窗口内滚轮越密，越接近 2 倍距离（默认关，见 _accel）
        accel_ratio = min(len(stamps) / self._stamp_cap, 1.0) if self._accel else 0.0

        lines = QApplication.wheelScrollLines()
        if lines <= 0:  # -1 = 系统未设，取 Windows 默认 3 行
            lines = 3
        step = max(1, bar.singleStep()) * lines
        notches = abs(delta) / _WHEEL_NOTCH
        distance = step * notches * self._step_ratio * (1.0 + accel_ratio)
        mark_scroll_activity()
        self._scroll_by(bar, -math.copysign(distance, delta))
        event.accept()
        return True

    def _accumulate_angle(self, delta: int, step_px: int) -> int:
        """把细分 angleDelta 折算成整数像素并保留小数残差。

        方向翻转时清零残差，避免正反滚互相抵消（会让手感变「黏」）。
        """
        if delta and (delta > 0) != (self._frac > 0):
            self._frac = 0.0
        self._frac += delta * step_px / _WHEEL_NOTCH
        whole = int(self._frac)
        if whole:
            self._frac -= whole
        return whole

    def _ensure_timer(self) -> QTimer:
        """按需创建短过渡计时器。"""
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(_FRAME_INTERVAL_MS)
            self._timer.timeout.connect(self._tick)
        return self._timer

    # ── 短过渡推进 ──────────────────────────────────────

    def _scroll_by(self, bar, delta_value: float) -> None:
        """按 delta_value 滚动；smooth_ms>0 时分帧推进，否则一次到位。"""
        if self._smooth_ms <= 0:
            bar.setValue(int(bar.value() + delta_value))
            return
        try:
            cur = bar.value()
        except RuntimeError:
            return
        # 动画中途外部改过值（吸底/拖滚动条）→ 丢弃旧动画，从当前值重开
        if self._anim_bar is not bar or (self._last_set is not None and cur != self._last_set):
            self._stop_animation()
        if self._anim_target is None:
            self._anim_start = cur
            self._anim_target = cur
        self._anim_bar = bar
        self._anim_target = int(max(bar.minimum(), min(bar.maximum(), self._anim_target + delta_value)))
        self._elapsed.start()
        timer = self._ensure_timer()
        if not timer.isActive():
            timer.start()

    def _tick(self) -> None:
        bar = self._anim_bar
        target = self._anim_target
        if bar is None or target is None:
            self._stop_animation()
            return
        try:
            cur = bar.value()
        except RuntimeError:
            self._stop_animation()
            return
        total = max(1, self._smooth_ms)
        t = self._elapsed.elapsed() / total
        if t >= 1.0:
            if cur != target:
                bar.setValue(target)
            self._stop_animation()
            return
        p = 1.0 - (1.0 - t) ** 3  # ease-out cubic
        v = int(round(self._anim_start + (target - self._anim_start) * p))
        if v != cur:
            bar.setValue(v)
        self._last_set = bar.value()

    def _stop_animation(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._anim_bar = None
        self._anim_target = None
        self._last_set = None


def install_fast_wheel(
    area: QAbstractScrollArea,
    *,
    step_ratio: float = _DEFAULT_STEP_RATIO,
    accel_window_ms: int = _DEFAULT_ACCEL_WINDOW_MS,
    accel_stamp_cap: int = _DEFAULT_ACCEL_STAMP_CAP,
    native_scrollbar: bool = True,
    smooth_ms: int = _DEFAULT_SMOOTH_MS,
    accel: bool = False,
) -> bool:
    """关闭 qfw 平滑动画并装上「一次步进 + 加速」接管器（幂等）。

    Args:
        area: 任意 QAbstractScrollArea（qfw ScrollArea / SingleDirectionScrollArea / 原生）。
        step_ratio: 单格距离放大系数，1.5 = qfw 默认。
        accel_window_ms: 连续滚轮的统计窗口，窗口内越密越快（qfw 默认 500ms）。
        accel_stamp_cap: 加速饱和所需的窗口内滚轮事件数（qfw 默认 15）。
        native_scrollbar: 换掉 qfw 悬浮自绘条以恢复滚动 blit（默认开；
            聊天区这类对悬浮条观感敏感的调用点可传 False）。
        smooth_ms: 每格滚动的过渡时长（0 = 一次到位）。分帧在真实窗口上
            实测有颜抖（定时器与刷新不同步 + 整数步进），默认关。
        accel: 连续滚轮加速（窗口内越密越远）。一次到位下会造成每格
            距离不一致，默认关。

    Returns:
        True = 本次完成安装；False = 已装过或对象不合法。
    """
    if area is None:
        return False
    if getattr(area, _INSTALLED_ATTR, None) is not None:
        return False

    # ① 关平滑：两种 qfw 滚动区接口不同
    if SmoothMode is not None:
        set_mode = getattr(area, "setSmoothMode", None)  # ScrollArea(mode, orientation)
        smooth = getattr(area, "smoothScroll", None)  # SingleDirectionScrollArea
        smooth_del = getattr(area, "scrollDelagate", None)  # ScrollArea 的 delegate
        try:
            if smooth is not None:
                smooth.setSmoothMode(SmoothMode.NO_SMOOTH)
            elif smooth_del is not None:
                for name in ("verticalSmoothScroll", "horizonSmoothScroll"):
                    engine = getattr(smooth_del, name, None)
                    if engine is not None:
                        engine.setSmoothMode(SmoothMode.NO_SMOOTH)
            elif callable(set_mode):
                set_mode(SmoothMode.NO_SMOOTH, Qt.Vertical)
        except Exception:  # noqa: BLE001 - 平滑关不掉也不能影响滚动可用性
            pass

    # ② 换原生条恢复 blit（必须在装接管器之前，不然首帧读到的是悬浮条）
    if native_scrollbar:
        try:
            _switch_to_native_bar(area)
        except Exception:  # noqa: BLE001 - 换条失败也不能让滚动不可用
            pass
    # ③ 装接管器（viewport hook：事件先到 viewport）
    flt = _FastWheelFilter(area)
    flt._step_ratio = step_ratio
    flt._window_ms = accel_window_ms
    flt._stamp_cap = accel_stamp_cap
    flt._smooth_ms = smooth_ms
    flt._accel = accel
    target = area.viewport() if hasattr(area, "viewport") else area
    target.installEventFilter(flt)
    setattr(area, _INSTALLED_ATTR, flt)  # 挂住引用，防 GC 后过滤器失效
    return True
