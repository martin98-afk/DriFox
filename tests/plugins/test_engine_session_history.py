# -*- coding: utf-8 -*-
"""EngineSessionImpl._history 完整累积 + system 去重回归测试

子任务 #33（用户最终裁决）：引擎会话历史**不做任何按条数/字节截断**、不做
按模型窗口适配，全量发送给模型，超出模型窗口由 provider API 报错（用户明确
接受）。原 A1 截断方案（max_history 尾窗）已按裁决移除。

== 契约（测试即规格） ==

- ``_history`` 完整累积：连续 N 轮（含超时取消路径）后长度严格等于全部轮次
  累积，无任何丢失。
- system 去重（规范化，非截断）：system 仅会话开头一条（会话开始时缓存，
  会话内不变），非首条 system 一律不入史（不比对内容）。去重让「报错就
  报错」来自真正的超限而非多条 system 的格式错误。
- 隔离 core 的 SessionManager / ``session.messages`` / loader 重载语义、
  evict 路径、双引擎隔离一律不受影响。

执行链说明：monkeypatch 实例属性 ``s._executor.execute`` 为同步 fake
（直接回调 messages_updated + finished，绕真实网络/线程）；HookPolicy.NONE
下 hook 触发链自然走空，无需额外隔离。

fake 上报语义（与真实链路对齐）：worker.finished_with_messages →
messages_updated 的载荷**不含入口 user**（worker 的
_current_session_messages 起点是隔离 session 的 get_context_messages()=空，
入口 user 只进 API 请求）。每轮净增 = 入口 user + 上报 assistant = 2 条。
"""

import pytest

from app.core.conversation.engine_session import EngineSessionImpl

_TOTAL_ROUNDS = 10  # 含首轮


def _round_msgs(tag: str, seq: int = 0):
    """单轮 turn 的 worker 上报消息（只含 worker 新增的 assistant）。

    上报带自增序号，使各轮内容可区分（完整累积断言可逐条比对）。
    """
    return [
        {"role": "assistant", "content": f"{tag}-{seq}-answer", "model_name": "bench-mock"},
    ]


def _make_execute_fake(tag_prefix: str, hang: bool = False):
    """构造同步 fake execute：上报一轮消息并 finished（hang=True 时只回 started）。

    返回 (fake_execute, calls)；calls 收集每轮请求的 messages 快照长度，
    供诊断（不作为断言依据）。
    """
    calls: list = []
    seq = [0]

    def fake_execute(*, messages, llm_config, tools, callbacks, direct_signals=True, **kwargs):
        calls.append(len(messages))
        if hang:
            return True  # 已启动但不回调 → adapter.wait 超时
        callbacks["messages_updated"](_round_msgs(tag_prefix, seq[0]))
        seq[0] += 1
        callbacks["finished"]("ok")
        return True

    return fake_execute, calls


def _make_engine():
    return EngineSessionImpl(engine_name="a1-hist", get_model_config=lambda: {})


# ══════════════ 完整累积（#33 核心：无任何截断） ══════════════

def test_full_accumulation(monkeypatch):
    """连续 10 轮（首轮带 system）后 len(_history) 严格等于全部轮次累积。

    公式：首轮 [SYS, u0, a0]=3 条，后续每轮 [u_i]+[a_i]=+2 → 3+2×9=21 条。
    system 恒在首位，各轮 user/assistant 内容逐条可辨、无丢失。
    """
    engine = _make_engine()
    fake, _ = _make_execute_fake("r", hang=False)
    monkeypatch.setattr(engine._executor, "execute", fake)
    try:
        engine.turn(system="SYS", user="u0", auto_history=True)
        assert len(engine._history) == 3
        assert engine._history[0]["content"] == "SYS"

        for i in range(1, _TOTAL_ROUNDS):
            r = engine.turn(user=f"u{i}", auto_history=True)
            assert r.ok, f"第 {i} 轮应成功: {r.error}"

        # 核心：严格等于全部累积，无任何截断丢失
        assert len(engine._history) == 3 + 2 * (_TOTAL_ROUNDS - 1), (
            f"历史应完整累积 21 条，实际 {len(engine._history)}（不得按条数裁剪）"
        )
        assert engine._history[0]["content"] == "SYS"
        # 逐条抽验：每个入口 user 与对应上报 assistant 都在，顺序正确
        for i in range(_TOTAL_ROUNDS):
            base = 1 + 2 * i
            assert engine._history[base]["role"] == "user"
            assert engine._history[base]["content"] == f"u{i}"
            assert engine._history[base + 1]["role"] == "assistant"
            assert engine._history[base + 1]["content"] == f"r-{i}-answer"
    finally:
        engine.cleanup()


