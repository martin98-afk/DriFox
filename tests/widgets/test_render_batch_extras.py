# -*- coding: utf-8 -*-
"""渲染副本注入（message_extras）测试：materialize_batch_with_extras 纯逻辑。"""

from app.widgets.ui_helpers import materialize_batch_with_extras


def test_no_sentinel_returns_same_list():
    batch = [{"role": "assistant", "content": "hi"}]
    out = materialize_batch_with_extras(batch, "s1", lambda sid, idxs: {0: {"reasoning_content": "x"}})
    assert out is batch


def test_sentinel_messages_patched_via_copy():
    batch = [
        {"role": "assistant", "content": "a", "_x_idx": 0},
        {"role": "user", "content": "u"},
    ]
    calls = []

    def loader(sid, idxs):
        calls.append((sid, idxs))
        return {0: {"reasoning_content": "think"}}

    out = materialize_batch_with_extras(batch, "s1", loader)
    assert out[0] is not batch[0], "补回的消息应为浅拷贝"
    assert out[0]["reasoning_content"] == "think"
    assert out[1] is batch[1], "无需补回的消息原样返回"
    assert calls == [("s1", [0])]
    assert "reasoning_content" not in batch[0], "原 batch 不被污染"


def test_loader_miss_returns_original():
    batch = [{"role": "tool", "content": "r", "_x_idx": 7}]
    out = materialize_batch_with_extras(batch, "s1", lambda sid, idxs: {})
    assert out[0] is batch[0]


def test_loader_exception_safe():
    batch = [{"role": "assistant", "content": "a", "_x_idx": 3}]

    def boom(sid, idxs):
        raise RuntimeError("db down")

    out = materialize_batch_with_extras(batch, "s1", boom)
    assert out[0] is batch[0]


def test_not_callable_or_empty_session_id_noop():
    batch = [{"role": "assistant", "_x_idx": 1}]
    assert materialize_batch_with_extras(batch, "", lambda *a: {}) is batch
    assert materialize_batch_with_extras(batch, "s1", None) is batch
