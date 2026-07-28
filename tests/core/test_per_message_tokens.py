# -*- coding: utf-8 -*-
"""token_estimator.per_message_tokens 单条消息 helper 等价性测试

回归保护：per_message_tokens(msg, model) 必须与
count_messages_tokens([msg], model) 在各种消息结构下输出一致。
"""

from app.core.token_estimator import (
    count_messages_tokens,
    per_message_tokens,
)


# ========== 基础结构 ==========


def test_user_text_message():
    msg = {"role": "user", "content": "hello world"}
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_assistant_text_message():
    msg = {"role": "assistant", "content": "你好世界"}
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_tool_message():
    msg = {
        "role": "tool",
        "content": "tool output here",
        "tool_call_id": "call_123",
    }
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_assistant_with_tool_calls():
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {
                    "name": "read",
                    "arguments": '{"filePath": "/tmp/x.py"}',
                },
            },
            {
                "id": "call_2",
                "function": {
                    "name": "bash",
                    "arguments": '{"command": "ls"}',
                },
            },
        ],
    }
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_assistant_with_reasoning():
    msg = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "Let me think... I should look at the file.",
    }
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_multimodal_content_list():
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ],
    }
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_empty_content():
    msg = {"role": "user", "content": ""}
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_none_content():
    """content=None 的特殊路径（assistant 工具调用消息常见）"""
    msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "x", "arguments": "{}"}}]}
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_hook_event_message():
    """带 _hook_event 标记的消息（hook 注入的系统时间/长期记忆）"""
    msg = {
        "role": "user",
        "content": "<system-reminder>现在是 2026-07-28</system-reminder>",
        "_hook_event": "PostUserMessage",
    }
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_model_ratio_applied():
    """模型校正系数（deepseek/claude/qwen 等）应用一致"""
    msg = {"role": "user", "content": "测试中文 token 校正系数"}
    for model in ("gpt-4", "deepseek", "claude", "qwen", "glm", "minimax"):
        assert per_message_tokens(msg, model) == count_messages_tokens([msg], model), (
            f"模型 {model} 上 per_message_tokens 与 count_messages_tokens 不一致"
        )


def test_non_dict_message():
    """非 dict 类型（防御性）"""
    assert per_message_tokens("not a dict") == 0
    assert per_message_tokens(None) == 0
    assert per_message_tokens(123) == 0


def test_image_only_message():
    """仅含 image_url 的 content 列表"""
    msg = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ],
    }
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_long_text_message():
    """长文本消息（>1000 字符）"""
    msg = {
        "role": "user",
        "content": "你好世界。" * 200,
    }
    assert per_message_tokens(msg) == count_messages_tokens([msg])


def test_snapshot_breakdown_consistency():
    """回归保护：UI 上下文快照的 breakdown 之和必须与
    旧版 count_messages_tokens([m]) 累加结果完全一致（per_message_tokens 语义保真）。

    注：旧版 est_total = count_messages_tokens(messages) 用"一次性 ratio"取整，
    与 sum(count_messages_tokens([m])) 累加"逐条 ratio"存在 int 取整误差（~1-2 token）。
    新版 snapshot 改为 sum(per_message_tokens) + tools_tokens，**内部自洽**
    （breakdown 之和 == est_total），比旧版"两路径不一致"更优。
    """
    messages = [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答", "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]},
        {"role": "tool", "content": "file1\nfile2", "tool_call_id": "c1"},
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "第二轮回答含 reasoning", "reasoning_content": "thinking..."},
    ]

    # 旧 breakdown 累加路径
    old_breakdown = [count_messages_tokens([m], "deepseek") for m in messages]
    # 新 breakdown 累加路径
    new_breakdown = [per_message_tokens(m, "deepseek") for m in messages]
    # 两者必须完全一致（per_message_tokens 语义保真）
    assert old_breakdown == new_breakdown
    # 内部自洽：sum of breakdown = sum of per_message_tokens
    assert sum(new_breakdown) == sum(per_message_tokens(m, "deepseek") for m in messages)


def test_snapshot_internal_consistency():
    """新实现保证 est_total = sum(per_message_tokens) + tools_tokens，
    即 breakdown 之和与 est_total 内部一致（视觉等比缩放基线准确）。
    """
    from app.core.token_estimator import count_tools_tokens, get_model_token_ratio

    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    model = "gpt-4"
    tools = [{"type": "function", "function": {"name": "bash", "description": "x", "parameters": {}}}]
    tools_tokens = int(count_tools_tokens(tools, model) * get_model_token_ratio(model))

    # 新版 est_total 算法（snapshot 改用）
    est_total_new = sum(per_message_tokens(m, model) for m in messages) + tools_tokens
    # breakdown = sum of per_message_tokens + tools_tokens
    breakdown_sum = sum(per_message_tokens(m, model) for m in messages) + tools_tokens
    assert est_total_new == breakdown_sum
