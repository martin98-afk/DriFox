# -*- coding: utf-8 -*-
"""B1 subagent 增量 API 消息缓存回归（T4g 清单 4 用例）

## 背景
subagent 每轮 _make_api_call 对完整 current_messages 全量 revive+serialize+revoke
（O(全历史)）。B1 对齐 chat_worker 模式引入 _api_messages_cache：
- loop 基线一次性 set，写点（续命/assistant/pending/tool+hook/hook 注入）增量 append
- _make_api_call 命中直接用 cache，跳过每轮全量序列化
- _materialize_for_send P4 直通：标志明确无图时返回原引用零拷贝

## 纪律红线
cache 内容与 vision 标志严格同步，拿不准回退拷贝，绝不把 image_ref 发给 provider。
"""

import copy
from unittest.mock import MagicMock

from app.core.workers.subagent_worker import SubAgentExecutor


def _build_executor() -> SubAgentExecutor:
    """最小构造：不 start 线程，只测缓存方法与 _make_api_call 序列化路径"""
    return SubAgentExecutor(
        task_id="t_api_cache",
        agent_name="worker",
        task_description="test",
        llm_config={"API_KEY": "sk-test", "模型名称": "gpt-4o", "API_URL": "https://api.openai.com/v1"},
        agent_manager=None,
        tool_executor=None,
    )


def _long_history(n: int) -> list:
    """n 轮会话的内部格式消息（user/assistant 交替，含工具段形态）"""
    msgs = [{"role": "system", "content": "你是测试子代理"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"第 {i} 轮提问：请分析模块 {i} 的边界条件"})
        msgs.append({"role": "assistant", "content": f"第 {i} 轮回答：边界条件包括输入域、并发与超时。"})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": f"工具结果 {i}"})
    return msgs