def test_timeout_path_full_accumulation(monkeypatch):
    """超时取消分支同样完整累积：8 轮正常（17 条）+1 轮超时（入口 user 入史，无上报）。

    超时路径写入 msgs + round_messages([]) → 严格 18 条，无丢失。
    """
    engine = _make_engine()
    fake_ok, _ = _make_execute_fake("r", hang=False)
    fake_hang, _ = _make_execute_fake("r", hang=True)
    try:
        monkeypatch.setattr(engine._executor, "execute", fake_ok)
        engine.turn(system="SYS", user="u0", auto_history=True)
        for i in range(1, 8):
            engine.turn(user=f"u{i}", auto_history=True)
        assert len(engine._history) == 17

        # 切换为 hang：本轮永不 finished → turn 内部走超时取消分支
        monkeypatch.setattr(engine._executor, "execute", fake_hang)
        r = engine.turn(user="u-timeout", auto_history=True, timeout=0.3)
        assert r.timed_out, "hang 场景应返回 timed_out"

        # 核心：超时路径同样完整累积（17 + 入口 user = 18），无截断
        assert len(engine._history) == 18, (
            f"超时路径应完整累积 18 条，实际 {len(engine._history)}"
        )
        assert engine._history[-1]["content"] == "u-timeout"
    finally:
        engine.cleanup()


def test_duplicate_system_dedup(monkeypatch):
    """system 去重（规范化，非截断）：连续 turn 带同一条 SYS → 只保留一份。

    每轮 msgs = _history + [SYS, u_i] 拼接会重复带入 SYS，去重后每轮净增
    2 条（u_i + a_i），SYS 恒一份；其余历史完整累积。
    """
    engine = _make_engine()
    fake, _ = _make_execute_fake("r", hang=False)
    monkeypatch.setattr(engine._executor, "execute", fake)
    try:
        for i in range(5):  # 每轮都带同一条 system
            r = engine.turn(system="SYS", user=f"u{i}", auto_history=True)
            assert r.ok, f"第 {i} 轮应成功: {r.error}"

        # SYS 只出现一次
        sys_entries = [m for m in engine._history if m["role"] == "system"]
        assert len(sys_entries) == 1 and sys_entries[0]["content"] == "SYS", (
            f"同一条 system 重复入史 {len(sys_entries)} 次，应去重为 1"
        )
        # 去重不影响完整累积：3 + 2×4 = 11 条
        assert len(engine._history) == 11, (
            f"去重后其余历史应完整累积 11 条，实际 {len(engine._history)}"
        )
        # 尾部最新一轮完整
        assert engine._history[-1]["content"] == "r-4-answer"
    finally:
        engine.cleanup()


