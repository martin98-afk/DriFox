# -*- coding: utf-8 -*-
"""T9：TLS consolidate 投影释放（登记表 + 惰性剔除）守护测试

问题：_consolidate_cache_local 是 threading.local，ChatSession.release_messages /
messages.setter 释放/替换消息体后，worker 线程 TLS 里的旧列表投影仍被缓存 key
（id(list), …）钉住无法回收（单会话最大 26.9MB 滞留）。
修复：invalidate_consolidate_cache 登记失效列表，各线程下次 consolidate 入口
按 id 剔除自己的 TLS 条目。
"""
import gc
import tracemalloc

from app.core.conversation.message_content import (
    _get_consolidate_cache,
    _pending_invalidate,
    consolidate_messages,
    invalidate_consolidate_cache,
)

def _big_messages(n: int = 2000):
    return [{"role": "user", "content": "x" * 1024, "timestamp": f"t{i}"} for i in range(n)]

class TestInvalidateReleaseFreesTlsProjection:
    def test_invalidate_release_frees_tls_projection(self):
        """release 序列：登记失效 → del 原列表 → 新列表入口触发 purge → 消息体回收 ≥90%"""
        tracemalloc.start()
        msgs = _big_messages()
        consolidate_messages(msgs)
        stale_id = id(msgs)
        entries = _get_consolidate_cache()["_entries"]
        assert any(k[0] == stale_id for k in entries), "前置：TLS 已有该列表投影"
        baseline = tracemalloc.get_traced_memory()[0]
        invalidate_consolidate_cache(msgs)
        del msgs
        gc.collect()
        # content 字符串仍被 TLS 投影条目引用，此时几乎不下降（守护 purge 的必要性）
        pinned = tracemalloc.get_traced_memory()[0]
        assert pinned >= baseline * 0.5, (
            f"del 后投影应仍钉住主要内存（content 字符串；原 dict 骨架即刻回收）："
            f"baseline={baseline} pinned={pinned}"
        )
        # SessionManager 真实序列：新列表（新 id）进入 consolidate → 入口 purge 清旧 id
        fresh = [{"role": "user", "content": f"y{i}", "timestamp": f"n{i}"} for i in range(4)]
        consolidate_messages(fresh)
        entries = _get_consolidate_cache()["_entries"]
        assert not any(k[0] == stale_id for k in entries), "purge 后 TLS 不应残留已释放列表条目"
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        assert after <= baseline * 0.1, (
            f"消息体应随投影剔除回收 ≥90%：baseline={baseline} after={after}"
        )

    def test_invalidate_id_reuse_safe(self):
        """登记存在时新列表 consolidate 结果正确（同线程入口 purge 不误伤新条目）"""
        old = _big_messages(8)
        consolidate_messages(old)
        invalidate_consolidate_cache(old)
        fresh = [{"role": "user", "content": f"新{i}", "timestamp": f"n{i}"} for i in range(8)]
        out = consolidate_messages(fresh)
        assert len(out) == 8
        assert out[0]["content"] == "新0" and out[7]["content"] == "新7"
        assert all(m.get("role") == "user" for m in out)

    def test_pending_registry_capacity_capped(self):
        """登记表容量封顶，防无限增长（单条 16B，超限挤出极端场景退回旧滞留行为）"""
        from app.core.conversation.message_content import _PENDING_INVALIDATE_MAX

        _pending_invalidate.clear()
        for i in range(_PENDING_INVALIDATE_MAX + 10):
            invalidate_consolidate_cache([{"role": "user", "content": str(i)}])
        assert len(_pending_invalidate) <= _PENDING_INVALIDATE_MAX
        _pending_invalidate.clear()  # 清场，不影响其他用例
