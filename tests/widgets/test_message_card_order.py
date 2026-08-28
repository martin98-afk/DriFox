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

from PySide6.QtWidgets import QApplication

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
    return [b.get("tool_call_id") for b in card._content_data if isinstance(b, dict) and b.get("type") == "tool_result"]


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


# ──────────────────────────────────────────────
# 方案 D（统一 data-order 排序）+ 方案 C（折叠框边界兜底）AST 回归
# ──────────────────────────────────────────────


def _extract_src() -> str:
    p = Path(__file__).resolve().parent.parent.parent / "app" / "widgets" / "message_card.py"
    return p.read_text(encoding="utf-8")


def test_reorganize_content_uses_data_order():
    """AST：reorganizeContent 的 getPos 必须优先 data-order 排序。

    修复"JS 注入工具块（不在本次 blocks）getPos 无 posMap → 1e9 → 恒排最后"
    导致的思考/工具不交错问题（方案 D 核心）。
    """
    src = _extract_src()
    # reorganizeContent 函数体内的 getPos 必须读取 data-order 并 parseFloat
    assert re.search(r"getAttribute\(['\"]data-order['\"]\)", src), (
        "reorganizeContent 的 getPos 必须读取 data-order 属性"
    )
    assert "parseFloat(od)" in src, "getPos 必须 parseFloat(data-order) 后参与排序"
    # data-order 必须优先于 posMap（在 posMap 兜底之前返回）
    od_pos = src.find("getAttribute('data-order')")
    posmap_pos = src.find("posMap['bk:' + bk]")
    assert od_pos != -1 and posmap_pos != -1 and od_pos < posmap_pos, (
        "getPos 中 data-order 必须优先于 posMap（data-order 分支在前）"
    )


def test_inject_tool_streaming_html_sets_data_order():
    """AST：_inject_tool_streaming_html 新建工具块时必须注入 data-order。

    方案 D-1：Python 端按 _tool_insert_anchors / _tool_call_order 计算
    tool/think 序列位置，JS 新建块时 setAttribute('data-order', ...)。
    """
    import ast as _ast

    tree = _ast.parse(_extract_src())
    target = None
    for node in _ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_inject_tool_streaming_html":
            target = node
            break
    assert target is not None, "未找到 _inject_tool_streaming_html 方法"
    func_src = textwrap.dedent(ast.unparse(target))

    # 必须从稳定锚点计算 tool/think 序列位置（data-order 值来源）
    # 🆕 Bug B 方案 F：改用 _tool_anchor_pos（块引用定位）替代 _tool_insert_anchors
    # （int 索引在其他工具结果插入/思考追加后失效 → finish 重渲染顺序错乱）。
    assert "_tool_anchor_pos(tool_call_id)" in func_src, (
        "data-order 计算必须读取稳定锚点 _tool_anchor_pos（块引用定位）"
    )
    # 必须生成 setAttribute('data-order', ...) JS
    assert "setAttribute('data-order'" in func_src, "JS 新建工具块时必须 setAttribute('data-order', ...)"
    # 同锚点多工具按启动序号细分
    assert "_tool_call_order.get(tool_call_id" in func_src, (
        "同锚点多工具必须按 _tool_call_order 启动序号细分 data-order"
    )


def test_save_and_restore_preserves_data_order():
    """AST：_build_save_and_restore_js restore 时必须保留 data-order。

    方案 D-2：restore 用 outerHTML 重建后显式从保存的 html 重新取回 data-order。
    """
    src = _extract_src()
    # 保存端：save 时记录 outerHTML（天然含 data-order）
    assert "html:el.outerHTML" in src, "save 端必须保存 outerHTML（含 data-order）"
    # 恢复端：显式从保存的 html 匹配 data-order 并设置
    assert re.search(r"b\.html\.match\(/data-order", src), (
        "restore 端必须从保存的 html 匹配 data-order 并重新设置（显式兜底）"
    )


def test_append_tool_result_preserves_data_order():
    """AST：append_tool_result 替换流式块 / 追加完成块时必须同步 data-order。

    方案 D-3：流式块替换为完成态时继承原 data-order；无已有块追加时注入。
    """
    src = _extract_src()
    # 流式块替换时继承旧 data-order
    assert "var _odOld = existing.getAttribute('data-order')" in src, (
        "append_tool_result 替换流式块时必须继承原 data-order"
    )
    # 追加完成块时注入 data-order（与流式注入同口径，f-string 占位符带花括号）
    assert "data-order', {_order_value_js}" in src, "append_tool_result 追加完成块时必须注入 data-order"


