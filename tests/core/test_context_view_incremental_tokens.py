# -*- coding: utf-8 -*-
"""ContextView per-message token 增量重算回归测试（实施单 #I3）。

覆盖完成标准：
- 替换/追加单条消息仅重算该条，未改条目 token 复用（spy 计数验证）
- 各形态消息（str/list/tool_calls/reasoning）下 used_tokens 与全量
  count_messages_tokens 恒等价
- 对齐异常回退全量、外部绕过契约改写 messages 的自愈路径
"""

import pytest

from app.core.context.view import ContextView, _msg_fingerprint
from app.core.infra.token_estimator import count_messages_tokens

def _base_messages(n=20):
    """混合形态消息：tool_calls / tool 回包 / 多模态 content / reasoning / 纯文本"""
    msgs = []
    for i in range(n):
        if i % 5 == 0:
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
            ]})
        elif i % 5 == 1:
            msgs.append({"role": "tool", "tool_call_id": f"c{i - 1}", "content": "x" * 200})
        elif i % 5 == 2:
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": "你好 " * 30},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
            ]})
        elif i % 5 == 3:
            msgs.append({"role": "assistant", "content": "回答" * 40, "reasoning_content": "思考" * 25})
        else:
            msgs.append({"role": "user", "content": "消息" * 60})
    return msgs

def _make_view(n=20):
    return ContextView(messages=_base_messages(n), budget=10**9, target_tokens=10**9)

@pytest.fixture
def token_spy(monkeypatch):
    """计数 view 模块内 per_message_tokens 调用次数（= 增量重算的条数）"""
    import app.core.context.view as view_mod
    from app.core.infra.token_estimator import per_message_tokens as real

    calls = {"n": 0}

    def spy(msg):
        calls["n"] += 1
        return real(msg)

    monkeypatch.setattr(view_mod, "per_message_tokens", spy)
    return calls

def test_initial_build_matches_full_count(token_spy):
    v = _make_view()
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_replace_single_message_recounts_only_that(token_spy):
    v = _make_view()
    v.used_tokens  # 建档
    token_spy["n"] = 0
    msgs = [dict(m) for m in v.messages]
    msgs[10] = {"role": "user", "content": "全新内容" * 100}
    v.replace_messages(msgs)
    assert token_spy["n"] == 1  # 仅被改条重算
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_append_message_recounts_only_new(token_spy):
    v = _make_view()
    v.used_tokens
    token_spy["n"] = 0
    msgs = list(v.messages)
    msgs.append({"role": "user", "content": "追加" * 50})
    v.replace_messages(msgs)
    assert token_spy["n"] == 1
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_drop_head_reuses_all_suffix(token_spy):
    """尾保留场景（tier 常见：drop 头部 k 条）→ 零重算"""
    v = _make_view(40)
    v.used_tokens
    token_spy["n"] = 0
    v.replace_messages(list(v.messages[10:]))
    assert token_spy["n"] == 0
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_summarize_middle_recounts_only_new_entry(token_spy):
    """摘要化（中间多条换一条）→ 仅新条重算，前后缀复用"""
    v = _make_view(30)
    v.used_tokens
    token_spy["n"] = 0
    msgs = v.messages[:10] + [{"role": "user", "content": "摘要"}] + v.messages[20:]
    v.replace_messages(msgs)
    assert token_spy["n"] == 1
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_identical_replacement_zero_recount(token_spy):
    """同内容替换（tier 重建 dict 但内容等价）→ 指纹相等零重算"""
    v = _make_view()
    v.used_tokens
    token_spy["n"] = 0
    v.replace_messages([dict(m) for m in v.messages])
    assert token_spy["n"] == 0
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_pipeline_double_replace_stays_consistent(token_spy):
    """模拟两层 tier 各替换一条 → 两次增量后仍与全量一致"""
    v = _make_view(24)
    v.used_tokens
    msgs1 = [dict(m) for m in v.messages]
    msgs1[3] = {"role": "user", "content": "一层改动" * 80}
    v.replace_messages(msgs1)
    used1 = v.used_tokens
    msgs2 = list(msgs1)
    msgs2[20] = {"role": "tool", "tool_call_id": "c18", "content": "二层改动" * 90}
    v.replace_messages(msgs2)
    assert used1 == count_messages_tokens(msgs1)
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_replace_heals_after_external_mutation(token_spy):
    """外部绕过契约直改 messages（长度失配）→ replace 自动全量建档不崩溃"""
    v = _make_view()
    v.used_tokens
    v.messages = v.messages[:5]  # 数组仍 20 条 → 失配
    token_spy["n"] = 0
    msgs = list(v.messages) + [{"role": "user", "content": "x"}]
    v.replace_messages(msgs)
    assert token_spy["n"] == 6  # 全量建档（当前 6 条）
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_estimator_failure_falls_back_to_full_count(token_spy, monkeypatch):
    """增量路径异常 → 回退 count_messages_tokens 全量，语义不变"""
    import app.core.context.view as view_mod

    v = _make_view()
    v.used_tokens
    msgs = [dict(m) for m in v.messages]
    msgs[5] = {"role": "user", "content": "触发重算" * 10}

    def boom(msg):
        raise RuntimeError("estimator down")

    monkeypatch.setattr(view_mod, "per_message_tokens", boom)
    v.replace_messages(msgs)  # 中间段重算抛错 → 兜底全量
    assert v.used_tokens == count_messages_tokens(v.messages)

def test_fingerprint_distinguishes_token_relevant_fields():
    """指纹区分 token 相关字段；未消费字段不敏感（保守多算不错算）"""
    a = {"role": "user", "content": "abc"}
    b = {"role": "user", "content": "abd"}
    c = {"role": "user", "content": "abc", "irrelevant_field": 1}
    assert _msg_fingerprint(a) != _msg_fingerprint(b)
    assert _msg_fingerprint(a) == _msg_fingerprint(c)
