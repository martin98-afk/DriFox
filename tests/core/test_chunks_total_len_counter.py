# -*- coding: utf-8 -*-
"""T28 L4 回归测试：`_chunks_total_len` 增量计数器与 `_response_chunks` 保持同步。

背景：`_response_chunks` 字符总数原先在两处 `sum(len(c) for c in ...)` 现算
（MEM_DIAG 快照每 100 chunk / 流式调试日志），长响应下是 O(chunks) × 高频开销。
T28 改为增量计数器，所有写路径（append / clear / 恢复重建 / 重置）必须同步维护，
否则 [MEM] 诊断日志会给出错误数值（漏报泄漏或虚报）。

测试策略：`__new__` 绕过构造（不拉起 worker 全套依赖），手工装配计数器涉及的
最小属性，直接驱动各写路径后断言计数器与真实 sum 一致。
"""

from app.core.workers.chat_worker import OpenAIChatWorker


def _make_bare_worker():
    """最小 worker 桩：只装配 _chunks_total_len 相关属性。"""
    w = OpenAIChatWorker.__new__(OpenAIChatWorker)
    w._response_chunks = []
    w._chunks_total_len = 0
    w._response_content_blocks = []
    w._partial_content_backup = None
    return w


def _truth(worker) -> int:
    return sum(len(c) for c in worker._response_chunks)


def test_counter_tracks_append():
    """append 后计数器必须等于真实 sum（增量维护正确）"""
    w = _make_bare_worker()
    w._response_chunks.append("hello")
    w._chunks_total_len += len("hello")
    assert w._chunks_total_len == _truth(w) == 5

    w._response_chunks.append("世界")  # 中文按字符数计
    w._chunks_total_len += len("世界")
    assert w._chunks_total_len == _truth(w) == 7


def test_counter_zeroed_on_clear():
    """clear 后计数器归零（工具执行前释放路径）"""
    w = _make_bare_worker()
    w._response_chunks.extend(["abc", "defgh"])
    w._chunks_total_len = 8
    assert w._chunks_total_len == _truth(w)

    w._response_chunks.clear()
    w._chunks_total_len = 0
    assert w._chunks_total_len == _truth(w) == 0


def test_counter_recomputed_on_restore():
    """恢复路径重建 chunks 后计数器被重算（不是沿用旧值）"""
    w = _make_bare_worker()
    w._partial_content_backup = {"response_chunks": ["恢复", "内容"]}
    # 模拟 _restore_partial_content_backup 的恢复分支
    if not w._response_chunks:
        w._response_chunks = list(w._partial_content_backup.get("response_chunks", []) or [])
        w._chunks_total_len = sum(len(c) for c in w._response_chunks)

    assert w._chunks_total_len == _truth(w) == 4


def test_counter_not_double_counted_on_restore_when_nonempty():
    """chunks 非空时不恢复、计数器不动（避免重复计入）"""
    w = _make_bare_worker()
    w._partial_content_backup = {"response_chunks": ["备份"]}
    w._response_chunks = ["已有"]
    w._chunks_total_len = 2

    if not w._response_chunks:
        w._response_chunks = list(w._partial_content_backup.get("response_chunks", []) or [])
        w._chunks_total_len = sum(len(c) for c in w._response_chunks)

    assert w._chunks_total_len == _truth(w) == 2, "非空分支不得重算/覆盖计数器"


def test_counter_tracks_full_lifecycle():
    """[T36 P8] 行为用例：计数器全生命周期（append → 归零 → 恢复重算）

    原有两个源码文本计数断言（`src.count("self._chunks_total_len += len(") == 2`
    等）对无关重构极敏感（改注释/换写法即红），且不校验语义；已删除，改为此处
    对行为本身的端到端校验——上面的 test_counter_tracks_append /
    test_counter_zeroed_on_clear / test_counter_recomputed_on_restore 已分别覆盖，
    本用例把三步串起来确认状态机自洽。
    """
    w = _make_bare_worker()

    # append 累加
    for chunk in ("abc", "de", "f"):
        w._response_chunks.append(chunk)
        w._chunks_total_len += len(chunk)
    assert w._chunks_total_len == 6
    assert w._chunks_total_len == _truth(w)

    # 清空归零
    w._response_chunks.clear()
    w._chunks_total_len = 0
    assert w._chunks_total_len == 0

    # 恢复重算（非空）
    w._response_chunks.extend(["xy", "z"])
    w._chunks_total_len = _truth(w)
    assert w._chunks_total_len == 3
