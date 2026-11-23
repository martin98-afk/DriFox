# -*- coding: utf-8 -*-
"""count_messages_tokens is 缓存内容签名回归测试

背景（2026-11-17 诊断证实）：缓存以「列表对象身份 + model + tools_sig」判定命中，
同列表对象 in-place 累积（chat_worker.current_messages 跨工具迭代 extend、
ChatSession.messages append）时身份不变、内容已变 → 恒命中首轮旧值，圆环/
token_usage 全程卡首轮值。修复：命中后比对内容签名（len + 末条消息 id，
同 usage.py:_snapshot_sig 法），失配视为 MISS 重算。

保护面：内容增长必须重算、内容不变仍命中、删减场景检出、tools 路径同效。
"""

import pytest

from app.core.infra.token_estimator import (
    _lookup_token_cache,
    _tools_signature,
    count_messages_tokens,
)


def _mk_msgs(n: int) -> list:
    return [{"role": "user", "content": f"消息{i}内容" * 5} for i in range(n)]


# ========== chat_worker 主链路场景：同对象跨工具迭代累积 ==========

def test_same_list_append_recounts():
    """同列表对象 append 后再次 count 必须返回新值（脏命中修复主场景）"""
    msgs = _mk_msgs(2)
    t1 = count_messages_tokens(msgs)
    assert t1 > 0

    # 模拟 chat_worker:1336 append（用户/助手消息追加）
    msgs.append({"role": "assistant", "content": "新回复内容" * 20})
    t2 = count_messages_tokens(msgs)
    assert t2 > t1, f"append 后 token 必须增长: t1={t1}, t2={t2}"


def test_same_list_extend_tool_iteration():
    """同列表对象 extend（工具结果序列）后必须重算（chat_worker:2493 场景）"""
    msgs = _mk_msgs(1)
    t1 = count_messages_tokens(msgs)

    # 模拟工具迭代注入：assistant(tool_calls) + tool result
    msgs.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read", "arguments": '{"filePath": "/tmp/a.py"}'}}
                ],
            },
            {"role": "tool", "content": "文件内容" * 100, "tool_call_id": "c1"},
        ]
    )
    t2 = count_messages_tokens(msgs)
    assert t2 > t1, f"extend 后 token 必须增长: t1={t1}, t2={t2}"

    # 连续第二轮迭代（多次 extend 的多轮工具会话）
    msgs.append({"role": "assistant", "content": "最终回复" * 10})
    t3 = count_messages_tokens(msgs)
    assert t3 > t2, f"第二次追加后 token 必须继续增长: t2={t2}, t3={t3}"


def test_same_list_shrink_recounts():
    """同对象尾部弹出（压缩/截断场景）后必须重算"""
    msgs = _mk_msgs(3)
    t1 = count_messages_tokens(msgs)
    msgs.pop()
    t2 = count_messages_tokens(msgs)
    assert t2 < t1, f"弹出后 token 必须下降: t1={t1}, t2={t2}"


# ========== 缓存语义保留：内容不变的重复查询仍命中 ==========

def test_unchanged_content_repeat_returns_same_value():
    """内容不变的重复查询结果一致（缓存语义未破坏）"""
    msgs = _mk_msgs(2)
    t1 = count_messages_tokens(msgs)
    t2 = count_messages_tokens(msgs)
    t3 = count_messages_tokens(msgs)
    assert t1 == t2 == t3


def test_lookup_hits_unchanged_entry():
    """单元级：内容未变时 _lookup 命中（返回非 None）"""
    msgs = _mk_msgs(1)
    tools_sig = _tools_signature(None)
    count_messages_tokens(msgs)
    assert _lookup_token_cache(msgs, "gpt-4", tools_sig) is not None


def test_lookup_misses_after_append():
    """单元级：append 后签名失配，_lookup 返回 None（判 MISS 重算）"""
    msgs = _mk_msgs(1)
    tools_sig = _tools_signature(None)
    count_messages_tokens(msgs)
    msgs.append({"role": "assistant", "content": "追加内容"})
    assert _lookup_token_cache(msgs, "gpt-4", tools_sig) is None
    # 重算后条目被刷新，恢复命中
    count_messages_tokens(msgs)
    assert _lookup_token_cache(msgs, "gpt-4", tools_sig) is not None


# ========== tools 路径同效 ==========

def test_same_list_with_tools_recounts():
    """带 tools 的入口同样受内容签名保护"""
    tools = [{"type": "function", "function": {"name": "bash", "description": "x", "parameters": {}}}]
    msgs = _mk_msgs(1)
    t1 = count_messages_tokens(msgs, "gpt-4", tools=tools)
    msgs.append({"role": "user", "content": "工具会话中的追加消息" * 30})
    t2 = count_messages_tokens(msgs, "gpt-4", tools=tools)
    assert t2 > t1, f"tools 路径 append 后 token 必须增长: t1={t1}, t2={t2}"


# ========== 不同列表对象互不污染（原语义） ==========

def test_distinct_lists_cached_independently():
    """不同列表对象各自缓存，互不干扰（4-entry LRU 原语义）"""
    a = _mk_msgs(2)
    b = _mk_msgs(3)
    ta = count_messages_tokens(a)
    tb = count_messages_tokens(b)
    assert ta < tb
    # 重复查询不串值
    assert count_messages_tokens(a) == ta
    assert count_messages_tokens(b) == tb


# ========== subagent_worker 场景（同模式，参数化） ==========

@pytest.mark.parametrize("growth", ["append_one", "extend_many"])
def test_subagent_pattern_growth_recounts(growth):
    """subagent hook 场景同模式：current_messages 累积后 hook 内重算取新值"""
    msgs = _mk_msgs(1)
    t1 = count_messages_tokens(msgs)
    if growth == "append_one":
        msgs.append({"role": "assistant", "content": "回答" * 50})
    else:
        msgs.extend(_mk_msgs(2))
    t2 = count_messages_tokens(msgs)
    assert t2 > t1, f"subagent 场景 growth={growth} 后 token 必须增长: t1={t1}, t2={t2}"


def test_empty_list_edge():
    """空列表边界：_content_sig 对空列表返回 len=0，签名稳定"""
    msgs: list = []
    assert count_messages_tokens(msgs) == 0
    msgs.append({"role": "user", "content": "第一条"})
    assert count_messages_tokens(msgs) > 0
