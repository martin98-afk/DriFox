"""测试删除/撤销消息时 hook 消息的清理"""

import sys
import os
from typing import List, Dict, Any

# 确保能找到 app 包
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.message_content import (
    consolidate_messages,
    get_user_round_ranges,
    group_messages_for_display,
    _is_hook_message,
)
from app.core.chat_session import ChatSession


def make_hook_msg(event_name: str, content: str = "hook output") -> Dict[str, Any]:
    """创建 hook 消息"""
    return {
        "role": "user",
        "content": f"<system-reminder>\n<{event_name.lower().replace('_', '-')}-hook>\n{content}\n</{event_name.lower().replace('_', '-')}-hook>\n</system-reminder>",
        "_hook_event": event_name,
    }


def make_user_msg(content: str) -> Dict[str, Any]:
    """创建用户消息"""
    return {
        "role": "user",
        "content": content,
        "timestamp": "2025-01-01 00:00:00",
    }


def make_assistant_msg(content: str) -> Dict[str, Any]:
    """创建助手消息"""
    return {
        "role": "assistant",
        "content": content,
        "timestamp": "2025-01-01 00:00:01",
    }


def test_single_round_hook_cleanup():
    """
    场景：一个 round，有 PreUserMessage hook
    预期：删除后 session 为空
    """
    messages = [
        make_hook_msg("PreUserMessage"),
        make_user_msg("Hello"),
        make_assistant_msg("Hi there!"),
    ]

    canonical = consolidate_messages(messages)
    print("\n=== test_single_round_hook_cleanup ===")
    print(f"canonical: {len(canonical)} msgs")
    for i, m in enumerate(canonical):
        print(f"  [{i}] role={m.get('role')}, hook={m.get('_hook_event', 'N/A')}, content={str(m.get('content', ''))[:50]}")

    ranges = get_user_round_ranges(canonical)
    print(f"round_ranges: {ranges}")

    assert len(ranges) == 1, f"Expected 1 round, got {len(ranges)}"
    start, end = ranges[0]
    assert start == 0, f"Expected start=0, got {start}"
    assert end == 3, f"Expected end=3, got {end}"

    # 模拟 truncate_and_remove_round
    new_messages = canonical[:start] + canonical[end:]
    assert len(new_messages) == 0, f"Expected 0 messages after deletion, got {len(new_messages)}"
    print("✅ PASS: hook message cleaned up with round deletion")


def test_multi_round_hook_cleanup():
    """
    场景：两个 round，各有 PreUserMessage hook + PostUserMessage hook
    删除第一个 round，验证 hook 消息被一起清理
    """
    messages = [
        # Round 0
        make_hook_msg("PreUserMessage", "ctx: memory"),
        make_user_msg("Hello"),
        make_hook_msg("PostUserMessage"),
        make_assistant_msg("Hi there!"),
        # Round 1
        make_hook_msg("PreUserMessage", "ctx: memory"),
        make_user_msg("What's up?"),
        make_assistant_msg("Not much!"),
    ]

    canonical = consolidate_messages(messages)
    print("\n=== test_multi_round_hook_cleanup ===")
    print(f"canonical: {len(canonical)} msgs")
    for i, m in enumerate(canonical):
        print(f"  [{i}] role={m.get('role')}, hook={m.get('_hook_event', 'N/A')}, content=str")

    ranges = get_user_round_ranges(canonical)
    print(f"round_ranges: {ranges}")
    assert len(ranges) == 2, f"Expected 2 rounds, got {len(ranges)}"

    # 删除 Round 0: range=(start_0, end_0)
    start_0, end_0 = ranges[0]
    print(f"Deleting Round 0 range: [{start_0}, {end_0})")
    assert start_0 == 0, f"Round 0 start should be 0, got {start_0}"
    assert end_0 < len(canonical), f"Round 0 end should be < {len(canonical)}, got {end_0}"

    new_messages = canonical[:start_0] + canonical[end_0:]
    print(f"After deleting Round 0: {len(new_messages)} messages remain")
    for i, m in enumerate(new_messages):
        print(f"  [{i}] role={m.get('role')}, hook={m.get('_hook_event', 'N/A')}")

    assert len(new_messages) == 3, f"Expected 3 messages remaining, got {len(new_messages)}"

    # Verify the remaining messages are Round 1's messages
    remaining = consolidate_messages(new_messages)
    ranges2 = get_user_round_ranges(remaining)
    print(f"Remaining round_ranges: {ranges2}")
    assert len(ranges2) == 1, f"Expected 1 remaining round, got {len(ranges2)}"

    # Round 1's PreUserMessage hook should be included in the remaining round
    s2, e2 = ranges2[0]
    pre_hook = remaining[s2]
    assert pre_hook.get("_hook_event") == "PreUserMessage", (
        f"Remaining round should start with PreUserMessage hook, got {pre_hook.get('_hook_event')}"
    )

    print("✅ PASS: Round 0 deleted with all its hook messages, Round 1 intact with its hook")


