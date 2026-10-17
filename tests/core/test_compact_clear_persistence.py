# -*- coding: utf-8 -*-
"""`/compact --clear` 清空落库 + 压缩守卫时间线判据（main_widget.py）回归测试。

背景 bug（2026-10-11 用户报障「/compact --clear 好像无法清空当前消息列表了」）：

- 内存侧清空**成功**（`set_messages([])` 执行、日志有「即将清空」），
  但**从未可靠落库**：
  ① `_on_compact_clear_finished` 全程不置 `_session_dirty`、也不触发持久化；
  ② 两条保存链都有一刀切 `not messages` 的空消息守卫
     （main_widget 高频保存路径 + history_manager `_do_save`/`flush_async`），
     清空后的空列表正好被吞；
  → 结果：清空只在内存生效，DB 保留旧消息，之后任何 `ensure_messages` 重载
     （切会话 / 重开 / 重启）都把旧消息读回来 = 用户看到「清不掉」。
     DB 对照实证：成功清空的会话 `compaction_cache.active=True` 且 messages≈5；
     失败的仍是 550 条旧消息 + `active=False`（连清空写的缓存都没落库）。

- 放大因素 1：`_do_trigger_callback` 无条件 `+1 _send_epoch` 且顺手清
  `_post_compact_guard` → 旧 worker 的延迟全量快照到达时守卫已被解除/误放行。
  日志实证「压缩守卫拦截/放行」0 次命中，清空被旧快照复活。

修复点（本文件验证）：
- P1：清空后**立即同步落库** —— `history_manager.save_cleared_session`
  （直连 session_store + 穿透提交，绕开空守卫）。
- P2：空消息守卫收窄为**只拦释放态**（`_messages_released` 标记），
  显式清空的空列表照写。
- P3：压缩守卫首选判据改为**时间线切断点**（`_post_compact_cutoff_ms`）：
  旧快照尾部 ts_ms < 切断点 → 拦截；清空后新 worker ≥ 切断点 → 放行。
  时间戳不可判定时回退 epoch 判据。

设计说明：与 tests/core/test_auto_compact_clear_race.py 同风格 ——
SimpleNamespace + MethodType 绑定真实方法，避免 Qt 实例化重依赖。
"""

from types import MethodType, SimpleNamespace

import pytest

from app.main_widget import OpenAIChatToolWindow


def _recorder():
    """返回 (calls:list, callable)——记录任意调用。"""
    calls = []
    return calls, lambda *a, **k: calls.append((a, k))


# ══════════════════════════════════════════════════════════
# P3：压缩守卫时间线判据
# ══════════════════════════════════════════════════════════


def _mk_guard_fake(session_messages, *, cutoff=0, send_epoch=1, compact_epoch=1):
    """构造能跑通 `_on_messages_updated` 守卫段的 fake 窗口。

    与 test_auto_compact_clear_race._mk_guard_fake 对齐，额外支持
    `_post_compact_cutoff_ms`（时间线切断点）。
    """
    set_calls, set_rec = _recorder()
    session = SimpleNamespace(
        session_id="sess_0001",
        messages=session_messages,
        set_messages=set_rec,
    )
    fake = SimpleNamespace(
        _is_destroyed=False,
        _post_compact_guard=True,
        _post_compact_cutoff_ms=cutoff,
        _send_epoch=send_epoch,
        _post_compact_epoch=compact_epoch,
        session_manager=SimpleNamespace(get_current_session=lambda: session),
        _session_switched=False,
        _pending_session_hook=False,
        _truncation_sentinel=None,
        _pending_send_after_truncation=False,
        _pending_send_user_text=None,
        _history_preview_messages=None,
        _response_start_time=None,
        _current_provider_name="",
        _session_dirty=False,
        _refresh_context_usage_indicator=lambda: None,
    )
    fake._on_messages_updated = MethodType(OpenAIChatToolWindow._on_messages_updated, fake)
    # 守卫现调用 `self._max_message_ts_ms(...)`（静态方法）——SimpleNamespace 假件
    # 不会自动继承类方法，须显式绑定
    fake._max_message_ts_ms = OpenAIChatToolWindow._max_message_ts_ms
    fake.__dict__["_session"] = session
    return fake, set_calls


