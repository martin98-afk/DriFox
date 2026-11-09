"""paint 热点采样器 — 定位「哪个控件类在持续重绘」。

用途：主进程在不操作时仍占高 CPU、native 采样只能看到
`sipQWidget::paintEvent` 而看不出具体类名时，按类统计 Paint 事件次数，
直接给出 Top 榜单。

**默认开启**（用户零配合）：卡顿现场往往无法复现，日志里必须已经有数据。
正常情况下每轮只写一行 DEBUG（`[PAINT-PROF] 30s 24.3 次/秒（正常）`），
不打扰日志；频率超过 `_ALERT_CPS`（120 次/秒）时升级为一行 WARNING，
摘要里直接点名热点类，完整榜走 DEBUG。

环境变量只用于关闭或调参（不是开启开关）：

    DRIFOX_PAINT_PROFILE=0                  # 显式关闭
    DRIFOX_PAINT_PROFILE_WINDOW=60          # 采样窗口长度（秒，默认 30）

采样是**周期滚动**的：每轮结束打印并复位，持续监测，不会错过启动之后
很久才出现的卡顿。

实现要点（踩过的坑）：
- **不能**通过给 `QWidget.paintEvent` 打 monkey patch 来统计：Qt 的
  Paint 事件在 C++ 侧虚函数调度，Python 子类覆写的 `paintEvent` 不会
  流经父类的 Python 方法，补丁永远计数为 0（已实测确认）。
- 用 `QApplication.installEventFilter` 拦截 `QEvent.Paint` 才有效：
  事件派发必然经过过滤器，能拿到真实的控件对象与类名。
- 过滤器返回 False（不吞事件）；`eventFilter` 内读**模块级** `_stats`，
  `reset()` 用 `clear()` 原地清空 —— 重新绑定会让过滤器写进废弃字典，
  第二轮起统计恒为空。
- 只统计次数不统计耗时：在 eventFilter 里量时间差会把过滤器自身开销
  算进去，分层归因不可靠；耗时构成交给同刻 native 采样。
"""

from __future__ import annotations

import os
import time

_ENV_KEY = "DRIFOX_PAINT_PROFILE"
_ENV_WINDOW = "DRIFOX_PAINT_PROFILE_WINDOW"

_DEFAULT_WINDOW_S = 30.0
_TOP_N = 15
# 告警阈值（次/秒）：60fps 满帧也不可能超过这个量级 —— 正常界面重绘在
# 10~40 次/秒，超 120 意味着有控件在按定时器空转重绘（典型元凶场景）。
_ALERT_CPS = 120.0

_started = False
_filter = None
_stats: dict[str, int] = {}
_t0 = 0.0


