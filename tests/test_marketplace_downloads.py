# -*- coding: utf-8 -*-
"""插件市场下载量：同名求和 + 社区插件实时计数测试

覆盖：
- fetch_all 同名插件 downloads 求和（都有/一个有/都无/异常值）
- DownloadsFetcher 缓存命中/失败防抖/TTL 过期/批量查询
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

from ui.marketplace_manager import MarketplaceSourceManager  # noqa: E402


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
    from ui.downloads import DownloadsFetcher

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
    from ui.downloads import TTL

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
