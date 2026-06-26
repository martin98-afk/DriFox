# -*- coding: utf-8 -*-
"""
回归测试：客户端工具循环检测

背景：
- Qwen/DashScope 服务端会拒绝"连续多轮相同 (tool_name, arguments) 的工具调用"，
  返回 HTTP 400 InternalError.Algo.InvalidParameter。
- 同样的请求序列重试仍会被拒，必须客户端主动中断。
- DriFox 在 ChatWorker 主循环的每次 API 调用前扫描最近 N 轮 assistant 消息的
  tool_calls 签名（按 tool_name + arguments 计算），连续 N=3 轮完全一致就主动
  终止并发友好错误提示。

判断标准（与 qwen 服务端语义对齐）：
- 比较**内容**：tool_name + arguments（不是 tool_call_id，id 每轮新生成）
- 比较**轮次**：连续 N 轮 assistant 消息签名完全相同 → 循环
- 中间插入不同的 tool_call → 重置计数
"""

import sys
from pathlib import Path

# 仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent
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


def _truncate(messages, threshold=3):
    """Helper: 调用 `_truncate_repetitive_tool_calls`（static method）"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    return OpenAIChatWorker._truncate_repetitive_tool_calls(messages, threshold)


def test_truncate_removes_repetitive_rounds_keeps_first():
    """核心：3 轮重复 → 清理后只保留第 1 轮，且不再触发循环检测"""
    messages = [
        {"role": "user", "content": "读取文件"},
        make_assistant_msg([("read", {"path": "main_widget.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "main_widget.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "main_widget.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "文件内容..."),
    ]
    # 清理前应检测到循环
    assert _detect(messages) is not None, "清理前应检测到循环"

    sanitized = _truncate(messages, threshold=3)

    # 保留 user + 第1轮 assistant + tool 结果 = 3 条（不再插入终止提示）
    assert len(sanitized) == 3, f"清理后应剩 3 条，实际 {len(sanitized)}: {sanitized}"
    assert sanitized[0]["role"] == "user"
    assert sanitized[1]["role"] == "assistant"
    assert sanitized[1].get("tool_calls") is not None  # 第1轮 assistant 保留
    assert sanitized[2]["role"] == "tool"

    # 清理后不应再检测到循环
    assert _detect(sanitized) is None, "清理后不应再检测到循环"
    print("  ✓ 3 轮重复 → 清理后只保留第1轮，不再触发循环检测")


def test_truncate_noop_below_threshold():
    """不足阈值 → 不清理，原样返回"""
    messages = [
        {"role": "user", "content": "ls"},
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c0", ""),
        make_assistant_msg([("bash", {"command": "ls"})]),
        make_tool_msg("c1", ""),
    ]
    sanitized = _truncate(messages, threshold=3)
    assert sanitized == messages, "不足阈值不应修改消息"
    print("  ✓ 不足阈值 → 原样返回")


def test_truncate_preserves_preceding_context():
    """清理时应保留循环之前的所有上下文消息"""
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
    sanitized = _truncate(messages, threshold=3)
    # user + grep轮(assistant+tool) + read第1轮(assistant+tool) = 5条（不再插入终止提示）
    assert len(sanitized) == 5, f"应保留前序上下文，实际 {len(sanitized)}: {sanitized}"
    # 前序 grep 调用应保留
    assert sanitized[1]["tool_calls"][0]["function"]["name"] == "grep"
    # read 第1轮应保留
    assert sanitized[3]["tool_calls"][0]["function"]["name"] == "read"
    print("  ✓ 清理后保留前序上下文（grep + read第1轮）")


def test_truncate_allows_session_continuation():
    """端到端验证：清理后追加用户消息，模拟继续对话，不应触发循环检测"""
    messages = [
        {"role": "user", "content": "读取文件"},
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "内容"),
    ]
    sanitized = _truncate(messages, threshold=3)

    # 模拟用户继续发消息
    continued = sanitized + [{"role": "user", "content": "换一种方法试试"}]
    # 即使模型又调了一次同样的 read，也只算第2轮（历史1轮 + 新1轮 = 2轮），不到阈值3
    continued_with_new_call = continued + [
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_d"),
        make_tool_msg("call_d_0", "内容"),
    ]
    assert _detect(continued_with_new_call) is None, \
        "清理后即使再调1次同样的工具也不应触发（只有2轮，不到阈值3）"
    print("  ✓ 清理后会话可继续，即使再调1次也不触发循环检测")


def test_truncate_preserves_user_new_message():
    """
    🔴 关键场景：用户在卡死的会话里发新消息，截断必须保留用户的新消息。

    消息结构：[旧消息..., 3轮重复read, user(新消息)]
    截断后应保留：[旧消息..., read第1轮, user(新消息)]
    """
    messages = [
        {"role": "user", "content": "读取文件"},
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_a"),
        make_tool_msg("call_a_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_b"),
        make_tool_msg("call_b_0", "内容"),
        make_assistant_msg([("read", {"path": "main.py", "limit": 15})], "call_c"),
        make_tool_msg("call_c_0", "内容"),
        {"role": "user", "content": "用别的方法试试"},  # 用户的新消息
    ]
    sanitized = _truncate(messages, threshold=3)

    # 验证用户新消息被保留
    user_msgs = [m for m in sanitized if m.get("role") == "user"]
    assert len(user_msgs) == 2, f"应保留2条user消息（原始+新），实际 {len(user_msgs)}"
    assert user_msgs[-1]["content"] == "用别的方法试试", "用户新消息应在最后"

    # 最后一条应为用户新消息
    assert sanitized[-1]["role"] == "user", "最后一条应为用户新消息"

    # 清理后不应检测到循环
    assert _detect(sanitized) is None, "清理后不应检测到循环"
    print("  ✓ 截断保留用户新消息")


def test_truncate_handles_more_than_threshold_rounds():
    """
    🔴 关键场景：超过阈值的重复轮次（如5轮），截断必须清理所有重复轮次。

    5轮重复 read → 截断后只保留第1轮，移除第2~5轮。
    如果只移除最后2轮，前3轮还在 → 下次又触发循环检测。
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
    # 5轮重复，检测阈值3 → 应检测到循环
    assert _detect(messages) is not None, "5轮重复应检测到循环"

    sanitized = _truncate(messages, threshold=3)

    # 应只保留 user + read第1轮(assistant+tool) = 3条（不再插入终止提示）
    assert len(sanitized) == 3, f"5轮重复应清理为3条，实际 {len(sanitized)}: {sanitized}"
    assert sanitized[1].get("tool_calls") is not None  # 第1轮保留

    # 清理后不应检测到循环
    assert _detect(sanitized) is None, "清理后不应检测到循环"
    print("  ✓ 5轮重复 → 清理为3条（只保留第1轮），不再触发循环检测")


