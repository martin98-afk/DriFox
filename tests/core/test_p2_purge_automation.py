# -*- coding: utf-8 -*-
"""P2-3：purge 缺省自动化（T8.1 行 18 口径）。

三用例：标识符目录热重载后 importlib 取到新代码 / 声明照常 /
voice-input 类目录名不触发。
"""
import importlib
import sys
import types

from app.core.plugin_host_service import PluginHostService

resolve = PluginHostService._resolve_purge_prefixes
purge = PluginHostService._purge_module_prefixes


def test_identifier_dir_purge_picks_new_code(tmp_path, monkeypatch):
    """合法标识符目录名：隐式 purge 后 importlib 取到磁盘新代码。"""
    (tmp_path / "goodplug.py").write_text("VERSION = 'new'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    # 预塞旧模块对象（模拟 importlib 手动注册的滞留旧代码）
    stale = types.ModuleType("goodplug")
    stale.VERSION = "old"
    sys.modules["goodplug"] = stale

    assert resolve("goodplug", []) == ["goodplug"]
    purged = purge(["goodplug"])
    assert "goodplug" in purged
    mod = importlib.import_module("goodplug")  # 回到磁盘最新代码
    assert mod.VERSION == "new"


def test_declared_prefixes_still_work():
    """显式声明 module_prefixes 照常；非法标识符目录名不追加自动前缀。"""
    assert resolve("any-dir", ["assistant_hub_core"]) == ["assistant_hub_core"]
    assert resolve("voice-input", ["assistant_hub_core"]) == ["assistant_hub_core"]


def test_hyphen_dir_not_triggered():
    """voice-input 类目录名（含连字符，非标识符）不触发自动 purge。"""
    assert resolve("voice-input", []) == []
    assert resolve("my plug", []) == []
