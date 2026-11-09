"""paint 热点采样器 — 定位「哪个控件类在持续重绘」。

用途：主进程在不操作时仍占高 CPU、native 采样只能看到
`sipQWidget::paintEvent` 而看不出具体类名时，启用本采样器按类统计
Paint 事件次数与 `paintEvent` 耗时，直接给出 Top 榜单。

用法（环境变量开关，默认完全关闭，零开销）：

    DRIFOX_PAINT_PROFILE=1 python main.py
    DRIFOX_PAINT_PROFILE=1 DRIFOX_PAINT_PROFILE_WINDOW=60 python main.py

采样窗口默认 30 秒，到期向日志打印聚合报告（tag: `[PAINT-PROF]`）。

实现要点（踩过的坑）：
- **不能**通过给 `QWidget.paintEvent` 打 monkey patch 来统计：Qt 的
  Paint 事件在 C++ 侧虚函数调度，Python 子类覆写的 `paintEvent` 不会
  流经父类的 Python 方法，补丁永远计数为 0。
- 用 `QApplication.installEventFilter` 拦截 `QEvent.Paint` 才有效：
  事件派发必然经过过滤器，能拿到真实的控件对象与类名。
- 过滤器返回 False（不吞事件），并**只在计数时取样时间**：真正耗时
  用事件前后时间差统计会把 eventFilter 自身开销算进去，故只记录
  事件到达次数；耗时分项由 native 采样（sample / Instruments）给出。

诊断完成后本文件保留（默认不启用，无运行时成本）。
"""

from __future__ import annotations

import os
import time

_ENV_KEY = "DRIFOX_PAINT_PROFILE"
_ENV_WINDOW = "DRIFOX_PAINT_PROFILE_WINDOW"

_DEFAULT_WINDOW_S = 30.0
_TOP_N = 15

_started = False
_filter = None
_stats: dict[str, int] = {}
_t0 = 0.0


def enabled() -> bool:
    """是否启用（环境变量存在且不为 0/false/no/off）。"""
    raw = (os.environ.get(_ENV_KEY) or "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


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

    stats = _stats

    class _PaintFilter(QObject):
        def eventFilter(self, obj, ev):  # noqa: N802 - Qt 命名
            try:
                if ev.type() == QEvent.Paint and isinstance(obj, QWidget):
                    name = type(obj).__name__
                    stats[name] = stats.get(name, 0) + 1
            except RuntimeError:
                # C++ 对象已销毁：Qt 析构窗口期访问会抛，计数丢掉即可
                pass
            return False  # 绝不吞事件

    _filter = _PaintFilter()
    app.installEventFilter(_filter)

    _started = True
    _t0 = time.time()

    seconds = _window_seconds() if window_s is None else max(1.0, float(window_s))
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(report)
    timer.start(int(seconds * 1000))
    # 持有引用：局部 QTimer 被回收后定时器就没了
    _filter._report_timer = timer  # type: ignore[attr-defined]

    _log(f"[PAINT-PROF] 已启动，采样窗口 {seconds:.0f}s，到期打印 Paint 事件 Top{_TOP_N}")
    return True


def report() -> None:
    """打印聚合报告（可重复调用看增量）。"""
    elapsed = max(1e-6, time.time() - _t0) if _t0 else 1.0
    total = sum(_stats.values())

    by_count = sorted(_stats.items(), key=lambda kv: -kv[1])
    lines = [
        f"[PAINT-PROF] 窗口 {elapsed:.1f}s  Paint 事件总数={total}  "
        f"平均 {total / elapsed:.1f} 次/秒"
    ]
    lines.append(f"[PAINT-PROF] {'类名':<36s}{'次数':>8s}{'次/秒':>9s}{'占比':>8s}")
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

    lines.append(
        "[PAINT-PROF] 提示：次/秒 明显高于刷新率的类是空转重绘元凶；"
        "耗时构成见同刻 native 采样（sample / Instruments）"
    )
    for ln in lines:
        _log(ln)


def reset() -> None:
    """清空统计并重新计时。"""
    global _stats, _t0
    _stats = {}
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


def _log(msg: str) -> None:
    try:
        from loguru import logger

        logger.info(msg)
    except Exception:  # noqa: BLE001
        print(msg)
