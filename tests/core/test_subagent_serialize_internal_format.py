# -*- coding: utf-8 -*-
"""子智能体 chat/completions 分支必须过序列化单入口（回归：GLM 1214）

## 背景
compaction 子智能体继承主智能体历史（inherit_history），历史中 assistant.content
是内部格式数组（可含 tool_result block）。chat/completions 分支此前裸发原始消息，
严格校验的 provider（智谱 GLM）返回 400：
    {'error': {'code': '1214', 'message': 'messages[3].content[1].type类型错误'}}

## 断言
含 tool_result block 的内部格式消息发给 API 前，必须经 MessageSerializer 清洗：
- tool_result block 不出现在请求 messages 中（文本语义保留在 assistant.content）
- 标准格式消息（子智能体自产 assistant/tool）无损透传
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.workers.subagent_worker import SubAgentExecutor


def _build_executor() -> SubAgentExecutor:
    """最小构造：不 start 线程，只调 _make_api_call"""
    return SubAgentExecutor(
        task_id="t_serialize",
        agent_name="compaction",
        task_description="test",
        llm_config={"API_KEY": "sk-test", "模型名称": "glm-4.7", "API_URL": "https://open.bigmodel.cn/api/paas/v4"},
        agent_manager=None,
        tool_executor=None,
    )


@pytest.fixture()
def capture_client(monkeypatch):
    """替换 build_openai_client，捕获 chat.completions.create 的 kwargs"""
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[], usage=None)

    client = MagicMock()
    client.chat.completions.create.side_effect = _fake_create
    monkeypatch.setattr("app.utils.http_client.build_openai_client", lambda **kw: client)
    return captured


def _ensure_plugins_loaded():
    """确保系统插件（openai adapter + serializer）已注册（幂等）"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()


# 模拟 compaction 继承的主智能体历史：assistant 消息 content 为内部格式数组，
# 含 text + tool_result block（真实会话中工具调用后落库的形态）
_INTERNAL_HISTORY = [
    {"role": "user", "content": "帮我跑一下测试"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我来运行测试"},
            {
                "type": "tool_result",
                "name": "bash",
                "tool_call_id": "call_1",
                "arguments": {"command": "pytest -x"},
                "result": "14 passed",
                "success": True,
            },
        ],
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "pytest -x"}'},
            }
        ],
    },
    {"role": "user", "content": "## 子任务\n请压缩当前对话上下文"},
]


def test_tool_result_block_never_leaks_to_api(capture_client):
    """tool_result block 必须在发送前被序列化清洗（GLM 1214 回归）"""
    _ensure_plugins_loaded()
    executor = _build_executor()
    executor._make_api_call(list(_INTERNAL_HISTORY), llm_config=dict(executor.llm_config))

    sent = capture_client["messages"]
    assert sent, "请求必须包含 messages"
    for msg in sent:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                assert isinstance(block, dict)
                assert block.get("type") != "tool_result", f"tool_result block 泄漏进 API 请求: {block}"


def test_text_semantics_preserved_after_serialize(capture_client):
    """清洗后 assistant 文本语义保留（不能把内容洗没了）"""
    _ensure_plugins_loaded()
    executor = _build_executor()
    executor._make_api_call(list(_INTERNAL_HISTORY), llm_config=dict(executor.llm_config))

    sent = capture_client["messages"]
    all_text = "".join(
        str(block.get("text", "")) if isinstance(block, dict) else str(content)
        for msg in sent
        for content in [msg.get("content")]
        for block in (content if isinstance(content, list) else [{"type": "text", "text": content}])
    )
    assert "我来运行测试" in all_text
    assert "帮我跑一下测试" in all_text


def test_subagent_own_messages_survive_serialize(capture_client):
    """子智能体循环自产的标准格式消息（assistant+tool_calls / role=tool）过序列化无损"""
    _ensure_plugins_loaded()
    executor = _build_executor()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_9", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "file body"},
        {"role": "user", "content": "go on"},
    ]
    executor._make_api_call(messages, llm_config=dict(executor.llm_config))

    sent = capture_client["messages"]
    roles = [m["role"] for m in sent]
    assert "assistant" in roles and "tool" in roles
    tool_msgs = [m for m in sent if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0].get("tool_call_id") == "call_9"
    assert tool_msgs[0].get("content") == "file body"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
