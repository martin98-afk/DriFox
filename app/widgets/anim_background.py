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
"""
from __future__ import annotations

import logging
import time
import weakref
from typing import Dict, List, Optional

from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtGui import QImage, QImageReader

logger = logging.getLogger(__name__)

# 帧率上限：与 theme_manager._ANIM_FPS_CAP 同源，客户端再 clamp 一次，
# 防止插件绕过归一化直接构造 Source。
FPS_CAP = 15
# 时钟基准间隔：按上限帧率走，各源内部按自己的 fps 抽帧
_TICK_MS = max(1, 1000 // FPS_CAP)

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
        """到点才推进一帧；返回是否换了新帧"""
        if self.failed:
            return False
        interval = 1000.0 / self.fps
        if now_ms - self._last_advance_ms < interval:
            return False
        self._last_advance_ms = now_ms
        if not self._read_next():
            return False
        return True

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
        """回收没有订阅者的源（主题切换后旧素材不再占内存）"""
        for path in [p for p, s in self._sources.items() if not s.has_subscribers()]:
            self._sources.pop(path, None)

    def clear(self) -> None:
        self._sources.clear()

class BackgroundClock(QObject):
    """全局动画时钟

    只有一个 QTimer：多区域同时动时不会各起一个定时器互相抢主线程。
    没有任何可见订阅者时停表，窗口最小化/切走不烧 CPU。
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
        if not any_visible:
            self._timer.stop()

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
