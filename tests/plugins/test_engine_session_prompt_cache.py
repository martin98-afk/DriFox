# -*- coding: utf-8 -*-
"""[E8-项H] EngineSession BuildSystemPrompt/SessionStart 注入缓存回归。

守卫：
1. 缓存命中：多次取注入只触发一次 BuildSystemPrompt hook 链
2. 显式失效：invalidate_prompt_injections() 后重建（agent/技能/笔记热变场景）
3. 行为等价：turn 拼进 system 的注入内容与逐轮触发时一致
4. PreUserMessage 不入缓存（含本轮 user 消息，必须实时）
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.conversation.engine_session import EngineSessionImpl  # noqa: E402

@pytest.fixture()
def build_session():
    """最小 EngineSessionImpl（不启动 worker；hook 触发走 monkeypatch 计数）。"""
    sess = EngineSessionImpl(
        engine_name="test-prompt-cache",
        get_model_config=lambda: {"模型名称": "m"},
        tool_executor=None,
        agent_manager=None,
        backend=None,
    )
    yield sess
    sess.cleanup()

def _install_hook_counter(sess, bsp_outputs):
    """monkeypatch _trigger_engine_hook：按事件名计数；BuildSystemPrompt 返回固定注入，
    PreUserMessage 回显本轮消息（模拟触发成功，验证实时性）。"""
    calls = {"BuildSystemPrompt": 0, "PreUserMessage": 0}
    real = sess._trigger_engine_hook

    def counting(event_name, event, ctx_extra=None):
        if event_name in calls:
            calls[event_name] += 1
        if event_name == "BuildSystemPrompt":
            return list(bsp_outputs)
        if event_name == "PreUserMessage":
            return [f"用户消息:{(ctx_extra or {}).get('message', '')}"]
        return real(event_name, event, ctx_extra)

    sess._trigger_engine_hook = counting
    return calls

def test_prewarm_cache_hits_once(build_session):
    """连续取两次注入：BuildSystemPrompt hook 链只跑一次。"""
    calls = _install_hook_counter(build_session, ["说明书片段"])
    r1 = build_session._get_prewarm_injections()
    r2 = build_session._get_prewarm_injections()
    assert calls["BuildSystemPrompt"] == 1, f"缓存命中应只触发一次，实测 {calls['BuildSystemPrompt']} 次"
    assert r1 == r2 == ["说明书片段"]

def test_invalidate_rebuilds(build_session):
    """显式失效后重建（agent/技能/项目笔记热变场景）。"""
    calls = _install_hook_counter(build_session, ["片段"])
    build_session._get_prewarm_injections()
    assert calls["BuildSystemPrompt"] == 1
    build_session.invalidate_prompt_injections()
    build_session._get_prewarm_injections()
    assert calls["BuildSystemPrompt"] == 2, "失效后应重新触发 hook 链"

def test_return_is_defensive_copy(build_session):
    """返回拷贝：调用方 extend 不污染缓存本体。"""
    _install_hook_counter(build_session, ["片段"])
    r1 = build_session._get_prewarm_injections()
    r1.append("caller-junk")
    r2 = build_session._get_prewarm_injections()
    assert r2 == ["片段"], "缓存不得被调用方修改穿透"
    assert "caller-junk" not in build_session._prewarm_injections_cache

def test_turn_uses_cache_and_preuser_stays_realtime(build_session, monkeypatch):
    """turn 两次：BuildSystemPrompt 走缓存（1 次），PreUserMessage 每轮实时（2 次）；
    注入内容拼进 system 且含本轮 user 消息。"""
    calls = _install_hook_counter(build_session, ["缓存片段"])
    captured = []

    class _FakeExecutor:
        is_streaming = False

        def get_current_worker(self):
            return None

        def execute(self, *, messages, **k):
            captured.append(messages)
            return False  # 不真跑 worker

        def cleanup(self):
            pass

    monkeypatch.setattr(build_session, "_executor", _FakeExecutor())
    monkeypatch.setattr(
        type(build_session._adapter), "reset", lambda self: None, raising=False
    )

    build_session.turn(user="第一轮")
    build_session.turn(user="第二轮")

    assert calls["BuildSystemPrompt"] == 1, "BuildSystemPrompt 应命中缓存"
    assert calls["PreUserMessage"] == 2, "PreUserMessage 每轮必须实时触发"
    assert len(captured) == 2
    for msgs, user_text in zip(captured, ["第一轮", "第二轮"]):
        sys_msgs = [m for m in msgs if m.get("role") == "system"]
        assert sys_msgs and "缓存片段" in sys_msgs[0]["content"], "注入应拼进 system"
        assert user_text in sys_msgs[0]["content"], "PreUserMessage 注入应包含本轮 user 消息"

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
