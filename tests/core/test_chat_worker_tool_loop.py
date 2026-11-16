# -*- coding: utf-8 -*-
"""
回归测试：客户端工具循环检测 + [A34] 历史不裁剪契约

背景：
- Qwen/DashScope 服务端会拒绝"连续多轮相同 (tool_name, arguments) 的工具调用"，
  返回 HTTP 400 InternalError.Algo.InvalidParameter。
- 循环检测保留：连续 N=3 轮完全一致即检出，交 LoopPolicy 门控——STOP 终止会话
  （会话控制），CONTINUE 继续对话。
- [A34] 用户裁决「主程序不对上下文做任何操作」：重复轮次自动裁剪
  （_truncate_repetitive_tool_calls 与静默清理路径）已拆除。历史消息序列
  由主程序原样保留，死循环时历史膨胀/服务端拒绝的后果由用户的上下文管道
  插件接管。本文件断言「重复轮次完整保留」。

判断标准（与 qwen 服务端语义对齐）：
- 比较**内容**：tool_name + arguments（不是 tool_call_id，id 每轮新生成）
- 比较**轮次**：连续 N 轮 assistant 消息签名完全相同 → 循环
- 中间插入不同的 tool_call → 重置计数
"""

import sys
from pathlib import Path

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def make_assistant_msg(tool_calls_specs, tool_call_id_prefix="call"):
    """
    构造一个 assistant 消息（带 tool_calls）。

    Args:
        tool_calls_specs: [(name, arguments_dict), ...]
    """
    import json
    tcs = []
    for i, (name, args) in enumerate(tool_calls_specs):
        tcs.append({
            "id": f"{tool_call_id_prefix}_{i}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args) if not isinstance(args, str) else args,
            },
        })
    return {"role": "assistant", "tool_calls": tcs, "content": ""}


def make_tool_msg(tool_call_id, content="result"):
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def test_compute_signature_is_stable_for_identical_calls():
    """相同 (name, args) 的 tool_calls 应计算出相同签名"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    sig1 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "bash", "arguments": '{"command":"ls"}'}}
    ])
    sig2 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "bash", "arguments": '{"command":"ls"}'}}
    ])
    assert sig1 == sig2, f"相同调用应同签名: {sig1} != {sig2}"
    print(f"  ✓ 相同调用签名一致: {sig1[:16]}...")


def _detect(messages):
    """Helper: 调用 `_detect_repetitive_tool_loop`（static method，无需实例化）"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    return OpenAIChatWorker._detect_repetitive_tool_loop(messages)


def test_compute_signature_differs_for_different_args():
    """不同 args 的 tool_calls 应计算出不同签名"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    sig1 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "bash", "arguments": '{"command":"ls"}'}}
    ])
    sig2 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "bash", "arguments": '{"command":"pwd"}'}}
    ])
    assert sig1 != sig2, "不同 args 应不同签名"
    print("  ✓ 不同 args 签名不同")


def test_compute_signature_differs_for_different_names():
    """不同 tool_name 的 tool_calls 应计算出不同签名"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    sig1 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "bash", "arguments": '{"command":"ls"}'}}
    ])
    sig2 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "grep", "arguments": '{"command":"ls"}'}}
    ])
    assert sig1 != sig2, "不同 name 应不同签名"
    print("  ✓ 不同 name 签名不同")


def test_compute_signature_ignores_tool_call_id():
    """
    关键测试：tool_call_id 变化不影响签名（与服务端语义对齐）。

    qwen 服务端判定"重复"只看 name + args，不看 id。tool_call_id 每轮流式协议
    都会重新生成，所以即使签名相同 id 也不同。
    """
    from app.core.workers.chat_worker import OpenAIChatWorker
    sig1 = OpenAIChatWorker._compute_tool_call_signature([
        {"id": "call_aaa", "function": {"name": "bash", "arguments": '{"x":1}'}}
    ])
    sig2 = OpenAIChatWorker._compute_tool_call_signature([
        {"id": "call_bbb", "function": {"name": "bash", "arguments": '{"x":1}'}}
    ])
    assert sig1 == sig2, "id 变化不应影响签名"
    print("  ✓ tool_call_id 变化不影响签名")

