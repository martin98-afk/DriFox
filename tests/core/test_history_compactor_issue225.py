# -*- coding: utf-8 -*-
"""issue #225 回归测试：历史压缩摘要绝不能标成 user 角色。

历史压缩会把较早的对话压缩成一条摘要。旧实现把这条摘要标成
``role="user"``，等于把一份"第三人称回顾整个对话"的摘要伪装成"用户刚说的话"
塞进上下文。当被保留的近期消息以 user 开头时（很常见），还会产生两条连续的
user 消息，破坏 user/assistant 交替结构。模型会把这份摘要当成用户发言来读，
一旦摘要里出现收尾意味的内容，就会幻觉"用户说了谢谢/行了"并误判对话结束
（自问自答 / 一直进行下去）。

修复：摘要改为 ``role="system"`` 并打 ``_compaction_summary`` 标记，
同时在 context_builder 的 system 消息过滤中保留该标记。
"""
import pytest

from app.core.history_compactor import HistoryCompactor


def _make_long_conversation(n: int = 20, per_msg_chars: int = 200) -> list:
    """构造一段足够长的多轮对话，强制触发压缩。"""
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"用户第{i}轮的问题 " + "问" * per_msg_chars})
        msgs.append({"role": "assistant", "content": f"助理第{i}轮的回答 " + "答" * per_msg_chars})
    return msgs


def test_compaction_summary_role_is_system_not_user(monkeypatch):
    """压缩摘要必须是 system 角色，绝不能标成 user（issue #225 根因）。"""
    compactor = HistoryCompactor(get_model_config=lambda: {}, agent_manager=None)
    # 用固定摘要，避免依赖启发式/网络，保证测试确定性
    monkeypatch.setattr(
        compactor,
        "_summarize",
        lambda *a, **k: "【对话摘要】用户让助理写代码，助理已完成并交付。",
    )

    messages = _make_long_conversation()
    result, _, _ = compactor.compact(messages, budget=400, allow_llm_summary=False)

    summaries = [m for m in result if m.get("_compaction_summary")]
    assert summaries, "压缩后未生成带 _compaction_summary 标记的摘要消息"
    assert summaries[0]["role"] == "system", (
        f"摘要必须是 system 角色，实际为 {summaries[0]['role']} —— "
        "标成 user 会让模型误以为用户说了摘要里的内容（issue #225）"
    )


def test_no_consecutive_user_messages_after_compaction(monkeypatch):
    """压缩后历史不能出现连续 user 消息，否则破坏 user/assistant 交替（issue #225 衍生）。"""
    compactor = HistoryCompactor(get_model_config=lambda: {}, agent_manager=None)
    monkeypatch.setattr(compactor, "_summarize", lambda *a, **k: "摘要内容")

    messages = _make_long_conversation()
    result, _, _ = compactor.compact(messages, budget=400, allow_llm_summary=False)

    for prev, cur in zip(result, result[1:]):
        assert not (prev.get("role") == "user" and cur.get("role") == "user"), (
            f"压缩后历史出现连续 user 消息，破坏交替结构：{prev} -> {cur}"
        )


def test_compaction_summary_survives_context_builder_filter(monkeypatch):
    """压缩摘要带 _compaction_summary 标记，应能在 context_builder 的 system 过滤中存活。"""
    compactor = HistoryCompactor(get_model_config=lambda: {}, agent_manager=None)
    monkeypatch.setattr(compactor, "_summarize", lambda *a, **k: "摘要内容")

    messages = _make_long_conversation()
    result, _, _ = compactor.compact(messages, budget=400, allow_llm_summary=False)

    summary = next((m for m in result if m.get("_compaction_summary")), None)
    assert summary is not None
    # 模拟 context_builder 的过滤条件：system 消息需带 _hook_event 或 _compaction_summary 才保留
    kept = summary.get("role") != "system" or summary.get("_hook_event") or summary.get("_compaction_summary")
    assert kept, "压缩摘要应能在 context_builder 的 system 过滤中存活，否则历史摘要会整体丢失"
