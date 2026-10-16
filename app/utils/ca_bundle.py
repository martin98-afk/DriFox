# -*- coding: utf-8 -*-
"""TLS 根证书兜底 — 确保 OpenSSL 能找到可用的 CA bundle（进程最早期调用）。

背景（2026-10 实机定位）：
DriFox 打包环境（PyInstaller）下 ``ssl.get_default_verify_paths()`` 解析到的
默认 CA 路径可能为空/不存在，导致进程内**所有** HTTPS/wss 校验失败：

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    self-signed certificate in certificate chain

实证影响面：飞书 WS 长连接（lark SDK 建 wss）在 TLS 阶段即挂、消息全丢；
``urllib`` 上报下载量报同源错误。二者都不经 ``app/utils/tls_trust.py`` 的
httpx 通道，因此该 helper 覆盖不到，需要进程级兜底。

策略（幂等、不越权、零依赖）：
1. ``SSL_CERT_FILE`` 已由用户/环境显式设置 → 尊重之，不动
2. 默认 context 能加载出证书 → 无需介入
3. 否则取 certifi 的 cacert.pem 写入 ``SSL_CERT_FILE``，令 OpenSSL 全局生效

选 ``SSL_CERT_FILE`` 而非逐库传 context 的原因：环境变量对 ``ssl`` 模块是
全局的，一次性覆盖 urllib / requests / websockets / aiohttp 等全部走默认
验证的调用点，无需逐个改造。企业网自签 CA 场景由 ``tls_trust.py`` 的
truststore 通道处理（那些调用点显式传 context，不受本兜底影响）。

调用时机：必须在任何 TLS 连接建立前。放 ``main.py`` 环境变量区（与
PYTHONIOENCODING 同级），早于所有网络库 import。
"""

from __future__ import annotations

import os
import ssl

_applied = False


def ensure_ca_bundle() -> str:
    """确保进程内 OpenSSL 能找到可用 CA bundle。返回生效的 CA 路径（未介入返回空串）。

    幂等：重复调用只做一次实际探测。
    """
    global _applied
    if _applied:
        return ""
    _applied = True

    if os.environ.get("SSL_CERT_FILE"):
        return ""  # 已有显式配置，尊重之
    try:
        ctx = ssl.create_default_context()
        if ctx.get_ca_certs():
            return ""  # 默认 bundle 可用，无需兜底
    except Exception:
        pass  # 加载失败 → 继续走兜底

    try:
        import certifi

        ca = certifi.where()
        if ca and os.path.exists(ca):
            os.environ["SSL_CERT_FILE"] = ca
            return ca
    except Exception:
        pass
    return ""
