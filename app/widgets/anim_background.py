# -*- coding: utf-8 -*-
"""动画背景播放器（动画 WebP / GIF）

为什么不用 QMediaPlayer（2026-11-07 实测）：Qt5 的 QMediaPlayer 只是一层壳，
解码委托给系统媒体栈（Windows: DirectShow / Media Foundation，Linux: GStreamer，
macOS: AVFoundation）。同一份素材能不能播取决于那台机器装了什么解码器，
本机 MediaFoundation 注册被剥时表现为 InvalidMedia 且 errorString 为空。
跨平台一致这条路走不通。

改用动画 WebP：Qt 内置 qwebp 插件直接解码（实测 640x360@12fps 单帧 2.6~3.2ms，
1280x720 为 8.8~9.5ms），零外部依赖，三平台行为一致。

三个角色：
- AnimationSource   单个文件的帧源，逐帧解码；同一路径全局共享一个实例，
                    多个区域引用同一张动画时只解码一次
- AnimationRegistry 路径 → Source 注册表
- BackgroundClock   全局时钟（15fps 基准，各源按自己的 fps 抽帧），
                    没有可见订阅者时自动停表

性能约束：QMovie 的 CacheAll 会把全部帧解到内存（60 帧 640x360 约 55MB），
这里只保留当前帧，内存恒定。

[PERF] 解码移出主线程（2026-11-09）：WebP 单帧解码实测 1600x900 约 4.8ms，
15fps 下占主线程 7.2%。改为后台线程预解码到小容量帧队列，主线程 advance()
只从队列取现成帧（微秒级）；队列空（刚启动/刚重建）时回退同步解码，
行为与旧实现逐一等价。队列上限 3 帧，满则工作线程等待，内存最坏 +16.5MB。
"""
from __future__ import annotations

import collections
import logging
import threading
import time
import weakref
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QImage, QImageReader

logger = logging.getLogger(__name__)

