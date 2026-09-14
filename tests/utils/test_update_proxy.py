# -*- coding: utf-8 -*-
"""更新链路代理解析测试（app/utils/update_proxy.py）

覆盖：四模式 URL 改写 / httpx 参数 / requests session 参数 /
尾斜杠归一 / 空地址容错 / 地址校验 / 系统代理探测文案 / 内容判据。
纯函数测试，不联网。
"""

import pytest

from app.utils import update_proxy
from app.utils.config import Settings


@pytest.fixture
def cfg():
    """每个用例都从 direct 起步并还原，避免用例之间互相污染配置

    直接赋值而非 monkeypatch：`ConfigItem.value` 是 Qt 绑定属性，
    monkeypatch.setattr 只设实例属性、绕过 validator 与 valueChanged，
    与项目测试惯例（tests/config、tests/core 全用直接赋值）也不一致。
    """
    s = Settings.get_instance()
    saved = (
        s.update_proxy_mode.value,
        s.update_proxy_prefix.value,
        s.update_proxy_url.value,
    )
    s.update_proxy_mode.value = update_proxy.MODE_DIRECT
    s.update_proxy_prefix.value = "https://ghfast.top/"
    s.update_proxy_url.value = ""
    try:
        yield s
    finally:
        s.update_proxy_mode.value = saved[0]
        s.update_proxy_prefix.value = saved[1]
        s.update_proxy_url.value = saved[2]


DOWNLOAD_URL = "https://github.com/martin98-afk/DriFox/releases/download/v0.6.0/x.exe"


def test_direct_mode_rewrites_nothing(cfg):
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


def test_prefix_mode_prepends(cfg):
    cfg.update_proxy_mode.value = update_proxy.MODE_PREFIX
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == "https://ghfast.top/" + DOWNLOAD_URL


def test_prefix_trailing_slash_normalized(cfg):
    """带不带尾斜杠结果必须一致，否则会拼出 // 双斜杠"""
    cfg.update_proxy_mode.value = update_proxy.MODE_PREFIX
    cfg.update_proxy_prefix.value = "https://ghfast.top"
    without = update_proxy.rewrite_url(DOWNLOAD_URL)
    cfg.update_proxy_prefix.value = "https://ghfast.top/"
    with_slash = update_proxy.rewrite_url(DOWNLOAD_URL)
    assert without == with_slash
    assert "//github.com" in without


def test_prefix_empty_address_falls_back_direct(cfg):
    """选了 prefix 但没填地址 → 退化为直连，不抛异常"""
    cfg.update_proxy_mode.value = update_proxy.MODE_PREFIX
    cfg.update_proxy_prefix.value = ""
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


def test_http_mode_rewrites_nothing(cfg):
    cfg.update_proxy_mode.value = update_proxy.MODE_HTTP
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


def test_system_mode_rewrites_nothing(cfg):
    cfg.update_proxy_mode.value = update_proxy.MODE_SYSTEM
    assert update_proxy.rewrite_url(DOWNLOAD_URL) == DOWNLOAD_URL


def test_httpx_kwargs_direct_forces_no_env(cfg):
    """direct 必须显式 trust_env=False：proxy=None 不生效"""
    assert update_proxy.httpx_kwargs() == {"trust_env": False}


def test_httpx_kwargs_system_uses_lib_default(cfg):
    cfg.update_proxy_mode.value = update_proxy.MODE_SYSTEM
    assert update_proxy.httpx_kwargs() == {}


def test_httpx_kwargs_prefix_forces_no_env(cfg):
    """prefix 是显式指定，不该被环境变量二次干扰"""
    cfg.update_proxy_mode.value = update_proxy.MODE_PREFIX
    assert update_proxy.httpx_kwargs() == {"trust_env": False}


def test_httpx_kwargs_http_returns_proxy(cfg):
    cfg.update_proxy_mode.value = update_proxy.MODE_HTTP
    cfg.update_proxy_url.value = "http://127.0.0.1:7890"
    assert update_proxy.httpx_kwargs() == {"proxy": "http://127.0.0.1:7890"}


def test_httpx_kwargs_http_empty_addr_degrades(cfg):
    """http 模式没填地址 → 退化直连，不把空串当代理"""
    cfg.update_proxy_mode.value = update_proxy.MODE_HTTP
    cfg.update_proxy_url.value = ""
    assert update_proxy.httpx_kwargs() == {"trust_env": False}


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
    cfg.update_proxy_mode.value = update_proxy.MODE_SYSTEM
    s = _FakeSession()
    update_proxy.apply_to_session(s)
    assert s.trust_env is True


def test_apply_to_session_http(cfg):
    cfg.update_proxy_mode.value = update_proxy.MODE_HTTP
    cfg.update_proxy_url.value = "http://127.0.0.1:7890"
    s = _FakeSession()
    update_proxy.apply_to_session(s)
    assert s.trust_env is False
    assert s.proxies == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


def test_validate_prefix_rejects_socks5():
    ok, msg = update_proxy.validate(update_proxy.MODE_PREFIX, "socks5://127.0.0.1:1080")
    assert not ok
    assert "socks" in msg.lower() or "不支持" in msg


def test_validate_http_requires_port():
    ok, msg = update_proxy.validate(update_proxy.MODE_HTTP, "http://127.0.0.1")
    assert not ok
    assert "端口" in msg


def test_validate_http_rejects_socks5():
    ok, msg = update_proxy.validate(update_proxy.MODE_HTTP, "socks5://127.0.0.1:1080")
    assert not ok


def test_validate_accepts_valid():
    assert update_proxy.validate(update_proxy.MODE_PREFIX, "https://ghfast.top/")[0]
    assert update_proxy.validate(update_proxy.MODE_HTTP, "http://127.0.0.1:7890")[0]


def test_validate_empty_rejected():
    assert not update_proxy.validate(update_proxy.MODE_PREFIX, "")[0]
    assert not update_proxy.validate(update_proxy.MODE_HTTP, "")[0]


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
