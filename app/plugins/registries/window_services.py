# -*- coding: utf-8 -*-
"""窗口级插件服务槽 — 插件把「绑定某个窗口的对象」注册进来，主程序按窗口取用。

与 ``UIPluginRegistry.register_service`` 的分工：那个是全局单实例（无窗口语义，
如会话卡服务）；本表按 ``window_id`` 分桶，天然支持多窗口各持一份实例
（每窗口一个子智能体管理器即属此类）。

主程序侧只认「key + window_id」这两个参数，不认识任何具体业务；key 由插件定义。
未注册时 ``get`` 返回 None，调用方按「插件未加载」降级。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


class WindowServiceRegistry:
    """key → (plugin_name, instance)，分「全局」与「按窗口」两层。"""

    def __init__(self) -> None:
        self._global: Dict[str, Tuple[str, Any]] = {}
        self._by_window: Dict[str, Dict[str, Tuple[str, Any]]] = {}
        self._lock = threading.Lock()

    # ── 写入 ──

    def register(self, key: str, instance: Any, plugin_name: str = "", window_id: str = "") -> None:
        """注册服务。``window_id`` 为空表示全局（跨窗口共用一个实例）。"""
        if not key:
            return
        entry = (plugin_name, instance)
        prev: Optional[Tuple[str, Any]] = None
        with self._lock:
            if window_id:
                self._by_window.setdefault(window_id, {})[key] = entry
            else:
                prev = self._global.get(key)
                self._global[key] = entry
        if prev is not None and prev[0] != plugin_name:
            logger.info(f"[WindowServices] '{key}' 提供方由 {prev[0] or '内置'} 切换为 {plugin_name or '内置'}")

    def unregister_plugin(self, plugin_name: str) -> List[Any]:
        """摘除某插件注册的全部服务（插件卸载/热重载）。

        Returns:
            被摘除的实例列表，供调用方做业务收尾（停线程/取消任务）。
        """
        removed: List[Any] = []
        with self._lock:
            for key in [k for k, (owner, _) in self._global.items() if owner == plugin_name]:
                removed.append(self._global.pop(key)[1])
            for window_id in list(self._by_window.keys()):
                bucket = self._by_window[window_id]
                for key in [k for k, (owner, _) in bucket.items() if owner == plugin_name]:
                    removed.append(bucket.pop(key)[1])
                if not bucket:
                    self._by_window.pop(window_id, None)
        return removed

    def drop_window(self, window_id: str) -> List[Any]:
        """摘掉某窗口的全部服务（窗口关闭）。返回被摘除的实例列表。"""
        with self._lock:
            bucket = self._by_window.pop(window_id, None) or {}
        return [inst for _, inst in bucket.values()]

    # ── 读取 ──

    def get(self, key: str, window_id: str = "") -> Optional[Any]:
        """按窗口取服务：窗口桶优先，回退全局；都没有返回 None。"""
        if not key:
            return None
        with self._lock:
            if window_id:
                bucket = self._by_window.get(window_id)
                if bucket and key in bucket:
                    return bucket[key][1]
            entry = self._global.get(key)
        return entry[1] if entry else None

    def keys(self) -> List[str]:
        with self._lock:
            found = set(self._global)
            for bucket in self._by_window.values():
                found.update(bucket)
        return sorted(found)


_instance: Optional[WindowServiceRegistry] = None
_instance_lock = threading.Lock()


def get_window_services() -> WindowServiceRegistry:
    """进程级单例。"""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is None:
            _instance = WindowServiceRegistry()
        return _instance
