# -*- coding: utf-8 -*-
"""UI 驱动总线：跨线程操作投递主线程 + 结果回传 + 审计 + ARM 总闸。

设计约束（S2 POC 结论，见 poc 已清理记录）：
- **不提供 runJavaScript 封装**：QtWebEngine 5.15.2 下 runJavaScript 稳定触发
  renderer 0xC0000409（崩族②同签名）。渲染断言的替代方案：
  JS 高度回报信号（contentHeightChanged / _on_height_reported）+ grab() 像素多样性。
- 窗口形态禁 showMinimized（最小化态 Chromium 合成器随机崩），统一用
  :func:`place_offscreen`（show + move 出屏）。

线程模型：
- 驱动操作可能来自网关线程 / 测试线程 / 任意工作线程，而 QWidget 只能主线程动。
- :func:`invoke` 把 callable 投递主线程执行并阻塞取回结果；
  调用方已在主线程时直接同步执行（零开销路径）。
- 重入锁保证同一调用线程同时只有一个在飞操作（防嵌套 invoke 死锁排队）。
"""

from __future__ import annotations

import itertools
import threading
from typing import Any, Callable, Optional

from loguru import logger
from PyQt5.QtCore import QCoreApplication, QObject, Qt, pyqtSignal, pyqtSlot

_AUDIT_SINK_ADDED = False
_RLOCK = threading.RLock()
_REQ_IDS = itertools.count(1)


class DriverNotArmedError(RuntimeError):
    """总闸未 ARM 时拒绝执行任何驱动操作。"""


class DriverTimeoutError(TimeoutError):
    """主线程投递执行超时。"""


def _ensure_audit_sink() -> None:
    """审计日志落 logs/ui_driver.log（幂等；logs/ 目录由主程序保证存在）。"""
    global _AUDIT_SINK_ADDED
    if _AUDIT_SINK_ADDED:
        return
    try:
        logger.add("logs/ui_driver.log", rotation="5 MB", retention=3, level="DEBUG", enqueue=False)
    except Exception:  # noqa: BLE001 — sink 添加失败不阻断驱动
        pass
    _AUDIT_SINK_ADDED = True


# ── ARM 总闸 ──

_ARMED = False


def setArmed(value: bool) -> None:  # noqa: N802 — 对外 API 名按 S1 设计保持驼峰
    """驱动总闸：ARM 后才允许执行任何驱动操作（防生产误触）。"""
    global _ARMED
    _ARMED = bool(value)
    _ensure_audit_sink()
    logger.info(f"[ui-driver] armed={_ARMED}")


def is_armed() -> bool:
    """当前总闸状态。"""
    return _ARMED


# ── 主线程投递 ──


class _MainCaller(QObject):
    """住主线程的调用器：跨线程 emit 请求信号（自动 QueuedConnection），
    在主线程执行 callable 并把结果经 done 信号回传调用线程。

    请求 id 贯穿 requested/done：多工作线程并发投递时 done 是广播信号，
    每个等待方只消费与自己 req_id 匹配的回包，防止串台（S3a-r 必修 1）。
    """

    requested = pyqtSignal(int, object)
    done = pyqtSignal(int, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        # receiver 自身住主线程（parent=app），跨线程 emit 自动走 QueuedConnection
        self.requested.connect(self._run)

    @pyqtSlot(int, object)
    def _run(self, req_id: int, fn: Callable[[], Any]) -> None:
        try:
            self.done.emit(req_id, (True, fn()))
        except Exception as exc:  # noqa: BLE001 — 异常打包回传调用线程重抛
            logger.exception("[ui-driver] 主线程执行异常")
            self.done.emit(req_id, (False, exc))


_caller: Optional[_MainCaller] = None


def _get_caller() -> _MainCaller:
    global _caller
    if _caller is None:
        app = QCoreApplication.instance()
        if app is None:
            raise RuntimeError("QApplication 未创建，无法投递主线程")
        _caller = _MainCaller(app)
    return _caller


def invoke(fn: Callable[[], Any], timeout_ms: int = 15000) -> Any:
    """把 ``fn`` 投递主线程执行并阻塞返回其结果；主线程调用则同步直跑。

    Args:
        fn: 无参 callable，在主线程执行（可直接摸 QWidget）。
        timeout_ms: 等待主线程执行的超时；超时抛 :class:`DriverTimeoutError`。

    Raises:
        DriverNotArmedError: 总闸未 ARM。
        DriverTimeoutError: 主线程超时未回。
        Exception: fn 在主线程抛出的异常原样重抛到调用线程。
    """
    if not _ARMED:
        raise DriverNotArmedError("UI 驱动未 ARM：先调用 tools.ui_driver.setArmed(True)")
    app = QCoreApplication.instance()
    if app is None:
        raise RuntimeError("QApplication 未创建")
    # 主线程调用：直接同步执行，绕过信号往返
    if QThread_currentIsAppThread():
        return fn()
    _ensure_audit_sink()
    with _RLOCK:
        caller = _get_caller()
        req_id = next(_REQ_IDS)
        done_evt = threading.Event()
        box: dict[str, Any] = {"payload": None}

        def _on_done(done_id: int, payload: tuple) -> None:
            if done_id != req_id:  # 其他请求的回包：忽略（广播信号）
                return
            box["payload"] = payload
            done_evt.set()

        # 显式 DirectConnection：done 回包在主线程（emit 线程）直接执行
        # _on_done（仅 dict 写 + Event.set，线程安全），立即唤醒工作线程等待；
        # 若走 Auto/Queued 会投递到无事件循环的工作线程队列，回包永不派发
        _direct = getattr(Qt, "DirectConnection")
        caller.done.connect(_on_done, _direct)  # type: ignore[call-overload]
        try:
            caller.requested.emit(req_id, fn)  # 跨线程 → QueuedConnection → 主线程
            # 等待用 threading.Event：工作线程不需要跑 Qt 事件循环
            # （QEventLoop 版在非 QThread 工作线程存在回包派发时序问题，S3a-r 必修 2）
            if not done_evt.wait(timeout_ms / 1000.0):
                raise DriverTimeoutError(f"主线程执行超时（>{timeout_ms}ms）")
        finally:
            try:
                caller.done.disconnect(_on_done)
            except TypeError:
                pass
        ok, payload = box["payload"]
        if not ok:
            raise payload
        return payload


def QThread_currentIsAppThread() -> bool:
    """当前线程是否主线程（QApplication 所属线程）。"""
    from PyQt5.QtCore import QThread

    app = QCoreApplication.instance()
    if app is None:
        return False
    return QThread.currentThread() == app.thread()


def audit(op: str, detail: str = "") -> None:
    """驱动操作审计日志（ARM 校验后由各操作调用）。"""
    _ensure_audit_sink()
    logger.info(f"[ui-driver] {op}" + (f" :: {detail}" if detail else ""))


def guard(op: str) -> None:
    """ARM 总闸 + 审计一步完成（各公开操作的第一行）。"""
    if not _ARMED:
        raise DriverNotArmedError(f"UI 驱动未 ARM，拒绝操作 {op}：先调用 setArmed(True)")
    audit(op)


__all__ = [
    "DriverNotArmedError",
    "DriverTimeoutError",
    "audit",
    "guard",
    "invoke",
    "is_armed",
    "setArmed",
]
