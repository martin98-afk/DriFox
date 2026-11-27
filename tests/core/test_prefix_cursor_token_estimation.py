# -*- coding: utf-8 -*-
"""B4 前缀游标增量估算回归（T4f/T4g 清单 4 用例）

## 背景
无 usage 供应商（MiniMax 类）每轮 API 返回后走 count_messages_tokens 全量
估算；旧 content_sig 缓存（len + id(last)）在每轮 extend 后必 MISS → 每轮
O(全部消息) 重分词。B4 改前缀游标缓存：记录已计前缀 id 元组与 raw 累计，
前缀一致时仅对新增段增量分词；缩短/前缀位变化/末条原地改写一律全量兜底。

## 红线
增量口径与全量 count_messages_tokens 必须同值等价——游标失同步会导致估算
偏差影响 auto_compact_trigger 判定，等价性用例是本项守门员。
"""

import time

import pytest

from app.core.infra import token_estimator as te


@pytest.fixture(autouse=True)
def _isolate_caches():
    """游标与 is 缓存均为 thread-local：用例间强隔离"""
    te._get_prefix_cursors().clear()
    te._get_token_cache().clear()
    yield
    te._get_prefix_cursors().clear()
    te._get_token_cache().clear()


def _base_msg(i: int) -> dict:
    return {"role": "user", "content": f"第 {i} 轮提问：请分析模块 {i} 的边界条件与并发语义"}


def _reply_msg(i: int) -> dict:
    return {"role": "assistant", "content": f"第 {i} 轮回答：边界包括输入域、并发窗口与超时语义。"}


def test_incremental_matches_full_over_50_rounds():
    """用例①：N=200 消息分 50 轮逐轮 extend，游标路径 == 全量重算（逐字节同值）"""
    msgs = [{"role": "system", "content": "你是测试助手"}]
    for i in range(50):
        msgs.append(_base_msg(i))
        msgs.append(_reply_msg(i))
        # 游标路径：同一列表对象连续 extend
        inc = te.count_messages_tokens(msgs, model="gpt-4o")
        # 全量参照：全新列表对象（is 缓存/content_sig 均不可能命中）
        ref = te.count_messages_tokens(list(msgs), model="gpt-4o")
        assert inc == ref, f"轮 {i}: 增量 {inc} != 全量 {ref}"
        # 带 tools 的变体同验（tools_sig 变化走独立游标条目）
        tools = [{"name": "bash", "description": "运行命令", "parameters": {"type": "object"}}]
        inc_t = te.count_messages_tokens(msgs, model="gpt-4o", tools=tools)
        ref_t = te.count_messages_tokens(list(msgs), model="gpt-4o", tools=tools)
        assert inc_t == ref_t, f"轮 {i}(tools): 增量 {inc_t} != 全量 {ref_t}"


def test_shrink_falls_back_to_full():
    """用例②：列表缩短兜底——中途 pop 后下一轮结果正确（触发全量重算）"""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(10):
        msgs.append(_base_msg(i))
        msgs.append(_reply_msg(i))
    te.count_messages_tokens(msgs, model="gpt-4o")
    # 缩短：pop 末尾 4 条
    del msgs[-4:]
    inc = te.count_messages_tokens(msgs, model="gpt-4o")
    ref = te.count_messages_tokens(list(msgs), model="gpt-4o")
    assert inc == ref, f"缩短后游标路径结果错误: {inc} != {ref}"
    # 缩短后继续 append：游标已在全量兜底时重建，增量继续等价
    msgs.append(_base_msg(99))
    inc2 = te.count_messages_tokens(msgs, model="gpt-4o")
    ref2 = te.count_messages_tokens(list(msgs), model="gpt-4o")
    assert inc2 == ref2


