# -*- coding: utf-8 -*-
"""
回归测试：DeepSeek thinking mode 下 reasoning_content 必须回传（400 修复）

背景：
- DeepSeek Console 在 thinking mode 下要求 tool_calls 的 assistant 消息必须携带
  reasoning_content 字段（可为空串），否则返回 400：
  "The reasoning_content in the thinking mode must be passed back to the API."
- 用户通过 opencode.ai 中转 deepseek-v4-flash-free 时，detect_provider_family
  返回 "opencode" 而非 "deepseek"，导致 chat_worker._requires_reasoning_content()
  误判为 False，to_api_message 的兜底逻辑（补空串）从未生效。
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.message_content import messages_to_api, to_api_message


def _opencode_deepseek_config() -> dict:
    """opencode.ai 中转 deepseek-v4 系模型的典型配置（思考模式开启）"""
    return {
        "API_URL": "https://opencode.ai/zen/v1",
        "模型名称": "deepseek-v4-flash-free",
        "思考模式": True,
        "思考等级": "medium",
        "认证方式": "bearer",
    }


def _official_deepseek_config() -> dict:
    """官方 deepseek.com 配置"""
    return {
        "API_URL": "https://api.deepseek.com",
        "模型名称": "deepseek-v4-flash",
        "思考模式": True,
        "认证方式": "bearer",
    }


def _tool_call_assistant_msg(reasoning_content=None):
    """构造一条带 tool_calls 的 assistant 消息"""
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
    }
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return msg


# ---------- 核心断言：opencode 中转 deepseek 模型也必须要求 reasoning_content ----------


def test_opencode_deepseek_requires_reasoning_content():
    """opencode.ai 中转的 deepseek-v4 模型，思考模式开启时必须要求 reasoning_content"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config=_opencode_deepseek_config(),
    )
    assert worker._requires_reasoning_content() is True, (
        "opencode 中转 deepseek 模型在思考模式下必须要求 reasoning_content"
    )


def test_official_deepseek_requires_reasoning_content():
    """官方 deepseek.com 模型，思考模式开启时必须要求 reasoning_content"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config=_official_deepseek_config(),
    )
    assert worker._requires_reasoning_content() is True


def test_opencode_non_deepseek_does_not_require():
    """opencode.ai 中转非 deepseek 模型（如 kimi），不要求 reasoning_content"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config={
            "API_URL": "https://opencode.ai/zen/v1",
            "模型名称": "kimi-k3",
            "思考模式": True,
        },
    )
    assert worker._requires_reasoning_content() is False


def test_thinking_off_does_not_require():
    """思考模式关闭时不要求 reasoning_content"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    cfg = _opencode_deepseek_config()
    cfg["思考模式"] = False
    worker = OpenAIChatWorker(messages=[], session_messages=[], llm_config=cfg)
    assert worker._requires_reasoning_content() is False


# ---------- 核心断言：messages_to_api 兜底补空串 ----------


def test_opencode_deepseek_tool_call_gets_empty_reasoning():
    """opencode 中转 deepseek 模型：tool_calls assistant 消息必须补 reasoning_content 空串"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[_tool_call_assistant_msg()],
        session_messages=[],
        llm_config=_opencode_deepseek_config(),
    )
    api_messages = messages_to_api(
        worker.messages,
        requires_reasoning_content=worker._requires_reasoning_content(),
    )
    assert api_messages, "应生成至少一条 API 消息"
    asst = api_messages[0]
    assert asst["role"] == "assistant"
    assert "reasoning_content" in asst, "tool_calls assistant 必须带 reasoning_content 字段"
    assert asst["reasoning_content"] == ""


def test_existing_reasoning_is_preserved():
    """已存在的 reasoning_content 原样保留"""
    api_msg = to_api_message(
        _tool_call_assistant_msg(reasoning_content="thinking..."),
        requires_reasoning_content=True,
    )
    assert api_msg["reasoning_content"] == "thinking..."


# ---------- 补充：deepseek 官方路径同样兜底 ----------


def test_official_deepseek_tool_call_gets_empty_reasoning():
    """官方 deepseek 模型：tool_calls assistant 消息必须补 reasoning_content 空串"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[_tool_call_assistant_msg()],
        session_messages=[],
        llm_config=_official_deepseek_config(),
    )
    api_messages = messages_to_api(
        worker.messages,
        requires_reasoning_content=worker._requires_reasoning_content(),
    )
    asst = api_messages[0]
    assert "reasoning_content" in asst
    assert asst["reasoning_content"] == ""


# ---------- subagent_worker 的兜底 ----------


def test_subagent_worker_requires_reasoning_content():
    """subagent_worker 对 opencode 中转 deepseek 模型同样要求 reasoning_content"""
    from app.core.workers.subagent_worker import SubAgentExecutor

    # 静态逻辑与 chat_worker 一致，通过对象方法验证
    executor = SubAgentExecutor.__new__(SubAgentExecutor)
    assert executor._requires_reasoning_content(_opencode_deepseek_config()) is True
    assert executor._requires_reasoning_content(_official_deepseek_config()) is True
    assert (
        executor._requires_reasoning_content(
            {"API_URL": "https://opencode.ai/zen/v1", "模型名称": "kimi-k3", "思考模式": True}
        )
        is False
    )
    cfg = _opencode_deepseek_config()
    cfg["思考模式"] = False
    assert executor._requires_reasoning_content(cfg) is False


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
