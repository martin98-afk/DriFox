# -*- coding: utf-8 -*-
"""轮询退役验证 — start() 不再起线程，scan_now() 仍可用

背景：tools/providers 组件变更改由 backend watchfiles 主链驱动
（kernel KNOWN_COMPONENTS 已含 tools/providers → builtin_reloaders
_reload_tools/_reload_providers 调 scan_now）。PluginToolWatcher.start
与 ProviderWatcher.start 的轮询线程退役，方法保留为空转；scan_now()
语义不变，外部调用方无需改。
"""

import threading
import time

from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher
from app.plugins.loaders.provider_loader import ProviderWatcher


def test_tool_watcher_start_no_thread(tmp_path, monkeypatch):
    """PluginToolWatcher.start 不再创建轮询线程（_thread 保持 None）。"""
    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader._PLUGIN_ROOTS", [tmp_path])
    w = PluginToolWatcher(roots=[tmp_path])
    w.start()
    time.sleep(0.2)
    assert w._thread is None  # 不再创建轮询线程
    w.stop()


def test_provider_watcher_start_no_thread(tmp_path):
    """ProviderWatcher.start 不再创建轮询线程（_thread 保持 None）。"""
    w = ProviderWatcher(roots=[tmp_path])
    w.start()
    time.sleep(0.2)
    assert w._thread is None
    w.stop()


def test_scan_now_still_works(tmp_path):
    """scan_now 兼容直通：空目录扫描不抛异常，_loaded 保持空。"""
    w = PluginToolWatcher(roots=[tmp_path])
    w.scan_now()  # 空目录 → 幂等无操作
    assert w._loaded == {}


def test_provider_watcher_scan_now(tmp_path):
    """ProviderWatcher.scan_now：空目录扫描不抛异常（与 PluginToolWatcher 对称）。"""
    w = ProviderWatcher(roots=[tmp_path])
    w.scan_now()  # 空目录 → 幂等无操作
    # _root_tracker 重置后保持空；注册表无插件来源时 _loaded 维度不适用
    assert w._root_tracker == {}