def test_last_message_rewrite_falls_back_to_full():
    """用例③：末条原地改写（id 不变内容变）触发全量重算，结果正确"""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(8):
        msgs.append(_base_msg(i))
        msgs.append(_reply_msg(i))
    te.count_messages_tokens(msgs, model="gpt-4o")
    before = te.count_messages_tokens(list(msgs), model="gpt-4o")
    # 流式场景同款：末条 assistant 就地改写（同对象、内容变长）
    last = msgs[-1]
    last_id = id(last)
    last["content"] = _reply_msg(7)["content"] + " 补充：另有一处竞态需处理。" * 5
    assert id(msgs[-1]) == last_id
    inc = te.count_messages_tokens(msgs, model="gpt-4o")
    ref = te.count_messages_tokens(list(msgs), model="gpt-4o")
    assert inc == ref, f"末条改写后结果错误: {inc} != {ref}"
    assert inc > before, "内容变长后 token 必须增长（改写确实被捕捉）"


def test_append_only_counts_old_tail_and_new_items(monkeypatch):
    msgs = [_base_msg(i) for i in range(100)]
    te.count_messages_tokens(msgs)
    old_tail = msgs[-1]
    added = [{'role': 'assistant', 'content': '长回复。' * 200}, {'role': 'tool', 'content': 'ok'}]
    msgs.extend(added)
    original = te._compute_message_tokens
    expected = sum(original(msg, 'gpt-4') for msg in msgs)
    visited = []

    def tracked(msg, model):
        visited.append(msg)
        return original(msg, model)

    monkeypatch.setattr(te, '_compute_message_tokens', tracked)
    assert te.count_messages_tokens(msgs) == expected
    assert visited == [old_tail, *added]


def test_rewrite_old_tail_then_append_equal_cost_message():
    msgs = [_base_msg(0), {'role': 'user', 'content': 'ok'}]
    te.count_messages_tokens(msgs)
    msgs[-1]['content'] = '补充内容' * 100
    msgs.append({'role': 'user', 'content': 'ok'})
    assert te.count_messages_tokens(msgs) == sum(te._compute_message_tokens(msg, 'gpt-4') for msg in msgs)


def test_replaced_prefix_with_unchanged_tail_recounts():
    msgs = [_base_msg(0), _base_msg(1)]
    te.count_messages_tokens(msgs)
    msgs[0] = {'role': 'tool', 'content': '新增工具结果' * 100}
    assert te.count_messages_tokens(msgs) == sum(te._compute_message_tokens(msg, 'gpt-4') for msg in msgs)


def test_evicted_cursor_does_not_reuse_wrong_ratio():
    msgs = [_base_msg(0)]
    raw = te.count_messages_tokens(msgs, ratio=1.0)
    te._get_prefix_cursors().clear()
    assert te.count_messages_tokens(msgs, ratio=2.0) == raw * 2


def test_incremental_faster_than_full_over_50_rounds():
    """用例④：50 轮增量累计耗时 << 50 轮全量累计耗时（>5x）"""
    n_rounds = 50
    # 增量路径：同一列表对象逐轮 extend
    msgs_inc = [{"role": "system", "content": "sys"}]
    rounds = []
    for i in range(n_rounds):
        rounds.append((_base_msg(i), _reply_msg(i)))
    t0 = time.perf_counter()
    for base, reply in rounds:
        msgs_inc.append(base)
        msgs_inc.append(reply)
        te.count_messages_tokens(msgs_inc, model="gpt-4o")
    inc_ms = (time.perf_counter() - t0) * 1000
    # 全量路径：每轮用全新列表对象（游标不命中，全量重算）
    t0 = time.perf_counter()
    for base, reply in rounds:
        msgs_full = [{"role": "system", "content": "sys"}]
        for b, r in rounds[: rounds.index((base, reply)) + 1]:
            msgs_full.append(b)
            msgs_full.append(r)
        te.count_messages_tokens(msgs_full, model="gpt-4o")
    full_ms = (time.perf_counter() - t0) * 1000
    ratio_speed = full_ms / max(inc_ms, 1e-6)
    assert ratio_speed > 5, f"加速比 {ratio_speed:.1f}x 未达 5x（inc={inc_ms:.1f}ms full={full_ms:.1f}ms）"
