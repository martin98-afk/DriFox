# -*- coding: utf-8 -*-
"""watcher 引用计数式抑制 —— 多插件同时安装/卸载的并发安全回归测试

背景（卡死根因）：旧实现单一截止时间戳（PluginHostService._suppress_watcher_until
= now + duration），enable/disable/install/uninstall/config_sync 各自覆盖写入；
多插件**同时**装卸（市场并发上限 3）时先结束的 worker 把抑制清零，后结束 worker
仍在大规模写/删 plugins 目录 → watchfiles 事件风暴 + 半成品插件被 import +
主线程重载风暴 → UI 卡死。

修复：suppress/resume 改为引用计数（app.core.plugin_host_service 模块级函数），
最后一个 resume 才真正放开 watcher；截止时间戳仅作旧调用方（config_sync 直接写）
的兼容叠加。

覆盖：
1. suppress/resume 基本引用语义
2. 并发叠加：两个操作重叠时，先 resume 不解抑制（引用计数）
3. 截止时间戳旧调用方兼容（config_sync 直写仍有效）
4. 卸载路径 remove() 全程抑制 + 卸载成功后精准重载一次（含引用归零）

运行: python -m pytest tests/test_plugin_host_watcher_suppression.py -v
"""
import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

from app.core.plugin_host_service import (  # noqa: E402
    PluginHostService,
    resume_plugin_watcher,
    suppress_plugin_watcher,
)

DUR = 60.0


@pytest.fixture(autouse=True)
def _reset_suppression_state():
    """测试后复位类级抑制状态，避免跨用例污染"""
    yield
    PluginHostService._watcher_suppress_refs = 0
    PluginHostService._suppress_watcher_until = 0.0


class TestSuppressRefcount:
    def test_suppress_then_resume(self):
        suppress_plugin_watcher(DUR)
        assert PluginHostService._watcher_suppress_refs == 1
        assert PluginHostService._suppress_watcher_until > time.time()
        # QObject 实例可访问类级属性
        svc = PluginHostService.__new__(PluginHostService)
        assert svc._watcher_suppressed() is True

        resume_plugin_watcher()
        assert PluginHostService._watcher_suppress_refs == 0
        assert PluginHostService._suppress_watcher_until == 0.0
        assert svc._watcher_suppressed() is False

    def test_overlap_concurrent_ops_keep_suppression(self):
        """并发叠加：两个操作重叠时先 resume 不解抑制（引用计数，卡死根因回归）"""
        suppress_plugin_watcher(DUR)  # worker A
        suppress_plugin_watcher(DUR)  # worker B（并发中）
        assert PluginHostService._watcher_suppress_refs == 2

        # A 先完成：resume 一次 → 引用未归零，抑制必须保持（B 仍在写插件目录）
        resume_plugin_watcher()
        assert PluginHostService._watcher_suppress_refs == 1
        svc = PluginHostService.__new__(PluginHostService)
        assert svc._watcher_suppressed() is True, "并发操作未全部结束时不得解除抑制"

        # B 完成：最后一次 resume 才真正放开
        resume_plugin_watcher()
        assert PluginHostService._watcher_suppress_refs == 0
        assert svc._watcher_suppressed() is False

    def test_legacy_deadline_write_still_works(self):
        """config_sync 等旧调用方直写 _suppress_watcher_until 仍被识别为抑制"""
        PluginHostService._suppress_watcher_until = time.time() + 5.0
        svc = PluginHostService.__new__(PluginHostService)
        assert svc._watcher_suppressed() is True
        # 引用计数为 0 也可被截止时间戳抑制
        assert PluginHostService._watcher_suppress_refs == 0

    def test_extra_resume_is_noop(self):
        suppress_plugin_watcher(DUR)
        resume_plugin_watcher()
        resume_plugin_watcher()  # 无持有者时的多余 resume 不应把计数扣成负数
        assert PluginHostService._watcher_suppress_refs == 0