def test_maybe_finish_thinking_moves_out_of_content_placeholder():
    """AST：_maybe_finish_thinking_for_tool 替换 think-streaming 后必须迁移出正文区。

    方案 C：若替换后 think-block 仍在 #content-placeholder（未被 reorganizeContent
    迁移，如渲染节流/坞态切换），立即 moveChild 到 #tool-content，根治
    "思考框跑出折叠框"。
    """
    import ast as _ast

    tree = _ast.parse(_extract_src())
    target = None
    for node in _ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_maybe_finish_thinking_for_tool":
            target = node
            break
    assert target is not None, "未找到 _maybe_finish_thinking_for_tool 方法"
    func_src = textwrap.dedent(ast.unparse(target))

    # JS 必须判断父节点是否为 content-placeholder
    assert "newBlock.parentNode === " in func_src, "方案 C 必须判断替换后父节点（content-placeholder 兜底迁移）"
    # 必须迁移到 tool-content
    assert "appendChild(newBlock)" in func_src, "方案 C 必须把跑出折叠框的 think-block 迁回 #tool-content"


# ──────────────────────────────────────────────
# 方案 D 行为级回归（不触发 DOM，验证 data-order 与 posMap 同尺度）
# ──────────────────────────────────────────────

from app.widgets.message_card import _count_think_tool_prefix  # noqa: E402


def _data_order(card: MessageCard, tool_call_id: str) -> float:
    """复刻 message_card.py 内联的 data-order 计算（锚点前缀计数 + 序号细分）。"""
    anchor = card._tool_insert_anchors.get(tool_call_id) or 0
    base = float(_count_think_tool_prefix(card._content_data, anchor))
    order = card._tool_call_order.get(tool_call_id) or 0
    return base + order * 0.001


def test_data_order_matches_think_tool_prefix():
    """工具块 data-order = 锚点前 think/tool 块计数，与 posMap 同尺度可混合排序。

    真实流式时序：思考1 → 工具1调用(锚点=1) → 工具1结果 → 正文 → 思考2 →
    工具2调用(锚点=4)。t1 前缀 think/tool = [R1] = 1；t2 前缀 = [R1, T1, R2] = 3。
    JS 注入块按此 data-order 排序时，能与 markdown 渲染的思考块正确交错，
    而非无 posMap 时 getPos=1e9 恒沉底（"所有思考在前、所有工具在后"）。
    """
    card = _make_card()
    card.start_new_thinking_block()
    card.append_reasoning("思考一")
    card.update_tool_streaming("t1", "search", {"q": "a"})  # 锚点 = 1
    card.append_tool_result(tool_name="search", result="r1", tool_call_id="t1")
    card.append_text("正文")
    card.start_new_thinking_block()
    card.append_reasoning("思考二")
    card.update_tool_streaming("t2", "read", {"path": "b"})  # 锚点 = 4
    card.append_tool_result(tool_name="read", result="r2", tool_call_id="t2")

    assert _data_order(card, "t1") == 1.0, f"t1 前缀应为 1 个 think/tool 块，实际 {_data_order(card, 't1')}"
    # t2 为全局第 2 个工具（order=1）→ 基准 3.0 + 细分 0.001（跨锚点细分不越过整数位）
    assert _data_order(card, "t2") == 3.001, (
        f"t2 前缀应为 3 个 think/tool 块 + 序号细分，实际 {_data_order(card, 't2')}"
    )
    # 单调递增：排序后 JS 注入块与 markdown 渲染块交错顺序正确
    assert _data_order(card, "t1") < _data_order(card, "t2")


def test_data_order_same_anchor_subdivided_by_call_order():
    """同锚点多工具并行：data-order 按启动序号细分（结果乱序到达也不乱序）。"""
    card = _make_card()
    card.start_new_thinking_block()
    card.append_reasoning("思考")
    card.update_tool_streaming("ta", "search", {"q": "a"})  # 启动序号 0
    card.update_tool_streaming("tb", "read", {"path": "b"})  # 启动序号 1
    # 结果乱序到达：B 先到、A 后到
    card.append_tool_result(tool_name="read", result="rb", tool_call_id="tb")
    card.append_tool_result(tool_name="search", result="ra", tool_call_id="ta")

    assert _data_order(card, "ta") == 1.0, f"ta 应排在锚点细分首位，实际 {_data_order(card, 'ta')}"
    assert _data_order(card, "tb") == 1.001, f"tb 应按启动序号细分，实际 {_data_order(card, 'tb')}"
    assert _data_order(card, "ta") < _data_order(card, "tb")


