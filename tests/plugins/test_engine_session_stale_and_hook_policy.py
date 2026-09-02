# -*- coding: utf-8 -*-
"""EngineSession 两个实测缺陷的回归守卫（2026-09-02）。

背景：assistant_hub 记忆/Dream 改走 `create_engine_session` 后，第 2 次 turn()
起全部被 "Already streaming (is_alive=False)" 拒绝，且 Stop hook 的续命提醒
被注入进记忆整理的消息流。

守卫两条：
1. `_alive_worker()` 必须返回 isRunning() 的**实际结果**（不能只读不返回）。
2. 引擎声明的 `hook_policy` 枚举必须翻译成 hook_policy_id，否则被 worker 忽略。
"""

import pytest

from app.core.conversation.config import ConversationConfig, HookPolicy


# ── 缺陷 1：_alive_worker 忽略 isRunning() 返回值 ──────────────────


class _FakeWorker:
    def __init__(self, running=True, destroyed=False):
        self._running = running
        self._destroyed = destroyed

    def isRunning(self):
        if self._destroyed:
            raise RuntimeError("wrapped C/C++ object of type OpenAIChatWorker has been deleted")
        return self._running


def test_alive_worker_false_when_thread_exited():
    """线程已退出但 C++ wrapper 仍在 → 必须判为"不存活"（旧实现误判为存活）。

    这是 assistant_hub 第 2 轮 turn() 起全部失败的直接原因：
    finished 信号在 daemon 线程里丢失 → 线程退出但 executor 仍标记 streaming →
    _alive_worker 误判存活 → _reset_stale_streaming 不复位 → execute() 全拒绝。
    """
    from app.core.conversation.engine_session import EngineSessionImpl

    assert EngineSessionImpl._alive_worker(_FakeWorker(running=False)) is False


def test_alive_worker_true_when_running():
    from app.core.conversation.engine_session import EngineSessionImpl

    assert EngineSessionImpl._alive_worker(_FakeWorker(running=True)) is True


def test_alive_worker_false_when_cpp_destroyed():
    from app.core.conversation.engine_session import EngineSessionImpl

    # deleteLater 生效后访问 isRunning() 抛 RuntimeError → 视为不存活
    assert EngineSessionImpl._alive_worker(_FakeWorker(destroyed=True)) is False


def test_alive_worker_none():
    from app.core.conversation.engine_session import EngineSessionImpl

    assert EngineSessionImpl._alive_worker(None) is False


def test_reset_stale_streaming_clears_when_thread_exited():
    """线程已退出 → is_streaming / current_worker 复位 + worker 被回收。"""
    from app.core.conversation.engine_session import EngineSessionImpl

    class _FakeExecutor:
        def __init__(self):
            self._is_streaming = True
            self._current_worker = _FakeWorker(running=False)
            self.finalized = []

        @property
        def is_streaming(self):
            return self._is_streaming

        def get_current_worker(self):
            return self._current_worker

        def _finalize_worker_cleanup(self, worker):
            self.finalized.append(worker)

    ex = _FakeExecutor()
    sess = EngineSessionImpl.__new__(EngineSessionImpl)
    sess.engine_name = "test"
    sess._executor = ex
    stale_worker = ex._current_worker

    sess._reset_stale_streaming()

    assert ex.is_streaming is False
    assert ex._current_worker is None
    # 线程已退出 → 顺带回收 worker（cleanup + deleteLater），避免每轮残留一个 QThread
    assert ex.finalized == [stale_worker]


def test_reset_stale_streaming_keeps_when_running():
    """worker 真在跑 → 不复位（避免打断进行中的 turn）。"""
    from app.core.conversation.engine_session import EngineSessionImpl

    class _FakeExecutor:
        def __init__(self):
            self._is_streaming = True
            self._current_worker = _FakeWorker(running=True)

        @property
        def is_streaming(self):
            return self._is_streaming

        def get_current_worker(self):
            return self._current_worker

        def _finalize_worker_cleanup(self, worker):
            raise AssertionError("运行中的 worker 不应被回收")

    ex = _FakeExecutor()
    sess = EngineSessionImpl.__new__(EngineSessionImpl)
    sess.engine_name = "test"
    sess._executor = ex

    sess._reset_stale_streaming()
    assert ex.is_streaming is True
    assert ex._current_worker is not None


# ── 缺陷 2：引擎声明的 hook_policy 枚举必须翻译成 hook_policy_id ──


def _build_session(**kwargs) -> "ConversationConfig":
    """构造最小 EngineSessionImpl，返回其 ConversationConfig（不启动任何 worker）。"""
    from app.core.conversation.engine_session import EngineSessionImpl

    sess = EngineSessionImpl(
        engine_name="test-hook-policy",
        get_model_config=lambda: {"模型名称": "m"},
        tool_executor=None,
        agent_manager=None,
        backend=None,
        **kwargs,
    )
    try:
        return sess._executor._config
    finally:
        sess.cleanup()


def test_hook_policy_none_translates_to_plugin_id():
    """声明 NONE → hook_policy_id="none"（否则 worker 忽略枚举、回落主域激活=all）。"""
    cfg = _build_session(hook_policy=HookPolicy.NONE)
    assert cfg.hook_policy is HookPolicy.NONE
    assert cfg.hook_policy_id == "none"


def test_hook_policy_string_none_translates_to_plugin_id():
    cfg = _build_session(hook_policy="none")
    assert cfg.hook_policy_id == "none"


def test_hook_policy_default_is_none_not_all():
    """不传 hook_policy → 引擎默认 NONE，且必须真的落到 id="none"。

    契约（engine_session.py 模块 docstring）：插件循环默认不被动触发全局 hooks。
    """
    cfg = _build_session()
    assert cfg.hook_policy is HookPolicy.NONE
    assert cfg.hook_policy_id == "none"


def test_hook_policy_tool_events_only_maps_to_tool_only_id():
    """枚举值 tool_events_only 与插件 id tool_only 不同名，映射不能靠 enum.value。"""
    cfg = _build_session(hook_policy=HookPolicy.TOOL_EVENTS_ONLY)
    assert cfg.hook_policy_id == "tool_only"


def test_hook_policy_all_maps_to_all_id():
    cfg = _build_session(hook_policy=HookPolicy.ALL)
    assert cfg.hook_policy_id == "all"


def test_explicit_hook_policy_id_not_overwritten():
    """显式 id 优先，不被枚举翻译覆盖。"""
    cfg = _build_session(hook_policy=HookPolicy.NONE, hook_policy_id="team_member")
    assert cfg.hook_policy_id == "team_member"


def test_translated_hook_policy_id_is_resolvable():
    """翻译出来的 id 必须能在 HookPolicyRegistry 里取到对象（否则 worker 会 warning 回落）。"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components
    from app.plugins.registries.hook_policy_registry import HookPolicyRegistry

    warmup_runtime_components()
    registry = HookPolicyRegistry.get_instance()
    for pid in ("all", "tool_only", "none"):
        assert pid in registry.policies(), f"HookPolicy 插件 id {pid} 未注册"


def test_main_chat_config_keeps_hook_policy_id_none():
    """主对话引擎不声明 hook_policy → id 保持 None（worker 回落用户激活策略，行为不变）。"""
    assert ConversationConfig().hook_policy_id is None
    assert ConversationConfig().hook_policy is HookPolicy.ALL


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
