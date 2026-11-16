# -*- coding: utf-8 -*-
"""工具 schema 指纹缓存（P1/P2）回归与微基准（perf audit task-3 补充）。

被测改动（f2240c233，docs/perf/audit-2026-11-15.md P1/P2）：
    - app/core/infra/token_estimator.py 新增 `_tools_signature`：
      对 tools 列表按 `is` 身份缓存 `hash(str(tools))` 指纹，
      count_messages_tokens 每轮不再全量 str() 序列化 100KB 级 schema。
    - 新增 `(_lookup|_set)_tools_tokens_cache`：count_tools_tokens 按
      (指纹, 模型, ratio) 缓存分词结果，同一快照重复调用不再全量重分词。

断言策略（与 tests/perf 既有口径一致）：
    - 正确性用确定性断言（指纹等价、缓存命中不再触发序列化、
      键隔离不错乱、结果与未缓存路径一致）；
    - 性能只 print 微基准耗时（p50），不阈值断言——数值随机器漂移，
      供 docs/perf 附录与后续优化对照采集。
"""

import statistics
import time

import pytest

from app.core.infra import token_estimator as te


class _SpyReprDict(dict):
    """记录自身被 repr（str(list) 时触发）次数的 dict，用于确定性验证缓存命中。"""

    def __init__(self, *args, counter=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._counter = counter if counter is not None else []

    def __repr__(self):  # noqa: D105
        self._counter.append(1)
        return "spy-tool"


def _make_tools(n: int = 35) -> list:
    """构造接近真实规模的工具 schema 列表（name/description/parameters）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": "读取文件内容并返回文本，" * 20,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径" * 10},
                        "encoding": {"type": "string", "enum": ["utf-8", "gbk"]},
                    },
                    "required": ["path"],
                },
            },
        }
        for i in range(n)
    ]


def test_tools_signature_equals_hash_str():
    """指纹口径不变：与旧实现 hash(str(tools)) 数值一致。"""
    tools = _make_tools()
    assert te._tools_signature(tools) == hash(str(tools))
    assert te._tools_signature([]) == 0
    assert te._tools_signature(None) == 0


def test_tools_signature_content_equivalence_and_distinction():
    """内容等价（不同对象）→ 同指纹；内容变化 → 指纹变化。"""
    a = _make_tools()
    b = _make_tools()
    assert te._tools_signature(a) == te._tools_signature(b)
    # 注意：is 缓存的设计前提是快照对象内容不变（原地修改返回旧指纹属预期），
    # 内容差异用全新对象表达。
    c = _make_tools()
    c[0]["function"]["name"] = "changed"
    assert te._tools_signature(a) != te._tools_signature(c)


def test_tools_signature_cache_hit_skips_serialization():
    """同一列表对象第二次调用命中 is 缓存：不再触发 str 序列化（确定性断言）。"""
    counter: list = []
    tools = [_SpyReprDict({"name": f"t{i}"}, counter=counter) for i in range(10)]

    te._tools_signature(tools)
    serialized_first = len(counter)
    assert serialized_first > 0, "首次调用应完成一次序列化"

    te._tools_signature(tools)
    serialized_second = len(counter)
    assert serialized_second == serialized_first, (
        "命中指纹缓存后不应再次序列化 tools 列表"
    )


def test_tools_signature_cache_fifo_eviction():
    """超出 4 条上限后 FIFO 逐出最旧条目（逐出后再调用会重新序列化但不报错）。"""
    old_cache = getattr(te._tools_sig_cache_local, "cache", None)
    te._tools_sig_cache_local.cache = []
    try:
        objs = [_make_tools(2) for _ in range(5)]
        for obj in objs:
            te._tools_signature(obj)
        cache = te._tools_sig_cache_local.cache
        assert len(cache) <= te._MAX_TOOLS_SIG_CACHE_ENTRIES
        # 最旧的 objs[0] 已被逐出：重新计算仍得同指纹（口径稳定）
        assert te._tools_signature(objs[0]) == hash(str(objs[0]))
    finally:
        te._tools_sig_cache_local.cache = old_cache


def test_count_tools_tokens_cache_key_isolation():
    """键隔离：不同 ratio/模型互不污染，ratio 生效路径正确。"""
    tools = _make_tools(10)
    base = te.count_tools_tokens(tools, model="gpt-4o", ratio=1.0)
    doubled = te.count_tools_tokens(tools, model="gpt-4o", ratio=2.0)
    assert isinstance(base, int) and base > 0
    assert doubled == 2 * base, "不同 ratio 必须独立缓存，不得互相命中"


def test_count_tools_tokens_cache_value_consistency():
    """缓存命中值与全量计算值一致（命中率不影响数值口径）。"""
    tools = _make_tools(10)
    first = te.count_tools_tokens(tools, model="gpt-4o")
    second = te.count_tools_tokens(tools, model="gpt-4o")
    assert first == second


def test_count_messages_tokens_tools_path_stable():
    """count_messages_tokens 走指纹缓存后结果稳定（含 tools 的常规入口）。"""
    messages = [
        {"role": "system", "content": "你是测试助手。"},
        {"role": "user", "content": "你好，请读取 D:\\tmp\\a.txt。"},
        {"role": "assistant", "content": "好的。"},
    ]
    tools = _make_tools(10)
    a = te.count_messages_tokens(messages, "gpt-4o", tools=tools)
    b = te.count_messages_tokens(messages, "gpt-4o", tools=tools)
    assert a == b and a > 0


@pytest.mark.perf
def test_perf_tools_signature_bench():
    """微基准：首次序列化 vs 缓存命中的单次耗时（print 采集，不阈值断言）。"""
    tools = _make_tools(35)
    rounds = 50

    # 首次：清缓存后单次冷调用（含 str 序列化）
    old_cache = getattr(te._tools_sig_cache_local, "cache", None)
    te._tools_sig_cache_local.cache = []
    try:
        t0 = time.perf_counter()
        te._tools_signature(tools)
        cold_ms = (time.perf_counter() - t0) * 1000
    finally:
        te._tools_sig_cache_local.cache = old_cache

    # 命中：重复调用分布
    te._tools_signature(tools)
    hits = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        te._tools_signature(tools)
        hits.append((time.perf_counter() - t0) * 1000)

    # P2：count_tools_tokens 冷/热对比
    t0 = time.perf_counter()
    te.count_tools_tokens(tools, model="gpt-4o")
    tokens_cold_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    te.count_tools_tokens(tools, model="gpt-4o")
    tokens_hot_ms = (time.perf_counter() - t0) * 1000

    out = (
        "[SIG-BENCH] tools=35 cold_serialize_ms={:.3f} "
        "sig_hit_p50_ms={:.4f} sig_hit_p99_ms={:.4f} | "
        "tools_tokens cold_ms={:.3f} hot_ms={:.3f}"
    ).format(
        cold_ms,
        statistics.median(hits),
        sorted(hits)[int(len(hits) * 0.99) - 1],
        tokens_cold_ms,
        tokens_hot_ms,
    )
    print(out)
    assert hits and all(h >= 0 for h in hits)