def test_data_order_without_anchor_matches_append_tail():
    """无锚点（历史会话等非流式路径）→ data-order 兜底取末尾位置，与 append 沉底一致。

    锚点缺失时 data-order 基准 = 当前末尾 think/tool 块计数，保证工具块
    排序不早于任何已有块，与 _content_data.append 兜底行为吻合。
    """
    card = _make_card()
    card.append_text("正文")
    card.append_tool_result(tool_name="search", result="r", tool_call_id="hist_1")

    # 无锚点 → anchor 兜底 0 → 前缀计数 0 → data-order 0.0（排在现有 think/tool 块之后）
    assert _data_order(card, "hist_1") == 0.0, f"无锚点 data-order 应为 0.0，实际 {_data_order(card, 'hist_1')}"
    # 数据层仍 append 末尾（修复前行为）
    assert _types(card) == ["text", "tool_result"]


# ──────────────────────────────────────────────
# 方案 D+（流式完成时刻顺序错乱）修复回归
# 根因：_perform_update 非流式分支复用 _cached_streaming_html（流式语义：
# thinking 渲染为 .think-streaming，无 data-block-key）→ reorganizeContent 查不到
# posMap → getPos=1e9 沉底；且 save/restore 插入只比较带 data-order 的子节点，
# 流式完成瞬间"所有思考在前、所有工具在后"（折叠框从底部移到上部的那一刻）。
# 修复：finish_streaming 清流式缓存强制完成态渲染（think 带稳定 block-key）；
#       reorganizeContent 为缺 data-order 的 markdown 块补齐（posMap + 排其前流式工具数）。
# ──────────────────────────────────────────────


def test_finish_streaming_clears_streaming_html_cache():
    """AST：finish_streaming 必须清除流式语义缓存 HTML。

    不清理则 finish 非流式分支复用 _cached_streaming_html（thinking 渲染为
    .think-streaming 无 data-block-key），在"坞态归位/折叠框从底到顶"的重排中对
    思考块查不到 posMap → 错序（Bug B 第三条路径的 finish 变体）。
    """
    src = _extract_src()
    import ast as _ast

    tree = _ast.parse(src)
    target = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "finish_streaming":
            target = node
            break
    assert target is not None, "未找到 finish_streaming 方法"
    body = textwrap.dedent(ast.unparse(target))
    assert "_cached_streaming_html = None" in body, (
        "finish_streaming 必须把 _cached_streaming_html 置 None，强制完成态渲染"
    )


def test_reorganize_content_assigns_data_order_for_markdown_blocks():
    """AST：reorganizeContent 必须为缺 data-order 的 markdown 块补齐。

    流式完成时 save/restore 插入"仍在流式"的工具块，其插入循环只比较带
    data-order 的子节点；若 think/完成工具块（markdown 渲染、仅有 block-key/
    tool-call-id）缺 data-order，插入循环会绕过它们 → 工具块 appendChild 沉底，
    "思考在前、工具在后"。修复：move 后按 posMap + 排在前流式工具数补齐 data-order。
    """
    src = _extract_src()
    assert "getAttribute('data-streaming')" in src and "setAttribute('data-order'" in src, (
        "reorganizeContent 必须读取流式标志并补齐 data-order"
    )
    assert re.search(r"posMap\['bk:' \+", src), "补齐 data-order 必须用 posMap 的 block-key 定位"
    assert re.search(r"posMap\['tcid:' \+", src), "补齐 data-order 必须用 posMap 的 tool-call-id 定位"
    assert "parseFloat(od)" in src


def test_skeleton_cache_version_bumped_for_dplus():
    """AST：方案 D+ 改动了骨架 JS（reorganizeContent），必须递增 _SKELETON_CACHE_VERSION。"""
    src = _extract_src()
    m = re.search(r"_SKELETON_CACHE_VERSION = (\d+)", src)
    assert m and int(m.group(1)) >= 4, f"方案 D+ 改动骨架 JS 后必须 >= 4，实际 {m.group(1) if m else 'None'}"


