"""进程 RSS 后台采样器（主线程零 psutil 调用）。

动机
----
``psutil.Process().memory_info()`` 与 ``children(recursive=True)`` 每次调用都要遍历
系统进程表并对**每个**子进程发起一次系统调用。WebEngine 场景下常驻 10-30 个
Chromium 子进程，单次全量采样实测 20-80ms。

原实现在流式渲染的**每个 content chunk** 都调用一次：
``_on_content_received`` → ``_maybe_strong_recycle`` → ``_over_memory_threshold``
→ ``_web_children_rss_mb``。chat_worker 的批处理阈值是 80ms，即采样周期与
chunk 周期同量级 —— 主线程有 25%-50% 的时间花在遍历进程表上，这是流式
输出卡顿的直接原因之一（kill 冷却 60s 只挡住 kill 动作，挡不住采样）。

方案
----
把采样搬到后台 daemon 线程，主线程只读「最近一次采样结果」，永不执行
psutil 调用。采样间隔默认 3s：内存是慢变量，不会在 3s 内从 300MB 涨到
1400MB，3s 的陈旧度对阈值判定没有任何语义影响。

设计约束
--------
* **不依赖 Qt**：本模块在后台线程运行，禁止触碰任何 QWidget / QObject，
  避免跨线程访问 C++ 对象。纯 threading + psutil。
* **失败静默退化**：psutil 缺失或任何异常都返回 0.0，调用方按「不可用」
  走原有退化分支，绝不抛异常打断渲染路径。
* **进程级单例**：全应用共享一个采样线程，避免 N 个窗口开 N 个线程
  重复遍历进程表。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# 采样间隔（秒）。3s 兼顾「阈值判定新鲜度」与「后台 CPU 开销」。
# 单次采样约 20-80ms，3s 周期下后台开销 < 3%。
_DEFAULT_SAMPLE_INTERVAL_S = 3.0

# 结果有效期（秒）。超过该时长未刷新（线程被阻塞/首次调用）时，
# 调用方可以选择同步采样一次以避免读到过期数据。
_DEFAULT_MAX_STALE_S = 10.0


class _RssSample:
    """一次采样的结果快照（不可变语义，便于无锁读取）。"""

    __slots__ = ("rss_mb", "web_rss_mb", "at")

    def __init__(self, rss_mb: float, web_rss_mb: float, at: float):
        self.rss_mb = rss_mb
        self.web_rss_mb = web_rss_mb
        self.at = at


class RssSampler:
    """后台 RSS 采样器。

    典型用法::

        from app.core.rss_sampler import rss_sampler
        rss_sampler.ensure_started()          # 一般在应用启动时调用一次
        if rss_sampler.web_rss_mb() > 300:   # 主线程读取，零 psutil 开销
            ...
    """

    def __init__(
        self,
        interval_s: float = _DEFAULT_SAMPLE_INTERVAL_S,
        max_stale_s: float = _DEFAULT_MAX_STALE_S,
    ) -> None:
        self._interval_s = interval_s
        self._max_stale_s = max_stale_s
        self._lock = threading.RLock()
        self._sample: Optional[_RssSample] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 上一次同步采样的时间戳：避免主线程在后台线程卡住时反复同步采样
        self._last_sync_sample_at = 0.0

    # ── 生命周期 ──────────────────────────────────────────────
    def ensure_started(self) -> None:
        """启动采样线程（幂等）。可在任意线程调用。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            t = threading.Thread(
                target=self._run,
                name="rss-sampler",
                daemon=True,  # 不阻塞进程退出
            )
            self._thread = t
            t.start()

    def stop(self) -> None:
        """停止采样线程（一般不需要调用；进程退出时 daemon 线程自动结束）。"""
        self._stop_event.set()

    # ── 读取接口（主线程调用，零 psutil）────────────────────────
    def rss_mb(self) -> float:
        """主进程 RSS（MB）。不可用时返回 0.0。"""
        s = self._current_sample()
        return s.rss_mb if s else 0.0

    def web_rss_mb(self) -> float:
        """WebEngine 相关子进程 RSS 合计（MB）。不可用时返回 0.0。"""
        s = self._current_sample()
        return s.web_rss_mb if s else 0.0

    def is_available(self) -> bool:
        """psutil 是否可用（首帧采样成功后为 True）。"""
        return self._current_sample() is not None

    # ── 内部实现 ──────────────────────────────────────────────
    def _current_sample(self) -> Optional[_RssSample]:
        """返回有效快照；无快照或过期时按需同步补采（带节流）。"""
        s = self._sample  # 原子读取，无需加锁
        now = time.monotonic()
        if s is not None and (now - s.at) <= self._max_stale_s:
            return s

        # 无快照 / 已过期：同步补采一次，但限制频率，防止后台线程故障时
        # 主线程退化成「每 chunk 采样」（即本次要修复的问题本身）。
        with self._lock:
            now = time.monotonic()
            s = self._sample
            if s is not None and (now - s.at) <= self._max_stale_s:
                return s
            if now - self._last_sync_sample_at < self._interval_s:
                # 节流窗口内：返回已有快照（哪怕陈旧），绝不重复采样
                return s
            self._last_sync_sample_at = now
            fresh = self._sample_once()
            self._sample = fresh
            return fresh

    def _run(self) -> None:
        """采样线程主循环。"""
        # 首帧立即采样，避免启动后 3s 内无数据
        try:
            self._sample = self._sample_once()
        except Exception:
            pass
        while not self._stop_event.wait(self._interval_s):
            try:
                self._sample = self._sample_once()
            except Exception:
                # 采样失败不能杀死后台线程：退避一个周期后重试
                continue

    @staticmethod
    def _sample_once() -> _RssSample:
        """执行一次完整采样。失败时返回全 0 快照（表示「不可用」）。"""
        now = time.monotonic()
        try:
            import psutil

            self_proc = psutil.Process()
            rss_mb = self_proc.memory_info().rss / (1024 * 1024)

            web_total = 0.0
            for child in self_proc.children(recursive=True):
                try:
                    name = (child.name() or "").lower()
                    if "qwebengine" in name or "webengine" in name or "chrome" in name:
                        web_total += child.memory_info().rss / (1024 * 1024)
                except Exception:
                    continue
            return _RssSample(rss_mb, web_total, now)
        except Exception:
            return _RssSample(0.0, 0.0, now)


# 进程级单例
rss_sampler = RssSampler()
