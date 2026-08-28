# -*- coding: utf-8 -*-
"""插件市场下载量：同名求和 + 社区插件实时计数测试

覆盖：
- fetch_all 同名插件 downloads 求和（都有/一个有/都无/异常值）
- DownloadsFetcher 缓存命中/失败防抖/TTL 过期/批量查询
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

# 唯一包名加载 plugin-marketplace/ui：避免与其他插件（context-usage-stats 等）的 ui 包
# 抢占 sys.modules["ui"] 导致全量收集失败（T8）。相对导入 .proxy/.data 由 __package__ 解析。
if "pm_ui" not in sys.modules:
    import types

    _pkg = types.ModuleType("pm_ui")
    _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
    _pkg.__package__ = "pm_ui"
    sys.modules["pm_ui"] = _pkg

from pm_ui.marketplace_manager import MarketplaceSourceManager  # noqa: E402


# ── Task 1: 同名求和 ──────────────────────────────────────


def _mk_mgr(tmp_path, sources_data):
    """构造 manager：get_sources 返回两个市场，fetch_marketplace 返回对应数据"""
    mgr = MarketplaceSourceManager.__new__(MarketplaceSourceManager)
    mgr._cache_dir = tmp_path
    mgr._status_file = tmp_path / "status.json"
    mgr._fetch_status = {}
    mgr._sources_file = tmp_path / "sources.json"

    src_defs = []
    for i, data in enumerate(sources_data):
        src_defs.append({"name": f"m{i}", "source": {"source": "url", "url": f"x{i}"}})

    def fake_sources():
        return src_defs

    def fake_fetch(src_def, force=False):
        idx = next(i for i, s in enumerate(src_defs) if s["name"] == src_def["name"])
        return sources_data[idx]

    mgr.get_sources = fake_sources
    mgr.fetch_marketplace = fake_fetch
    return mgr


def test_fetch_all_sums_downloads_same_name(tmp_path):
    """同名插件两个市场都有 downloads → 求和"""
    mgr = _mk_mgr(
        tmp_path,
        [
            {"name": "m0", "plugins": [{"name": "p1", "version": "1.0", "downloads": 10}]},
            {"name": "m1", "plugins": [{"name": "p1", "version": "1.0", "downloads": 5}]},
        ],
    )
    plugins, _, _ = mgr.fetch_all()
    assert len(plugins) == 1
    assert plugins[0]["downloads"] == 15


def test_fetch_all_one_missing_downloads(tmp_path):
    """同名插件一个有 downloads 一个没有 → 用有的那个"""
    mgr = _mk_mgr(
        tmp_path,
        [
            {"name": "m0", "plugins": [{"name": "p1", "version": "1.0", "downloads": 7}]},
            {"name": "m1", "plugins": [{"name": "p1", "version": "1.0"}]},
        ],
    )
    plugins, _, _ = mgr.fetch_all()
    assert plugins[0]["downloads"] == 7


def test_fetch_all_no_downloads_anywhere(tmp_path):
    """同名插件都无 downloads → 合并后无 downloads（后续走实时查询）"""
    mgr = _mk_mgr(
        tmp_path,
        [
            {"name": "m0", "plugins": [{"name": "p1", "version": "1.0"}]},
            {"name": "m1", "plugins": [{"name": "p1", "version": "1.0"}]},
        ],
    )
    plugins, _, _ = mgr.fetch_all()
    assert len(plugins) == 1
    assert plugins[0].get("downloads", 0) == 0


def test_fetch_all_bad_downloads_value(tmp_path):
    """downloads 为 None/字符串等异常值 → or 0 兜底不崩"""
    mgr = _mk_mgr(
        tmp_path,
        [
            {"name": "m0", "plugins": [{"name": "p1", "version": "1.0", "downloads": None}]},
            {"name": "m1", "plugins": [{"name": "p1", "version": "1.0", "downloads": "5"}]},
        ],
    )
    plugins, _, _ = mgr.fetch_all()
    assert plugins[0]["downloads"] == 5


# ── Task 2: DownloadsFetcher ──────────────────────────────


def _mk_fetcher(monkeypatch, responses: dict):
    """构造 fetcher：mock _get_one 返回给定 {name: count}，None 表示失败"""
    from pm_ui.downloads import DownloadsFetcher

    fetcher = DownloadsFetcher()

    def fake_get_one(name):
        return responses.get(name)

    monkeypatch.setattr(fetcher, "_get_one", fake_get_one)
    return fetcher


def test_fetch_missing_returns_counts(monkeypatch):
    fetcher = _mk_fetcher(monkeypatch, {"p1": 10, "p2": 3})
    result = fetcher.fetch_missing(["p1", "p2", "p3"])
    assert result == {"p1": 10, "p2": 3}  # p3 失败（None）→ 不入结果


def test_fetch_missing_cache_hit_no_second_call(monkeypatch):
    fetcher = _mk_fetcher(monkeypatch, {"p1": 10})
    calls = []

    orig_get_one = fetcher._get_one

    def counting_get_one(name):
        calls.append(name)
        return orig_get_one(name)

    monkeypatch.setattr(fetcher, "_get_one", counting_get_one)
    # 第一次：查
    r1 = fetcher.fetch_missing(["p1"])
    assert r1 == {"p1": 10}
    # 第二次：缓存命中，不重查
    r2 = fetcher.fetch_missing(["p1"])
    assert r2 == {"p1": 10}
    assert calls == ["p1"]


def test_fetch_missing_fail_dedupe_within_fail_ttl(monkeypatch):
    fetcher = _mk_fetcher(monkeypatch, {})  # 全部失败
    calls = []

    orig_get_one = fetcher._get_one

    def counting_get_one(name):
        calls.append(name)
        return orig_get_one(name)

    monkeypatch.setattr(fetcher, "_get_one", counting_get_one)
    fetcher.fetch_missing(["p1"])
    fetcher.fetch_missing(["p1"])  # 失败防抖内：不重试
    assert calls == ["p1"]


def test_fetch_missing_ttl_expiry_refetches(monkeypatch):
    from pm_ui.downloads import TTL

    fetcher = _mk_fetcher(monkeypatch, {"p1": 10})
    calls = []

    orig_get_one = fetcher._get_one

    def counting_get_one(name):
        calls.append(name)
        return orig_get_one(name)

    monkeypatch.setattr(fetcher, "_get_one", counting_get_one)
    fetcher.fetch_missing(["p1"])
    # 推进时间超过 TTL
    fetcher._cache["p1"] = (fetcher._cache["p1"][0], fetcher._cache["p1"][1] - TTL - 1)
    fetcher.fetch_missing(["p1"])
    assert calls == ["p1", "p1"]


# ── Task 3: UI 集成（渲染触发查询 + 回填重渲染）────────────


def _mk_card(monkeypatch, plugins):
    """构造卡片 + 返回插件数据"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pm_ui.cards import MarketplaceCard
    from pm_ui.marketplace_manager import MarketplaceSourceManager

    monkeypatch.setattr(
        MarketplaceSourceManager,
        "get_sources",
        lambda self: [{"name": "fake", "source": {"source": "url", "url": "x"}}],
    )
    monkeypatch.setattr(
        MarketplaceSourceManager,
        "fetch_marketplace",
        lambda self, src, force=False: {"name": "fake", "plugins": plugins},
    )
    card = MarketplaceCard()
    card.show()
    return card