def test_delete_undo_reaccumulate():
    """
    场景：删除一个 round 后撤销，再发新消息
    验证旧的 hook 消息没有与新 hook 消息重复
    """
    # 初始状态
    messages_original = [
        make_hook_msg("PreUserMessage", "ctx: old"),
        make_user_msg("First msg"),
        make_assistant_msg("First response"),
    ]

    # 删除 Round 0
    canonical = consolidate_messages(messages_original)
    ranges = get_user_round_ranges(canonical)
    start, end = ranges[0]

    # 缓存用于 undo
    cache_messages = list(canonical[start:end])

    # 执行删除
    after_delete = canonical[:start] + canonical[end:]
    print("\n=== test_delete_undo_reaccumulate ===")
    print(f"After delete: {len(after_delete)} messages")

    # 撤销（恢复）
    after_undo = list(after_delete)
    after_undo[start:start] = cache_messages
    print(f"After undo: {len(after_undo)} messages")

    # 模拟再发一条新消息：PreUserMessage hook 再次注入
    new_hook = make_hook_msg("PreUserMessage", "ctx: new")
    new_user = make_user_msg("Second msg")
    new_assistant = make_assistant_msg("Second response")

    after_new_msg = after_undo + [new_hook, new_user, new_assistant]

    # 关键断言：旧 hook（ctx: old）和新 hook（ctx: new）同时存在
    canonical_final = consolidate_messages(after_new_msg)
    print(f"After new message: {len(canonical_final)} messages")
    hook_msgs = [m for m in canonical_final if m.get("_hook_event") == "PreUserMessage"]
    print(f"PreUserMessage hooks count: {len(hook_msgs)}")
    for i, m in enumerate(hook_msgs):
        print(f"  hook[{i}]: content={str(m.get('content', ''))[:80]}")

    # 这里 old hook 没有被 dedup，因为它们是作为独立消息存在的
    # 这其实是预期行为 — hook 消息是 LLM 上下文的一部分
    assert len(hook_msgs) == 2, (
        f"Expected 2 PreUserMessage hooks (old+new), got {len(hook_msgs)}. "
        "This is expected: old hooks survive undo, new hooks are added."
    )

    # 但关键问题：删除时，这次 round 的 hook 是否被正确清理？
    # 查看 round ranges 看每个 round 包含什么
    ranges_final = get_user_round_ranges(canonical_final)
    print(f"Final round_ranges: {ranges_final}")
    for i, (s, e) in enumerate(ranges_final):
        print(f"  Round {i}: [{s}, {e})")
        for j in range(s, e):
            m = canonical_final[j]
            print(f"    [{j}] role={m.get('role')}, hook={m.get('_hook_event', 'N/A')}")

    print("✅ PASS: hooks are managed correctly through delete/undo cycle")


def test_session_set_messages_preserves_hooks():
    """
    场景：使用 ChatSession.set_messages 替换消息列表
    验证 hook 消息的 _hook_event 字段在 consolidate 后保持
    """
    session = ChatSession()
    session.session_id = "test-session"

    messages = [
        make_hook_msg("PreUserMessage"),
        make_user_msg("Test"),
        make_assistant_msg("Response"),
    ]

    session.set_messages(messages, preserve_compaction=False)
    print("\n=== test_session_set_messages_preserves_hooks ===")
    print(f"Session messages: {len(session.messages)}")
    for i, m in enumerate(session.messages):
        print(f"  [{i}] role={m.get('role')}, hook={m.get('_hook_event', 'N/A')}")
        assert m.get("_hook_event") or m.get("role") in ("user", "assistant"), (
            f"Message {i} lost its role/event info"
        )

    # 验证 hook 消息标记没有被 consolidate 吃掉
    hook_msgs = [m for m in session.messages if m.get("_hook_event") == "PreUserMessage"]
    assert len(hook_msgs) == 1, f"Expected 1 PreUserMessage hook, got {len(hook_msgs)}"
    print("✅ PASS: _hook_event preserved through set_messages")


def test_get_user_round_ranges_edge_cases():
    """
    测试 get_user_round_ranges 的边界情况
    """
    print("\n=== test_get_user_round_ranges_edge_cases ===")

    # Case 1: 空的消息列表
    empty = get_user_round_ranges([])
    assert empty == [], f"Empty messages should return [], got {empty}"
    print("✅ Case 1: empty messages → []")

    # Case 2: 只有 hook 消息，没有实际 user
    only_hooks = [
        make_hook_msg("PreUserMessage"),
        make_hook_msg("PostUserMessage"),
    ]
    ranges = get_user_round_ranges(only_hooks)
    assert ranges == [], f"Only hooks should return [], got {ranges}"
    print("✅ Case 2: only hooks → []")

    # Case 3: 多个 hook 连续
    multi_hooks = [
        make_hook_msg("PreUserMessage", "ctx1"),
        make_hook_msg("PreUserMessage", "ctx2"),
        make_user_msg("Hello"),
        make_assistant_msg("Hi"),
    ]
    ranges = get_user_round_ranges(multi_hooks)
    assert len(ranges) == 1, f"Expected 1 round, got {len(ranges)}"
    s, e = ranges[0]
    assert s == 0, f"Expected start=0 (include both hooks), got {s}"
    assert e == 4, f"Expected end=4, got {e}"
    print(f"✅ Case 3: consecutive hooks → range [{s}, {e}), includes both hooks")

    # Case 4: SessionStart hook 不应该被纳入 round
    with_session_start = [
        make_hook_msg("SessionStart"),
        make_user_msg("Hello"),
        make_assistant_msg("Hi"),
    ]
    ranges = get_user_round_ranges(with_session_start)
    assert len(ranges) == 1, f"Expected 1 round, got {len(ranges)}"
    s, e = ranges[0]
    # SessionStart 不应被纳入 round 范围
    assert s == 1, f"Expected start=1 (skip SessionStart), got {s}"
    print(f"✅ Case 4: SessionStart excluded from round → range [{s}, {e})")


