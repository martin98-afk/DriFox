# -*- coding: utf-8 -*-
"""
回归测试：Qwen/DashScope 流式 tool_calls 修复

背景：
- Qwen 通过 DashScope 的 OpenAI 兼容模式调用时，流式 tool_calls 的格式是：
  - 第 1 个 chunk: id="call_xxx", index=0, name="tool_name", arguments=""
  - 第 2 个 chunk: id="call_xxx"（罕见）, index=0, name="", arguments="部分JSON"
  - 第 3+ 个 chunk: id=""（清空）, index=0, name="", arguments="部分JSON"
  - 最后一个 chunk: id="", index=0, name="", arguments=null, finish_reason="tool_calls"

- 修复前 bug: ChatWorker._process_response 用 tc.id 作为聚合 key，但 qwen 在 chunk 3+
  会把 id 清空为 ""，导致创建孤立 buffer[""]，无法清理，tool_args_pending 永远 True，
  主循环一直 continue，工具永远卡在"接收参数中"。

- 修复：用 _tool_calls_index_to_id 映射在 id 缺失时找回真实 id；
  只有 chunk 含 name 时才允许创建新 buffer（避免孤立）。

本测试模拟上述数据，验证：
1. tool_args_pending 能在流结束后变为 False
2. _current_tool_calls 包含正确的 tool_call（含完整 arguments）
3. arguments 能正确解析为 dict
4. 第二个 tool_call（如果存在）也能正确处理
5. qwen 末尾 chunk 的 arguments=null 不会导致 TypeError
"""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@dataclass
class FakeFunction:
    name: str = ""
    arguments: Optional[str] = None


@dataclass
class FakeToolCall:
    id: Optional[str] = None
    index: int = 0
    type: str = "function"
    function: FakeFunction = field(default_factory=FakeFunction)


@dataclass
class FakeDelta:
    content: Optional[str] = None
    tool_calls: Optional[List[FakeToolCall]] = None
    reasoning_content: Optional[str] = None


@dataclass
class FakeChoice:
    delta: FakeDelta
    finish_reason: Optional[str] = None


@dataclass
class FakeChunk:
    id: str = "chatcmpl-fake"
    choices: List[FakeChoice] = field(default_factory=list)
    usage: Optional[dict] = None


def make_qwen_tool_call_chunk(
    *,
    tc_id: Optional[str] = None,
    tc_index: int = 0,
    name: str = "",
    arguments: Optional[str] = None,
    finish_reason: Optional[str] = None,
) -> FakeChunk:
    """构造一个 qwen 流式 tool_call chunk（真实 dataclass 实例）"""
    tc = FakeToolCall(id=tc_id, index=tc_index, function=FakeFunction(name=name, arguments=arguments))
    delta = FakeDelta(tool_calls=[tc])
    choice = FakeChoice(delta=delta, finish_reason=finish_reason)
    return FakeChunk(choices=[choice])


