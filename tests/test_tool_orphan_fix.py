# -*- coding: utf-8 -*-
"""
回归测试：孤儿 tool_call / 孤儿 tool 结果双向修复

背景（MiniMax 2013 错误）：
- `tool result's tool id(call_xxx) not found (2013)`：请求中 tool 结果引用的
  id 在之前的 assistant.tool_calls 中找不到。
- 根因1：`_build_response_message_sequence` Phase 5 误从 tool_result_map 取
  tool_call（tool 结果 dict 无 id/function 键），序列化后 assistant 声明
  {id:"", function:{name:null}}，服务端无法与真实 id 的 tool 结果配对。
- 根因2：`_fix_tool_result_order` / `_clean_orphan_tool_calls` 只清理
  "assistant 声明无结果" 方向，反向孤儿（结果无声明）漏修，
  2013 自动修复重试时 was_fixed=False，错误直达用户。
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _make_worker():
    from app.core.workers.chat_worker import OpenAIChatWorker

    return OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config={"模型名称": "gpt-4"},
    )


def _make_tool_result(tc_id, name="read"):
    return {
        "role": "tool",
        "tool_call_id": tc_id,
        "name": name,
        "arguments": {"path": "a.py"},
        "content": "file content",
        "success": True,
    }


# ========== Phase 5：孤儿 tool_result 分支的 tool_call 结构 ==========


def test_phase5_orphan_tool_result_uses_call_map():
    """流式 blocks 无 marker 时，tool 结果应从 tool_call_map 回填正确声明结构。

    修复前：assistant.tool_calls 被塞入 tool 结果 dict（无 id/function 键），
    序列化后 {id:"", function:{name:null}} → MiniMax 2013。
    """
    worker = _make_worker()
    # 无 marker：所有 tool results 落入 Phase 5（Orphan tool_result）
    worker._response_content_blocks = []
    worker._current_tool_calls = {}
    worker._tool_calls_buffer = {}

    real_id = "call_01a067b24b4474c1ae1c939e"
    sequence = worker._build_response_message_sequence([_make_tool_result(real_id)])

    asst_msgs = [m for m in sequence if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst_msgs, "应产出带 tool_calls 的 assistant 消息"
    tc = asst_msgs[0]["tool_calls"][0]
    assert tc["id"] == real_id, f"tool_call.id 应为真实 id，实际: {tc.get('id')!r}"
    assert tc["function"]["name"] == "read", f"function.name 应为 read，实际: {tc.get('function', {}).get('name')!r}"
    print("  ✓ Phase 5 孤儿 tool_result 回填正确声明结构")


def test_phase5_serialized_declaration_matches_result_id():
    """Phase 5 产物经消息序列化后，assistant 声明 id 与 tool 结果 id 一致。"""
    from app.core.message_content import normalize_message

    worker = _make_worker()
    worker._response_content_blocks = []
    worker._current_tool_calls = {}
    worker._tool_calls_buffer = {}

    real_id = "call_01a067b24b4474c1ae1c939e"
    sequence = worker._build_response_message_sequence([_make_tool_result(real_id)])

    asst = next(m for m in sequence if m.get("role") == "assistant" and m.get("tool_calls"))
    normalized = normalize_message(asst)
    assert normalized is not None
    tc = normalized["tool_calls"][0]
    assert tc["id"] == real_id, f"normalize 后 id 不应丢失，实际: {tc.get('id')!r}"
    assert tc["function"]["name"], "normalize 后 function.name 不应为空"
    print("  ✓ 序列化后声明 id 与 tool 结果 id 配对一致")


# ========== _fix_tool_result_order：反向孤儿清理 ==========


def test_fix_removes_orphan_tool_result_without_declaration():
    """tool 结果存在但之前的 assistant 均未声明该 id → 应删除该 tool 消息。

    修复前 was_fixed=False，2013 自动修复失效。
    """
    worker = _make_worker()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "让我查一下"},  # tool_calls 已丢失
        {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan"},
        {"role": "user", "content": "继续"},
    ]
    fixed, modified = worker._fix_tool_result_order(messages)
    assert modified, "存在孤儿 tool 结果时应报告已修复"
    tool_ids = [m.get("tool_call_id") for m in fixed if m.get("role") == "tool"]
    assert "call_orphan" not in tool_ids, "孤儿 tool 结果应被移除"
    print("  ✓ 无声明孤儿 tool 结果被移除")


def test_fix_keeps_paired_tool_results():
    """正常配对（assistant 声明在前、结果在后）不应误删。"""
    worker = _make_worker()
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_A", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "call_B", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_A", "content": "a"},
        {"role": "tool", "tool_call_id": "call_B", "content": "b"},
        {"role": "assistant", "content": "done"},
    ]
    fixed, modified = worker._fix_tool_result_order(messages)
    assert not modified, "正常配对不应触发修复"
    assert len([m for m in fixed if m.get("role") == "tool"]) == 2
    print("  ✓ 正常配对保持原样")


def test_fix_mixed_orphan_and_paired():
    """混合场景：正常配对保留、孤儿移除、assistant 方向孤儿声明也清理。"""
    worker = _make_worker()
    messages = [
        {"role": "user", "content": "hi"},
        # assistant 声明 call_A(有结果) + call_C(无结果，中断遗留)
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_A", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "call_C", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_A", "content": "a"},
        # 孤儿结果：无任何 assistant 声明 call_X
        {"role": "tool", "tool_call_id": "call_X", "content": "x"},
        {"role": "user", "content": "继续"},
    ]
    fixed, modified = worker._fix_tool_result_order(messages)

    tool_ids = [m.get("tool_call_id") for m in fixed if m.get("role") == "tool"]
    assert tool_ids == ["call_A"], f"应只剩 call_A，实际: {tool_ids}"

    asst = next(m for m in fixed if m.get("role") == "assistant")
    kept_ids = [tc["id"] for tc in asst["tool_calls"]]
    assert kept_ids == ["call_A"], f"assistant 应只保留 call_A 声明，实际: {kept_ids}"
    assert modified
    print("  ✓ 混合场景双向清理正确")


# ========== history_manager._clean_orphan_tool_calls：持久化守门员 ==========


def test_clean_orphan_tool_calls_removes_reverse_orphans():
    """持久化前应同步清理反向孤儿（结果无声明）。"""
    from app.utils.history_manager import _clean_orphan_tool_calls

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "好的"},
        {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan"},
    ]
    cleaned = _clean_orphan_tool_calls(messages)
    tool_ids = [m.get("tool_call_id") for m in cleaned if m.get("role") == "tool"]
    assert "call_orphan" not in tool_ids, "持久化前应移除反向孤儿"
    print("  ✓ 持久化守门员清理反向孤儿")


def test_clean_orphan_tool_calls_keeps_paired():
    """守门员不应误删正常配对。"""
    from app.utils.history_manager import _clean_orphan_tool_calls

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_A", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_A", "content": "a"},
    ]
    cleaned = _clean_orphan_tool_calls(messages)
    assert len([m for m in cleaned if m.get("role") == "tool"]) == 1
    assert cleaned[1]["tool_calls"][0]["id"] == "call_A"
    print("  ✓ 持久化守门员保留正常配对")


# ========== 2013 修复链：畸形结果 dict → 声明结构端到端 ==========


def test_tool_result_dict_as_declaration_survives_normalize():
    """历史脏数据（tool 结果 dict 混入 tool_calls）经 normalize 后产生空 id 声明。

    模拟修复前 Phase 5 产出的脏消息被持久化后重新加载的场景：
    normalize_message 不丢弃该消息，但 id 为空 → 服务端 2013。
    该测试固化 normalize 行为，说明脏数据即使入库也会被修复函数拦下。
    """
    from app.core.message_content import normalize_message

    dirty_asst = {
        "role": "assistant",
        "content": "",
        # 修复前的脏数据：tool 结果 dict 直接当 tool_call
        "tool_calls": [{"role": "tool", "tool_call_id": "call_x", "name": "read", "content": "..."}],
    }
    normalized = normalize_message(dirty_asst)
    # normalize 后 id 必然为空（tool 结果 dict 无 id 键）——这正是 2013 的直接来源
    assert normalized is not None
    assert normalized["tool_calls"][0]["id"] == ""
    print("  ✓ 固化：脏声明经 normalize 后 id 为空（2013 直接来源）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        print(f"▶ {fn.__name__}")
        fn()
    print(f"\n全部 {len(fns)} 个测试通过")
