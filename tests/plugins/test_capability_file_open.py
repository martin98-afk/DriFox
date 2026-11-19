# -*- coding: utf-8 -*-
"""能力 file.open：注册接管项 / 声明门控 / 打开降级 / 注销。

覆盖三层：
1. catalog 登记（medium、value_style=value、ui 面）
2. provider 门面（register_opener / unregister_opener / openers / open）
3. 宿主注册表的声明门控（register_file_opener 要求 manifest 声明 file.open）
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    reg = UIPluginRegistry.get_instance()
    saved = dict(reg._file_openers)
    reg._file_openers.clear()
    yield
    reg._file_openers.clear()
    reg._file_openers.update(saved)
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _api(monkeypatch, plugin="demo", declared=None):
    """取 file.open 门面（显式 declared，不走 manifest 解析）。"""
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("ui")
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="ui",
        host=SimpleNamespace(_window_id="w1"),
        plugin=plugin,
        declared={"file.open"} if declared is None else declared,
    )
    return pool["file.open"]


def _opener(calls, accept=True):
    def can_open(path):
        calls.append(("can", path))
        return accept

    def open_file(path):
        calls.append(("open", path))
        return accept

    return can_open, open_file


# ──────────────── catalog ────────────────


def test_catalog_registered():
    from app.plugins.capabilities import catalog
    from app.plugins.contracts.capabilities import CapabilityLevel

    spec = catalog.get("file.open")
    assert spec is not None
    assert spec.level is CapabilityLevel.MEDIUM
    assert spec.value_style == "value"
    assert spec.surfaces == frozenset({"ui"})
    assert spec.group == "file"


# ──────────────── provider 门面 ────────────────


def test_facade_keys(monkeypatch):
    api = _api(monkeypatch)
    assert set(api) == {"open", "register_opener", "unregister_opener", "openers"}
    assert all(callable(fn) for fn in api.values())


def test_register_and_list(monkeypatch):
    api = _api(monkeypatch)
    calls = []
    can_open, open_file = _opener(calls)
    assert api["register_opener"]("preview", can_open, open_file, priority=7) is True

    listed = api["openers"]()
    assert len(listed) == 1
    assert listed[0] == {"plugin": "demo", "opener_id": "preview", "priority": 7}


def test_register_rejects_bad_args(monkeypatch):
    api = _api(monkeypatch)
    assert api["register_opener"]("", lambda p: True, lambda p: True) is False
    assert api["register_opener"]("x", None, lambda p: True) is False
    assert api["openers"]() == []


def test_register_denied_without_declaration(monkeypatch):
    """插件未声明 file.open：注册被拒（门控在宿主注册表侧）。"""
    from app.plugins.capabilities.registry import CapabilityRegistry

    monkeypatch.setattr(CapabilityRegistry, "resolve_declared", lambda self, plugin: frozenset())
    api = _api(monkeypatch, declared={"file.open"})
    calls = []
    can_open, open_file = _opener(calls)
    assert api["register_opener"]("preview", can_open, open_file) is False
    assert api["openers"]() == []


def test_register_allowed_for_legacy_plugin(monkeypatch):
    """旧插件未写 capabilities 清单（declared=None）按兼容期放行。"""
    from app.plugins.capabilities.registry import CapabilityRegistry

    monkeypatch.setattr(CapabilityRegistry, "resolve_declared", lambda self, plugin: None)
    api = _api(monkeypatch)
    calls = []
    can_open, open_file = _opener(calls)
    assert api["register_opener"]("preview", can_open, open_file) is True


def test_unregister_opener(monkeypatch):
    api = _api(monkeypatch)
    calls = []
    can_open, open_file = _opener(calls)
    api["register_opener"]("preview", can_open, open_file)
    assert api["unregister_opener"]("preview") is True
    assert api["unregister_opener"]("preview") is False
    assert api["openers"]() == []


# ──────────────── 打开路由 ────────────────


def test_open_hits_opener_chain(monkeypatch):
    api = _api(monkeypatch)
    calls = []
    can_open, open_file = _opener(calls)
    api["register_opener"]("preview", can_open, open_file)

    assert api["open"]("/tmp/a.md") is True
    assert calls == [("can", "/tmp/a.md"), ("open", "/tmp/a.md")]


def test_open_prefers_high_priority(monkeypatch):
    api = _api(monkeypatch)
    order = []

    def make(tag):
        def can_open(path):
            return True

        def open_file(path):
            order.append(tag)
            return True

        return can_open, open_file

    api["register_opener"]("low", *make("low"), priority=1)
    api["register_opener"]("high", *make("high"), priority=10)
    assert api["open"]("/tmp/a.md") is True
    assert order == ["high"]


def test_open_fallback_to_system(monkeypatch):
    """无人接管：fallback=True 时交系统程序。"""
    from PyQt5.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True))
    api = _api(monkeypatch)
    assert api["open"]("/tmp/a.md") is True
    assert opened and opened[0].endswith("/tmp/a.md")


def test_open_fallback_disabled(monkeypatch):
    """fallback=False：无人接管直接 False，不碰系统程序。"""
    from PyQt5.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url) or True))
    api = _api(monkeypatch)
    assert api["open"]("/tmp/a.md", fallback=False) is False
    assert opened == []


def test_open_empty_path(monkeypatch):
    api = _api(monkeypatch)
    assert api["open"]("") is False


# ──────────────── 插件声明契约 ────────────────


def _capabilities(plugin: str) -> list:
    manifest = Path(__file__).resolve().parents[2] / "plugins" / plugin / ".drifox-plugin" / "plugin.json"
    return list(json.loads(manifest.read_text(encoding="utf-8")).get("capabilities") or [])


def test_file_tree_declares_capabilities():
    caps = _capabilities("file-tree")
    assert "file.open" in caps
    assert "window.workbench" in caps


def test_artifacts_manager_declares_file_open():
    assert "file.open" in _capabilities("artifacts-manager")
