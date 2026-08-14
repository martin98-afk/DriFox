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

import threading

import httpx

# 占位值：SDK 构造 client 要求非空 api_key，剥头后该值不会出现在请求中
_PLACEHOLDER_API_KEY = "not-needed"

# openai resources 子模块预导入锁：防止多线程首次访问 client.chat/client.responses
# 时并发 import 子模块，触发 Python 3.14 import 锁死锁检测
# （_ModuleLock deadlock，见 openai/_module_client.py 的 LazyProxy 懒加载机制）
_OPENAI_RESOURCES_LOCK = threading.Lock()
_OPENAI_RESOURCES_LOADED = False

_OPENAI_RESOURCES_MODULES = (
    "openai.resources.chat",
    "openai.resources.chat.completions",
    "openai.resources.responses",
    "openai.resources.responses.responses",
    "openai.resources.completions",
    "openai.resources.embeddings",
    "openai.resources.models",
    # 插件常用端点（gpt-image-2 → images，voice-reply → audio）
    "openai.resources.images",
    "openai.resources.audio",
    "openai.resources.files",
)


def preload_openai_resources() -> None:
    """主线程预导入 openai resources 子模块，消除运行时多线程并发导入死锁。

    openai SDK 的 client.chat / client.responses 等是 LazyProxy 懒加载：
    首次访问才 import 对应子模块（openai.resources.chat 等）。
    多个 worker 线程（对话/子智能体/压缩/摘要）首次同时访问不同端点时，
    Python 3.14 的 import 锁会检测到循环等待并抛 deadlock。

    本函数在启动早期（主线程、worker 线程创建前）一次性完成全部导入；
    内部加锁保证即使被并发调用也只会执行一次。
    """
    global _OPENAI_RESOURCES_LOADED
    if _OPENAI_RESOURCES_LOADED:
        return
    with _OPENAI_RESOURCES_LOCK:
        if _OPENAI_RESOURCES_LOADED:
            return
        import importlib

        for module_name in _OPENAI_RESOURCES_MODULES:
            try:
                importlib.import_module(module_name)
            except Exception:
                # 单个子模块导入失败不影响其它模块（如 realtime 依赖额外包）
                continue
        _OPENAI_RESOURCES_LOADED = True


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

    # 兜底：构造 client 前确保 resources 子模块已加载（主线程已预导入时无开销）
    preload_openai_resources()

    api_key = (api_key or "").strip()
    kwargs = {"api_key": api_key or _PLACEHOLDER_API_KEY, "base_url": base_url}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if not api_key:
        # 免 key 调用：自定义 transport 剥离 authorization 头
        kwargs["http_client"] = httpx.Client(timeout=timeout, transport=_StripAuthTransport())
    return OpenAI(**kwargs)
