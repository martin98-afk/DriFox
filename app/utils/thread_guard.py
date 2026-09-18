# -*- coding: utf-8 -*-
"""
全局 QThread 安全守卫 — 系统级单点防护

安装方式：在 app/__init__.py 中导入一次：
    from app.utils.thread_guard import install_guard
    install_guard()

原理：
    Monkey-patch QThread.__init__，对所有 QThread 实例自动执行：
    1. 重设 parent 为全局隐藏 QObject — 即使卡片创建了 QThread(self)，
       widget 销毁时 QThread 也不会被 Qt 父链级联销毁。
    2. 全局强引用跟踪 — Python GC 无法回收仍在运行中的 QThread，
       即使卡片代码执行了 self._worker_thread = None。

适用场景：
    - 热重载卸载卡片
    - 关闭聊天窗口
    - QThread 引用丢失 + Python GC
    - 任何 QThread 先于底层 OS 线程被销毁的路径
"""

import atexit
import logging as _logging
import threading as _threading
import time as _time
from typing import Set

from PyQt5.QtCore import QObject, QThread

# ── 全局隐藏 QObject ──────────────────────────────────
# 所有 QThread 的 parent 被重定向到此对象，生命周期 = 应用进程。
# 任何 widget 销毁链都无法波及此对象下的 QThread。
_thread_anchor: QObject = QObject()

# ── 全局强引用集合 ────────────────────────────────────
# 保持对 ALL 运行中 QThread 的强引用，防止 Python GC 提前回收。
_running_threads: Set[QThread] = set()


def _on_thread_finished(thread: QThread) -> None:
    """线程正常结束后从墓地移除"""
    # 防御：防止 QThread 子类覆写 finished 信号导致传入非 QThread 对象
    if isinstance(thread, QThread):
        _running_threads.discard(thread)


def _on_thread_destroyed(thread: QThread) -> None:
    """线程被销毁后从墓地移除"""
    # 防御：防止 QThread 子类覆写 destroyed 信号导致传入非 QThread 对象
    if isinstance(thread, QThread):
        _running_threads.discard(thread)


def install_guard() -> None:
    """安装 QThread 安全守卫（monkey-patch QThread.__init__）

    此函数可多次调用，幂等。
    """
    # 检查是否已安装
    if getattr(QThread, "__init__", None) is getattr(install_guard, "_patched", None):
        return

    original_init = QThread.__init__

    def _safe_init(self, parent=None):
        # ── 0. M7 主线程守卫：非主线程创建 QThread 仅告警 ──
        # QThread 对象理想上应归属主线程（与 QApplication 同线程）；在 worker
        # 线程里 new QThread 会让对象 affinity 落在创建线程上，是退出期销毁
        # 顺序错乱的隐患（"QThread: Destroyed while running" qFatal）。
        #
        # ★ 判定必须用 Python 层 threading，绝不能调 QThread.currentThread()：
        #   本函数就是 QThread.__init__，而 sip 为「非 Qt 管理的线程」（adopted
        #   thread，典型 ThreadPoolExecutor 的 tool_parallel_*）生成 QThread 包装
        #   对象时会走 __init__ —— 每调一次 currentThread() 就再进一次 _safe_init，
        #   无限递归把 C 栈打满，报 "Stack overflow (used 1954 kB)"。
        #   （2026-09-16 子智能体 subagent_para 派发必崩即此路径）
        #
        # ★ 只告警不抛错：工具线程池里创建 QThread 是当前架构的既定路径
        #   （SubAgentExecutor 正是在 tool_parallel_* 中构造），硬抛会直接废掉
        #   整个子智能体通路。退出期 qFatal 的实际防护由下面的 parent 重定向 +
        #   全局强引用承担，不依赖本判定。
        if _threading.current_thread() is not _threading.main_thread():
            _watchdog_logger.warning(
                "[ThreadGuard] 非主线程创建 QThread: %s (native_id=%s)，"
                "对象 affinity 归属该线程，退出期存在销毁顺序风险",
                _threading.current_thread().name,
                _threading.get_ident(),
            )
        # ★ 看门狗起点：记录创建时间戳，用于卡死检测
        self._guard_start_ts = _time.monotonic()
        # ── 1. 重设 parent → 全局隐藏锚点 ──
        # 不管调用方传了什么 parent（哪怕是 widget），
        # 都改为 _thread_anchor，防止 widget 销毁时连带销毁运行中的 QThread
        global _thread_anchor
        if not _cpp_alive(_thread_anchor):
            # 自愈：锚点 C++ 对象被外部环境（如测试组合污染）删除时就地重建。
            # QThread 构造不可失败——炸掉调用方只会把故障扩散到业务路径。
            _thread_anchor = QObject()
        original_init(self, _thread_anchor)

        # ── 2. 全局强引用跟踪 ──
        # 即使卡片代码执行 self._worker_thread = None，
        # Python GC 也无法回收此 QThread，因为 _running_threads 持有引用
        _running_threads.add(self)
        self.finished.connect(lambda t=self: _on_thread_finished(t))
        self.destroyed.connect(lambda t=self: _on_thread_destroyed(t))

    QThread.__init__ = _safe_init
    # 标记已安装
    install_guard._patched = _safe_init  # type: ignore[attr-defined]

    # ── 族① 根治：退出期 PyQt 不得销毁仍被引用的 C++ 对象 ──
    # 默认开启时，解释器 shutdown 阶段 PyQt 会 delete 所有仍被 Python 拥有的
    # QObject：anchor 级联 delete children（含 finalize 超时被放生、仍在运行
    # 的 worker）→ "QThread: Destroyed while thread is still running" qFatal
    # → 0xC0000409 闪退。关闭后这些对象直接泄漏到进程死亡，由内核回收；
    # 需要落盘/收尾的资源都走 atexit/closeEvent 显式路径，不依赖析构。
    try:
        from PyQt5 import sip as _sip_mod

        _sip_mod.setdestroyonexit(False)
    except Exception:  # noqa: BLE001
        pass

    # ── 退出期清理注册（幂等，模块级标志防重复注册）──
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_cleanup_threads_at_exit)
        _atexit_registered = True