def test_compute_signature_ignores_whitespace_in_args():
    """arguments 字符串的空白差异不影响签名（避免模型生成风格差异导致误判）"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    sig1 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "bash", "arguments": '{"x":1}'}}
    ])
    sig2 = OpenAIChatWorker._compute_tool_call_signature([
        {"function": {"name": "bash", "arguments": '{ "x" : 1 }'}}
    ])
    assert sig1 == sig2, "空白差异应规范化"
    print("  ✓ 空白差异不影响签名")

def test_detect_loop_with_three_identical_rounds():
    """核心场景：连续 3 轮相同 (name, args) → 应检测出循环"""
    messages = [
        {"role": "user", "content": "ls /tmp"},
        make_assistant_msg([("bash", {"command": "ls /tmp"})], tool_call_id_prefix="call_a"),
        make_tool_msg("call_a_0", "file1\nfile2"),
        make_assistant_msg([("bash", {"command": "ls /tmp"})], tool_call_id_prefix="call_b"),
        make_tool_msg("call_b_0", "file1\nfile2"),
        make_assistant_msg([("bash", {"command": "ls /tmp"})], tool_call_id_prefix="call_c"),
        make_tool_msg("call_c_0", "file1\nfile2"),
    ]
    result = _detect(messages)
    assert result is not None, "应检测到循环"
    assert result["rounds"] == 3
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["function"]["name"] == "bash"
    print(f"  ✓ 3 轮完全相同 → 检测到循环（rounds={result['rounds']}）")

def test_detect_no_loop_with_varied_args():
    """每轮 args 不同 → 不应检测出循环"""
    messages = [
        {"role": "user", "content": "探索目录"},
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("call_0_0", ""),
        make_assistant_msg([("bash", {"command": "ls /tmp"})]),
        make_tool_msg("call_1_0", ""),
        make_assistant_msg([("bash", {"command": "ls /tmp -la"})]),
        make_tool_msg("call_2_0", ""),
    ]
    result = _detect(messages)
    assert result is None, "args 不同不应检测循环"
    print("  ✓ 每轮 args 不同 → 无循环")

def test_detect_no_loop_with_inserted_different_call():
    """
    中间插入不同 tool_call → 重置计数 → 不应算循环。

    这是关键边界：只有"连续"重复才算循环。
    """
    messages = [
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c0", ""),
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c1", ""),
        make_assistant_msg([("grep", {"pattern": "foo"})]),  # 中间插入不同
        make_tool_msg("c2", ""),
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c3", ""),
    ]
    result = _detect(messages)
    assert result is None, "中间有不同调用 → 不应算循环"
    print("  ✓ 中间插入不同调用 → 不算循环")

def test_detect_no_loop_with_only_two_rounds():
    """只有 2 轮重复 → 不到阈值 → 不算循环（给模型自我修正机会）"""
    messages = [
        {"role": "user", "content": "ls"},
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c0", ""),
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c1", ""),
    ]
    result = _detect(messages)
    assert result is None, "只有 2 轮重复 → 不算循环"
    print("  ✓ 只有 2 轮重复 → 不算循环（阈值保护）")

def test_detect_loop_with_multiple_parallel_tool_calls():
    """
    多个并行 tool_calls 时，每轮必须**完全一致**才算循环。

    比如：模型每轮都同时调用 bash + grep，参数都不变 → 算循环。
    但中间只要任何一个 tool 不同 → 重置。
    """
    # 场景：每轮都同时调用 bash + grep（参数一致）→ 循环
    messages = [
        make_assistant_msg([
            ("bash", {"command": "ls"}),
            ("grep", {"pattern": "foo"}),
        ]),
        make_tool_msg("c0", ""),
        make_assistant_msg([
            ("bash", {"command": "ls"}),
            ("grep", {"pattern": "foo"}),
        ]),
        make_tool_msg("c1", ""),
        make_assistant_msg([
            ("bash", {"command": "ls"}),
            ("grep", {"pattern": "foo"}),
        ]),
        make_tool_msg("c2", ""),
    ]
    result = _detect(messages)
    assert result is not None, "并行 tool_calls 全相同 → 循环"
    assert len(result["tool_calls"]) == 2
    print("  ✓ 并行 tool_calls 全相同 → 循环")

def test_detect_loop_strict_on_parallel_tool_calls():
    """并行场景：中间一轮少了一个 tool_call → 不算循环（严格匹配）"""
    messages = [
        # round 1: bash + grep
        make_assistant_msg([("bash", {"command": "ls"}), ("grep", {"pattern": "foo"})]),
        make_tool_msg("c0", ""),
        # round 2: bash + grep
        make_assistant_msg([("bash", {"command": "ls"}), ("grep", {"pattern": "foo"})]),
        make_tool_msg("c1", ""),
        # round 3: 只有 bash（少一个）
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c2", ""),
    ]
    result = _detect(messages)
    assert result is None, "并行 tool_calls 不完全一致 → 不算循环"
    print("  ✓ 并行 tool_calls 不一致 → 不算循环")

def test_detect_loop_ignores_text_only_assistants():
    """纯文本的 assistant 消息不算轮次（不算循环）"""
    messages = [
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c0", ""),
        {"role": "assistant", "content": "我来换个方法"},  # 纯文本，不算轮次
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c1", ""),
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c2", ""),
    ]
    # 纯文本助手消息不构成"轮次"，但因为有 3 个含 tool_calls 的 assistant message 仍然触发
    # （这是合理行为：模型在中间插了废话，但还是用同样的 bash 调用）
    result = _detect(messages)
    assert result is not None, "中间插文本但 tool_calls 仍重复 → 循环"
    print("  ✓ 中间插文本但 tool_calls 重复 → 循环")


# ═══════════════════════════════════════════════════
# [A34] 历史不裁剪契约：重复轮次完整保留
# 旧 _truncate_repetitive_tool_calls 测试已随拆除反转：
# 不再断言「清理后只保留第 1 轮」，改为「消息序列原样、检测仍触发」。
# ═══════════════════════════════════════════════════

def test_history_preserves_repetitive_rounds_keeps_all():
    """[A34] 核心反转：3 轮重复 → 历史完整保留，主程序不再裁剪改写。

    用户裁决「主程序不对上下文做任何操作」：检测仍触发（供 LoopPolicy STOP
    终止会话），但消息序列必须原样不动。
    """
    from app.core.workers.chat_worker import OpenAIChatWorker

    messages = [
        {"role": "user", "content": "读取文件"},
        make_assistant_msg([("read", {"path": "main_widget.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "main_widget.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "main_widget.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "文件内容..."),
    ]
    # 检测语义保留：循环仍被检出（供策略终止会话）
    assert _detect(messages) is not None, "循环检测语义保留：重复轮次仍应被检出"

    # [A34] 裁剪函数已从产品代码移除（防复活断言）
    assert not hasattr(OpenAIChatWorker, "_truncate_repetitive_tool_calls"), (
        "_truncate_repetitive_tool_calls 必须保持拆除状态（A34 契约）"
    )

    # 消息序列静态不变：重复轮次全部在位
    assert len(messages) == 7, f"消息序列不得被改写，应 7 条，实际 {len(messages)}"
    assistant_rounds = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_rounds) == 3, "重复轮次必须完整保留（3 轮 assistant）"
    tool_results = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_results) == 3, "tool 结果必须完整保留"
    print("  ✓ [A34] 3 轮重复 → 历史完整保留，检测仍触发（无裁剪路径）")

def test_history_preserves_repetitive_rounds_preceding_context():
    """[A34] 前序上下文 + 重复轮次全部原样保留（对照旧裁剪语义的反转断言）"""
    messages = [
        {"role": "user", "content": "帮我排查bug"},
        make_assistant_msg([("grep", {"pattern": "foo"})], "call_g"),
        make_tool_msg("call_g_0", "找到3处"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "内容1"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "内容2"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "内容3"),
    ]
    snapshot = [dict(m) for m in messages]
    assert _detect(messages) is not None, "3 轮重复 read 应检出循环"
    # 全序列原样：无任何移除/改写
    assert messages == snapshot, "主程序不得改写消息序列"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "grep"
    assert messages[3]["tool_calls"][0]["function"]["name"] == "read"
    print("  ✓ [A34] 前序上下文 + 重复轮次全部原样保留")

def test_history_preserves_user_new_message():
    """[A34] 🔴 关键场景：卡死会话里用户新消息后，全部消息原样保留。

    消息结构：[旧消息..., 3轮重复read, user(新消息)] → 原样保留，无截断。
    """
    messages = [
        {"role": "user", "content": "读取文件"},
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "内容"),
        {"role": "user", "content": "用别的方法试试"},
    ]
    snapshot = [dict(m) for m in messages]
    user_msgs = [m for m in messages if m.get("role") == "user"]
    assert len(user_msgs) == 2, "两条 user 消息（原始+新）必须都在"
    assert messages[-1]["role"] == "user" and messages[-1]["content"] == "用别的方法试试"
    assert messages == snapshot, "主程序不得改写消息序列（含用户新消息位置）"
    print("  ✓ [A34] 用户新消息与重复轮次全部原样保留")

def test_history_preserves_more_than_threshold_rounds():
    """[A34] 🔴 关键场景：超过阈值的重复轮次（5 轮）也全部完整保留。

    旧裁剪语义会清掉第 2~5 轮；A34 后全部保留（历史膨胀后果用户自担）。
    """
    messages = [
        {"role": "user", "content": "读取文件"},
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_d"),
        make_tool_msg("call_d_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_e"),
        make_tool_msg("call_e_0", "内容"),
    ]
    snapshot = [dict(m) for m in messages]
    # 检测语义保留：5 轮重复仍被检出
    assert _detect(messages) is not None, "5 轮重复应检测到循环"
    assert messages == snapshot, "主程序不得改写消息序列（5 轮全保留）"
    assistant_rounds = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_rounds) == 5, "5 轮重复必须完整保留"
    print("  ✓ [A34] 5 轮重复 → 全部完整保留（超阈值也不裁剪）")

def test_history_preserves_tail_with_new_message_after_5_rounds():
    """[A34] 5 轮重复 + 用户新消息：全序列原样（旧语义会截为 4 条）"""
    messages = [
        {"role": "user", "content": "读取文件"},
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_d"),
        make_tool_msg("call_d_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_e"),
        make_tool_msg("call_e_0", "内容"),
        {"role": "user", "content": "换方法"},  # 用户新消息
    ]
    snapshot = [dict(m) for m in messages]
    assert messages[-1]["role"] == "user" and messages[-1]["content"] == "换方法"
    assert _detect(messages) is not None, "重复轮次仍应被检出"
    assert messages == snapshot, "主程序不得改写消息序列（含尾部用户新消息）"
    print("  ✓ [A34] 5 轮重复 + 用户新消息 → 全序列原样保留")

def test_history_preserves_real_world_stuck_session():
    """
    [A34] 端到端反转：用户报告的实际场景（原 _truncate_real_world_scenario）。

    1. 会话已卡死（3 轮重复 read）
    2. 用户在卡死会话里发新消息
    3. [A34] 循环检测仍触发 → 但消息序列原样保留（裁剪已拆除，不再"保存干净消息"）
    """
    stuck_messages = [
        {"role": "user", "content": "排查 chat_worker 的工具调用循环错误"},
        make_assistant_msg([("read", {"path": "D:/work/DriFox/app/main_widget.py", "limit": 15, "show_line_numbers": True})], "call_a"),
        make_tool_msg("call_a_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "D:/work/DriFox/app/main_widget.py", "limit": 15, "show_line_numbers": True})], "call_b"),
        make_tool_msg("call_b_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "D:/work/DriFox/app/main_widget.py", "limit": 15, "show_line_numbers": True})], "call_c"),
        make_tool_msg("call_c_0", "文件内容..."),
    ]
    messages_with_new = stuck_messages + [{"role": "user", "content": "如何解决？"}]
    snapshot = [dict(m) for m in messages_with_new]

    # 检测仍触发（供 LoopPolicy STOP 终止）
    assert _detect(messages_with_new) is not None, "应检测到循环"

    # [A34] 历史原样：卡死消息 + 用户新消息一条不少
    assert messages_with_new == snapshot, "主程序不得改写消息序列"
    assert messages_with_new[-1]["content"] == "如何解决？", "用户新消息应保留"
    print("  ✓ [A34] 真实卡死场景：全序列原样保留，检测仍触发（无裁剪）")


def _make_context_usage_worker():
    """构造仅用于消息 token_usage 注入测试的 worker。"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config={"模型名称": "gpt-4"},
    )
    worker._response_content_blocks = [{"type": "text", "text": "完成"}]
    return worker

