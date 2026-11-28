"""上下文处理结束后仍由缓存持有的 Python 堆，显式运行可查看 MiB 数值。"""

import gc
import json
import random
import tracemalloc
from importlib import import_module

import psutil
import pytest

pytestmark = pytest.mark.perf
MIB = 1024 * 1024


@pytest.mark.parametrize("tier", ["normalize", "shape"])
def test_tool_text_cache_retained_heap(tier):
    module = import_module(f"plugins.system-context.context_tiers.tool_{tier}")
    if tier == "normalize":
        transform, memo = module.normalize_text, module._norm_memo
    else:
        transform, memo = module.shape_json_text, module._shape_memo

    memo.clear()
    tracemalloc.start()
    try:
        for i in range(128):
            text = '{"value":"' + str(i) + "x" * (128 * 1024) + '"}   \n'
            result = transform(text)
            assert str(i) in result and result != text
        del text, result
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0]
        print(f"{tier} retained {retained / MIB:.3f} MiB")
        assert retained < 5 * MIB
    finally:
        tracemalloc.stop()
        memo.clear()


def test_estimator_after_session_cleanup(qapp):
    from app.core.infra import memory_governor, token_estimator

    memory_governor._cleanup_global_lru_caches()
    tracemalloc.start()
    try:
        for i in range(4):
            messages = [{"role": "tool", "content": str(i) + "x" * (2 * MIB)}]
            assert token_estimator.count_messages_tokens(messages) > 0
        del messages
        memory_governor._cleanup_global_lru_caches()
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0]
        print(f"session cleanup retained {retained / MIB:.3f} MiB")
        assert retained < MIB
    finally:
        tracemalloc.stop()


def test_saved_extras_cache_retained_heap(tmp_path, monkeypatch):
    from app.core.store.serde import serialize
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    monkeypatch.setattr(SessionStore, "_instance", None)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    store = SessionStore(str(tmp_path))
    repo = store._session_repo
    rng = random.Random(9)
    tracemalloc.start()
    try:
        for i in range(32):
            value = serialize(rng.randbytes(256 * 1024).hex())
            repo._write_extras(str(i), {0: {"diff": value}})
        del value
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0]
        print(f"saved extras retained {retained / MIB:.3f} MiB")
        assert retained < MIB
        rng.seed(9)
        for i in range(32):
            assert repo.load_extras_for_session(str(i)) == {0: {"diff": rng.randbytes(256 * 1024).hex()}}
    finally:
        tracemalloc.stop()
        store.close()


def test_tool_history_process_rss(tmp_path, monkeypatch):
    from app.core.context.pipeline import ContextPipeline
    from app.plugins.managers.plugin_config_store import PluginConfigStore
    from app.plugins.registries.context_policy_registry import ContextPolicyRegistry

    monkeypatch.setattr(PluginConfigStore, "_path", lambda self, name: tmp_path / name / "config.json")
    registry = ContextPolicyRegistry()
    for name in ("tool_normalize", "tool_offload", "tool_shape", "tool_prune"):
        import_module(f"plugins.system-context.context_tiers.{name}").register(registry)
    pipeline = ContextPipeline(registry)

    def ingest(i):
        text = '{"n":' + str(i) + ',"text":"' + "x" * 40960 + '"}   \n'
        msg = {"role": "tool", "name": "log", "tool_call_id": f"call_{i}", "content": text}
        (result,) = pipeline.ingest_tool_results([msg], {"context_limit": 128000}, "memory-replay")
        assert result["_context_ingested"] and len(result["content"]) < 500
        assert json.loads(result["content"])["n"] == i
        return result

    ingest(-1)
    gc.collect()
    proc = psutil.Process()
    before = proc.memory_info().rss
    history = [ingest(i) for i in range(512)]
    gc.collect()
    print(f"{len(history)} tool results, RSS growth {(proc.memory_info().rss - before) / MIB:.2f} MiB")