# ── 看门狗：定期扫描 _running_threads，检测卡死线程 ──
_STUCK_TIMEOUT_S = 60  # 卡死阈值（秒）：线程创建后超过此时间未结束视为可疑
_WATCHDOG_INTERVAL_S = 30  # 扫描间隔（秒）
_atexit_wait_ms = 500  # 退出期单个线程 wait 上限（毫秒）
_atexit_budget_s = 1.0  # 退出期清理总时长预算（秒），超时仅告警不硬中断
_watchdog_lock = _threading.Lock()
_watchdog_thread = None
_watchdog_logger = _logging.getLogger("thread_guard.watchdog")
_atexit_registered = False


def _cpp_alive(obj: QObject) -> bool:
    """探测底层 C++ 对象是否仍存活。

    isinstance 只校验 Python 包装器的类型，不校验 sip 内部的 C++ 指针；
    包装器可以完全合法，而它指向的对象已经被 Qt 在主线程删掉了。
    """
    try:
        from PyQt5 import sip as _sip

        return not _sip.isdeleted(obj)
    except Exception:
        return False


def _scan_stuck_threads() -> None:
    """扫描一轮 _running_threads：报告疑似卡死线程，并剔除 C++ 侧已析构的条目。

    独立成函数是为了能被单测直接驱动（原逻辑内嵌 while True + sleep 30s，无法验证）。
    """
    now = _time.monotonic()
    with _watchdog_lock:
        snapshot = list(_running_threads)
    for thread in snapshot:
        if not isinstance(thread, QThread):
            continue
        # ★ 本函数跑在纯 Python 线程里，下面的 isRunning() 要穿透到 C++ 对象；
        #   而 _running_threads 持强引用防的是 Python GC，挡不住主线程把 C++ 对象
        #   析构。两条路径必踩空：① snapshot 拷完之后、调用之前主线程销毁该线程；
        #   ② 销毁未触发 destroyed 回调，条目永久滞留 → 每轮扫描稳定抛 RuntimeError，
        #   看门狗线程整个死掉。故：探活先行 + 调用兑底，失效条目就地剔除。
        if not _cpp_alive(thread):
            with _watchdog_lock:
                _running_threads.discard(thread)
            continue
        try:
            running = thread.isRunning()
        except RuntimeError:
            with _watchdog_lock:
                _running_threads.discard(thread)
            continue
        if not running:
            continue
        start_ts = getattr(thread, "_guard_start_ts", None)
        if start_ts is None:
            continue
        elapsed = now - start_ts
        if elapsed > _STUCK_TIMEOUT_S:
            _watchdog_logger.warning(
                "[ThreadGuard] 检测到疑似卡死线程: %s (已运行 %.0fs, 阈值 %ds)",
                type(thread).__name__,
                elapsed,
                _STUCK_TIMEOUT_S,
            )