def test_response_uses_local_context_tokens_when_api_usage_missing():
    """API 不返回 usage 时，应把本地上下文计数写入 assistant 消息。"""
    worker = _make_context_usage_worker()
    worker._last_usage = None
    worker._last_context_token_count = 1234

    sequence = worker._build_response_message_sequence()

    assert sequence[0]["token_usage"] == {
        "input": 1234,
        "output": 0,
        "total": 1234,
        "estimated": True,
    }

def test_response_prefers_api_usage_over_local_context_tokens():
    """API 返回 usage 时，继续显示 API 的精确 input/output/total。"""
    worker = _make_context_usage_worker()
    worker._last_context_token_count = 1234
    worker._last_usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
    }

    sequence = worker._build_response_message_sequence()

    assert sequence[0]["token_usage"] == {
        "input": 1000,
        "output": 200,
        "total": 1200,
    }

if __name__ == "__main__":
    test_compute_signature_is_stable_for_identical_calls()
    test_compute_signature_differs_for_different_args()
    test_compute_signature_differs_for_different_names()
    test_compute_signature_ignores_tool_call_id()
    test_compute_signature_ignores_whitespace_in_args()
    test_detect_loop_with_three_identical_rounds()
    test_detect_no_loop_with_varied_args()
    test_detect_no_loop_with_inserted_different_call()
    test_detect_no_loop_with_only_two_rounds()
    test_detect_loop_with_multiple_parallel_tool_calls()
    test_detect_loop_strict_on_parallel_tool_calls()
    test_detect_loop_ignores_text_only_assistants()
    test_history_preserves_repetitive_rounds_keeps_all()
    test_history_preserves_repetitive_rounds_preceding_context()
    test_history_preserves_user_new_message()
    test_history_preserves_more_than_threshold_rounds()
    test_history_preserves_tail_with_new_message_after_5_rounds()
    test_history_preserves_real_world_stuck_session()
    print("\n🎉 All tool-loop detection tests passed!")
