# -*- coding: utf-8 -*-
"""OpenAI client 构造辅助：无 API key 时剥离 Authorization 头，实现免 key 匿名调用。

背景：
- openai SDK 构造 client 强制要求非空 api_key（空字符串直接抛 OpenAIError），
  且 api_key 非空时必然发送 `Authorization: Bearer <key>` 头。
- 对免 key 端点（OpenCode 免费模型、本地 Ollama 等）：
  - 服务端不校验 key 或支持匿名调用 → 应不带 Authorization 头
  - 传占位 key 反而可能被拒（实测 OpenCode 对 `Bearer not-needed` 返回 401 Invalid API key）
- 因此：api_key 为空时用自定义 transport 剥离 authorization 头，实现真正的免 key 调用；
  云端认证端点无 key 时请求发出后服务端返回 401，走现有错误处理。
"""

import httpx

# 占位值：SDK 构造 client 要求非空 api_key，剥头后该值不会出现在请求中
_PLACEHOLDER_API_KEY = "not-needed"


class _StripAuthTransport(httpx.HTTPTransport):
    """请求发出前移除 Authorization 头，实现免 key 匿名调用"""

    def handle_request(self, request):
        request.headers.pop("authorization", None)
        return super().handle_request(request)


def build_openai_client(api_key: str, base_url: str, timeout=None):
    """构造 OpenAI client。

    参数：
        api_key: 配置中的 API_KEY（可能为空）
        base_url: API 端点
        timeout: SDK 超时（float / httpx.Timeout），None 表示不覆盖默认

    返回：
        openai.OpenAI 实例。api_key 为空时不发送 Authorization 头。
    """
    from openai import OpenAI

    api_key = (api_key or "").strip()
    kwargs = {"api_key": api_key or _PLACEHOLDER_API_KEY, "base_url": base_url}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if not api_key:
        # 免 key 调用：自定义 transport 剥离 authorization 头
        kwargs["http_client"] = httpx.Client(timeout=timeout, transport=_StripAuthTransport())
    return OpenAI(**kwargs)