def _watchdog_loop() -> None:
    """看门狗循环：每 _WATCHDOG_INTERVAL_S 秒扫描一次 _running_threads。

    对每个仍在运行的 QThread，若自创建起超过 _STUCK_TIMEOUT_S 秒仍未结束，
    输出 WARNING 日志（仅记录，不自动终止——避免误杀长任务）。
    """
    while True:
        _time.sleep(_WATCHDOG_INTERVAL_S)
        _scan_stuck_threads()


def start_watchdog() -> None:
    """启动看门狗后台线程（幂等）。

    守护线程，进程退出时自动结束。需在 install_guard() 之后调用。
    """
    global _watchdog_thread
    with _watchdog_lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return
        _watchdog_thread = _threading.Thread(
            target=_watchdog_loop,
            daemon=True,
            name="ThreadGuardWatchdog",
        )
        _watchdog_thread.start()


def _cleanup_threads_at_exit() -> None:
    """解释器退出期收敛所有存活 QThread（T15 主根因修复）。

    背景：退出时若 QThread 仍在运行，Qt 打印 "QThread: Destroyed while
    thread is still running" 并 qFatal（0xC0000409 闪退）。固定收尾顺序：
    requestInterruption()（配合业务层 M6 中断检查点）→ quit()（退出
    run() 内的事件循环）→ wait(500)（限时等线程自然结束）。

    纪律：
    - 绝不调 deleteLater / setParent(None)：退出期主线程事件循环已停，
      跨线程的延迟删除/重父化本身是另一条 qFatal 链；线程对象交由
      进程终局回收。
    - 绝不调 terminate()：强制终止不释放线程持有的锁与资源。
    - 每线程 try/except 兜底：C++ 侧已析构等 RuntimeError 不允许打断
      其余线程的收尾（atexit 链上抛错会吞掉后续清理步骤）。
    """
    start = _time.monotonic()
    with _watchdog_lock:
        snapshot = list(_running_threads)
    survivors = []
    for thread in snapshot:
        try:
            if not isinstance(thread, QThread) or not _cpp_alive(thread):
                continue  # Python 包装器活着但 C++ 已析构 / 已非 QThread
            if not thread.isRunning():
                continue
            thread.requestInterruption()
            thread.quit()
            thread.wait(_atexit_wait_ms)
        except Exception:  # noqa: BLE001
            continue
        # wait 超时仍存活的线程记入幸存者（finalize 超时被放生的流式 worker）
        try:
            if _cpp_alive(thread) and thread.isRunning():
                survivors.append(thread)
        except RuntimeError:
            pass
    # ── 族① 双保险：幸存线程脱离 anchor 父链 ──
    # anchor（模块级 QObject）析构时 Qt 会级联 delete 全部 children；
    # 幸存线程被级联 delete → 同一条 qFatal。脱钩后 anchor 无 children
    # 可删；配合 install_guard 里的 setdestroyonexit(False)，PyQt 退出期
    # 也不再 delete —— OS 线程随进程死亡由内核回收。
    # 注意：绝不从 _running_threads 丢弃幸存者（强引用防止 GC 回收
    # wrapper 时 sip delete 运行中线程，同一条 qFatal）。
    for thread in survivors:
        try:
            if thread.parent() is _thread_anchor:
                thread.setParent(None)
        except Exception:  # noqa: BLE001
            continue
    elapsed = _time.monotonic() - start
    if elapsed > _atexit_budget_s:
        _watchdog_logger.warning(
            "[ThreadGuard] atexit 清理耗时 %.0fms，超过 %.0fs 预算（%d 个存活线程）",
            elapsed * 1000,
            _atexit_budget_s,
            len(snapshot),
        )
