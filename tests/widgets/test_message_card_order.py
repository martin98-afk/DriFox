# -*- coding: utf-8 -*-
"""回归测试：消息卡片内思考/工具调用按实际到达顺序交错（Bug B 修复）。

根因
----
同一消息卡片内"所有思考集中在卡片顶部、所有工具调用集中在卡片底部"，
未按实际流式到达顺序交错。三层根因：
  A. `append_tool_result` 用 `_content_data.append` 恒在末尾 → 工具结果排在
     text/reasoning 之后（思考/正文先渲染、工具沉底）
  B. `append_reasoning` 合并到"最后一个 reasoning 块" → 多轮思考堆积，
     且新思考可能合并进已完成的旧块
  C. `_render_markdown_to_html_cached` 把 reasoning 前置拼接到 raw_md 最前

修复（message_card.py）：
  A. `update_tool_streaming` 记录插入锚点（工具调用时 `_content_data` 长度），
     `append_tool_result` 改 `insert(锚点)`；同锚点多工具按启动序号保序；
     无锚点（历史渲染）兜底 append 末尾
  B. `start_new_thinking_block` 登记 `_active_thinking_block`，
     `append_reasoning` 只追加活动块；`_maybe_finish_thinking_for_tool`
     优先活动块且完成后置空
  C. 删除 `_render_markdown_to_html_cached` 的前置拼接（reasoning 已作为
     `<think>` 块按顺序嵌入 raw_md）

测试说明
-------
MessageCard 默认 `_lazy_rendered=False`（viewer 懒创建），本测试只断言
`_content_data` 数据层顺序，不触发 DOM 渲染，无需真实 viewer。
"""

import ast
import re
import sys
import textwrap
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from app.widgets.message_card import MessageCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_card() -> MessageCard:
    """构造 assistant MessageCard（懒渲染模式，仅操作数据层）。"""
    _ensure_qapp()
    return MessageCard(role="assistant")


def _types(card: MessageCard):
    """提取 _content_data 的块类型序列（str 块原样保留）。"""
    return [b.get("type") if isinstance(b, dict) else b for b in card._content_data]


def _tool_ids(card: MessageCard):
    """提取 _content_data 中 tool_result 块的 tool_call_id 序列。"""
    return [
        b.get("tool_call_id")
        for b in card._content_data
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]


def test_interleaved_think_tool_text_order():
    """核心：思考→工具→正文→工具→正文 多轮流式序列，_content_data 顺序正确交错。"""
    card = _make_card()

    # 思考 1
    card.start_new_thinking_block()
    card.append_reasoning("思考一")
    # 工具 1 调用 + 结果
    card.update_tool_streaming("tool_1", "search", {"q": "x"})
    card.append_tool_result(tool_name="search", arguments={"q": "x"}, result="r1", tool_call_id="tool_1")
    # 正文 1
    card.append_text("正文一")
    # 工具 2 调用 + 结果
    card.update_tool_streaming("tool_2", "read", {"path": "f"})
    card.append_tool_result(tool_name="read", arguments={"path": "f"}, result="r2", tool_call_id="tool_2")
    # 正文 2
    card.append_text("正文二")

    # 修复前：["reasoning", "text", "text", "tool_result", "tool_result"]（思考/正文在前、工具沉底）
    # 修复后：思考/工具/正文按到达顺序交错
    assert _types(card) == ["reasoning", "tool_result", "text", "tool_result", "text"], (
        f"块顺序应按流式到达交错，实际 {_types(card)}"
    )
    # 内容归属正确
    rs = [b for b in card._content_data if isinstance(b, dict) and b.get("type") == "reasoning"]
    assert rs[0]["content"] == "思考一"


def test_multi_round_thinking_blocks_not_merged():
    """两轮思考必须各自独立成块（append_reasoning 只追加活动块，不合并进旧块）。"""
    card = _make_card()

    # 第一轮思考 → 工具调用结束思考
    card.start_new_thinking_block()
    card.append_reasoning("第一轮思考")
    card.update_tool_streaming("t1", "search", {"q": "a"})
    card.append_tool_result(tool_name="search", result="r", tool_call_id="t1")
    # 第二轮思考
    card.start_new_thinking_block()
    card.append_reasoning("第二轮思考")

    rs = [b for b in card._content_data if isinstance(b, dict) and b.get("type") == "reasoning"]
    assert len(rs) == 2, f"两轮思考应为 2 个独立块，实际 {len(rs)}"
    assert rs[0]["content"] == "第一轮思考", "第一轮思考内容不得被第二轮污染"
    assert rs[1]["content"] == "第二轮思考", "第二轮思考必须落在自己的新块"


def test_same_anchor_multi_tool_keeps_call_order():
    """同锚点多工具并行：结果乱序到达也必须按调用顺序排列（启动序号保序）。"""
    card = _make_card()

    card.start_new_thinking_block()
    card.append_reasoning("思考")
    # 一轮并行调用两个工具（锚点相同）
    card.update_tool_streaming("ta", "search", {"q": "a"})  # 启动序号 0
    card.update_tool_streaming("tb", "read", {"path": "b"})  # 启动序号 1
    # 结果乱序到达：B 先到、A 后到
    card.append_tool_result(tool_name="read", result="rb", tool_call_id="tb")
    card.append_tool_result(tool_name="search", result="ra", tool_call_id="ta")

    # 按调用顺序（ta 先调用 → ta 结果在前），而非到达顺序
    assert _tool_ids(card) == ["ta", "tb"], f"同锚点多工具应按调用序，实际 {_tool_ids(card)}"


