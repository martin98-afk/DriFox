# -*- coding: utf-8 -*-
"""marketplace proxy 配置模块测试

覆盖：三种模式的 URL 改写 / httpx_kwargs / git_clone_args /
配置持久化往返 / 损坏回退 / 地址校验 / 未启用零开销。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

# 唯一包名加载 plugin-marketplace/ui：避免与其他插件的 ui 包抢占 sys.modules["ui"]（T8）
if "pm_ui" not in sys.modules:
    import types

    _pkg = types.ModuleType("pm_ui")
    _pkg.__path__ = [str(PLUGIN_MARKETPLACE / "ui")]
    _pkg.__package__ = "pm_ui"
    sys.modules["pm_ui"] = _pkg

from pm_ui.proxy import ProxyConfig, get_proxy_config  # noqa: E402

GIT_URL = "https://github.com/martin98-afk/drifox-plugins.git"
RAW_URL = "https://raw.githubusercontent.com/martin98-afk/drifox-plugins/main/marketplace.json"


def _mk(tmp_path: Path, enabled=True, mode="prefix", address="https://ghfast.top/") -> ProxyConfig:
    p = ProxyConfig(file=tmp_path / "proxy.json")
    assert p.save(enabled, mode, address)
    return p


def test_rewrite_prefix(tmp_path):
    p = _mk(tmp_path)
    assert p.rewrite_url(GIT_URL) == "https://ghfast.top/" + GIT_URL
    assert p.rewrite_url(RAW_URL) == "https://ghfast.top/" + RAW_URL


def test_rewrite_selfhost(tmp_path):
    p = _mk(tmp_path, mode="selfhost", address="https://my-proxy.deno.dev/")
    assert p.rewrite_url(GIT_URL) == "https://my-proxy.deno.dev/" + GIT_URL


def test_rewrite_http_unchanged(tmp_path):
    p = _mk(tmp_path, mode="http", address="http://127.0.0.1:7890")
    assert p.rewrite_url(GIT_URL) == GIT_URL


def test_rewrite_disabled_zero_overhead(tmp_path):
    p = _mk(tmp_path, enabled=False)
    assert p.rewrite_url(GIT_URL) == GIT_URL
    assert p.httpx_kwargs() == {}
    url, extra = p.git_clone_args(GIT_URL)
    assert url == GIT_URL and extra == []


def test_httpx_kwargs_http(tmp_path):
    p = _mk(tmp_path, mode="http", address="http://127.0.0.1:7890")
    assert p.httpx_kwargs() == {"proxy": "http://127.0.0.1:7890"}


def test_httpx_kwargs_prefix_empty(tmp_path):
    p = _mk(tmp_path)
    assert p.httpx_kwargs() == {}


def test_git_clone_args_http(tmp_path):
    p = _mk(tmp_path, mode="http", address="http://127.0.0.1:7890")
    url, extra = p.git_clone_args(GIT_URL)
    assert url == GIT_URL
    assert extra == ["-c", "http.proxy=http://127.0.0.1:7890"]


def test_git_clone_args_prefix(tmp_path):
    p = _mk(tmp_path)
    url, extra = p.git_clone_args(GIT_URL)
    assert url == "https://ghfast.top/" + GIT_URL
    assert extra == []


def test_save_load_roundtrip(tmp_path):
    f = tmp_path / "proxy.json"
    p = ProxyConfig(file=f)
    assert p.save(True, "http", "http://127.0.0.1:7890")
    q = ProxyConfig(file=f)
    assert q.enabled and q.mode == "http" and q.address == "http://127.0.0.1:7890"


def test_load_corrupted_file(tmp_path):
    f = tmp_path / "proxy.json"
    f.write_text("{not-json", encoding="utf-8")
    p = ProxyConfig(file=f)
    assert not p.enabled


def test_validate_rules():
    assert ProxyConfig.validate("prefix", "https://ghfast.top/") == (True, "")
    assert ProxyConfig.validate("selfhost", "https://my-proxy.deno.dev/") == (True, "")
    assert ProxyConfig.validate("http", "http://127.0.0.1:7890") == (True, "")
    # 缺协议
    ok, _ = ProxyConfig.validate("prefix", "ghfast.top/")
    assert not ok
    # http 模式缺端口
    ok, _ = ProxyConfig.validate("http", "http://127.0.0.1")
    assert not ok
    # 空地址
    ok, _ = ProxyConfig.validate("prefix", "  ")
    assert not ok
    # 未知模式
    ok, _ = ProxyConfig.validate("socks5", "http://x:1")
    assert not ok


def test_save_invalid_address_rejected(tmp_path):
    p = _mk(tmp_path, enabled=False)
    assert not p.save(True, "prefix", "not-a-url")
    assert not p.enabled  # 未落盘生效


def test_fetch_marketplace_proxy_fallback_direct(monkeypatch, tmp_path):
    """代理请求失败时回退直连拉取市场数据"""
    import json as _json

    from pm_ui import marketplace_manager as mm

    # 注入临时代理配置（前缀模式，指向不可达地址 → 必失败）
    proxy = ProxyConfig(file=tmp_path / "proxy.json")
    assert proxy.save(True, "prefix", "https://127.0.0.1:1/")
    monkeypatch.setattr(mm, "get_proxy_config", lambda: proxy)

    # 直连成功返回假市场数据
    fake_data = {"name": "drifox-official", "plugins": [{"name": "demo"}]}
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if url.startswith("https://127.0.0.1:1/"):
            raise RuntimeError("proxy unreachable")
        resp = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: fake_data})()
        return resp

    monkeypatch.setattr(mm.httpx, "get", fake_get)

    src = {"name": "drifox-official", "source": {"source": "url", "url": "https://raw.githubusercontent.com/x/y/main/marketplace.json"}}
    mgr = mm.MarketplaceSourceManager.__new__(mm.MarketplaceSourceManager)
    mgr._cache_dir = tmp_path
    mgr._status_file = tmp_path / "status.json"
    mgr._fetch_status = {}
    data = mgr.fetch_marketplace(src, force=True)

    assert data["plugins"][0]["name"] == "demo"
    # 先代理后直连：两次请求
    assert len(calls) == 2
    assert calls[0].startswith("https://127.0.0.1:1/")
    assert calls[1] == "https://raw.githubusercontent.com/x/y/main/marketplace.json"
    # 失败状态已记录
    assert mgr._fetch_status["drifox-official"]["ok"] is True


def test_installer_proxy_fallback_direct(monkeypatch, tmp_path):
    """git clone 代理失败时回退直连重跑一次"""
    from pm_ui import installer as inst

    proxy = ProxyConfig(file=tmp_path / "proxy.json")
    assert proxy.save(True, "prefix", "https://127.0.0.1:1/")
    monkeypatch.setattr(inst, "get_proxy_config", lambda: proxy)
    # 上报下载量是后台网络请求，测试中禁用
    monkeypatch.setattr(inst, "report_plugin_install", lambda name: None)

    calls = []

    def fake_sparse(self, url, subpath, ref, cache_dir, extra_args=None):
        calls.append((url, extra_args))
        if url.startswith("https://127.0.0.1:1/"):
            raise RuntimeError("proxy unreachable")
        # 模拟 clone 产物：在 cache_dir 下生成文件（后续 move 到 target）
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "__init__.py").write_text("# demo", encoding="utf-8")

    monkeypatch.setattr(inst.PluginInstaller, "_sparse_clone", fake_sparse)

    p = inst.PluginInstaller.__new__(inst.PluginInstaller)
    p._plugins_dir = tmp_path / "plugins"
    p._cache_dir = tmp_path / "cache"

    target = tmp_path / "plugins" / "demo"

    ok = p._download_and_move("demo", "https://github.com/x/demo.git", ".", "main", target)
    assert ok
    # 候选序列（9c7337c1 引入多候选重试后）：代理带 .git → 代理去 .git → 直连带 .git（成功即停）
    assert len(calls) == 3
    assert calls[0][0].startswith("https://127.0.0.1:1/")  # 代理（带 .git）失败
    assert calls[1][0].startswith("https://127.0.0.1:1/")  # 代理去 .git 失败
    assert calls[2][0] == "https://github.com/x/demo"  # 直连去 .git 成功（candidate #3）
    assert calls[2][1] == []  # 直连无额外参数
    assert (target / "__init__.py").exists()
