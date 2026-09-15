"""WebEngine renderer 内存看门狗（病态进程主动 terminate，唤醒既有自愈链）。

背景（2026-09-15「对话变长全白屏」事故取证）
--------------------------------------------
长对话 + 大 base64 图（单会话 20+ 张截图，单张 1.3-1.6MB）场景下，承载
可见卡片的那一个 renderer 进程（Chromium 按 site 复用，卡片几乎全部落在
同一个进程）内存单调涨到 3GB+：V8 soft-OOM / 事件循环窒息 → 其上所有视图
白屏，新建对话的视图也落在同一进程 → 「新建任何对话都是空的」。

此时 renderer 进程**并未崩溃**，``renderProcessTerminated`` 不触发，
message_card 的三重自愈链（context_lost / renderCrashed / needRecreate）
全程沉睡，用户只能重启应用恢复。

策略
----
后台线程低频采样本进程 QtWebEngineProcess（``--type=renderer``）子进程的
per-pid RSS；同一 PID 连续 N 个周期超阈值判定为「病态」→ 发 ``rendererSick``
信号。宿主把该进程 terminate：Chromium 向其上所有视图广播
``renderProcessTerminated``，既有自愈链接管恢复（重载骨架 → 补渲；反复病态
走 needRecreate 整卡重建），新视图落在新进程、内存清零。

这相当于把「重启就好」程序化：kill 是手段，恢复全靠既有自愈链，不另造恢复逻辑。

与 B4 强回收层的关系：B4（main_widget._kill_lru_unloaded_renderers）只 kill
**离屏已卸载**批次的 renderer，``_pid_still_in_use`` 护栏永远保护「在用」
renderer——而本事故的病态进程恰是在用进程，B4 覆盖不到。本模块补上这块。

诊断开关：``DRIFOX_RENDERER_WATCHDOG=0`` 整体停用；
``DRIFOX_RENDERER_SICK_MB=<int>`` 覆盖病态阈值。
"""

import os
import threading
import time
from typing import Dict, Optional

from loguru import logger
from PyQt5.QtCore import QObject, pyqtSignal

# 每 PID RSS 病态阈值（MB）。实测病态案例 3171MB（2026-09-15 22:41 取证）；
# 含 20 张 base64 图的会话 renderer 峰值约 300-800MB，取中间偏上留足余量。
_SICK_RSS_MB = float(os.environ.get("DRIFOX_RENDERER_SICK_MB", "1280"))
# 连续超阈周期数（× 周期 60s = 3 分钟确认期）：过滤大图一次性渲染的瞬时峰值
_SICK_STREAK = 3
# 采样周期（秒）。psutil 全子进程遍历 20-80ms，放后台线程，主线程零成本
_INTERVAL_S = 60.0
# 全局触发冷却（秒）：kill 后新进程从零爬坡，冷却防冷却期内误连杀
_COOLDOWN_S = 600.0
# 环境开关（诊断/回退用）
_ENABLED = os.environ.get("DRIFOX_RENDERER_WATCHDOG", "1") != "0"


class RendererWatchdog(QObject):
    """WebEngine renderer 病态检测器（进程级单例，跨窗口共享）。"""

    rendererSick = pyqtSignal(int, float)  # (pid, rss_mb)

    _instance: Optional["RendererWatchdog"] = None

    def __init__(self) -> None:
        super().__init__()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._streaks: Dict[int, int] = {}
        self._last_fired_at = 0.0

    @classmethod
    def get_instance(cls) -> "RendererWatchdog":
        if cls._instance is None:
            cls._instance = RendererWatchdog()
        return cls._instance

    def ensure_started(self) -> None:
        """启动后台采样线程（幂等）。停用开关打开时不启动。"""
        if not _ENABLED:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="renderer-watchdog", daemon=True)
        self._thread.start()
        logger.info(
            f"[R-watchdog] 启动：阈值 {_SICK_RSS_MB:.0f}MB × 连续 {_SICK_STREAK} 周期，"
            f"周期 {_INTERVAL_S:.0f}s，冷却 {_COOLDOWN_S:.0f}s"
        )

    def stop(self) -> None:
        """停采样线程（应用退出时调用；daemon 线程不阻塞退出）。"""
        self._stop_event.set()

    # ── 内部：后台线程侧 ─────────────────────────────────────────

    def _run(self) -> None:
        # 首拍先等一个周期：应用刚启动 renderer 内存爬坡属正常，不参与判定
        while not self._stop_event.wait(_INTERVAL_S):
            try:
                self._sample_once()
            except Exception:
                # 采样失败退避一周期，绝不让线程死掉
                continue

    def _sample_once(self) -> None:
        import psutil

        rss_by_pid: Dict[int, float] = {}
        for child in psutil.Process().children(recursive=True):
            try:
                name = (child.name() or "").lower()
                if "qwebengine" not in name and "webengine" not in name:
                    continue
                # 只看 renderer：GPU/utility/network 进程不承载卡片 DOM
                if "--type=renderer" not in " ".join(child.cmdline() or []):
                    continue
                rss_by_pid[child.pid] = child.memory_info().rss / (1024 * 1024)
            except Exception:
                continue

        # 已消失的 PID：计数清掉（kill 成功 / 进程自然退出）
        for pid in list(self._streaks):
            if pid not in rss_by_pid:
                del self._streaks[pid]

        now = time.monotonic()
        for pid, rss in rss_by_pid.items():
            if rss <= _SICK_RSS_MB:
                self._streaks.pop(pid, None)
                continue
            streak = self._streaks.get(pid, 0) + 1
            self._streaks[pid] = streak
            logger.debug(f"[R-watchdog] pid={pid} rss={rss:.0f}MB 连续超阈 {streak}/{_SICK_STREAK}")
            if streak >= _SICK_STREAK and now - self._last_fired_at >= _COOLDOWN_S:
                self._streaks[pid] = 0
                self._last_fired_at = now
                logger.warning(
                    f"[R-watchdog] 判定病态 renderer pid={pid} rss={rss:.0f}MB，"
                    f"请求 terminate 以唤醒 renderProcessTerminated 自愈链"
                )
                # 跨线程信号：Qt 自动 queued 投递到接收者（主线程）
                self.rendererSick.emit(pid, rss)
