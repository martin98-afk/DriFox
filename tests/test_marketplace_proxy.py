# -*- coding: utf-8 -*-
"""marketplace proxy 配置模块测试

覆盖：三种模式的 URL 改写 / httpx_kwargs / git_clone_args /
配置持久化往返 / 损坏回退 / 地址校验 / 未启用零开销。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))

from ui.proxy import ProxyConfig, get_proxy_config  # noqa: E402

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