def _msgs(*ts_values):
    """构造带 ts_ms 的消息（None → 不带 ts_ms 字段）。"""
    out = []
    for i, ts in enumerate(ts_values):
        msg = {"role": "user", "content": f"m{i}"}
        if ts is not None:
            msg["ts_ms"] = ts
        out.append(msg)
    return out


class TestCompactGuardTimeline:
    def test_blocks_old_worker_snapshot_by_timeline(self, qapp):
        """🔴 核心回归：epoch 已变但消息时间戳早于切断点 → 仍须拦截。

        这正是「epoch 单独用时等于没拦」的场景：_do_trigger_callback 主动
        +1 epoch 后，旧 worker 延迟快照会因「epoch 已变」被误放行。
        时间线判据（尾部 ts < cutoff）不受影响。
        """
        cutoff = 1_000_000
        fake, set_calls = _mk_guard_fake(
            session_messages=[],
            cutoff=cutoff,
            send_epoch=9,  # epoch 已变（会被旧的 epoch 判据误当新 worker）
            compact_epoch=8,
        )
        incoming = _msgs(cutoff - 5000, cutoff - 4000, cutoff - 3000)  # 全部早于切断点
        fake._on_messages_updated(incoming)
        assert len(set_calls) == 0, "旧快照（尾部 ts < 切断点）必须被拦截，不得复活已清空会话"
        assert fake._post_compact_guard is False, "守卫单次使用后自清零"
        assert fake._post_compact_cutoff_ms == 0

    def test_passes_new_worker_by_timeline(self, qapp):
        """清空后新 worker（尾部 ts ≥ 切断点）必须放行，结果写入 session。"""
        cutoff = 1_000_000
        fake, set_calls = _mk_guard_fake(
            session_messages=[],
            cutoff=cutoff,
            send_epoch=1,
            compact_epoch=1,  # epoch 未变：旧 epoch 判据会误拦
        )
        incoming = _msgs(cutoff - 100, cutoff + 10, cutoff + 250)  # 尾部窗口含新消息
        fake._on_messages_updated(incoming)
        assert len(set_calls) == 1, "新 worker 结果必须放行（否则 UI 永不显示大模型回复）"
        assert set_calls[0][0][0] == incoming
        assert fake._post_compact_guard is False
        assert fake._post_compact_cutoff_ms == 0

    def test_falls_back_to_epoch_without_timestamps(self, qapp):
        """无 ts_ms（老数据 / 构造期消息）→ 回退 epoch 判据。"""
        # epoch 相等 → 旧快照 → 拦截
        fake, set_calls = _mk_guard_fake(
            session_messages=[],
            cutoff=1_000_000,
            send_epoch=3,
            compact_epoch=3,
        )
        fake._on_messages_updated(_msgs(None, None))  # 无 ts_ms
        assert len(set_calls) == 0

        # epoch 已变 → 新 worker → 放行
        fake2, set_calls2 = _mk_guard_fake(
            session_messages=[],
            cutoff=1_000_000,
            send_epoch=4,
            compact_epoch=3,
        )
        fake2._on_messages_updated(_msgs(None, None))
        assert len(set_calls2) == 1

    def test_cutoff_zero_falls_back_to_epoch(self, qapp):
        """未记录切断点（cutoff=0）→ 走 epoch 判据，保持旧语义。"""
        fake, set_calls = _mk_guard_fake(
            session_messages=[],
            cutoff=0,
            send_epoch=5,
            compact_epoch=5,
        )
        fake._on_messages_updated(_msgs(1_500_000, 1_600_000))  # 有 ts 但无 cutoff
        assert len(set_calls) == 0, "无切断点时退化为 epoch 相等 → 拦截旧快照"

    def test_short_window_ignores_tail_synthetic_messages(self, qapp):
        """尾部窗口取最大 ts：收尾阶段追加的无 ts 合成消息不应让判定落空。"""
        cutoff = 1_000_000
        fake, set_calls = _mk_guard_fake(
            session_messages=[],
            cutoff=cutoff,
            send_epoch=3,
            compact_epoch=3,
        )
        # 前段是旧消息，尾部夹一条无 ts 的合成消息
        incoming = _msgs(cutoff - 5000, cutoff - 4000) + [{"role": "system", "content": "错误提示"}]
        fake._on_messages_updated(incoming)
        assert len(set_calls) == 0, "尾窗最大 ts 仍是旧值 → 拦截"


