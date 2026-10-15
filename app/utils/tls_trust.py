# -*- coding: utf-8 -*-
"""系统证书链信任 helper — TLS 验证走操作系统信任库（仅供主程序内部使用）。

仅 app/ 内部模块（http_client / update_proxy）引用；插件一律内联各自的
helper（providers 插件保持零主程序依赖，见 system-providers/providers/*）。

背景：企业网络/代理常对 HTTPS 做 TLS 拦截（插入自签 CA）。该 CA 通常已装入
系统信任库（浏览器/官方客户端放行），但 Python 的 ssl 默认用 certifi/内置
CA 列表，不读系统信任库 → CERTIFICATE_VERIFY_FAILED: self-signed certificate
in certificate chain。truststore（pip 官方同款方案）把验证委托给系统信任库：
macOS 钥匙串 / Windows 证书存储 / Linux 系统 CA 目录。

用法（ctx 为 None 时各库自动走默认验证，无需调用方分支）：
    ctx = system_ssl_context()
    httpx.Client(verify=ctx)
"""
import ssl
import threading

_CONTEXT: ssl.SSLContext | None = None
_RESOLVED = False
_LOCK = threading.Lock()


def system_ssl_context() -> ssl.SSLContext | None:
    """返回信任系统证书链的 SSLContext（进程级缓存）；truststore 不可用返回 None。"""
    global _CONTEXT, _RESOLVED
    with _LOCK:
        if not _RESOLVED:
            _RESOLVED = True
            try:
                import truststore

                _CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:  # noqa: BLE001 —— 未安装/平台不支持时静默回退默认验证
                _CONTEXT = None
        return _CONTEXT
