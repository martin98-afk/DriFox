# -*- coding: utf-8 -*-
"""
API 客户端模块 — 统一 LLM API 调用层

消除 OpenAIChatWorker 和 SubAgentExecutor 之间的重复代码：
- 请求参数构建（PARAM_SCHEMA 映射、思考模式、认证头）
- max_tokens 上限计算
- 重试与错误处理（网络错误、限流、服务端过载）
- 流式 / 非流式响应解析

设计原则：
- ChatAPIClient 持有 HTTP 客户端，可被多个 worker 复用
- 流式调用（ChatWorker）和非流式调用（SubAgentExecutor）共享同一套请求构建逻辑
- 重试逻辑统一在 _make_api_call 中，worker 不需要自己实现
"""
import base64
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Generator

import httpcore
import httpx
from openai import (
    BadRequestError, RateLimitError, APIError, APIConnectionError,
)
from loguru import logger

from app.constants import PARAM_SCHEMA
from app.core.provider_profile import get_provider_profile


# 预编译正则
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class ChatAPIClient:
    """统一 LLM API 客户端

    Args:
        llm_config: 模型配置字典
        stream: 是否使用流式输出（True=ChatWorker 流式，False=SubAgent 非流式）
        is_cancelled_getter: 可选回调，返回当前是否已取消（用于重试等待中检查）
    """

    def __init__(
        self,
        llm_config: Dict[str, Any],
        stream: bool = True,
        is_cancelled_getter: Optional[Callable[[], bool]] = None,
    ):
        self.llm_config = llm_config
        self.stream = stream
        self._is_cancelled_getter = is_cancelled_getter or (lambda: False)
        self._http_client: Optional[Any] = None

    # ============================================================
    # 公共方法
    # ============================================================

    def build_request_kwargs(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """构建 API 请求参数字典

        Args:
            messages: 消息列表
            tools: 工具 schema（可选）

        Returns:
            请求参数字典，可直接传入 client.chat.completions.create(**kwargs)
        """
        api_key = self.llm_config.get("API_KEY", "").strip()
        base_url = self.llm_config.get("API_URL") or None
        model = str(self.llm_config.get("模型名称", "gpt-4o"))

        skip_params = {"temperature", "top_p", "presence_penalty", "frequency_penalty"}
        if model and (model.startswith("o1") or model.startswith("o3")):
            skip_params.update({"temperature", "top_p"})

        extra_body = {}
        for cn_key, value in self.llm_config.items():
            if cn_key in ("API_KEY", "API_URL", "模型名称", "系统提示", "启用技能"):
                continue
            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key and _VALID_IDENTIFIER_PATTERN.match(cn_key):
                en_key = cn_key
            if not en_key or en_key in skip_params:
                continue
            if en_key in ("max_tokens",):
                continue
            extra_body[en_key] = value

        # max_tokens 处理
        max_tokens = self.llm_config.get("最大Token")
        if max_tokens is not None:
            extra_body["max_tokens"] = self.cap_max_output_tokens(model, max_tokens)

        # 思考模式（DeepSeek / 智谱AI）
        profile = get_provider_profile(self.llm_config)
        provider_family = profile["family"]

        if provider_family == "deepseek":
            thinking_mode = self.llm_config.get("思考模式")
            if thinking_mode is True:
                extra_body["thinking"] = {"type": "enabled"}
            elif thinking_mode is False:
                extra_body["thinking"] = {"type": "disabled"}
                extra_body.pop("reasoning_effort", None)
        elif provider_family == "zhipu":
            thinking_mode = self.llm_config.get("思考模式")
            if thinking_mode is True:
                extra_body["thinking"] = {"type": "enabled"}
            elif thinking_mode is False:
                extra_body["thinking"] = {"type": "disabled"}

        # 认证头
        auth_headers = None
        auth_type = self.llm_config.get("认证方式", "bearer")
        if auth_type == "bce":
            auth_str = f"{api_key}:{api_key}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            auth_headers = {"Authorization": f"Basic {b64_auth}"}

        is_o1 = model.startswith("o1") or model.startswith("o3")

        kwargs = {
            "model": model,
            "messages": messages,
            "stream": self.stream,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            kwargs["tools"] = tools
        if auth_headers:
            kwargs["extra_headers"] = auth_headers

        return kwargs, is_o1

    def cap_max_output_tokens(self, model: str, requested: int) -> int:
        """计算 max_tokens 合理上限

        核心原则：用户明确设置的 max_tokens 应被尊重，
        仅对已知的模型特定限制做软限制。
        """
        try:
            requested_int = int(requested)
        except Exception:
            return requested

        profile = get_provider_profile(self.llm_config)
        if requested_int <= 0:
            return int(profile.get("max_output_tokens", 8192))

        absolute_limit = int(profile.get("absolute_limit", 65536))
        family = profile.get("family", "")
        model_name = (model or "").lower()

        if family == "openai":
            if "gpt-4-turbo" in model_name and requested_int > 4096:
                return 4096
        # 其他 family 不做额外限制

        return min(requested_int, absolute_limit)

    def make_api_call(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        retry_count: int = 3,
        retry_delay: float = 2.0,
    ) -> Generator[Any, None, None]:
        """调用 LLM API（支持流式和非流式，自动重试）

        Yields:
            流式模式：yield 每个 chunk
            非流式模式：yield 单个 response 对象后立即返回

        Raises:
            超过重试次数后抛出最终异常
        """
        kwargs, _ = self.build_request_kwargs(messages, tools)

        api_key = self.llm_config.get("API_KEY", "").strip() or "dummy"
        base_url = self.llm_config.get("API_URL") or None

        from app.core.retry_helper import create_api_call_with_retry
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=120.0,
        )

        def create_completion():
            return client.chat.completions.create(**kwargs)

        response = create_api_call_with_retry(client, create_completion)

        if self.stream:
            yield from response
        else:
            yield response