def test_truncate_preserves_tail_with_new_message_after_5_rounds():
    """5轮重复 + 用户新消息：截断后保留第1轮 + 终止提示 + 用户新消息"""
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
    sanitized = _truncate(messages, threshold=3)

    # user + read第1轮(assistant+tool) + user(新) = 4条（不再插入终止提示）
    assert len(sanitized) == 4, f"应保留4条，实际 {len(sanitized)}: {sanitized}"
    assert sanitized[-1]["role"] == "user"
    assert sanitized[-1]["content"] == "换方法"
    assert _detect(sanitized) is None, "清理后不应检测到循环"
    print("  ✓ 5轮重复 + 用户新消息 → 保留第1轮 + 用户新消息")


def test_truncate_real_world_scenario():
    """
    端到端模拟用户报告的实际场景：
    1. 会话已卡死（3轮重复 read main_widget.py）
    2. 用户重启 DriFox，在卡死会话里发新消息
    3. 循环检测触发 → 截断清理 → 保存干净消息
    4. 用户再发消息 → 不再触发循环 → 正常对话
    """
    # Step 1: 卡死会话的消息（从磁盘加载）
    stuck_messages = [
        {"role": "user", "content": "排查 chat_worker 的工具调用循环错误"},
        make_assistant_msg([("read", {"path": "D:/work/DriFox/app/main_widget.py", "limit": 15, "show_line_numbers": True})], "call_a"),
        make_tool_msg("call_a_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "D:/work/DriFox/app/main_widget.py", "limit": 15, "show_line_numbers": True})], "call_b"),
        make_tool_msg("call_b_0", "文件内容..."),
        make_assistant_msg([("read", {"path": "D:/work/DriFox/app/main_widget.py", "limit": 15, "show_line_numbers": True})], "call_c"),
        make_tool_msg("call_c_0", "文件内容..."),
    ]

    # Step 2: 用户发新消息
    messages_with_new = stuck_messages + [{"role": "user", "content": "如何解决？"}]
    assert _detect(messages_with_new) is not None, "应检测到循环"

    # Step 3: 截断清理
    sanitized = _truncate(messages_with_new, threshold=3)
    assert _detect(sanitized) is None, "清理后不应检测到循环"
    assert sanitized[-1]["content"] == "如何解决？", "用户新消息应保留"

    # Step 4: 模拟模型正常回复（不再循环）
    after_reply = sanitized + [
        {"role": "assistant", "content": "这个问题可以通过修改截断逻辑来解决..."},
    ]
    assert _detect(after_reply) is None, "正常回复不应触发循环"
    print("  ✓ 真实场景：卡死会话 → 发新消息 → 截断清理 → 继续对话，全流程通过")


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
    test_truncate_removes_repetitive_rounds_keeps_first()
    test_truncate_noop_below_threshold()
    test_truncate_preserves_preceding_context()
    test_truncate_allows_session_continuation()
    test_truncate_preserves_user_new_message()
    test_truncate_handles_more_than_threshold_rounds()
    test_truncate_preserves_tail_with_new_message_after_5_rounds()
    test_truncate_real_world_scenario()
    print("\n🎉 All tool-loop detection tests passed!")