# -*- coding: utf-8 -*-
"""协议传输器契约 — 请求发出与响应归一（现覆盖 chat/completions 与所有插件协议）。

分派约定（worker 侧零协议知识）：
- ModelAdapter 在 protocol_flags().extra["transport"] 携带本契约实现时，worker 把
  请求发出完全委托给 transport；无 adapter 声明时，worker 回退
  TransportRegistry 的默认实现（system-transports/openai_chat）。
  /v1/responses 为 worker 原生通道（二期迁移）。
- create_stream 返回值被迭代时必须产出 StreamEvent（见 stream_sink.py），由
  StreamEventSink.consume 消费驱动 worker 状态机——协议差异在 transport 与 sink
  内部消化，不外溢到 worker。

与 adapter/serializer 的分工：adapter 决定「走哪个协议」（protocol + transport 声明），
serializer 决定「消息长什么样」，transport 决定「请求怎么发、响应怎么归一」，
sink 决定「事件如何驱动状态机」。
新增协议 = 新插件（transport + 可选 sink），主程序（worker/backend）零改动。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ProtocolTransport(Protocol):
    """协议传输器接口（无状态实现，随 adapter flags 携带）

    输出语义：create_stream 返回的流被迭代时产出
    `app.plugins.contracts.stream_sink.StreamEvent`（归一化事件），
    由 StreamEventSink.consume 消费驱动 worker 状态机。

    可选能力（worker 经 hasattr 探测，未实现则用默认行为）：
    - set_client_factory(factory)：注入复用的 HTTP 客户端（worker 持连接池）
    - set_cap_max_tokens(fn)：注入 token 上限钳制函数（fn(model, requested) -> int）
    - sink_id：声明配套 sink 的 id（缺省回退 StreamSinkRegistry 默认实现）
    """

    id: str

    # 能力声明：False 时 worker 不启用流式（如 o1/o3 系列的 chat/completions 非流式）。
    # worker 据此决定 stream 参数，不再按模型名硬编码判定。
    supports_streaming: bool

    def create_stream(
        self,
        llm_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        auth_headers: Optional[Dict[str, str]] = None,
        api_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """发请求并返回可迭代 StreamEvent 的流（需支持 close()）。

        Args:
            llm_config: 模型配置（API_URL / 模型名称 / 温度 / 思考模式等全量参数）
            messages: 内部格式消息列表（transport 自行经 serializer 转为协议形态）
            tools: OpenAI function 格式工具列表（无工具时 None）
            auth_headers: worker 已算好的认证/伪装头（extra_headers 可调用值求值结果）
            api_messages: worker 已完成序列化（且可能经自愈修正）的 API 格式消息。
                传入时直接使用，避免与 worker 的消息缓存/修正结果不一致；
                为 None 时按 messages 自行序列化（独立使用场景）。

        Returns:
            可迭代 + 可 close() 的流对象；失败抛 RuntimeError（走 worker 通用重试/报错链）
        """
        ...

    def classify_error(self, error_str: str) -> Optional[str]:
        """把服务端错误串映射为 worker 自愈动作 kind（无匹配返回 None）。

        kind 为**闭集**（worker 侧实现固定，新增需改 worker）：
        - "tool_order"：tool result 顺序错（2013）→ worker 调 _fix_tool_result_order
        - "missing_args"：工具参数丢失 → worker 调 _try_recover_tool_arguments

        修复动作**不在 transport 内执行**（它不持有 worker 的消息缓存与源头列表），
        只做错误串 → kind 的映射，落地写回由 worker 负责。
        """
        ...
