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
        "tool.register",
        "tool.unregister",
    }


def test_declared_flat_name_not_expanded(reg, monkeypatch, tmp_path):
    """A3 扁平名别名已退役：声明 ["lsp"] 不再展开为 lsp.query（池构建时不可用）。"""
    _fake_pm(monkeypatch, "demo", {"capabilities": ["lsp"]}, str(tmp_path))
    assert reg.resolve_declared("demo") == {"lsp"}


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


def test_manifest_change_without_wiring_keeps_stale_cache(reg, monkeypatch, tmp_path):
    """S1 语义反转后的新契约：manifest 变更但未接线 invalidate → 仍命中旧缓存。

    S1 去掉 stat 兜底后，「变更感知」唯一通道是 diff 点显式调 invalidate_declared；
    此用例钉死「无接线不失效」，防止实现退回靠 stat 兼底的旧语义。
    """
    from app.plugins.managers.plugin_manager import PluginManager

    info = SimpleNamespace(manifest={"capabilities": ["session.read"]}, path=str(tmp_path))
    monkeypatch.setattr(
        PluginManager, "get_instance", classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: info))
    )
    assert reg.resolve_declared("demo") == {"session.read"}

    # 只改 manifest 内容，不 invalidate：签名 (path, repr(raw), api_version) 变了，
    # 但缓存不感知（签名只在写入时计算，读取只比对），必须仍命中旧值。
    info.manifest = {"capabilities": ["ui.notify"]}
    assert reg.resolve_declared("demo") == {"session.read"}

    # 显式 invalidate（S7 接线点的行为等价物）后才换新
    reg.invalidate_declared("demo")
    assert reg.resolve_declared("demo") == {"ui.notify"}


def test_invalidate_declared_plugin_clears_only_that_plugin(reg, monkeypatch):
    """M1-D1：精细失效只清目标插件，不碰其他插件缓存条目。"""
    from app.plugins.managers.plugin_manager import PluginManager

    calls: list[str] = []
    infos = {
        "A": SimpleNamespace(manifest={"capabilities": ["session.read"]}, path=""),
        "B": SimpleNamespace(manifest={"capabilities": ["model.config"]}, path=""),
    }

    def _get_plugin(name):
        calls.append(name)
        return infos.get(name)

    monkeypatch.setattr(
        PluginManager, "get_instance", classmethod(lambda cls: SimpleNamespace(get_plugin=_get_plugin))
    )
    assert reg.resolve_declared("A") == {"session.read"}
    assert reg.resolve_declared("B") == {"model.config"}
    b_entry = reg._declared_cache["B"]
    assert calls == ["A", "B"]

    reg.invalidate_declared("A")
    # 精细路径只 pop A：B 条目原对象保留
    assert "A" not in reg._declared_cache
    assert reg._declared_cache["B"] is b_entry

    # 再次 resolve A：重新解析（get_plugin 对 A 的调用计数 +1）；B 不被触碰
    assert reg.resolve_declared("A") == {"session.read"}
    assert calls.count("A") == 2
    assert calls.count("B") == 1


def test_invalidate_declared_empty_clears_all_and_bumps_version(reg, monkeypatch):
    """M1-D2：空插件名全量失效——声明缓存/池规格清空且版本戳恰 +1。"""
    from app.plugins.managers.plugin_manager import PluginManager

    infos = {
        name: SimpleNamespace(manifest={"capabilities": ["session.read"]}, path="")
        for name in ("A", "B", "C")
    }
    monkeypatch.setattr(
        PluginManager,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: infos.get(n))),
    )
    for name in ("A", "B", "C"):
        assert reg.resolve_declared(name) == {"session.read"}
    # 预先制造 _pool_specs 条目，验证全清真的清到它
    reg.build_pool(surface="ui", host="H", plugin="A", declared=None)
    assert reg._pool_specs

    version_before = reg._entries_version
    reg.invalidate_declared("")
    assert reg._declared_cache == {}
    assert reg._pool_specs == {}
    assert reg._entries_version == version_before + 1


def test_manifest_change_rebuilds_without_stat(reg, monkeypatch, tmp_path):
    """M1-F1（失效路径）：去 stat 后声明变更仍生效，且全程零 os.stat。

    钉死 S1 契约：声明唯一数据源是 PluginManager 内存 manifest dict，
    变更感知走 invalidate_declared，解析路径禁止回退成每次 os.stat(path)。
    """
    import os

    from app.plugins.managers.plugin_manager import PluginManager

    info = SimpleNamespace(manifest={"capabilities": ["session.read"]}, path=str(tmp_path))
    monkeypatch.setattr(
        PluginManager, "get_instance", classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: info))
    )
    stat_calls = []
    real_stat = os.stat

    def _counting_stat(path, *args, **kwargs):
        stat_calls.append(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _counting_stat)

    narrow = reg.resolve_declared("demo")
    assert narrow == {"session.read"}

    # 窄→宽：失效后重解析必须拿到宽清单
    info.manifest = {"capabilities": ["session.read", "model.config", "ui.notify"]}
    reg.invalidate_declared("demo")
    wide = reg.resolve_declared("demo")
    assert wide == {"session.read", "model.config", "ui.notify"}
    assert "ui.notify" not in set(narrow)
    assert os.stat is not real_stat  # 确认 monkeypatch 生效，计数才有意义
    assert stat_calls == [], f"解析路径出现 os.stat 调用（回退成 stat 兑底）：{stat_calls}"
