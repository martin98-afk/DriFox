# -*- coding: utf-8 -*-
"""TLS 系统证书链信任链路：helper 缓存语义 + SSL 报错人话翻译 + _request 转译。

背景：企业网络/代理对 HTTPS 做 TLS 拦截（插入自签 CA）时，Python 默认验证链
不读系统信任库 → CERTIFICATE_VERIFY_FAILED。修复后 urlopen/httpx 统一走
truststore context（None 回退默认验证），SSL 类报错翻译成带指引的中文。
"""

import importlib.util
import ssl
import urllib.error
from pathlib import Path

import pytest

from app.utils.tls_trust import system_ssl_context

_BUDDY_AUTH_FILE = Path("plugins/system-providers/providers/buddy_auth.py")

pytestmark = pytest.mark.skipif(not _BUDDY_AUTH_FILE.exists(), reason="system-providers 插件未安装")


def _load_buddy_auth():
    spec = importlib.util.spec_from_file_location("buddy_auth_test", _BUDDY_AUTH_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── app.utils.tls_trust ─────────────────────────────────


def test_system_ssl_context_returns_context_or_none():
    """truststore 可用返回 SSLContext；不可用回退 None（不抛异常）"""
    ctx = system_ssl_context()
    assert ctx is None or isinstance(ctx, ssl.SSLContext)


def test_system_ssl_context_cached():
    """进程级缓存：两次调用同一对象（避免轮询场景反复提取系统 CA）"""
    assert system_ssl_context() is system_ssl_context()


# ── buddy_auth 内联 helper 与报错翻译 ────────────────────


def test_buddy_auth_ssl_context_matches_semantics():
    """内联 helper 与主程序 helper 语义一致（None 回退，不抛异常）"""
    ba = _load_buddy_auth()
    ctx = ba._system_ssl_context()
    assert ctx is None or isinstance(ctx, ssl.SSLContext)


def test_friendly_ssl_error_translates_certificate_failure():
    """证书校验失败 → 翻译成带指引的人话"""
    ba = _load_buddy_auth()
    exc = urllib.error.URLError(
        OSError(8, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                   "self-signed certificate in certificate chain (_ssl.c:1000)")
    )
    msg = ba._friendly_ssl_error(exc)
    assert "证书" in msg
    assert "CERTIFICATE_VERIFY_FAILED" in msg  # 保留原文便于诊断
    assert "信任" in msg or "更换网络" in msg


def test_friendly_ssl_error_passes_through_plain_errors():
    """非 SSL 类网络错误原样透出，不被误翻译"""
    ba = _load_buddy_auth()
    exc = urllib.error.URLError(OSError("getaddrinfo failed"))
    assert ba._friendly_ssl_error(exc) == str(exc)


def test_request_translates_url_error(monkeypatch):
    """_request 遇 URLError（SSL 拦截）→ RuntimeError 中文指引（原 URLError 链式保留）"""
    ba = _load_buddy_auth()
    ssl_exc = urllib.error.URLError(
        ssl.SSLCertVerificationError(8, "[SSL: CERTIFICATE_VERIFY_FAILED] "
                                        "certificate verify failed: self-signed certificate "
                                        "in certificate chain")
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(ssl_exc))
    with pytest.raises(RuntimeError) as ei:
        ba._request("POST", "/v2/plugin/auth/state", body={})
    assert "证书" in str(ei.value)
    assert "信任" in str(ei.value) or "更换网络" in str(ei.value)
    assert ei.value.__cause__ is ssl_exc


def test_request_http_error_passthrough(monkeypatch):
    """HTTPError（4xx/5xx）仍走原读 body 逻辑，不被 URLError 分支吞掉"""
    ba = _load_buddy_auth()

    class _FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://copilot.tencent.com/x", 400, "Bad Request", {}, None)

        def read(self):
            return b'{"code": 10001, "message": "already claimed"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(_FakeHTTPError()))
    payload = ba._request("POST", "/v2/billing/meter/daily-checkin", body={})
    assert payload == {"code": 10001, "message": "already claimed"}