def enabled() -> bool:
    """是否启用。

    **默认开启**：这是给用户零配合的诊断设施 —— 卡顿现场无法复现时，
    日志里必须已经有数据。环境变量只用于**关掉它**或调参，不是开关：

        DRIFOX_PAINT_PROFILE=0      # 显式关闭（排查本设施的怀疑时用）
        DRIFOX_PAINT_PROFILE_WINDOW=60   # 调采样窗口长度

    代价：每次 Paint 事件多一次 dict 自增 + QEvent 类型比较（约 1μs），
    相对真实绘制（0.02~8ms）可忽略；报告只在超阈值时打一条，
    正常使用不会刷日志。
    """
    raw = (os.environ.get(_ENV_KEY) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _window_seconds() -> float:
    """采样窗口长度（秒）。"""
    raw = (os.environ.get(_ENV_WINDOW) or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return _DEFAULT_WINDOW_S


def start(window_s: float | None = None) -> bool:
    """安装 Paint 事件过滤器。返回是否成功启用。

    必须在 QApplication 创建之后调用；重复调用幂等。
    """
    global _started, _filter, _t0
    if _started or not enabled():
        return False

    from PyQt5.QtCore import QEvent, QObject, QTimer
    from PyQt5.QtWidgets import QApplication, QWidget

    app = QApplication.instance()
    if app is None:
        return False

    class _PaintFilter(QObject):
        def eventFilter(self, obj, ev):  # noqa: N802 - Qt 命名
            try:
                if ev.type() == QEvent.Paint and isinstance(obj, QWidget):
                    name = type(obj).__name__
                    # ⚠️ 必须读模块级 _stats 而不是外层局部变量：reset() 会
                    # 把 _stats 换成新 dict，闭包持有的旧引用会从此写进无人
                    # 读取的字典 → 周期采样第二轮起恒为空。
                    cur = _stats
                    cur[name] = cur.get(name, 0) + 1
            except RuntimeError:
                # C++ 对象已销毁：Qt 析构窗口期访问会抛，计数丢掉即可
                pass
            return False  # 绝不吞事件

    _filter = _PaintFilter()
    app.installEventFilter(_filter)

    _started = True
    _t0 = time.time()

    # 周期性滚动采样：默认开启后不能只测一次 30 秒 —— 卡顿往往发生在启动
    # 之后很久（用户切到某张长卡片、流式输出中）。每轮结束打印（正常则
    # DEBUG 一行、异常则 WARNING）后立即复位重新计时，形成持续监测。
    seconds = _window_seconds() if window_s is None else max(1.0, float(window_s))
    timer = QTimer()
    timer.timeout.connect(_tick)
    timer.start(int(seconds * 1000))
    # 持有引用：局部 QTimer 被回收后定时器就没了
    _filter._report_timer = timer  # type: ignore[attr-defined]
    return True


def _tick() -> None:
    """一轮采样结束：出报告并复位，继续下一轮。"""
    try:
        report()
    finally:
        reset()


def report(force: bool = False) -> None:
    """打印聚合报告。

    **默认静默**：总频率正常（低于 ``_ALERT_CPS``）时只输出一行 DEBUG，
    不打扰正常使用的日志。总频率异常（主进程不操作仍持续重绘）时升级为
    WARNING，并在 DEBUG 里给出完整 Top 榜，便于事后取证。

    Args:
        force: True 则无论频率高低都打完整报告（手动排查 / 测试用）。
    """
    elapsed = max(1e-6, time.time() - _t0) if _t0 else 1.0
    total = sum(_stats.values())
    cps = total / elapsed
    by_count = sorted(_stats.items(), key=lambda kv: -kv[1])

    if not force and cps < _ALERT_CPS:
        _log(f"[PAINT-PROF] {elapsed:.0f}s {cps:.1f} 次/秒（正常）", level="DEBUG")
        return

    # 异常：一行 WARNING 摘要（便于 grep），完整榜走 DEBUG（不刷用户日志）
    top_desc = "  ".join(f"{n}={c}" for n, c in by_count[:3]) or "无"
    _log(
        f"[PAINT-PROF] 重绘异常：{elapsed:.0f}s 内 Paint 事件 {total} 次"
        f"（{cps:.1f} 次/秒，阈值 {_ALERT_CPS:.0f}）→ 热点类 {top_desc}",
        level="WARNING",
    )

    lines = [f"[PAINT-PROF] {'类名':<36s}{'次数':>8s}{'次/秒':>9s}{'占比':>8s}"]
    for name, cnt in by_count[:_TOP_N]:
        share = (cnt / total * 100) if total else 0.0
        lines.append(
            f"[PAINT-PROF] {name:<36s}{cnt:>8d}{cnt / elapsed:>9.1f}{share:>7.1f}%"
        )
    if len(by_count) > _TOP_N:
        rest = sum(c for _, c in by_count[_TOP_N:])
        lines.append(
            f"[PAINT-PROF] {'（其余 %d 类）' % (len(by_count) - _TOP_N):<36s}{rest:>8d}"
            f"{rest / elapsed:>9.1f}{(rest / total * 100) if total else 0:>7.1f}%"
        )
    for ln in lines:
        _log(ln, level="DEBUG")


def reset() -> None:
    """清空统计并重新计时。

    ⚠️ 用 `clear()` 原地清空而不是重新绑定：`_stats` 被 `_PaintFilter`
    的 eventFilter 通过模块级引用读取，重新绑定时如果过滤器持有旧引用
    就会写进废弃字典（周期采样第二轮起恒为空）。
    """
    global _t0
    _stats.clear()
    _t0 = time.time()


def stop() -> None:
    """停止采样并移除过滤器（幂等）。"""
    global _started, _filter
    if not _started:
        return
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None and _filter is not None:
        try:
            app.removeEventFilter(_filter)
        except RuntimeError:
            pass
    _filter = None
    _started = False


def _log(msg: str, level: str = "INFO") -> None:
    """写日志。level 支持 INFO / DEBUG / WARNING（loguru 同名方法）。"""
    try:
        from loguru import logger

        getattr(logger, level.lower(), logger.info)(msg)
    except Exception:  # noqa: BLE001
        print(msg)
