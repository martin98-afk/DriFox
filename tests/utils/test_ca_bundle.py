# -*- coding: utf-8 -*-
"""CA bundle 兜底语义：环境变量优先 / 默认可用不介入 / 失效时回退 certifi。

背景（2026-10 实机定位）：打包环境（PyInstaller）下 ssl 默认 CA 路径可能失效，
导致进程内所有 HTTPS/wss 校验失败 —— 实证飞书 WS 长连接在 TLS 阶段即挂、
urllib 报 CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain。
app/utils/ca_bundle.py 在进程最早期兜底：默认 bundle 不可用时把 certifi 的
cacert.pem 写进 SSL_CERT_FILE，令 OpenSSL 全局生效。

测试口径：三分支行为 + 幂等 + 不越权（不覆盖显式配置）。
"""
import os
import ssl
from pathlib import Path

import pytest

from app.utils import ca_bundle


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """每个用例重置模块级幂等标志与环境变量，避免相互污染。"""
    monkeypatch.setattr(ca_bundle, "_applied", False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    yield


def test_explicit_env_not_overridden(monkeypatch):
    """分支 3：用户/环境已显式设置 SSL_CERT_FILE → 尊重之，不介入。"""
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca.pem")
    ca_bundle.ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"


def test_default_bundle_ok_no_intervention(monkeypatch):
    """分支 1：默认 context 能加载出证书 → 不写环境变量。

    真实环境（开发机/macOS 系统 CA 可用）即此分支。
    """
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        pytest.skip("本机默认 CA 不可用，此分支不适用")

    ca_bundle.ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ


def test_fallback_to_certifi_when_default_broken(monkeypatch):
    """分支 2：默认 CA 加载不出 → 回退 certifi，且路径指向存在的 cacert.pem。"""
    _real_ctx = ssl.create_default_context

    class _EmptyCtx:
        def get_ca_certs(self, binary_form=False):
            return []

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: _EmptyCtx())
    result = ca_bundle.ensure_ca_bundle()

    assert result, "默认 CA 失效时必须回退出可用 bundle"
    assert os.environ["SSL_CERT_FILE"] == result
    assert Path(result).exists()
    assert "cacert.pem" in result
    # 兜底 bundle 必须真的能用来验证（用真实 context 加载证书，绕开上面的假函数）
    assert _real_ctx(cafile=result).get_ca_certs()


def test_default_ctx_raises_still_falls_back(monkeypatch):
    """默认 context 构造直接抛异常（打包环境更极端的失效形态）也要兜底。"""

    def _boom(*a, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(ssl, "create_default_context", _boom)
    result = ca_bundle.ensure_ca_bundle()
    assert result and Path(result).exists()


def test_idempotent(monkeypatch):
    """幂等：重复调用只生效一次，第二次返回空（不再探测/不重复写）。"""
    first = ca_bundle.ensure_ca_bundle()
    second = ca_bundle.ensure_ca_bundle()
    assert second == ""
    # 第二次不改变第一次的结论
    if first:
        assert os.environ["SSL_CERT_FILE"] == first


def test_no_certifi_available_returns_empty(monkeypatch):
    """certifi 缺失（极端裁剪环境）→ 返回空且不崩，不写坏环境变量。"""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "certifi":
            raise ImportError("simulated missing certifi")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    class _EmptyCtx:
        def get_ca_certs(self, binary_form=False):
            return []

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **kw: _EmptyCtx())
    result = ca_bundle.ensure_ca_bundle()
    assert result == ""
    assert "SSL_CERT_FILE" not in os.environ