# ──────────────────────────────────────────────
# 方案 E（坞态归位瞬间"所有思考在前、所有工具在后"复发）修复回归
# 根因：_build_save_and_restore_js 的 save 阶段把所有 data-tool-call-id 块
# （含"仍在流式、尚未进入 _content_data"的工具块）从 #tool-content 移除 →
# reorganizeContent 补 data-order 时 toolContent.children 里已无流式块 →
# _streamFloors 恒为空 → 思考/完成工具块补齐的 data-order 缺少"排在其前的
# 流式工具数"修正 → restore 按保存的 data-order 插回时与思考块尺度不一致 →
# 找不到比它大的节点 → appendChild 沉底 → 折叠框内"所有思考在前、所有工具在后"
# （简洁模式坞态归位瞬间，即 finish_streaming 触发非流式渲染的那一刻）。
# 修复：save 阶段把流式块（data-streaming="true"）的 data-order 暂存到
# window.__pendingStreamFloors；reorganizeContent 的 _streamFloors 初始化时
# 合并该数组（与 DOM 收集并集），恢复"排在其前的流式工具数"修正。
# ──────────────────────────────────────────────


def test_save_and_restore_stashes_streaming_orders():
    """AST：_build_save_and_restore_js 的 save 阶段必须暂存流式块 data-order。

    不暂存则 reorganizeContent 的 _streamFloors 收集不到被 save 移除的流式块，
    补 data-order 缺修正 → restore 沉底（坞态归位瞬间"思考在前、工具在后"）。
    """
    src = _extract_src()
    import ast as _ast

    tree = _ast.parse(src)
    target = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "_build_save_and_restore_js":
            target = node
            break
    assert target is not None, "未找到 _build_save_and_restore_js 方法"
    body = textwrap.dedent(ast.unparse(target))

    # IIFE 开头必须重置暂存数组（防止跨渲染残留）
    assert "window.__pendingStreamFloors=[]" in body, "save/restore IIFE 开头必须重置 __pendingStreamFloors"
    # save 循环内：仅流式块（data-streaming="true"）暂存其 data-order（floor）
    assert "getAttribute('data-streaming')==='true'" in body, "save 必须识别流式块"
    assert "window.__pendingStreamFloors.push(Math.floor(_pfo))" in body, (
        "save 必须把流式块 data-order 暂存进 __pendingStreamFloors"
    )
    assert "parseFloat(el.getAttribute('data-order'))" in body, "暂存前必须解析流式块 data-order"


def test_reorganize_content_merges_pending_stream_floors():
    """AST：reorganizeContent 的 _streamFloors 必须合并 window.__pendingStreamFloors。

    只有 DOM 收集会在 save 移除流式块后失效（恒为空），合并暂存数组才能
    在坞态归位瞬间恢复"排在其前的流式工具数"修正，统一 sort 与 restore
    插入的 data-order 尺度。
    """
    src = _extract_src()
    assert "(window.__pendingStreamFloors || []).slice()" in src, (
        "_streamFloors 必须初始化为 window.__pendingStreamFloors 副本（再合并 DOM 收集）"
    )
    # 暂存合并必须位于 DOM 收集之前（_streamFloors 声明处）
    stash_pos = src.find("window.__pendingStreamFloors || []")
    dom_collect_pos = src.find("_allKids[_sf].getAttribute('data-streaming')")
    assert stash_pos != -1 and dom_collect_pos != -1 and stash_pos < dom_collect_pos, (
        "暂存数组合并必须先于 DOM 流式块收集"
    )


def test_skeleton_cache_version_bumped_for_plan_e():
    """AST：方案 E 改动了骨架 JS（reorganizeContent 的 _streamFloors 初始化），必须递增版本。"""
    src = _extract_src()
    m = re.search(r"_SKELETON_CACHE_VERSION = (\d+)", src)
    assert m and int(m.group(1)) >= 5, f"方案 E 改动骨架 JS 后必须 >= 5，实际 {m.group(1) if m else 'None'}"


# ──────────────────────────────────────────────
# 方案 F（数据层稳定锚点）+ 方案 G（save/restore 后强制 sort）回归
# 根因：1) append_tool_result 用 int 索引锚点（调用时列表长度），其他工具结果插入/
#        思考/正文追加后索引偏移 → _content_data 顺序错 → finish 完整重渲染（清缓存
#        强制 markdown 重建）时思考/工具错乱。修复：块引用锚点 _tool_anchor_refs +
#        _tool_anchor_pos（index(ref)+1，抗偏移）；append_text 原地追加保引用稳定。
#      2) save/restore 后 tool-content 键序列与 __lastOrder 相同 → _orderChanged diff
#        误判跳过 sort，但物理顺序已被 restore/迁移打乱。修复：补齐过 data-order
#        （_assignedDataOrder）即强制 sort。
# ──────────────────────────────────────────────


