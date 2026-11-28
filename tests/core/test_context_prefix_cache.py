"""入库、会话重载和发送投影之间的缓存前缀契约。"""
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Barrier

import orjson
import pytest

from app.core.context.budget import check_request_budget
from app.core.context.pipeline import ContextPipeline
from app.core.conversation.message_content import consolidate_messages
from app.core.tools import tool_result_persister
from app.plugins.contracts.message_serializer import SerializeContext
from app.plugins.contracts.model_adapter import ProtocolFlags
from app.plugins.registries.context_policy_registry import ContextPolicyRegistry


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_result_persister, "get_app_data_dir", lambda: tmp_path)
    registry = ContextPolicyRegistry()
    for tier in ("tool_normalize", "tool_offload", "tool_shape", "tool_prune"):
        module = import_module(f"plugins.system-context.context_tiers.{tier}")
        module.register(registry)
    return ContextPipeline(registry)


def tool_message(content):
    return {"role": "tool", "tool_call_id": "call_1", "name": "log", "content": content}


def test_short_batch_offloads_in_the_correct_session(pipeline, tmp_path):
    barrier = Barrier(2)

    def ingest(sid):
        content = (sid + " log line\n") * 7000
        original = tool_message(content)
        barrier.wait()
        result = pipeline.ingest_tool_results([original], {"context_limit": 256000}, sid)[0]
        assert original["content"] == content
        folder = tmp_path / "projects" / sid / "tool-results"
        path, = folder.glob("*.txt")
        assert path.read_text(encoding="utf8") == content
        assert str(path) in result["content"]
        assert pipeline.ingest_tool_results([original], {}, sid)[0]["content"] == result["content"]
        assert list(folder.glob("*.txt")) == [path]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(ingest, ["parent", "subagent-child"]))


@pytest.mark.parametrize("responses", [False, True], ids=["chat", "responses"])
def test_appending_after_reload_preserves_wire_prefix(pipeline, monkeypatch, responses):
    shape = import_module("plugins.system-context.context_tiers.tool_shape")
    monkeypatch.setattr(shape, "cfg_int", lambda *args: 20000)
    serializer = import_module("plugins.system-serializers.serializers.openai").OpenAIChatSerializer()
    ctx = SerializeContext(flags=ProtocolFlags(use_responses_api=responses))
    config = {"context_limit": 128000}
    call = {"id": "call_1", "type": "function", "function": {"name": "log", "arguments": "{}"}}
    text = orjson.dumps({"message": "abc" * 1500}).decode()
    batch = pipeline.ingest_tool_results([tool_message(text)], config, "parent")
    first = [
        {"role": "system", "content": "固定指令"},
        {"role": "user", "content": "查看日志"},
        {"role": "assistant", "tool_calls": [call]},
        *batch,
    ]
    original = serializer.serialize(first, ctx)
    # 跨越原阈值、收紧 JSON 骨架化参数，同时模拟会话从磁盘载入。
    monkeypatch.setattr(shape, "cfg_int", lambda *args: 1000)
    history = list(consolidate_messages(orjson.loads(orjson.dumps(first))))
    history.extend([{"role": "assistant", "content": "完成"}, {"role": "user", "content": "继续" * 12000}])
    for project in (pipeline.project_for_ui, pipeline.project_for_send):
        projected = project(history, {"context_limit": 8000}).messages
        assert projected[3]["content"] == text
        replay = serializer.serialize(projected, ctx)
        prefix = original.input_items if responses else original.messages
        replay_items = replay.input_items if responses else replay.messages
        assert len(prefix) == (3 if responses else 4)
        assert len(replay_items) == len(prefix) + 2
        assert orjson.dumps(replay_items[:len(prefix)]) == orjson.dumps(prefix)
        assert replay.instructions == original.instructions
        assert b"_context_ingested" not in orjson.dumps(replay_items)


def test_budget_check_counts_tools_and_leaves_payload_intact(monkeypatch):
    from app.core.context import budget

    warnings = []
    monkeypatch.setattr(budget.logger, "warning", warnings.append)
    config = {"context_limit": 4096, "max_output_tokens": 2048}
    msgs = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "log", "description": "example " * 5000}}]
    original = orjson.dumps([msgs, tools])
    check_request_budget(msgs, [], config)
    assert not warnings
    check_request_budget(msgs, tools, config)
    assert len(warnings) == 1
    assert orjson.dumps([msgs, tools]) == original


@pytest.mark.parametrize("responses", [False, True], ids=["chat", "responses"])
def test_legacy_json_keeps_first_sent_representation(pipeline, responses):
    # 老版本在工具续轮直接序列化此段，重放时不能追补 JSON 骨架化。
    serializer = import_module("plugins.system-serializers.serializers.openai").OpenAIChatSerializer()
    ctx = SerializeContext(flags=ProtocolFlags(use_responses_api=responses))
    msg = tool_message(orjson.dumps({"result": "abcdefgh" * 1500}).decode())
    expected = serializer.serialize([msg], ctx)
    for tail in ("hi", "long " * 50000):
        projected = pipeline.project_for_send([msg, {"role": "user", "content": tail}], {})
        actual = serializer.serialize(projected.messages[:1], ctx)
        assert orjson.dumps(actual.messages) == orjson.dumps(expected.messages)
        assert orjson.dumps(actual.input_items) == orjson.dumps(expected.input_items)