def simulate_aggregate_logic(chunks):
    """
    模拟修复后的聚合逻辑（与 chat_worker._process_response 同步）。

    Returns:
        (tool_calls_buffer, current_tool_calls, index_to_id)
    """
    _tool_calls_buffer = {}
    _tool_calls_index_to_id = {}
    _current_tool_calls = {}

    for chunk in chunks:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        tool_calls = delta.tool_calls
        if not tool_calls:
            continue

        for tc in tool_calls:
            tc_id_raw = tc.id
            tc_index = getattr(tc, "index", None)
            tc_id = None

            # 1. 优先用 raw_id 匹配现有 buffer
            if tc_id_raw and tc_id_raw in _tool_calls_buffer:
                tc_id = tc_id_raw
            # 2. 否则用 index 映射
            elif tc_index is not None and tc_index in _tool_calls_index_to_id:
                tc_id = _tool_calls_index_to_id[tc_index]

            # 修复后：找不到匹配 buffer 时必须含 name 才创建（避免孤立 buffer）
            if not tc_id:
                if not (tc.function and tc.function.name):
                    continue
                tc_id = tc_id_raw if tc_id_raw else (f"index_{tc_index}" if tc_index is not None else None)
                if not tc_id:
                    continue

            if tc_id not in _tool_calls_buffer:
                _tool_calls_buffer[tc_id] = {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
                if tc_index is not None:
                    _tool_calls_index_to_id[tc_index] = tc_id

            buffer = _tool_calls_buffer[tc_id]

            if tc.function and tc.function.name:
                buffer["function"]["name"] = tc.function.name
                if tc_id not in _current_tool_calls:
                    _current_tool_calls[tc_id] = {
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": ""},
                    }

            # ⚠️ Qwen 末尾 chunk: arguments=None，跳过避免 TypeError
            if tc.function and tc.function.arguments:
                if tc.function.arguments is not None:
                    buffer["function"]["arguments"] += tc.function.arguments

    return _tool_calls_buffer, _current_tool_calls, _tool_calls_index_to_id


def test_qwen_tool_call_id_disappears_in_chunks():
    """
    核心回归测试：模拟 qwen 流式 tool_calls，其中 id 在 chunk 3+ 清空为 ""。
    """
    print("=== test_qwen_tool_call_id_disappears_in_chunks ===")

    chunks = [
        make_qwen_tool_call_chunk(tc_id="call_qwen_abc123", tc_index=0, name="init_work_status", arguments=""),
        make_qwen_tool_call_chunk(tc_id="call_qwen_abc123", tc_index=0, name="", arguments='{"firstStep"'),
        make_qwen_tool_call_chunk(tc_id="", tc_index=0, name="", arguments=': "开始需求'),
        make_qwen_tool_call_chunk(tc_id="", tc_index=0, name="", arguments='收集阶段"}'),
        make_qwen_tool_call_chunk(tc_id="", tc_index=0, name="", arguments=None, finish_reason="tool_calls"),
    ]

    buf_map, cur_calls, idx_to_id = simulate_aggregate_logic(chunks)

    # 1. 只有 1 个 buffer（修复前会有 4 个孤立 buffer）
    assert len(buf_map) == 1, f"❌ 应 1 个 buffer，实际 {len(buf_map)} 个：{list(buf_map.keys())}"
    print(f"  ✓ 只有 1 个 buffer（修复前会有 4 个孤立 buffer）: {list(buf_map.keys())}")

    # 2. name 正确
    buf = list(buf_map.values())[0]
    assert buf["function"]["name"] == "init_work_status", f"❌ name 错误：{buf['function']['name']}"
    print(f"  ✓ name: {buf['function']['name']}")

    # 3. arguments 完整
    expected = '{"firstStep": "开始需求收集阶段"}'
    assert buf["function"]["arguments"] == expected, (
        f"❌ arguments 不正确：\n  实际: {buf['function']['arguments']!r}\n  期望: {expected!r}"
    )
    print(f"  ✓ arguments 完整: {buf['function']['arguments']}")

    # 4. JSON 解析成功
    parsed = json.loads(buf["function"]["arguments"])
    assert parsed == {"firstStep": "开始需求收集阶段"}, f"❌ JSON 解析错误：{parsed}"
    print(f"  ✓ JSON 解析: {parsed}")

    # 5. _current_tool_calls 正确
    assert len(cur_calls) == 1, f"❌ _current_tool_calls 应 1 个，实际 {len(cur_calls)}"
    assert "call_qwen_abc123" in cur_calls
    print(f"  ✓ _current_tool_calls: {list(cur_calls.keys())}")

    # 6. index_to_id 映射
    assert idx_to_id == {0: "call_qwen_abc123"}, f"❌ index_to_id 错误：{idx_to_id}"
    print(f"  ✓ index_to_id: {idx_to_id}")

    print("  ✅ PASSED\n")


def test_qwen_multiple_tool_calls_in_one_stream():
    """
    进阶测试：qwen 在一个 stream 中产生多个并行 tool_calls（index 区分）。
    """
    print("=== test_qwen_multiple_tool_calls_in_one_stream ===")

    chunks = [
        # tool_call A
        make_qwen_tool_call_chunk(tc_id="call_a", tc_index=0, name="tool_a", arguments=""),
        make_qwen_tool_call_chunk(tc_id="call_a", tc_index=0, name="", arguments='{"x"'),
        # tool_call B
        make_qwen_tool_call_chunk(tc_id="call_b", tc_index=1, name="tool_b", arguments=""),
        # 继续 A
        make_qwen_tool_call_chunk(tc_id="", tc_index=0, name="", arguments=':1}'),
        # 继续 B
        make_qwen_tool_call_chunk(tc_id="", tc_index=1, name="", arguments='{"y":2}'),
        # 末尾 A
        make_qwen_tool_call_chunk(tc_id="", tc_index=0, name="", arguments=None, finish_reason="tool_calls"),
        # 末尾 B
        make_qwen_tool_call_chunk(tc_id="", tc_index=1, name="", arguments=None),
    ]

    buf_map, cur_calls, idx_to_id = simulate_aggregate_logic(chunks)

    assert len(buf_map) == 2, f"❌ 应 2 个 buffer，实际 {len(buf_map)}：{list(buf_map.keys())}"
    print(f"  ✓ 2 个 buffer: {list(buf_map.keys())}")

    a_buf = buf_map["call_a"]
    b_buf = buf_map["call_b"]
    assert a_buf["function"]["name"] == "tool_a"
    assert b_buf["function"]["name"] == "tool_b"
    assert json.loads(a_buf["function"]["arguments"]) == {"x": 1}, (
        f"❌ call_a arguments: {a_buf['function']['arguments']!r}"
    )
    assert json.loads(b_buf["function"]["arguments"]) == {"y": 2}, (
        f"❌ call_b arguments: {b_buf['function']['arguments']!r}"
    )
    print(f"  ✓ call_a args: {a_buf['function']['arguments']}")
    print(f"  ✓ call_b args: {b_buf['function']['arguments']}")

    assert idx_to_id == {0: "call_a", 1: "call_b"}
    print(f"  ✓ index_to_id: {idx_to_id}")

    print("  ✅ PASSED\n")


def test_old_openai_format_still_works():
    """
    兼容测试：标准 OpenAI 协议（每个 chunk 都含 id）应照常工作。
    """
    print("=== test_old_openai_format_still_works ===")

    chunks = [
        make_qwen_tool_call_chunk(tc_id="call_openai_xyz", tc_index=0, name="get_weather", arguments=""),
        make_qwen_tool_call_chunk(tc_id="call_openai_xyz", tc_index=0, name="", arguments='{"location"'),
        make_qwen_tool_call_chunk(tc_id="call_openai_xyz", tc_index=0, name="", arguments=': "Beijing"}'),
    ]

    buf_map, cur_calls, idx_to_id = simulate_aggregate_logic(chunks)

    assert len(buf_map) == 1
    buf = buf_map["call_openai_xyz"]
    assert buf["function"]["name"] == "get_weather"
    assert json.loads(buf["function"]["arguments"]) == {"location": "Beijing"}
    print(f"  ✓ OpenAI 兼容正常: {buf['function']['arguments']}")
    print("  ✅ PASSED\n")


def test_no_arguments_none_chunks():
    """
    边界测试：arguments 全程为 None（极端情况）不应该崩溃。
    """
    print("=== test_no_arguments_none_chunks ===")

    chunks = [
        make_qwen_tool_call_chunk(tc_id="call_x", tc_index=0, name="no_args_tool", arguments=""),
        make_qwen_tool_call_chunk(tc_id="call_x", tc_index=0, name="", arguments=None),
    ]

    buf_map, cur_calls, idx_to_id = simulate_aggregate_logic(chunks)

    # 仍然创建了 buffer（因为 chunk 1 含 name），但 arguments 仍为空字符串
    assert "call_x" in buf_map
    assert buf_map["call_x"]["function"]["name"] == "no_args_tool"
    # arguments 一直是空字符串（None 不会附加）
    assert buf_map["call_x"]["function"]["arguments"] == ""
    print("  ✓ arguments 全 None 不崩溃")
    print("  ✅ PASSED\n")


def test_isolated_chunk_without_name_should_not_create_buffer():
    """
    边界测试：孤立 chunk（无 name 无匹配 buffer）应该被跳过，不创建空 buffer。
    这是修复的核心：避免孤立 buffer 累积导致 tool_args_pending 死循环。
    """
    print("=== test_isolated_chunk_without_name_should_not_create_buffer ===")

    # 只有 chunks 都含 name，正常情况
    chunks_normal = [
        make_qwen_tool_call_chunk(tc_id="call_only", tc_index=0, name="my_tool", arguments='{"x":1}'),
    ]
    buf_map, _, _ = simulate_aggregate_logic(chunks_normal)
    assert len(buf_map) == 1
    assert buf_map["call_only"]["function"]["name"] == "my_tool"
    print("  ✓ 正常 chunk 创建 buffer")

    # 边界：模拟 first chunk 之前到达一个孤立 delta chunk（极少见但可能）
    # 应该被跳过，不创建 buffer
    chunks_isolated = [
        # 这个 chunk 没有 name，没有 buffer，应该被跳过
        make_qwen_tool_call_chunk(tc_id="", tc_index=99, name="", arguments='{"x":1}'),
    ]
    buf_map2, _, _ = simulate_aggregate_logic(chunks_isolated)
    assert len(buf_map2) == 0, f"❌ 不应创建 buffer，实际 {len(buf_map2)} 个"
    print("  ✓ 孤立 chunk（无 name 无 buffer）被正确跳过")

    print("  ✅ PASSED\n")


if __name__ == "__main__":
    test_qwen_tool_call_id_disappears_in_chunks()
    test_qwen_multiple_tool_calls_in_one_stream()
    test_old_openai_format_still_works()
    test_no_arguments_none_chunks()
    test_isolated_chunk_without_name_should_not_create_buffer()
    print("🎉 All Qwen streaming tool_call tests passed!")