def test_append_text_preserves_block_identity():
    """行为：append_text 必须原地追加文本块（不重建列表），保住工具锚点引用。

    append_text_block 在末尾非 text 块时走 ensure_content_blocks 重建整个列表，
    所有块 dict 对象被替换 → _tool_anchor_refs 引用失效 → 稳定锚点退化。
    """
    card = _make_card()
    card.start_new_thinking_block()
    card.append_reasoning("思考一")
    card.update_tool_streaming("tool_1", "search", {"q": "x"})
    # 末尾是 reasoning（非 text）→ append_text 不得重建列表
    card.append_text("正文一")
    # 工具锚点引用必须仍然存活于列表中（append_text_block 重建列表会失效）
    ref = card._tool_anchor_refs.get("tool_1")
    assert ref is not None and any(b is ref for b in card._content_data), (
        "append_text 后 _tool_anchor_refs 引用必须仍指向列表中的块（原地追加）"
    )


def test_tool_result_inserts_at_stable_anchor():
    """行为：核心用户场景——思考1→工具1→正文1→思考2→工具2→正文2，
    工具结果到达后数据层必须交错正确（思考在前工具在后的数据层根因回归）。
    """
    card = _make_card()
    card.start_new_thinking_block()
    card.append_reasoning("思考一")
    card.update_tool_streaming("tool_1", "search", {"q": "x"})
    card.append_text("正文一")
    card.start_new_thinking_block()
    card.append_reasoning("思考二")
    card.update_tool_streaming("tool_2", "read", {"path": "f"})
    card.append_text("正文二")

    card.append_tool_result(tool_name="search", result="r1", tool_call_id="tool_1")
    card.append_tool_result(tool_name="read", result="r2", tool_call_id="tool_2")

    # 修复前：['reasoning','tool_result','text','tool_result','reasoning','text']
    # （tool_2 结果插到思考二前 → finish 完整重渲染时"思考在前、工具在后"）
    assert _types(card) == [
        "reasoning", "tool_result", "text", "reasoning", "tool_result", "text",
    ], f"工具结果必须按调用位置交错，实际 {_types(card)}"
    assert _tool_ids(card) == ["tool_1", "tool_2"]


def test_tool_anchor_pos_uses_ref_not_index():
    """行为：_tool_anchor_pos 必须返回块引用定位（index(ref)+1），而非失效的 int 索引。"""
    card = _make_card()
    card.start_new_thinking_block()
    card.append_reasoning("思考一")
    card.update_tool_streaming("tool_1", "search", {"q": "x"})
    card.append_text("正文一")
    card.start_new_thinking_block()
    card.append_reasoning("思考二")
    card.update_tool_streaming("tool_2", "read", {"path": "f"})
    card.append_text("正文二")
    # tool_1 结果先插入 → 列表偏移
    card.append_tool_result(tool_name="search", result="r1", tool_call_id="tool_1")
    # tool_2 锚点 = 思考二块之后（引用定位），int 索引（3）此时指向思考二本身
    assert card._tool_anchor_pos("tool_2") == 4, (
        f"tool_2 稳定锚点应为 4（思考二块之后），实际 {card._tool_anchor_pos('tool_2')}"
    )


def test_reorganize_content_force_sort_after_data_order_assign():
    """AST：补齐过 data-order（_assignedDataOrder）必须强制 sort。

    save/restore 后键序列与 __lastOrder 相同 → diff 误判跳过 sort，但物理顺序
    已被 restore/迁移打乱 → 折叠框内"思考在前、工具在后"（方案 G）。
    """
    src = _extract_src()
    assert "_assignedDataOrder = false" in src, "必须声明 _assignedDataOrder 标志"
    assert "_assignedDataOrder = true" in src, "补齐 data-order 时必须置位标志"
    assert "_orderChanged = _assignedDataOrder ||" in src, (
        "_orderChanged 必须因 _assignedDataOrder 强制为 true（跳过键序列 diff 误判）"
    )


def test_skeleton_cache_version_bumped_for_plan_fg():
    """AST：方案 F/G 改动了骨架 JS（reorganizeContent），必须递增版本。"""
    src = _extract_src()
    m = re.search(r"_SKELETON_CACHE_VERSION = (\d+)", src)
    assert m and int(m.group(1)) >= 6, f"方案 F/G 改动骨架 JS 后必须 >= 6，实际 {m.group(1) if m else 'None'}"
