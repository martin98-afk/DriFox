# -*- coding: utf-8 -*-
"""[E6] 游标化批实施回归测试：流式主链 O(n²) 消除后的行为等价锁定。

覆盖四块，全部围绕「逐字节等价」这一硬门槛：
1. 项5 _stream_text_snapshot 游标推进：parts 不清空 + solid 指针，
   无新段拍 O(1) 直返（acc 对象身份不变），有新段只拼新增。
2. 项1 append_text 块级 "_pend" + _solidify_text_blocks 固化：随机切分
   （含 1 字符 chunk / emoji 中点劈开 / <think> 劈开 / fence 行劈开）逐字节
   对拍全文直拼；固化幂等。
3. 协议状态机 vs 全文检测函数对拍（fuzz 200 轮）：think/tag/tool 逐拍严格
   等价；fence 在「text_so_far 以换行收尾」拍严格等价（不完整尾行由
   fence_carry 延后一拍判定，属设计内语义，终态补换行后对拍）。
4. 项2 reasoning 活动块 key 去内容化：_bk_sig 活动分支无内容本体
   （同长度不同内容 sig/key 相等），fingerprint 入参锁定（无全文 sha1），
   固化定型后自动切回全文指纹。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app.widgets.message_card as mc  # noqa: E402
import app.widgets.render.block_render as br  # noqa: E402
from app.widgets.card_render_core import (  # noqa: E402
    _has_unclosed_chart_fence,
    _has_unclosed_registered_tag,
    _has_unclosed_think,
)


def _make_card():
    """assistant 卡 + 流式计时启动（_stream_text_parts 在此初始化）。"""
    card = mc.MessageCard(role="assistant")
    card.start_elapsed_tracking()
    return card


def _split_chunks(text: str, rng: random.Random) -> list:
    """随机切分：含 1 字符 chunk、多字节字符（emoji/中文）中点劈开。"""
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        step = rng.choice([1, 1, 2, 3, 5, 11, 37])
        chunks.append(text[i : i + step])
        i += step
    return chunks


# ── 项5：_stream_text_snapshot 游标推进 ──────────────────────────


def test_snapshot_cursor_byte_equivalence(qapp):
    """随机切分逐批采样，snapshot 累计串与全文 join 逐字节一致。"""
    rng = random.Random(20261119)
    text = "hello <think>思考</think> 🎉 emoji劈开 中文段落\n\n```echarts\n{}\n```\n" * 7
    card = _make_card()
    expect = ""
    for chunk in _split_chunks(text, rng):
        card.update_content(chunk)
        expect += chunk
        assert card._stream_text_snapshot() == expect
    assert card._stream_text_snapshot() == text


def test_snapshot_no_new_parts_returns_cached_acc(qapp):
    """无新段拍：solid 不动、acc 对象身份不变（O(1) 直返，无重拼拷贝）。"""
    card = _make_card()
    card.update_content("AAAA")
    acc1 = card._stream_text_snapshot()
    solid1 = card._stream_text_parts_solid
    # 无新 chunk 的采样拍：直返缓存 acc（同一对象），游标不动
    acc2 = card._stream_text_snapshot()
    assert acc2 is acc1
    assert card._stream_text_parts_solid == solid1 == 1


def test_snapshot_clear_resets_solid(qapp):
    """_stream_text_clear 后 solid 同步复位，新流从头累计。"""
    card = _make_card()
    card.update_content("AAAA")
    card._stream_text_snapshot()
    assert card._stream_text_parts_solid == 1
    card._stream_text_clear()
    assert card._stream_text_parts_solid == 0
    card.update_content("XY")
    assert card._stream_text_snapshot() == "XY"


def test_snapshot_multiple_readers_idempotent(qapp):
    """多读取方连续读（footer 拍 + live_watch + finish）：只固化一次。"""
    card = _make_card()
    card.update_content("AA")
    a = card._stream_text_snapshot()
    b = card._stream_text_snapshot()
    card.update_content("BB")
    c1 = card._stream_text_snapshot()
    c2 = card._stream_text_snapshot()
    assert a == b == "AA"
    assert c1 is c2 == "AABB"
    assert card._stream_text_parts_solid == 2


# ── 项1：append_text "_pend" + 固化等价 ──────────────────────────


def test_append_text_solidify_byte_equivalence(qapp):
    """随机切分（含劈开的协议标记/fence 行）喂 append_text，途中固化，
    块 text 与全文直拼逐字节一致。"""
    rng = random.Random(42)
    text = (
        "段落一，中文。 <think>hidden</think> tail\n\n"
        '```echarts\n{"a":1}\n```\n\n'
        'plain 🎉🚀 text <tool>{"x":1}</tool> end'
    )
    card = _make_card()
    expect = ""
    for chunk in _split_chunks(text, rng):
        card.append_text(chunk)
        expect += chunk
        # 途中任时点固化（读权威方挂点），固化后再 append 继续积累
        card._solidify_text_blocks()
        blk = card._content_data[-1]
        assert blk["type"] == "text"
        assert not blk.get("_pend")
        assert blk["text"] == expect
    # 终态：不再追加时 append_text 走新块分支（末块已是 text → 原地 _pend）
    card.append_text("!")
    card._solidify_text_blocks()
    assert card._content_data[-1]["text"] == expect + "!"


def test_solidify_idempotent(qapp):
    """固化幂等可重入：连调多次内容不变、_pend 不复活。"""
    card = _make_card()
    card.append_text("AB")
    card.append_text("CD")
    card._solidify_text_blocks()
    text1 = card._content_data[-1]["text"]
    card._solidify_text_blocks()
    card._solidify_text_blocks()
    assert card._content_data[-1]["text"] == text1 == "ABCD"
    assert not card._content_data[-1].get("_pend")


def test_get_plain_text_solidifies_pending(qapp):
    """读权威方挂点：get_plain_text 先固化（导出/复制口径 = 渲染口径）。"""
    card = _make_card()
    card.append_text("hello ")
    card.append_text("world")
    assert card.get_plain_text() == "hello world"
    blk = card._content_data[-1]
    assert not blk.get("_pend")
    assert blk["text"] == "hello world"


def test_build_incremental_md_solidifies_pending(qapp):
    """读权威方挂点：_build_incremental_md（_lazy_markdown_cb）先固化。"""
    card = _make_card()
    card.append_text("part1 ")
    card.append_text("part2")
    md = card._build_incremental_md()
    assert "part1 part2" in md
    blk = card._content_data[-1]
    assert not blk.get("_pend")
    assert blk["text"] == "part1 part2"


def test_finish_solidifies_and_resets_proto_state(qapp):
    """落定清账：finish 后块无 _pend、协议状态机复位（数据层持久化干净）。"""
    card = _make_card()
    card.append_text("pre <think>abc</think> post")
    card.finish_streaming()
    blk = card._content_data[-1]
    assert not blk.get("_pend")
    assert blk["text"] == "pre <think>abc</think> post"
    assert card._proto_state is None


# ── 协议状态机 vs 全文检测函数对拍 ────────────────────────────────

_FUZZ_ATOMS = [
    "plain text ",
    "中文段落，标点。",
    "<think>",
    "</think>",
    "<tool>",
    "</tool>",
    "<mood>",
    "</mood>",
    "<div class='x'>",  # 非协议 tag：状态机与全文函数都应忽略
    "```echarts\n",
    "```python\n",
    "~~~mermaid\n",
    "body line\n",
    "```\n",
    "~~~\n",
    "<th",
    "ink>",
    "`",
    "``",
    "🎉",
]


def _fuzz_text(rng: random.Random) -> str:
    return "".join(rng.choice(_FUZZ_ATOMS) for _ in range(rng.randint(3, 30)))


def test_proto_state_matches_fulltext_fuzz(qapp):
    """fuzz 200 轮：状态机四元组与全文检测函数对拍。

    think/tag/tool：逐拍严格等价（无行边界概念）。
    fence：text_so_far 以 \\n 收尾的拍严格等价（尾行完整 → fence_carry 空）；
    其余拍状态机可能延后一拍（设计内语义，终态补 \\n 后对拍兜底）。
    """
    for seed in (7, 101, 999, 31415, 2026):
        rng = random.Random(seed)
        for round_no in range(40):
            text = _fuzz_text(rng)
            card = _make_card()
            card._proto_state_reset()
            st = card._proto_state
            so_far = ""
            for chunk in _split_chunks(text, rng):
                so_far += chunk
                got = card._proto_scan_chunk(st, chunk)
                want_think = _has_unclosed_think(so_far)
                want_tag = _has_unclosed_registered_tag(so_far)
                assert got[0] == want_think, f"round{round_no} think mismatch at {so_far!r}"
                assert got[1] == want_tag, f"round{round_no} tag mismatch at {so_far!r}"
                # tool：全文口径 count("<tool>") > count("</tool>")
                want_tool = so_far.count("<tool>") > so_far.count("</tool>")
                assert got[2] == want_tool, f"round{round_no} tool mismatch at {so_far!r}"
                # fence：仅尾行完整（以 \n 收尾）时对拍
                if so_far.endswith("\n"):
                    want_fence = _has_unclosed_chart_fence(so_far)
                    assert got[3] == want_fence, f"round{round_no} fence mismatch at {so_far!r}"
            # 终态：补换行终结不完整尾行后，fence 读值必须与全文一致
            want_final = _has_unclosed_chart_fence(text + "\n")
            got_final = card._proto_scan_chunk(st, "\n")
            assert got_final[3] == want_final, f"round{round_no} fence final mismatch"


def test_proto_state_chunk_split_protocol_marks(qapp):
    """<think> 被 chunk 劈开（<thi + nk>）由尾部 carry 兜住，不漏判不重判。"""
    card = _make_card()
    card._proto_state_reset()
    st = card._proto_state
    got = card._proto_scan_chunk(st, "abc <thi")
    # tag 宽松拦：`<thi` 尾巴与全文版 _PARTIAL_TAG_TAIL_RE 同语义 → True
    assert got == (False, True, False, False)
    got = card._proto_scan_chunk(st, "nk>hidden")
    assert got[0] is True  # think 未闭合
    got = card._proto_scan_chunk(st, "body </thi")
    assert got[0] is True
    got = card._proto_scan_chunk(st, "nk> tail")
    assert got == (False, False, False, False)  # 闭合，carry 清空
    assert st["carry"] == ""


def test_proto_state_reset_between_streams(qapp):
    """流重置：_proto_state None → 首批懒建从零开始（新流不继承旧状态）。"""
    card = _make_card()
    card.append_text("<think>leak")
    assert card._proto_state is not None
    assert card._proto_state["think_open"] is True
    card._proto_state = None  # 模拟流重置（finish/cleanup 同款）
    card.append_text("fresh")
    assert card._proto_state["think_open"] is False


# ── 项2：reasoning 活动块 key 去内容化 ────────────────────────────


def test_bk_sig_reasoning_active_has_no_content_body(qapp):
    """活动块 sig 去内容本体：同长度不同内容的 reasoning 块 sig 相等。"""
    from app.widgets.render.blocks import RenderCtx

    ctx = RenderCtx(streaming=True)
    active_a = {"type": "reasoning", "content": "AAAA", "_pend": ["X"]}
    active_b = {"type": "reasoning", "content": "BBBB", "_pend": ["Y"]}
    sig_a = br._bk_sig(active_a, ctx)
    sig_b = br._bk_sig(active_b, ctx)
    assert sig_a == sig_b  # 无内容本体（仅 len + compact）
    # 内容增长 → len 变 → sig 失据
    active_a["content"] = "AAAAA"
    assert br._bk_sig(active_a, ctx) != sig_a


def test_block_key_active_reasoning_skips_full_sha1(qapp, monkeypatch):
    """活动块 fingerprint 入参锁定：无全文本体（零 sha1），定型后切回全文指纹。"""
    from app.widgets.render.blocks import RenderCtx

    calls = []
    real_fp = br.fingerprint

    def spy_fp(*parts):
        calls.append(parts)
        return real_fp(*parts)

    monkeypatch.setattr(br, "fingerprint", spy_fp)
    ctx = RenderCtx(streaming=True)
    blk = {"type": "reasoning", "content": "seed", "_pend": ["more"]}
    br.block_key(blk, ctx, completed=False)
    assert calls, "活动块应重算 key（首次无驻留）"
    assert calls[-1] == ("reasoning", 4, False, False)  # len 替代全文，无 sha1 输入
    # 定型（固化 pop _pend）→ 切回全文指纹
    blk.pop("_pend")
    calls.clear()
    br.block_key(blk, ctx, completed=True)
    assert calls[-1] == ("reasoning", "seed", True, False)


def test_block_key_active_length_only_no_sha1_growth(qapp):
    """同长度不同内容的活动块 key 相等（指纹不含内容 → 每批零 sha1 重算）。"""
    from app.widgets.render.blocks import RenderCtx

    ctx = RenderCtx(streaming=True)
    a = {"type": "reasoning", "content": "x" * 100, "_pend": ["a"]}
    b = {"type": "reasoning", "content": "y" * 100, "_pend": ["b"]}
    assert br.block_key(a, ctx, False) == br.block_key(b, ctx, False)


# ── 项2：append_reasoning _pend 固化等价 ─────────────────────────


def test_append_reasoning_solidify_equivalence(qapp):
    """逐 chunk append_reasoning 途中固化，content 与直拼逐字节一致。"""
    card = _make_card()
    expect = ""
    for chunk in ["思考A", "继续", "🎉"]:
        card.append_reasoning(chunk)
        expect += chunk
        card._solidify_text_blocks()
        blk = card._content_data[-1]
        assert blk["type"] == "reasoning"
        assert not blk.get("_pend")
        assert blk["content"] == expect
    card.append_reasoning("尾段")
    card._solidify_text_blocks()
    assert card._content_data[-1]["content"] == expect + "尾段"


def test_reasoning_active_key_switches_after_solidify(qapp):
    """端到端：流式活动块 key 随 len 推进，固化后 key 换全文指纹（diff 替换落地）。"""
    from app.widgets.render.blocks import RenderCtx

    ctx = RenderCtx(streaming=True)
    card = _make_card()
    card.append_reasoning("思考")
    blk = card._content_data[-1]
    key_active = br.block_key(dict(blk), ctx, False)
    card._solidify_text_blocks()
    key_final = br.block_key(blk, ctx, True)
    assert key_active != key_final  # 定型切换：live 骨架 → 完整思考卡


def test_mixed_text_reasoning_interleave(qapp):
    """text/reasoning 交错流式：两类块各自 _pend 独立固化，互不串块。"""
    card = _make_card()
    card.append_reasoning("R1")
    card.append_text("T1")
    card.append_reasoning("R2", force_new_block=True)
    card.append_text("T2")
    card._solidify_text_blocks()
    kinds = [(b["type"], b.get("text") or b.get("content")) for b in card._content_data]
    assert kinds == [("reasoning", "R1"), ("text", "T1"), ("reasoning", "R2"), ("text", "T2")]
    assert all(not b.get("_pend") for b in card._content_data)
