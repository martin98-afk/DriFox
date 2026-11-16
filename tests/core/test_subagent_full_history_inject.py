# -*- coding: utf-8 -*-
"""子智能体父历史全文注入回归测试（#35：注入预算拆除）。

用户裁决：主程序不再对子智能体注入历史做预算性丢弃——父历史全文注入，
超模型窗口由 provider API 报错（后果自担）。

旧实现：context budget × ratio（agent 配置 0.1~0.8）从最新向前填充、
放不下整条丢弃、tool 角色一律跳过（动机=工具结果长，省预算）。
新实现：完整注入 + 保留请求合法性清洗（空 str 消息跳过、multimodal 原样），
tool 角色不再跳过（标准协议角色，配对完整即合法）。

超窗口报错为预期行为：测试用小语料避开真窗口限制，只断言注入完整性。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.workers.subagent_worker import SubAgentExecutor


class _FakeAgent:
    """最小 agent 配置替身（预算字段已废弃，不应再被读取）。"""

    def __init__(self):
        self.inherit_history_budget_ratio = 0.1  # 旧实现会把历史裁到 10%；新实现必须无视


def _make_worker():
    """最小 SubAgentExecutor（绕 __init__ 的重依赖，直接设用到的字段）。"""
    w = SubAgentExecutor.__new__(SubAgentExecutor)
    w.llm_config = {"模型名称": "bench-model"}
    return w


def test_full_history_injected_no_budget_drop():
    """全文注入：历史全部保留，预算比例（哪怕 0.1）不再造成丢弃。"""
    w = _make_worker()
    history = []
    for i in range(30):
        history.append({"role": "user", "content": f"q{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})
    out = SubAgentExecutor._build_inherited_context(w, _FakeAgent(), history)

    assert len(out) == 60, f"全文注入应保留全部 60 条，实际 {len(out)}"
    assert out[0]["content"] == "q0"
    assert out[-1]["content"] == "a29"


def test_tool_role_no_longer_skipped():
    """tool 角色不再被跳过（跳过动机是省预算，非格式合法性）。"""
    w = _make_worker()
    history = [
        {"role": "user", "content": "run"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file body"},
        {"role": "assistant", "content": "done"},
    ]
    out = SubAgentExecutor._build_inherited_context(w, _FakeAgent(), history)

    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool", "assistant"], f"tool 应保留：实际 {roles}"
    assert out[2]["tool_call_id"] == "c1", "tool 消息的配对字段应保留"


def test_empty_str_messages_skipped_multimodal_kept():
    """请求合法性清洗：空 str 消息跳过；multimodal list 原样保留（含全空 text）。"""
    w = _make_worker()
    history = [
        {"role": "user", "content": ""},  # 空 str → 跳过
        {"role": "user", "content": [{"type": "text", "text": ""}, {"type": "image_url", "image_url": {"url": "file://x.png"}}]},
        {"role": "user", "content": "hi"},
    ]
    out = SubAgentExecutor._build_inherited_context(w, _FakeAgent(), history)

    assert len(out) == 2, f"空 str 跳过、multimodal 保留：实际 {len(out)}"
    assert isinstance(out[0]["content"], list), "multimodal 应原样保留 list"
    assert out[0]["content"][1]["type"] == "image_url"


def test_truncate_count_repairs_tool_pairing():
    """inherit_history_count 截断切开配对 → truncate_messages_at 修复悬空（调用处行为）。

    直接验证修复函数的输入输出契约：截断点落在 assistant(tool_calls) 之后、
    tool 结果之前 → 悬空声明被剥掉，请求不会带非法 tool 消息。
    """
    from app.core.conversation.message_content import truncate_messages_at

    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "body"},
        {"role": "assistant", "content": "done"},
    ]
    # 模拟 [-2:] 截断：只剩 [tool, assistant] → tool 无声明者 → 应被剥
    cut = history[-2:]
    repaired = truncate_messages_at(cut, len(cut) - 1)
    assert all(m.get("role") != "tool" for m in repaired), "孤立 tool 结果应被剥掉"
    assert repaired[-1]["content"] == "done"