# ══════════════════════════════════════════════════════════
# `_max_message_ts_ms` 静态助手
# ══════════════════════════════════════════════════════════


class TestMaxMessageTsMs:
    def test_returns_tail_window_max(self):
        msgs = [{"ts_ms": 10}, {"ts_ms": 30}, {"ts_ms": 20}]
        assert OpenAIChatToolWindow._max_message_ts_ms(msgs) == 30

    def test_window_limits_scope(self):
        msgs = [{"ts_ms": 999}, {"ts_ms": 1}, {"ts_ms": 2}]
        assert OpenAIChatToolWindow._max_message_ts_ms(msgs, window=2) == 2

    def test_zero_when_no_usable_ts(self):
        assert OpenAIChatToolWindow._max_message_ts_ms([]) == 0
        assert OpenAIChatToolWindow._max_message_ts_ms([{"role": "user"}]) == 0
        # 非 int（字符串 ts）不参与判定
        assert OpenAIChatToolWindow._max_message_ts_ms([{"ts_ms": "123"}]) == 0
        # bool 是 int 子类，但生产路径不会写入；此处只保证不崩
        assert OpenAIChatToolWindow._max_message_ts_ms([{"ts_ms": None}]) == 0


# ══════════════════════════════════════════════════════════
# P1：清空回调必须立即同步落库
# ══════════════════════════════════════════════════════════


def _mk_clear_fake(*, msgs=None, save_ok=True):
    """构造能跑通 `_on_compact_clear_finished` 主流程的 fake 窗口。"""
    set_calls, set_rec = _recorder()

    class _FakeSession:
        """模拟 ChatSession 的 compaction_cache 属性语义（set_compaction_cache
        写入后 property 能读到），SimpleNamespace 做不到这一点。"""

        def __init__(self, messages):
            self.session_id = "sess_0001"
            self.messages = messages
            self.compaction_state = {}
            self.compaction_cache = {}
            self.messages_released = False
            # API 权威上下文占用字段（P6 复位对象）
            self.last_api_prompt_tokens = 90000
            self.last_api_message_count = len(messages)
            self.last_api_prompt_from_usage = True

        def set_messages(self, messages, **kwargs):
            set_rec(messages, **kwargs)
            self.messages = messages

        def set_compaction_cache(self, cache):
            self.compaction_cache = cache

    session = _FakeSession(msgs if msgs is not None else [{"role": "user", "content": "旧对话"}])
    save_calls, save_rec = _recorder()

    def _save_cleared_session(sid, **kwargs):
        save_rec(sid, **kwargs)
        return save_ok

    fake = SimpleNamespace(
        _is_destroyed=False,
        _auto_compact_in_progress=True,
        backend=SimpleNamespace(
            # executor 不在 _running_tasks（已被 get_finished_tasks 取走）→ 走
            # _finished_tasks 分支；无 error 即继续清空
            sub_agent_manager=SimpleNamespace(_running_tasks={}, _finished_tasks={"compact_1": {"error": None}}),
            chat_engine=SimpleNamespace(alive=True),
            stop_streaming=lambda *a, **k: None,
            trigger_session_event=lambda *a, **k: None,
        ),
        session_manager=SimpleNamespace(sessions=[session], get_current_session=lambda: session),
        _is_streaming=False,
        _current_assistant_card=None,
        _set_ai_state=lambda *a, **k: None,
        _toggle_send_stop=lambda *a, **k: None,
        _sync_team_mail_on_stop=lambda *a, **k: None,
        _session_card_cache={},
        _current_session_id="sess_0001",
        _pending_session_hook=False,
        _display_current_session=lambda: None,
        _check_and_process_pending=lambda: None,
        _safe_resume_team_mail_after_compact=lambda: None,
        _send_epoch=3,
        _post_compact_epoch=-1,
        _post_compact_cutoff_ms=0,
        _post_compact_guard=False,
        _session_dirty=True,
        history_manager=SimpleNamespace(save_cleared_session=_save_cleared_session),
        _api_usage_kwargs=OpenAIChatToolWindow.__dict__["_api_usage_kwargs"],
    )
    fake._on_compact_clear_finished = MethodType(OpenAIChatToolWindow._on_compact_clear_finished, fake)
    fake.__dict__.update(_set_calls=set_calls, _save_calls=save_calls, _session_obj=session)
    return fake


