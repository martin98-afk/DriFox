# -*- coding: utf-8 -*-
"""watcher 单例合约验证 — 无线程语义，scan_now() 仍可用

背景：tools/providers 组件变更改由 backend watchfiles 主链驱动
（kernel KNOWN_COMPONENTS 已含 tools/providers → builtin_reloaders
_reload_tools/_reload_providers 调 scan_now）。PluginToolWatcher 与
ProviderWatcher 均无线程/轮询语义；ensure_plugin_tool_watcher 为
进程级惰性单例。scan_now() 语义不变，外部调用方无需改。
"""

from app.plugins.loaders.plugin_tool_loader import (
    PluginToolWatcher,
    ensure_plugin_tool_watcher,
)
from app.plugins.loaders.provider_loader import ProviderWatcher


def test_tool_watcher_singleton_and_no_thread(tmp_path, monkeypatch):
    """ensure_plugin_tool_watcher 单例：两次调用同实例；watcher 无线程属性。"""
    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader._PLUGIN_ROOTS", [tmp_path])
    # 重置进程级单例，保证本测试构造的是干净实例
    import app.plugins.loaders.plugin_tool_loader as mod

    old = mod._plugin_watcher
    mod._plugin_watcher = None
    try:
        w1 = ensure_plugin_tool_watcher()
        w2 = ensure_plugin_tool_watcher()
        assert w1 is w2  # 单例
        assert not hasattr(w1, "_thread"), "退役后不应有线程槽位"
        w1.scan_now()  # 空目录 → 幂等无操作
    finally:
        mod._plugin_watcher = old


def test_provider_watcher_no_thread(tmp_path):
    """ProviderWatcher 无线程语义：无线程槽位，start/stop 不存在。"""
    w = ProviderWatcher(roots=[tmp_path])
    assert not hasattr(w, "_thread"), "退役后不应有线程槽位"
    assert not hasattr(w, "stop"), "退役后不应保留 stop"
    w.scan_now()  # 空目录 → 幂等无操作
    assert w._root_tracker == {}


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