def _make_installer(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    disabled_dir = tmp_path / "plugins-disabled"
    plugins_dir.mkdir()
    disabled_dir.mkdir()
    installer = object.__new__(__import__("ui.installer", fromlist=["PluginInstaller"]).PluginInstaller)
    installer._plugins_dir = plugins_dir
    installer._disabled_dir = disabled_dir
    installer._cache_dir = tmp_path / "cache"
    installer._system_dir = tmp_path / "system-plugins"
    installer._inst_map_cache = None
    installer._inst_map_ts = 0.0
    installer._status_map_cache = None
    installer._status_map_ts = 0.0
    installer._manifest_cache = {}
    installer.last_error = ""
    installer._quarantine_dir = tmp_path / ".plugin_quarantine"
    return installer


def _build_fake_plugin(base: Path, name: str) -> Path:
    plugin = base / name
    for i in range(3):  # 模拟整树（卸载会产生多个 delete 事件）
        sub = plugin / f"mod{i}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "x.py").write_text("# fake")
    return plugin


class TestDedupStamp:
    def test_stamp_watcher_dedup_after_targeted(self):
        """targeted 重载成功后预写 watcher 去重键（抑制安装后残留事件的重复重载）"""
        from PyQt5.QtCore import QObject

        svc = PluginHostService.__new__(PluginHostService)
        QObject.__init__(svc)
        cache: dict = {}
        svc._watcher_dedup_cache = cache

        svc._stamp_watcher_dedup_for(
            "plug-a",
            {"ui": True, "tools": True, "agents": 3, "hooks": False, "commands": False, "_event_seq": 7},
        )
        assert ("plug-a", "ui") in cache
        assert ("plug-a", "tools") in cache
        assert ("plug-a", "agents") in cache  # 正计数视为成功
        assert ("plug-a", "hooks") not in cache, "失败组件不预写去重键"
        assert not any(k[0] == "plug-a" and k[1].startswith("_") for k in cache), "下划线内部键不写"

    def test_stamp_noop_guards(self):
        from PyQt5.QtCore import QObject

        svc = PluginHostService.__new__(PluginHostService)
        QObject.__init__(svc)
        svc._watcher_dedup_cache = {}
        # 空插件名 / sentinel / 非 dict / 空缓存对象 全部静默 no-op
        svc._stamp_watcher_dedup_for("", {"ui": True})
        svc._stamp_watcher_dedup_for(PluginHostService._NEW_PLUGIN_SENTINEL, {"ui": True})
        svc._stamp_watcher_dedup_for("plug-a", None)
        # 空 dict 缓存已存在 + 合法请求 → 正常写入（空缓存不是 no-op 理由）
        svc._stamp_watcher_dedup_for("plug-a", {"ui": True})
        assert list(svc._watcher_dedup_cache.keys()) == [("plug-a", "ui")]
        # 实例完全无缓存属性（真实 __init__ 前）→ getattr 默认 None → no-op 不抛
        svc2 = PluginHostService.__new__(PluginHostService)
        QObject.__init__(svc2)
        svc2._stamp_watcher_dedup_for("plug-a", {"ui": True})


class TestInstallerRemoveSuppression:
    def test_uninstall_removes_under_suppression_and_reloads_once(self, tmp_path, monkeypatch):
        """卸载：整树删除期间持抑制引用；结束后精准重载目标插件一次，引用归零"""
        from ui.installer import PluginInstaller

        installer = _make_installer(tmp_path)
        _build_fake_plugin(installer._plugins_dir, "some-plug")

        targeted: list = []
        full: list = []

        class _FakeSvc:
            def reload_plugin_targeted(self, name, action=None):
                targeted.append((name, action))

            def reload_plugin_subsystems(self):
                full.append(True)

        monkeypatch.setattr(PluginHostService, "get_instance", classmethod(lambda cls: _FakeSvc()))
        # 测试无事件循环：QTimer.singleShot 改为立即执行
        monkeypatch.setattr("PyQt5.QtCore.QTimer.singleShot", staticmethod(lambda msec, fn: fn()))

        ok = installer.uninstall("some-plug")
        assert ok is True
        assert not (installer._plugins_dir / "some-plug").exists()
        # 精准重载一次且不触发全量
        assert targeted == [("some-plug", "uninstalled")], f"实际: {targeted}"
        assert full == []
        # 卸载结束后抑制引用必须归零（否则 watcher 永久停摆）
        assert PluginHostService._watcher_suppress_refs == 0
        assert PluginHostService._suppress_watcher_until == 0.0

    def test_uninstall_remove_returns_true_cleanup_safe(self, tmp_path, monkeypatch):
        """卸载不存在的插件：不重载（removed=False），引用仍归零（finally 兜底）"""
        from ui.installer import PluginInstaller

        installer = _make_installer(tmp_path)
        targeted: list = []

        class _FakeSvc:
            def reload_plugin_targeted(self, name, action=None):
                targeted.append((name, action))

        monkeypatch.setattr(PluginHostService, "get_instance", classmethod(lambda cls: _FakeSvc()))
        monkeypatch.setattr("PyQt5.QtCore.QTimer.singleShot", staticmethod(lambda msec, fn: fn()))

        ok = installer.uninstall("ghost-plug")
        assert ok is True
        assert targeted == [], "目录不存在时不应触发重载"
        assert PluginHostService._watcher_suppress_refs == 0