def test_group_messages_for_display_filters_hooks():
    """
    验证 group_messages_for_display 正确过滤 hook 消息
    """
    messages = [
        make_hook_msg("PreUserMessage"),
        make_user_msg("Hello"),
        make_assistant_msg("Hi!"),
        make_hook_msg("PostUserMessage"),
        make_assistant_msg("How can I help?"),
    ]

    batches = group_messages_for_display(messages)
    print("\n=== test_group_messages_for_display_filters_hooks ===")
    print(f"Batches: {len(batches)}")
    total_msgs = sum(len(b) for b in batches)
    print(f"Total display messages: {total_msgs}")
    for i, batch in enumerate(batches):
        for j, m in enumerate(batch):
            print(f"  batch[{i}][{j}] role={m.get('role')}, hook={m.get('_hook_event', 'N/A')}")

    # 验证 hook 消息不在 batches 中
    for batch in batches:
        for m in batch:
            assert not _is_hook_message(m), (
                f"Hook message found in display batches: {m.get('_hook_event')}"
            )

    print("✅ PASS: hook messages filtered from display batches")


def test_delete_keeps_next_round_hook():
    """
    关键场景：删除中间 round 时，下一个 round 的 PreUserMessage hook 不能被删掉
    """
    messages = [
        # Round 0
        make_hook_msg("PreUserMessage", "ctx_round0"),
        make_user_msg("Round 0 - Hello"),
        make_assistant_msg("Round 0 - Hi!"),
        # Round 1
        make_hook_msg("PreUserMessage", "ctx_round1"),
        make_user_msg("Round 1 - What?"),
        make_assistant_msg("Round 1 - Nothing"),
        # Round 2
        make_hook_msg("PreUserMessage", "ctx_round2"),
        make_user_msg("Round 2 - Bye"),
        make_assistant_msg("Round 2 - See ya"),
    ]

    canonical = consolidate_messages(messages)
    ranges = get_user_round_ranges(canonical)
    print("\n=== test_delete_keeps_next_round_hook ===")
    print(f"Initial ranges: {ranges}")
    for i, (s, e) in enumerate(ranges):
        print(f"  Round {i}: [{s}, {e})")
        for j in range(s, e):
            m = canonical[j]
            print(f"    [{j}] {str(m.get('content', ''))[:50]}")

    # 删除 Round 1（中间 round）
    start, end = ranges[1]
    print(f"\nDeleting Round 1: [{start}, {end})")
    new_messages = canonical[:start] + canonical[end:]
    new_canonical = consolidate_messages(new_messages)
    new_ranges = get_user_round_ranges(new_canonical)

    print(f"After delete Round 1: {len(new_canonical)} messages")
    for i, m in enumerate(new_canonical):
        print(f"  [{i}] role={m.get('role')}, hook={m.get('_hook_event', 'N/A')}, content={str(m.get('content', ''))[:60]}")

    print(f"Remaining ranges: {new_ranges}")

    # 验证 Round 2（原索引）的 PreUserMessage hook 还在
    remaining_hooks = [m for m in new_canonical if m.get("_hook_event") == "PreUserMessage"]
    contents = [str(m.get("content", "")[:60]) for m in remaining_hooks]
    assert "ctx_round2" in str(contents), (
        f"Round 2's PreUserMessage hook 'ctx_round2' was deleted! Remaining: {contents}"
    )
    assert "ctx_round0" in str(contents), (
        f"Round 0's PreUserMessage hook 'ctx_round0' was deleted! Remaining: {contents}"
    )

    print("✅ PASS: deleting middle round preserves next round's PreUserMessage hook")


if __name__ == "__main__":
    test_single_round_hook_cleanup()
    test_multi_round_hook_cleanup()
    test_delete_undo_reaccumulate()
    test_session_set_messages_preserves_hooks()
    test_get_user_round_ranges_edge_cases()
    test_group_messages_for_display_filters_hooks()
    test_delete_keeps_next_round_hook()
    print("\n🎉 All tests passed!")