def _pump(seconds=0.2):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    import time as _t

    deadline = _t.time() + seconds
    while _t.time() < deadline:
        if app is not None:
            app.processEvents()
        _t.sleep(0.01)


def test_render_no_fetch_when_live_query_disabled(monkeypatch, tmp_path):
    """实时查询关闭（_DOWNLOADS_LIVE_QUERY_ENABLED=False）→ 不查询、不回填

    2026-08 决策：官方源 downloads 靠 market.json 自带字段，社区源关闭
    CountAPI 实时查询（key 不存在大量 404 + 服务无批量接口）。
    恢复实时查询时：置开关 True 并把本测试改回「查询 → 回填」断言。
    """
    from pm_ui import cards as cards_mod

    assert cards_mod._DOWNLOADS_LIVE_QUERY_ENABLED is False, "实时查询开关应为关闭状态"

    fetched = []

    def fake_fetch_missing(names):
        fetched.extend(names)
        return {}

    monkeypatch.setattr(
        cards_mod,
        "get_downloads_fetcher",
        lambda: type("F", (), {"fetch_missing": fake_fetch_missing})(),
    )

    plugins = [{"name": "community-p1", "version": "1.0", "_marketplace": "community"}]
    card = _mk_card(monkeypatch, plugins)
    card.show_card()
    _pump(0.5)

    # 重新触发渲染（若查询开启，此处会发起查询）
    card._render_plugins(plugins)
    _pump(0.5)

    assert fetched == [], "实时查询关闭时不应发起查询"
    assert plugins[0].get("downloads") is None, "不应回填 downloads"


def test_render_no_fetch_when_all_have_downloads(monkeypatch):
    """所有插件都有 downloads → 不触发查询"""
    from ui import cards as cards_mod

    plugins = [{"name": "official-p1", "version": "1.0", "downloads": 99, "_marketplace": "official"}]
    card = _mk_card(monkeypatch, plugins)

    fetched = []

    def fake_fetch_missing(names):
        fetched.extend(names)
        return {}

    monkeypatch.setattr(cards_mod, "get_downloads_fetcher", lambda: type("F", (), {"fetch_missing": fake_fetch_missing})())
    card.show_card()
    card._render_plugins(plugins)
    _pump(0.3)

    assert fetched == [], "全部有 downloads 时不应发起查询"
