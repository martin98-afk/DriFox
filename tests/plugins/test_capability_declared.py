# -*- coding: utf-8 -*-
"""manifest 声明解析：四态（有清单/空清单/api=2 无清单/api=1 无清单）+ 缓存 + 别名。"""
from types import SimpleNamespace

import pytest


def _fake_pm(monkeypatch, plugin_name, manifest, path=""):
    from app.plugins.managers.plugin_manager import PluginManager

    info = SimpleNamespace(manifest=manifest, path=path)
    fake = SimpleNamespace(get_plugin=lambda name: info if name == plugin_name else None)
    monkeypatch.setattr(PluginManager, "get_instance", classmethod(lambda cls: fake))


@pytest.fixture()
def reg():
    from app.plugins.capabilities.registry import CapabilityRegistry

    r = CapabilityRegistry()
    yield r
    r.clear()


def test_declared_list(reg, monkeypatch, tmp_path):
    _fake_pm(monkeypatch, "demo", {"capabilities": ["session.read", "tool.*"]}, str(tmp_path))
    assert reg.resolve_declared("demo") == {
        "session.read",
        "tool.read",
        "tool.inject",
        "tool.execute",
    }


def test_declared_alias_flat_name(reg, monkeypatch, tmp_path):
    _fake_pm(monkeypatch, "demo", {"capabilities": ["lsp"]}, str(tmp_path))
    assert reg.resolve_declared("demo") == {"lsp.query"}


def test_declared_empty_list(reg, monkeypatch, tmp_path):
    _fake_pm(monkeypatch, "demo", {"capabilities": []}, str(tmp_path))
    assert reg.resolve_declared("demo") == frozenset()


def test_api_v2_without_capabilities_is_strict(reg, monkeypatch, tmp_path, log_capture):
    _fake_pm(monkeypatch, "demo", {"api_version": 2}, str(tmp_path))
    assert reg.resolve_declared("demo") == frozenset()
    assert any("api_version>=2" in r for r in log_capture)


def test_api_v1_without_capabilities_is_compat(reg, monkeypatch, tmp_path, log_capture):
    _fake_pm(monkeypatch, "demo", {"api_version": 1}, str(tmp_path))
    assert reg.resolve_declared("demo") is None
    assert any("按旧语义全量注入" in r for r in log_capture)


def test_missing_manifest_is_compat(reg, monkeypatch, tmp_path):
    _fake_pm(monkeypatch, "demo", {}, str(tmp_path))
    assert reg.resolve_declared("demo") is None


def test_unknown_plugin_is_compat(reg, monkeypatch):
    _fake_pm(monkeypatch, "demo", {"api_version": 2}, "")
    assert reg.resolve_declared("other") is None


def test_empty_plugin_name_is_compat(reg):
    assert reg.resolve_declared("") is None


def test_cache_hits_avoid_reparse(reg, monkeypatch, tmp_path):
    """缓存命中：manifest 未变时复用已解析结果；查询失败时沿用缓存（不 fail-open）。"""
    from app.plugins.managers.plugin_manager import PluginManager

    info = SimpleNamespace(manifest={"capabilities": ["session.read"]}, path=str(tmp_path))
    fake = SimpleNamespace(get_plugin=lambda n: info)
    monkeypatch.setattr(PluginManager, "get_instance", classmethod(lambda cls: fake))
    assert reg.resolve_declared("demo") == {"session.read"}

    # 缓存已写入：同一 plugin 重复查询不该重新解析（值不变）
    assert reg.resolve_declared("demo") == {"session.read"}

    # 查询抛异常：沿用缓存值，不退化为全量放行
    monkeypatch.setattr(
        PluginManager,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: (_ for _ in ()).throw(RuntimeError("boom")))),
    )
    assert reg.resolve_declared("demo") == {"session.read"}


def test_query_failure_without_cache_is_strict(reg, monkeypatch):
    """无缓存的查询失败按空清单处理（最严口径，绝不放大权限）。"""
    from app.plugins.managers.plugin_manager import PluginManager

    monkeypatch.setattr(
        PluginManager,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: (_ for _ in ()).throw(RuntimeError("boom")))),
    )
    assert reg.resolve_declared("fresh") == frozenset()


def test_manifest_change_invalidates_cache(reg, monkeypatch, tmp_path):
    """manifest 变更（mtime 变）后重新解析。"""
    import os
    import time

    from app.plugins.managers.plugin_manager import PluginManager

    info = SimpleNamespace(manifest={"capabilities": ["session.read"]}, path=str(tmp_path))
    monkeypatch.setattr(
        PluginManager, "get_instance", classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: info))
    )
    assert reg.resolve_declared("demo") == {"session.read"}

    info.manifest = {"capabilities": ["ui.notify"]}
    later = time.time() + 5
    os.utime(tmp_path, (later, later))
    assert reg.resolve_declared("demo") == {"ui.notify"}