def _ensure_plugins_loaded():
    """确保系统插件（serializer/adapter）已注册（幂等，与 materialize 测试同口径）"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()


def test_cache_incremental_matches_full_serialize():
    """用例①：写点增量 append 的 cache 与全量 serialize 产物逐字节一致（N=200 长会话）"""
    import orjson

    _ensure_plugins_loaded()
    ex = _build_executor()
    msgs = _long_history(200)
    # 模拟 loop 基线 set（写点 E5a）
    ex._set_api_messages_cache(ex._serialize_for_api(copy.deepcopy(msgs)).messages)
    # 模拟两批写点增量（续命提问 + tool/hook 段，E5b/E5e 同款）
    new_batch = [
        {"role": "user", "content": "强制续命：请总结以上分析"},
        {"role": "assistant", "content": "总结：共 200 轮边界分析，无遗漏。"},
        {"role": "tool", "tool_call_id": "call_final", "content": "收尾工具结果"},
        {"role": "user", "content": "[hook] PostToolUse 校验通过"},
    ]
    msgs.extend(new_batch)
    ex._append_to_api_cache(copy.deepcopy(new_batch))
    # 全量重算参照
    fresh = ex._serialize_for_api(copy.deepcopy(msgs)).messages
    cache = ex._api_messages_cache
    assert cache is not None
    assert len(cache) == len(fresh), f"cache 长度 {len(cache)} != 全量 {len(fresh)}"
    a = orjson.dumps(cache, option=orjson.OPT_SORT_KEYS)
    b = orjson.dumps(fresh, option=orjson.OPT_SORT_KEYS)
    assert a == b, "增量 cache 与全量 serialize 产物不一致"


def test_p4_passthrough_identity_when_no_vision():
    """用例②：无图任务 P4 直通零拷贝（返回原引用，不做三级浅拷贝）"""
    ex = _build_executor()
    cache = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "纯文本提问"},
        {"role": "assistant", "content": [{"type": "text", "text": "纯文本回答"}]},
    ]
    ex._api_messages_cache = cache
    ex._api_cache_has_vision_refs = False  # 标志明确无图
    out = ex._materialize_for_send(cache)
    assert out is cache, "vision=False 必须零拷贝直通（返回原引用）"


def test_vision_flag_true_or_none_never_passthrough():
    """用例③：含图任务不走直通（vision True 走拷贝；None 拿不准也走拷贝）"""
    ex = _build_executor()
    img_block = {"type": "image_ref", "image_ref": {"file": "x.png", "session_id": "sid", "meta": "image/png"}}
    cache_with_img = [
        {"role": "user", "content": [{"type": "text", "text": "看图"}, dict(img_block)]},
    ]
    cache_no_img = [{"role": "user", "content": "纯文本"}]
    # True：明确有图 → 拷贝
    ex._api_messages_cache = cache_with_img
    ex._api_cache_has_vision_refs = True
    out = ex._materialize_for_send(cache_with_img)
    assert out is not cache_with_img, "vision=True 不得直通（image_ref 会外泄给 provider）"
    assert out[0] is not cache_with_img[0], "必须做消息骨架拷贝（revive 就地改写会污染 cache）"
    # None：拿不准 → 保守拷贝（红线：直通只认「明确无图」）
    ex._api_cache_has_vision_refs = None
    out2 = ex._materialize_for_send(cache_no_img)
    assert out2 is not cache_no_img, "vision=None 拿不准必须回退拷贝（不得直通）"
    # 直通分支只认 False：恢复 False 后同一输入直通
    ex._api_cache_has_vision_refs = False
    out3 = ex._materialize_for_send(cache_no_img)
    assert out3 is cache_no_img


def test_extend_writepoint_keeps_cache_in_sync():
    """用例④：tool/hook 段 extend 写点后 cache 长度与内容同步（E5e 挂钩语义）"""
    _ensure_plugins_loaded()
    ex = _build_executor()
    msgs = _long_history(20)
    ex._set_api_messages_cache(ex._serialize_for_api(copy.deepcopy(msgs)).messages)
    cache0 = ex._api_messages_cache
    assert cache0 is not None
    base_len = len(cache0)
    tool_results = [
        {"role": "tool", "tool_call_id": "call_x", "content": "工具 A 结果"},
        {"role": "tool", "tool_call_id": "call_y", "content": "工具 B 结果"},
    ]
    hook_messages = [{"role": "user", "content": "[hook] PostToolUse 注入"}]
    msgs.extend(tool_results)
    msgs.extend(hook_messages)
    ex._append_to_api_cache(tool_results)
    ex._append_to_api_cache(hook_messages)
    fresh = ex._serialize_for_api(copy.deepcopy(msgs)).messages
    cache = ex._api_messages_cache
    assert cache is not None and len(cache) == len(fresh) == base_len + 3
    import orjson

    assert orjson.dumps(cache, option=orjson.OPT_SORT_KEYS) == orjson.dumps(fresh, option=orjson.OPT_SORT_KEYS)


def test_invalidate_resets_both_fields():
    """防御守卫：_invalidate_api_cache 同时置空 cache 与 vision 标志"""
    ex = _build_executor()
    ex._api_messages_cache = [{"role": "user", "content": "x"}]
    ex._api_cache_has_vision_refs = False
    ex._invalidate_api_cache()
    assert ex._api_messages_cache is None
    assert ex._api_cache_has_vision_refs is None


def test_make_api_call_hits_cache_without_full_serialize(monkeypatch):
    """命中路径：_make_api_call 直接用 cache，不再全量 serialize（serialize 计数=0）"""
    _ensure_plugins_loaded()
    ex = _build_executor()
    ex._api_messages_cache = [{"role": "user", "content": "缓存命中应直接用我"}]
    ex._api_cache_has_vision_refs = False

    calls = {"serialize": 0}
    orig = ex._serialize_for_api

    def _counting_serialize(*a, **kw):
        calls["serialize"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(ex, "_serialize_for_api", _counting_serialize)

    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return iter([])

    client = MagicMock()
    client.chat.completions.create.side_effect = _fake_create
    monkeypatch.setattr("app.utils.http_client.build_openai_client", lambda **kw: client)

    ex._make_api_call([{"role": "user", "content": "这一段不应触发全量 serialize"}], llm_config=dict(ex.llm_config))
    assert calls["serialize"] == 0, "cache 命中时不得再走全量 serialize"
    assert captured.get("messages") == [{"role": "user", "content": "缓存命中应直接用我"}], "发送体必须是 cache 内容"