def test_subsequent_system_ignored(monkeypatch):
    """非首条 system 一律不入史（#P2 口径：system 仅会话开头一条，会话内不变）。

    SYS-A→SYS-B→SYS-A 三轮 → 历史中 system 仅 ["SYS-A"]，其余消息完整累积。
    """
    engine = _make_engine()
    fake, _ = _make_execute_fake("r", hang=False)
    monkeypatch.setattr(engine._executor, "execute", fake)
    try:
        engine.turn(system="SYS-A", user="u0", auto_history=True)
        engine.turn(system="SYS-B", user="u1", auto_history=True)  # 非首条 → 忽略
        engine.turn(system="SYS-A", user="u2", auto_history=True)  # 非首条 → 忽略

        sys_entries = [m["content"] for m in engine._history if m["role"] == "system"]
        assert sys_entries == ["SYS-A"], (
            f"非首条 system 应全部忽略：实际 {sys_entries}"
        )
        # 其余消息完整累积：首轮 3 条 + 后两轮各 2 条 = 7 条
        assert len(engine._history) == 7, (
            f"去重后其余历史应完整累积 7 条，实际 {len(engine._history)}"
        )
    finally:
        engine.cleanup()


# ══════════════ 语义保全（现有实现即应 PASS） ══════════════

def test_session_messages_untouched(monkeypatch):
    """turn 轮次不触碰隔离 core 的 session.messages，loader release/ensure 重载语义完好。"""
    engine = _make_engine()
    fake, _ = _make_execute_fake("r", hang=False)
    monkeypatch.setattr(engine._executor, "execute", fake)
    try:
        sm = engine._core.session_manager
        reloaded = []

        def loader(sid):
            reloaded.append(sid)
            return [{"role": "user", "content": "reloaded"}]

        sm.set_messages_loader(loader)
        session = sm.get_current_session()
        session.add_user_message("seed-1")
        session.add_user_message("seed-2")
        session.add_user_message("seed-3")

        for i in range(_TOTAL_ROUNDS):
            engine.turn(system="SYS", user=f"u{i}", auto_history=True)

        assert len(session.messages) == 3, "turn 轮次不得触碰隔离 core 的 session.messages"
        assert session.messages[0]["content"] == "seed-1"

        # release / ensure 重载语义完好（ensure_messages 无返回值，经属性体现）
        session.release_messages()
        assert session.messages_released
        assert session.message_count == 3, "释放后元数据条数保留"
        session.ensure_messages()
        assert not session.messages_released
        # consolidate_messages 会 normalize 消息（附加 params 等字段），断言语义而非逐字节
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "reloaded"
        assert reloaded == [session.session_id]
    finally:
        engine.cleanup()


def test_evict_not_triggered(monkeypatch):
    """长跑后隔离 core 仍单会话：显式 _evict_if_needed() 不炸（_on_evict=None 路径）。"""
    engine = _make_engine()
    fake, _ = _make_execute_fake("r", hang=False)
    monkeypatch.setattr(engine._executor, "execute", fake)
    try:
        sm = engine._core.session_manager
        for i in range(_TOTAL_ROUNDS):
            engine.turn(system="SYS", user=f"u{i}", auto_history=True)

        assert len(sm.sessions) == 1, "隔离 core 全程只应有初始化那一个会话"
        # 显式触发淘汰：_on_evict 为 None 时必须安静通过（不抛、不改会话数）
        sm._evict_if_needed()
        assert len(sm.sessions) == 1
    finally:
        engine.cleanup()


def test_two_engines_isolated():
    """两个 EngineSessionImpl 交叉 turn，_history 互不串（实例属性天然隔离）。"""
    e1 = _make_engine()
    e2 = _make_engine()
    try:
        for engine, tag in ((e1, "one"), (e2, "two")):
            fake, _ = _make_execute_fake(tag, hang=False)
            # 直接赋值实例属性（本用例无 monkeypatch 参数；实例随即 cleanup 丢弃）
            engine._executor.execute = fake
        for i in range(3):
            e1.turn(user=f"one-{i}", auto_history=True)
            e2.turn(user=f"two-{i}", auto_history=True)

        assert len(e1._history) == 6
        assert len(e2._history) == 6
        assert all("one" in m["content"] for m in e1._history), "e1 不得混入 e2 内容"
        assert all("two" in m["content"] for m in e2._history), "e2 不得混入 e1 内容"
    finally:
        e1.cleanup()
        e2.cleanup()
