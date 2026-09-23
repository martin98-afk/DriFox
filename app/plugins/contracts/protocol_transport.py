# -*- coding: utf-8 -*-
"""第三协议传输器契约 — 非 openai/responses 协议的请求发出与响应归一。

分派约定（worker 侧零协议知识）：
- ModelAdapter 在 protocol_flags().extra["transport"] 携带本契约实现时，worker 把
  请求发出完全委托给 transport；openai chat/completions 与 /v1/responses 为 worker
  原生通道，不经此契约（它们长在主程序里且久经测试，插件化属后续纯重构项）。
- create_stream 返回值必须是与 OpenAI SDK chunk 迭代兼容的流（choices[0].delta /
  finish_reason / usage），由 worker 的 _process_response 统一消费——协议差异在
  transport 内部消化（如 SSE→chunk 适配），不外溢。

与 adapter/serializer 的分工：adapter 决定「走哪个协议」（protocol + transport 声明），
serializer 决定「消息长什么样」，transport 决定「请求怎么发、响应怎么归一」。
新增协议 = 新插件三件套，主程序（worker/backend）零改动。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ProtocolTransport(Protocol):
    """协议传输器接口（无状态实现，随 adapter flags 携带）"""

    def create_stream(
        self,
        llm_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        auth_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """发请求并返回可迭代响应流（OpenAI chunk 兼容形状，需支持 close()）。

        Args:
            llm_config: 模型配置（API_URL / 模型名称 / 温度 / 思考模式等全量参数）
            messages: 内部格式消息列表（transport 自行经 serializer 转为协议形态）
            tools: OpenAI function 格式工具列表（无工具时 None）
            auth_headers: worker 已算好的认证/伪装头（extra_headers 可调用值求值结果）

        Returns:
            可迭代 + 可 close() 的流对象；失败抛 RuntimeError（走 worker 通用重试/报错链）
        """
        ...
