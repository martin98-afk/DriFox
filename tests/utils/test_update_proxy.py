# -*- coding: utf-8 -*-
"""更新链路代理解析测试（app/utils/update_proxy.py）

覆盖：四模式 URL 改写 / httpx 参数 / requests session 参数 /
尾斜杠归一 / 空地址容错 / 地址校验 / 系统代理探测文案 / 内容判据。
纯函数测试，不联网。

隔离策略：`update_proxy` 的配置读取全部经模块级 `_cfg()` 入口，测试里
替换它为轻量假对象，不碰全局 `Settings` 单例。原因：全量跑 tests/utils/
时，前序测试（test_app_about_to_quit_receivers 末尾的 gc.collect()）会
把单例持有的 ConfigItem 回收掉，之后读写必抛 RuntimeError；本模块只
验证代理解析逻辑，不该被跨文件时序问题牵连。
"""

import pytest

from app.utils import update_proxy


class _FakeItem:
    """模拟 qfluentwidgets.ConfigItem 的 .value 读写"""

    def __init__(self, value):
        self.value = value


class _FakeSettings:
    """只暴露 update_proxy 真正读取的三个配置项"""

    def __init__(self, mode=update_proxy.MODE_DIRECT, prefix="https://ghfast.top/", url=""):
        self.update_proxy_mode = _FakeItem(mode)
        self.update_proxy_prefix = _FakeItem(prefix)
        self.update_proxy_url = _FakeItem(url)


@pytest.fixture
def cfg(monkeypatch):
    """注入假配置；每条用例独享实例，天然无跨用例污染"""
    fake = _FakeSettings()
    monkeypatch.setattr(update_proxy, "_cfg", lambda: fake)
    return fake


def _mode(cfg, value) -> None:
    cfg.update_proxy_mode.value = value


def _prefix(cfg, value) -> None:
    cfg.update_proxy_prefix.value = value


def _url(cfg, value) -> None:
    cfg.update_proxy_url.value = value


DOWNLOAD_URL = "https://github.com/martin98-afk/DriFox/releases/download/v0.6.0/x.exe"


# ── URL 改写 ──


def test_direct_mode_rewrites_nothing(cfg):
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


def test_prefix_mode_prepends(cfg):
    _mode(cfg, update_proxy.MODE_PREFIX)
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == "https://ghfast.top/" + DOWNLOAD_URL


def test_prefix_trailing_slash_normalized(cfg):
    """带不带尾斜杠结果必须一致，否则会拼出 // 双斜杠"""
    _mode(cfg, update_proxy.MODE_PREFIX)
    _prefix(cfg, "https://ghfast.top")
    without = update_proxy.rewrite_url(DOWNLOAD_URL)
    _prefix(cfg, "https://ghfast.top/")
    with_slash = update_proxy.rewrite_url(DOWNLOAD_URL)
    assert without == with_slash
    assert "//github.com" in without


def test_prefix_empty_address_falls_back_direct(cfg):
    """选了 prefix 但没填地址 → 退化为直连，不抛异常"""
    _mode(cfg, update_proxy.MODE_PREFIX)
    _prefix(cfg, "")
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


def test_http_mode_rewrites_nothing(cfg):
    _mode(cfg, update_proxy.MODE_HTTP)
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


def test_system_mode_rewrites_nothing(cfg):
    _mode(cfg, update_proxy.MODE_SYSTEM)
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


# ── httpx 参数 ──


def test_httpx_kwargs_direct_forces_no_env(cfg):
    """direct 必须显式 trust_env=False：proxy=None 不生效"""
    assert update_proxy.httpx_kwargs() == {"trust_env": False}


def test_httpx_kwargs_system_uses_lib_default(cfg):
    _mode(cfg, update_proxy.MODE_SYSTEM)
    assert update_proxy.httpx_kwargs() == {}


def test_httpx_kwargs_prefix_forces_no_env(cfg):
    """prefix 是显式指定，不该被环境变量二次干扰"""
    _mode(cfg, update_proxy.MODE_PREFIX)
    assert update_proxy.httpx_kwargs() == {"trust_env": False}


def test_httpx_kwargs_http_returns_proxy(cfg):
    _mode(cfg, update_proxy.MODE_HTTP)
    _url(cfg, "http://127.0.0.1:7890")
    assert update_proxy.httpx_kwargs() == {"proxy": "http://127.0.0.1:7890"}