# 帧率上限：与 theme_manager._ANIM_FPS_CAP 同源，客户端再 clamp 一次，
# 防止插件绕过归一化直接构造 Source。
FPS_CAP = 15
# 时钟基准间隔：按上限帧率走，各源内部按自己的 fps 抽帧
_TICK_MS = max(1, 1000 // FPS_CAP)
# 全部订阅者不可见时的降频轮询间隔。不能停表：停表后没有任何可见性回调
# 能重启它（Qt 不因窗口恢复可见唤醒已 stop 的 QTimer），窗口最小化/切走
# 再恢复后动画永久卡在最后一帧。500ms 空转只是一次弱引用列表遍历。
_IDLE_TICK_MS = 500
# 应用失焦（本进程无激活窗口，用户切到别的应用）时的降帧倍数。
# 分屏开发是常态：DriFox 挂在后台仍可见，但用户正在看的是别的窗口，
# 壁纸动效没必要全速。3 倍 = 12fps→4fps，timer 唤醒同步降 3 倍；
# 切回后最多一个降档间隔（~200ms）恢复全速，无感。
# 不合并进 _IDLE_TICK_MS：失焦档仍推进解码队列（回来时不卡帧重解码）。
_UNFOCUSED_FACTOR = 3
# 公开别名：测试与外部模块（theme_manager 同款 FPS_CAP 模式）引用用
TICK_MS = _TICK_MS
IDLE_TICK_MS = _IDLE_TICK_MS
UNFOCUSED_TICK_MS = _TICK_MS * _UNFOCUSED_FACTOR
# 预解码队列上限（帧）。3 帧 @15fps = 200ms 前瞻：足够吸收一次主线程卡顿
# （弹窗/流式长帧），又不会把内存抬太多（1600x900 单帧 5.5MB → 最坏 +16.5MB）。
# 队列满则工作线程等待，不会无限堆积。
_PREFETCH_MAX = 3

class AnimationSource:
    """一个动画文件的帧源

    同一 path 由 AnimationRegistry 保证只有一个实例；多个图层订阅同一
    source，共享解码结果。
    """

    def __init__(self, path: str, fps: int = 12):
        self.path = path
        try:
            self.fps = max(1, min(int(fps), FPS_CAP))
        except (TypeError, ValueError):
            self.fps = 12
        self.current: Optional[QImage] = None
        self.frame_count = 0
        self.failed = False
        self._subscribers: List[weakref.ref] = []
        self._last_advance_ms = -(10**9)  # 首帧立即推进
        self._reader = QImageReader(path)
        self._reader.setAutoTransform(True)
        # 预解码队列 + 线程原语。只用 threading（不依赖 Qt 线程）：这里纯计算，
        # 不需要事件循环/信号槽；跟线程只传 QImage（Qt 隐式共享，只读安全）。
        self._prefetch: collections.deque = collections.deque()
        self._prefetch_lock = threading.Lock()
        self._prefetch_space = threading.Condition(self._prefetch_lock)
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_stop = threading.Event()
        self._open()

    # ── 解码 ─────────────────────────────────────────

    def _open(self) -> None:
        self.frame_count = self._reader.imageCount()
        if not self._reader.canRead():
            self.failed = True
            logger.warning(f"[AnimBackground] 动画素材不可读: {self.path}")
            return
        # 先取首帧：调用方拿不到帧时可直接降级到静态图
        self._read_next()
        self._start_prefetch()

    # ── 预解码线程 ──────────────────

    def _start_prefetch(self) -> None:
        """启动后台解码线程（幂等）。

        线程只负责「把下一帧解出来放进队列」，不接触 Qt 对象树、不调任何
        widget 方法；跟线程只传 QImage（Qt 隐式共享，只读安全）。
        """
        if self.failed or self._prefetch_thread is not None:
            return
        t = threading.Thread(
            target=self._prefetch_loop,
            name="anim-prefetch",
            daemon=True,
        )
        self._prefetch_thread = t
        t.start()

    def _prefetch_loop(self) -> None:
        """后台循环：队列有空位就解一帧。"""
        reader = QImageReader(self.path)
        reader.setAutoTransform(True)
        while not self._prefetch_stop.is_set():
            with self._prefetch_lock:
                while len(self._prefetch) >= _PREFETCH_MAX and not self._prefetch_stop.is_set():
                    self._prefetch_space.wait(0.2)
                if self._prefetch_stop.is_set():
                    return
            img = reader.read()
            if img.isNull():
                # 播到尾：重开回到首帧（动画 WebP 的循环语义）
                reader = QImageReader(self.path)
                reader.setAutoTransform(True)
                img = reader.read()
                if img.isNull():
                    self.failed = True
                    logger.warning(f"[AnimBackground] 动画解码失败: {self.path}")
                    return
            # detach：跨线程共享的大图若不立刻深拷贝，后续 reader 复用可能
            # 改写同一块数据（QImage 写时复制只在写方可见）
            img = img.copy()
            with self._prefetch_lock:
                self._prefetch.append(img)

    def _take_prefetched(self) -> Optional[QImage]:
        """取一帧预解码结果；无可用帧返回 None。"""
        with self._prefetch_lock:
            if not self._prefetch:
                return None
            img = self._prefetch.popleft()
            if len(self._prefetch) < _PREFETCH_MAX:
                self._prefetch_space.notify()
            return img

    def stop_prefetch(self) -> None:
        """停止后台线程（registry.prune / 进程退出时调用）。"""
        self._prefetch_stop.set()
        with self._prefetch_lock:
            self._prefetch.clear()
            self._prefetch_space.notify_all()

    def _reopen(self) -> None:
        self._reader = QImageReader(self.path)
        self._reader.setAutoTransform(True)

    def _read_next(self) -> bool:
        img = self._reader.read()
        if img.isNull():
            # 播放到尾：重开 reader 回到第一帧（动画 WebP 的循环语义）
            self._reopen()
            img = self._reader.read()
            if img.isNull():
                self.failed = True
                logger.warning(f"[AnimBackground] 动画解码失败: {self.path}")
                return False
        self.current = img
        return True

    def advance(self, now_ms: int) -> bool:
        """到点才推进一帧；返回是否换了新帧

        优先取后台预解码帧（微秒级）；队列空时回退同步解码，保证刚启动 /
        刚重建（队列还没填）时也能出帧，行为与旧实现一致。
        """
        if self.failed:
            return False
        interval = 1000.0 / self.fps
        if now_ms - self._last_advance_ms < interval:
            return False
        self._last_advance_ms = now_ms
        img = self._take_prefetched()
        if img is not None:
            self.current = img
            return True
        # 兜底：预解码未就绪（队列尚未填满）→ 主线程同步解一帧
        return self._read_next()

    # ── 订阅 ─────────────────────────────────────────

    def subscribe(self, layer) -> None:
        if not any(r() is layer for r in self._subscribers):
            self._subscribers.append(weakref.ref(layer))

    def unsubscribe(self, layer) -> None:
        self._subscribers = [r for r in self._subscribers if r() not in (None, layer)]

    def notify(self) -> None:
        alive: List[weakref.ref] = []
        for ref in self._subscribers:
            layer = ref()
            if layer is None:
                continue
            alive.append(ref)
            try:
                layer.on_animation_frame(self.current)
            except RuntimeError:
                continue  # C++ 对象已销毁
        self._subscribers = alive

    def has_subscribers(self) -> bool:
        return any(r() is not None for r in self._subscribers)

class AnimationRegistry:
    """路径 → AnimationSource（同素材多区域共享解码）"""

    _instance: Optional["AnimationRegistry"] = None

    def __init__(self) -> None:
        self._sources: Dict[str, AnimationSource] = {}

    @classmethod
    def instance(cls) -> "AnimationRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def acquire(self, path: str, fps: int = 12) -> AnimationSource:
        src = self._sources.get(path)
        if src is None:
            src = AnimationSource(path, fps)
            self._sources[path] = src
        return src

    def prune(self) -> None:
        """回收没有订阅者的源（主题切换后旧素材不再占内存）

        必须同时停掉后台预解码线程：线程持有 reader 与队列帧，只从字典里
        pop 会让线程成为孤儿继续解码（daemon 虽不阻退出，但会白烧 CPU）。
        """
        for path in [p for p, s in self._sources.items() if not s.has_subscribers()]:
            src = self._sources.pop(path, None)
            if src is not None:
                src.stop_prefetch()

    def clear(self) -> None:
        for src in self._sources.values():
            src.stop_prefetch()
        self._sources.clear()

class BackgroundClock(QObject):
    """全局动画时钟

    只有一个 QTimer：多区域同时动时不会各起一个定时器互相抢主线程。
    没有任何可见订阅者时降频到 _IDLE_TICK_MS 轮询（解码与通知全跳过，
    只留列表遍历），窗口最小化/切走几乎不烧 CPU；恢复可见后下一拍
    自动回到全速。不能停表：停表后没有任何回调能重启它，窗口恢复
    后动画会永久卡帧（2026-11-08 用户实测「播一会就卡住」即此根因）。
    """

    _instance: Optional["BackgroundClock"] = None

    def __init__(self) -> None:
        super().__init__()
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.setTimerType(0)  # Qt.PreciseTimer = 0
        self._timer.timeout.connect(self._tick)
        self._sources: List[weakref.ref] = []

    @classmethod
    def instance(cls) -> "BackgroundClock":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def track(self, source: AnimationSource) -> None:
        if not any(r() is source for r in self._sources):
            self._sources.append(weakref.ref(source))
        # 无论 timer 是否已活跃都恢复全速：可能正停在 idle 降频间隔上，
        # 新源（如刚挂上的动画层）不该等最多 500ms 才动第一帧
        self._timer.setInterval(_TICK_MS)
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        now_ms = int(time.monotonic() * 1000)
        alive: List[weakref.ref] = []
        any_visible = False
        for ref in self._sources:
            src = ref()
            if src is None:
                continue
            alive.append(ref)
            if not src.has_subscribers():
                continue
            if not self._source_visible(src):
                continue
            any_visible = True
            if src.advance(now_ms):
                src.notify()
        self._sources = alive
        # 三档节流：可见且有焦点→全速抽帧；可见但失焦→降帧 3 倍（壁纸是
        # 氛围层，用户在看别的窗口时没必要全速重绘整窗）；全部不可见→降频
        # 轮询。不停表：停表后无人能重启，窗口恢复可见后动画就永远卡在
        # 最后一帧。
        if not any_visible:
            self._timer.setInterval(_IDLE_TICK_MS)
        elif self._app_focused():
            self._timer.setInterval(_TICK_MS)
        else:
            self._timer.setInterval(_TICK_MS * _UNFOCUSED_FACTOR)

    @staticmethod
    def _app_focused() -> bool:
        """本进程当前是否有激活窗口（用户是否在看本应用）。

        QApplication.activeWindow() 无激活窗口时返回 None。调用成本是一次
        窗口管理器查询，在 tick 频率（≤15 次/秒）下可忽略。
        """
        try:
            from PySide6.QtWidgets import QApplication

            return QApplication.activeWindow() is not None
        except Exception:
            return True  # 查询失败按有焦点处理，宁可全速不可卡帧

    @staticmethod
    def _source_visible(source: AnimationSource) -> bool:
        """源是否有可见订阅者（隐藏窗口/最小化的区域不推进解码）"""
        for ref in source._subscribers:
            layer = ref()
            if layer is None:
                continue
            try:
                if not layer.isVisible():
                    continue
            except RuntimeError:
                continue
            win = layer.window()
            if win is not None:
                try:
                    if win.isMinimized():
                        continue
                except RuntimeError:
                    pass
            return True
        return False

    def stop(self) -> None:
        self._timer.stop()

def reset_for_tests() -> None:
    """测试用：清空全局状态，避免跨用例串味"""
    clock = BackgroundClock._instance
    if clock is not None:
        clock.stop()
        clock._sources = []
    AnimationRegistry._instance = None