class TestCompactClearPersists:
    def test_clear_calls_save_cleared_session(self):
        """🔴 P1：清空回调必须调用 save_cleared_session 立即落库。"""
        fake = _mk_clear_fake()
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")

        wiped = fake._set_calls[0][0][0]
        assert wiped == [], "session 应先被清空"
        assert len(fake._save_calls) == 1, "清空后必须立即同步落库（否则 DB 保留旧消息）"
        args, kwargs = fake._save_calls[0]
        assert args[0] == "sess_0001"
        assert kwargs["compaction_cache"]["active"] is True, "落库须带上 active=True 的压缩缓存"

    def test_clear_resets_dirty_flag_on_success(self):
        """落库成功后清脏标记，避免后续无变更的重复全量保存。"""
        fake = _mk_clear_fake(save_ok=True)
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert fake._session_dirty is False

    def test_clear_keeps_dirty_flag_when_save_fails(self):
        """落库失败时不得清脏标记（否则真正的变更被当成已保存丢弃）。"""
        fake = _mk_clear_fake(save_ok=False)
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert fake._session_dirty is True

    def test_clear_records_timeline_cutoff(self):
        """清空须记录时间线切断点，供后续守卫区分旧快照 / 新 worker。"""
        fake = _mk_clear_fake()
        assert fake._post_compact_cutoff_ms == 0
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert fake._post_compact_cutoff_ms > 0, "应记录 wall-clock 切断点（毫秒）"
        assert fake._post_compact_guard is True

    def test_clear_resets_api_usage_fields(self):
        """🔴 P6：清空必须复位 API 权威占用字段，否则反复触发自动压缩。

        根因：这三个字段是 session 级跨轮缓存，只清 messages 不动它们 →
        快照的 API 权威值路径沿用旧值（len(messages) ≤ api_message_count
        时直接返回旧权威值）→ 清空后 ratio 仍 90% → 下一轮 hook 再次触发。
        """
        fake = _mk_clear_fake()
        target = fake._session_obj
        assert target.last_api_prompt_tokens > 0, "前提：清空前有 API 权威占用值"
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert target.last_api_prompt_tokens == 0
        assert target.last_api_message_count == 0
        # 复位为 None（三态"未知"）而非 False（"本地估算"）：
        # 后续首轮 API 调用回来时会正常写入 True
        assert target.last_api_prompt_from_usage is None

    def test_clear_persists_reset_api_usage(self):
        """复位的 API 占用字段必须随清空一起落库（否则重载又读回旧值）。"""
        fake = _mk_clear_fake()
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert len(fake._save_calls) == 1
        _, kwargs = fake._save_calls[0]
        assert kwargs["last_api_prompt_tokens"] == 0
        assert kwargs["last_api_message_count"] == 0
        assert kwargs["last_api_prompt_from_usage"] is None


class TestCompactClearReleasedState:
    def test_released_session_still_cleared(self):
        """🔴 早返回修正：被内存治理释放的会话（messages=[] + released）
        不得被误判为「空会话」而跳过清空 —— 释放态与真空在调用点不可区分，
        清空目标状态本就是空列表，重复清空幂等。

        修复前判据 `not target_session.messages` 会把释放态误杀
        （日志实证 `early-return: session missing/empty (target=True, msg_len=0)`）。
        """
        fake = _mk_clear_fake(msgs=[])
        target = fake._session_obj
        # 模拟释放态：messages 为空但打过 released 标记
        target.messages_released = True
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert len(fake._save_calls) == 1, "释放态会话也必须走清空 + 落库"

    def test_really_empty_session_early_returns(self):
        """真空会话（无 released 标记、无消息）→ 早返回，不做无意义清空。"""
        fake = _mk_clear_fake(msgs=[])
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert len(fake._set_calls) == 0, "真空会话不必清空"
        assert len(fake._save_calls) == 0