def test_late_tool_result_inserts_back_to_anchor():
    """工具结果晚于下一轮正文到达：仍插回工具调用发生的位置（正文之前）。"""
    card = _make_card()

    card.start_new_thinking_block()
    card.append_reasoning("思考")
    card.update_tool_streaming("t1", "search", {"q": "x"})  # 锚点 = 1
    # 工具执行期间/之后正文先到
    card.append_text("正文先到")
    # 工具结果晚到 → 必须插回锚点（正文之前）
    card.append_tool_result(tool_name="search", result="r", tool_call_id="t1")

    assert _types(card) == ["reasoning", "tool_result", "text"], (
        f"晚到的工具结果应插回锚点（正文之前），实际 {_types(card)}"
    )


def test_no_anchor_falls_back_to_append():
    """无锚点（历史会话渲染等非流式路径）→ 兜底 append 末尾，与修复前行为一致。"""
    card = _make_card()
    card.append_text("正文")
    card.append_tool_result(tool_name="search", result="r", tool_call_id="hist_1")

    assert _types(card) == ["text", "tool_result"], f"无锚点应 append 末尾，实际 {_types(card)}"


def test_render_markdown_no_reasoning_front_concat():
    """AST：_render_markdown_to_html_cached 不再前置拼接 reasoning（Bug B 修复 C 层）。"""
    src_path = Path(__file__).resolve().parent.parent.parent / "app" / "widgets" / "message_card.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_render_markdown_to_html_cached":
            target = node
            break
    assert target is not None, "未找到 _render_markdown_to_html_cached 方法"

    func_src = textwrap.dedent(ast.unparse(target))
    # reasoning 已作为 <think> 块按顺序嵌入 raw_md，不得再前置拼接
    assert not re.search(r"_render_think_block\s*\(.*\)\s*\+\s*raw_md", func_src), (
        "不得再把 reasoning 前置拼接到 raw_md 最前（思考恒顶部的根因）"
    )


def test_maybe_finish_thinking_empty_block_keeps_active_block():
    """空块 early-return 不置活动块为 None（有意等后续 reasoning chunks 追加）。

    边界场景：start_new_thinking_block 创建空 reasoning 块（content=""）后立即
    出现工具调用 → _maybe_finish_thinking_for_tool 应跳过（不置 None），
    后续 append_reasoning 仍追加到该活动块，内容不丢失。
    """
    card = _make_card()

    # 创建空思考块并登记活动块
    card.start_new_thinking_block()
    assert card._active_thinking_block is not None, "start_new_thinking_block 应登记活动块"
    assert not (card._active_thinking_block.get("content") or "").strip(), "前置：思考块内容为空"

    # 工具参数到达 → 触发 _maybe_finish_thinking_for_tool（空块 early-return）
    card.update_tool_streaming("t_empty", "search", {"q": "x"})

    # 关键断言：空块 early-return 不置 None（等后续 reasoning chunks）
    assert card._active_thinking_block is not None, (
        "空块 early-return 时不得置 _active_thinking_block = None（否则后续思考丢块）"
    )

    # 后续 reasoning chunk 追加到同一活动块
    card.append_reasoning("思考内容")
    assert card._active_thinking_block is not None
    assert card._active_thinking_block.get("content") == "思考内容", (
        "空块 early-return 后 append_reasoning 应追加到原活动块"
    )


def test_append_reasoning_no_active_block_falls_back_reverse_scan():
    """无活动块时兜底 reverse-scan 找最后一个 reasoning 块（历史/非流式路径）。

    边界场景：_active_thinking_block 为 None（未走 start_new_thinking_block 的
    流路径，如历史会话渲染），append_reasoning 必须从后向前扫描找到
    最后一个 reasoning 块追加，不新建块。
    """
    card = _make_card()

    # 构造含正文 + 已有思考块的数据（真实 dict 块）
    card._content_data = [
        {"type": "text", "text": "正文"},
        {"type": "reasoning", "content": "旧思考"},
    ]
    card._active_thinking_block = None

    card.append_reasoning("新思考")

    # 不新建块，追加到最后一个 reasoning 块
    rs = [b for b in card._content_data if isinstance(b, dict) and b.get("type") == "reasoning"]
    assert len(rs) == 1, f"reverse-scan 应复用已有 reasoning 块，实际 {len(rs)} 个"
    assert rs[0]["content"] == "旧思考新思考", f"内容应追加到已有块，实际 {rs[0]['content']!r}"


def test_append_reasoning_no_block_anywhere_creates_new():
    """无活动块且无任何 reasoning 块时 → 新建块（兜底路径末端）。"""
    card = _make_card()

    card._content_data = [{"type": "text", "text": "正文"}]
    card._active_thinking_block = None

    card.append_reasoning("新思考")

    rs = [b for b in card._content_data if isinstance(b, dict) and b.get("type") == "reasoning"]
    assert len(rs) == 1, f"无 reasoning 块时应新建，实际 {len(rs)} 个"
    assert rs[0]["content"] == "新思考"