def test_httpx_kwargs_http_empty_addr_degrades(cfg):
    """http 模式没填地址 → 退化直连，不把空串当代理"""
    _mode(cfg, update_proxy.MODE_HTTP)
    _url(cfg, "")
    assert update_proxy.httpx_kwargs() == {"trust_env": False}


# ── requests session 参数 ──


class _FakeSession:
    def __init__(self):
        self.trust_env = True
        self.proxies = {}


def test_apply_to_session_direct(cfg):
    s = _FakeSession()
    update_proxy.apply_to_session(s)
    assert s.trust_env is False
    assert s.proxies == {}


def test_apply_to_session_system(cfg):
    _mode(cfg, update_proxy.MODE_SYSTEM)
    s = _FakeSession()
    update_proxy.apply_to_session(s)
    assert s.trust_env is True


def test_apply_to_session_http(cfg):
    _mode(cfg, update_proxy.MODE_HTTP)
    _url(cfg, "http://127.0.0.1:7890")
    s = _FakeSession()
    update_proxy.apply_to_session(s)
    assert s.trust_env is False
    assert s.proxies == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


# ── 地址校验 ──


def test_validate_prefix_rejects_socks5():
    ok, msg = update_proxy.validate(update_proxy.MODE_PREFIX, "socks5://127.0.0.1:1080")
    assert not ok
    assert "socks" in msg.lower() or "不支持" in msg


def test_validate_http_requires_port():
    ok, msg = update_proxy.validate(update_proxy.MODE_HTTP, "http://127.0.0.1")
    assert not ok
    assert "端口" in msg


def test_validate_http_rejects_socks5():
    ok, _msg = update_proxy.validate(update_proxy.MODE_HTTP, "socks5://127.0.0.1:1080")
    assert not ok


def test_validate_accepts_valid():
    assert update_proxy.validate(update_proxy.MODE_PREFIX, "https://ghfast.top/")[0]
    assert update_proxy.validate(update_proxy.MODE_HTTP, "http://127.0.0.1:7890")[0]


def test_validate_empty_rejected():
    assert not update_proxy.validate(update_proxy.MODE_PREFIX, "")[0]
    assert not update_proxy.validate(update_proxy.MODE_HTTP, "")[0]


# ── 系统代理探测文案 ──


def test_probe_system_proxy_reports_disabled(monkeypatch):
    """系统代理关着但有残留地址 → 文案要说清「未启用」"""
    monkeypatch.setattr(
        update_proxy,
        "_read_system_proxy",
        lambda: {"enable": False, "server": "127.0.0.1:10808"},
    )
    text = update_proxy.probe_system_proxy()
    assert "未启用" in text


def test_probe_system_proxy_reports_enabled(monkeypatch):
    monkeypatch.setattr(
        update_proxy,
        "_read_system_proxy",
        lambda: {"enable": True, "server": "127.0.0.1:10808"},
    )
    text = update_proxy.probe_system_proxy()
    assert "127.0.0.1:10808" in text
    assert "未启用" not in text


def test_probe_system_proxy_reports_none(monkeypatch):
    monkeypatch.setattr(update_proxy, "_read_system_proxy", lambda: {"enable": False, "server": ""})
    text = update_proxy.probe_system_proxy()
    assert "未设置" in text or "未启用" in text


# ── 内容判据（防加速站首页假阳性）──


def test_content_judge_rejects_mirror_homepage():
    """加速站对不支持的路径返回 200 + 自家首页 HTML —— 必须判为不合格"""
    html = b'<!DOCTYPE html>\n<html lang="zh-CN">\n<head><meta charset="UTF-8">'
    assert not update_proxy._looks_like_text_content(html)


def test_content_judge_accepts_hash_file():
    """SHA256SUMS 形态：64 位十六进制 + 文件名，必须判为合格"""
    body = b"a29d3549e793a225fa540589e04dceaef6cd8f80  Drifox-Windows-Setup-v0.6.0.exe\n"
    assert update_proxy._looks_like_text_content(body)


def test_content_judge_rejects_empty_and_oversized():
    assert not update_proxy._looks_like_text_content(b"")
    assert not update_proxy._looks_like_text_content(b"x" * 5000)
