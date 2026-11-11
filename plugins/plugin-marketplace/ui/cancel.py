# -*- coding: utf-8 -*-
"""安装任务取消与进度上报（线程局部上下文）

为什么用线程局部而不是逐层传参：安装链路有 6 层
（install → _install_with_fallback → _install_by_source → _download_and_move
→ _fetch_into_cache → _sparse_clone / download_tarball），为携带 token/progress
给每一层加参数会污染全部中间层签名，且中间层本身不关心这两个值。

取消的两种打断方式：
- 可轮询的阻塞点（chunk 循环 / 子进程 poll 循环）→ 直接 check()
- 不可轮询的阻塞点（socket read 半开挂在 recv 上）→ 注册 abort 回调，
  cancel() 时从另一个线程 close socket / 杀进程树，让阻塞调用抛异常

非 GUI 调用方（marketplace_tools 的 AI 工具）不 bind，current() 返回 None，
check()/report() 空转，保持原有同步语义。
"""

import threading
from contextlib import contextmanager
from typing import Callable, List, Optional, Tuple


class InstallCancelled(RuntimeError):
    """用户主动取消安装（区别于网络/IO 失败：UI 不按「失败」展示、不写失败记录）"""


class CancelToken:
    """取消信号 + 中止回调集合

    中止回调用于打断无法轮询的阻塞调用；cancel() 先置事件再执行回调，
    阻塞点抛异常后由调用方走既有清理路径（删 cache 临时目录）。

    seal()：下载结束后封盘。落位/回滚/装依赖阶段不能被打断——此时取消会
    让插件目录处于半移动状态，故 seal 之后 check() 不再抛、cancel() 不再生效。
    """

    def __init__(self):
        self._event = threading.Event()
        self._sealed = False
        self._aborts: List[Tuple[object, Callable[[], None]]] = []
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() and not self._sealed

    @property
    def is_sealed(self) -> bool:
        return self._sealed

    def seal(self) -> None:
        """封盘：此后取消不再生效（进入不可中断的落位/依赖安装阶段）"""
        with self._lock:
            self._sealed = True
            self._aborts.clear()

    def cancel(self) -> None:
        """置取消位并执行全部中止回调（幂等；已封盘则忽略）"""
        with self._lock:
            if self._event.is_set() or self._sealed:
                return
            self._event.set()
            aborts = [fn for _, fn in self._aborts]
            self._aborts.clear()
        for fn in aborts:
            try:
                fn()
            except Exception:
                pass

    def add_abort(self, key: object, fn: Callable[[], None]) -> object:
        """注册中止回调，返回 key 供 remove_abort

        已取消时立即执行回调（调用方随后的 check() 会抛 InstallCancelled）。
        """
        fire = False
        with self._lock:
            if self._sealed:
                return key
            if self._event.is_set():
                fire = True
            else:
                self._aborts.append((key, fn))
        if fire:
            try:
                fn()
            except Exception:
                pass
        return key

    def remove_abort(self, key: object) -> None:
        with self._lock:
            self._aborts = [(k, fn) for k, fn in self._aborts if k is not key]

    def check(self) -> None:
        if self._sealed:
            return
        if self._event.is_set():
            raise InstallCancelled("已取消")


_local = threading.local()


@contextmanager
def bind(token: Optional[CancelToken], progress: Optional[Callable[[Optional[float], str], None]] = None):
    """在任务线程入口绑定取消/进度上下文（可嵌套）"""
    prev = getattr(_local, "ctx", None)
    _local.ctx = (token, progress)
    try:
        yield
    finally:
        _local.ctx = prev


def current() -> Optional[CancelToken]:
    ctx = getattr(_local, "ctx", None)
    return ctx[0] if ctx else None


def check() -> None:
    """取消点：已取消则抛 InstallCancelled"""
    ctx = getattr(_local, "ctx", None)
    if ctx and ctx[0] is not None:
        ctx[0].check()


def seal() -> None:
    """封盘当前 token：进入不可中断的落位/依赖安装阶段"""
    ctx = getattr(_local, "ctx", None)
    if ctx and ctx[0] is not None:
        ctx[0].seal()


def report(fraction: Optional[float], text: str = "") -> None:
    """上报进度：fraction=None 表示不确定（滚动条），0.0~1.0 表示确定"""
    ctx = getattr(_local, "ctx", None)
    if not ctx or ctx[1] is None:
        return
    try:
        ctx[1](fraction, text)
    except Exception:
        pass
