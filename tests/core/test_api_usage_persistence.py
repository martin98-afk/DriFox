# -*- coding: utf-8 -*-
"""API 权威上下文占用值（prompt_tokens）持久化 + 自适应校准回归测试

回归保护（用户报：「加载以前有明确 API 返回上下文长度的也会用本地重新计算，
而且计算出来的结果会和真实的数据差个几倍」）：

1. 历史会话加载：create_session_from_record 必须透传 last_api_prompt_tokens
   与三态标记 last_api_prompt_from_usage（旧实现漏传 → 恒为 0 → 本地重算）。
2. 保存链：history_manager 对这些字段按「None → 保留现值」处理，绝不能清零。
3. 落库：三态标记 None 必须写成 SQL NULL（写成 0 = "本地估算" = 权威值作废）。
4. 快照口径：from_api 为 True / None（老记录）时直接用 API 值；仅 False
   （明确来自本地估算）才回退。
5. 校准因子：同覆盖时用 API 真实值反校准本地估算，并在后续无 API 值时生效。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.infra.token_estimator import (
    CALIBRATION_MAX,
    CALIBRATION_MIN,
    blend_calibration,
    resolve_calibration,
)


# ────────────────────────── 校准纯函数 ──────────────────────────


def test_blend_calibration_first_observation():
    """首次观测直接采用（本地高估 2 倍 → 因子 0.5）"""
    assert blend_calibration(None, 0.5) == 0.5
    assert blend_calibration(0.0, 0.5) == 0.5


def test_blend_calibration_is_ema_and_clamped():
    """多轮观测走 EMA，且结果恒被 clamp 在合法区间"""
    cal = blend_calibration(None, 0.8)
    cal2 = blend_calibration(cal, 0.4)
    assert 0.4 < cal2 < cal  # 向新观测收敛但不到达（0.6/0.4 权重）
    # 极端观测：单轮就被 clamp 到边界内，不会一步把估算带飞
    assert blend_calibration(1.0, 999.0) == CALIBRATION_MAX
    # 持续极端观测：始终被边界兜住（EMA 渐进，边界硬顶）
    cal = 1.0
    for _ in range(20):
        cal = blend_calibration(cal, 0.0001)
    assert cal == CALIBRATION_MIN


def test_blend_calibration_rejects_invalid():
    """非正 / NaN 观测不得污染因子"""
    assert blend_calibration(0.8, 0) == 1.0
    assert blend_calibration(0.8, float("nan")) == 1.0


def test_resolve_calibration_model_mismatch_resets():
    """换模型即作废旧因子（不同分词器偏差方向可能相反）"""
    assert resolve_calibration(0.5, "deepseek-chat", "deepseek-chat") == 0.5
    assert resolve_calibration(0.5, "deepseek-chat", "qwen-max") == 1.0
    # 无有效值（<=0）→ 1.0
    assert resolve_calibration(0, "m", "m") == 1.0


# ────────────────────────── 加载侧透传 ──────────────────────────


def test_create_session_from_record_passes_api_usage():
    """历史会话加载：API 权威值与三态标记必须落到 ChatSession 上"""
    from app.widgets.ui_helpers import create_session_from_record

    record = {
        "session_id": "abc123",
        "title": "旧会话",
        "messages": [{"role": "user", "content": "hi"}],
        "last_api_prompt_tokens": 42123,
        "last_api_message_count": 12,
        "last_api_prompt_from_usage": True,
        "token_calibration": 0.62,
        "token_calibration_model": "deepseek-chat",
    }

    session = create_session_from_record(record, record["messages"], "旧会话")

    assert session.last_api_prompt_tokens == 42123
    assert session.last_api_message_count == 12
    assert session.last_api_prompt_from_usage is True
    assert abs(session.token_calibration - 0.62) < 1e-6
    assert session.token_calibration_model == "deepseek-chat"
    # 导出给保存链的字典必须齐全（漏一个就会在下次保存时被清零）
    exported = session.api_usage_dict()
    assert exported["last_api_prompt_tokens"] == 42123
    assert exported["last_api_prompt_from_usage"] is True


def test_create_session_from_record_legacy_keeps_unknown():
    """老记录无该列 → 三态为 None（未知，按"来自 API"处理），不能是 False"""
    from app.widgets.ui_helpers import create_session_from_record

    record = {
        "session_id": "legacy1",
        "title": "老会话",
        "messages": [{"role": "user", "content": "hi"}],
    }
    session = create_session_from_record(record, record["messages"], "老会话")
    assert session.last_api_prompt_from_usage is None


# ────────────────────────── 保存链保留现值 ──────────────────────────


def test_history_manager_keeps_existing_api_usage_fields():
    """save/update 不带这些字段时必须沿用现值（旧实现默认 0 = 每存一次清零）"""
    from app.utils.history_manager import HistoryManager

    existing = {
        "last_api_prompt_tokens": 42123,
        "last_api_message_count": 12,
        "last_api_prompt_from_usage": True,
        "token_calibration": 0.62,
        "token_calibration_model": "deepseek-chat",
    }
    provided = {k: None for k in existing}

    resolved = HistoryManager._resolve_api_usage_fields(None, provided, existing)

    assert resolved["last_api_prompt_tokens"] == 42123
    assert resolved["last_api_message_count"] == 12
    assert resolved["last_api_prompt_from_usage"] is True
    assert abs(resolved["token_calibration"] - 0.62) < 1e-6
    assert resolved["token_calibration_model"] == "deepseek-chat"


def test_history_manager_unknown_flag_stays_none():
    """未知标记（None）不得被落成 False —— False 意味着"这是本地估算值" """
    from app.utils.history_manager import HistoryManager

    resolved = HistoryManager._resolve_api_usage_fields(
        None,
        {
            "last_api_prompt_tokens": 100,
            "last_api_message_count": 3,
            "last_api_prompt_from_usage": None,
            "token_calibration": None,
            "token_calibration_model": None,
        },
        None,
    )
    assert resolved["last_api_prompt_from_usage"] is None


def test_sql_tri_state_mapping():
    """落库映射：True→1 / False→0 / None→NULL"""
    from app.core.store.session_repository import _to_sql_bool_or_null

    assert _to_sql_bool_or_null(True) == 1
    assert _to_sql_bool_or_null(False) == 0
    assert _to_sql_bool_or_null(None) is None


# ────────────────────────── 快照口径 ──────────────────────────


def _make_engine(messages, system_prompt="你是助手", budget=200000):
    """构造最小可用 UIEngine（复用 test_engine_snapshot_optimization 的模式）"""
    from app.core.engines.ui.engine import UIEngine

    session = SimpleNamespace()
    session.messages = messages
    session.system_prompt = system_prompt
    session.compaction_state = {"active": False}
    session.compaction_cache = {}
    session.last_api_prompt_tokens = 0
    session.last_api_message_count = 0
    session.last_api_prompt_from_usage = None
    session.token_calibration = 0.0
    session.token_calibration_model = ""

    engine = UIEngine.__new__(UIEngine)
    engine._get_model_config = lambda: {"模型名称": "gpt-4"}
    engine._get_agent_manager = MagicMock(return_value=None)
    engine._current_agent = "build"
    engine._session_manager = MagicMock()
    engine._session_manager.get_current_session.return_value = session
    core = MagicMock()
    core.context_builder.get_context_budget.return_value = budget
    engine._conversation_core = core
    engine._tools_schema_cache = {"timestamp": 1e18, "tools": [], "tokens": 0}
    engine._tool_executor = None
    engine._backend = None
    return engine, session


def test_snapshot_uses_api_value_when_flag_unknown():
    """老会话（标记未知）有 API 值 → 直接用，不再本地重算"""
    messages = [{"role": "user", "content": "中文内容测试" * 50}]
    engine, session = _make_engine(messages)

    snap = engine.get_context_usage_snapshot(
        api_prompt_tokens=30000,
        api_message_count=len(messages),
        from_api=None,  # 老记录：未知
    )
    assert snap["used_tokens"] == 30000


def test_snapshot_ignores_local_estimate_value():
    """标记明确为 False（值来自本地估算）→ 回退本地估算，不当权威值"""
    messages = [{"role": "user", "content": "中文内容测试" * 50}]
    engine, session = _make_engine(messages)

    snap_false = engine.get_context_usage_snapshot(
        api_prompt_tokens=30000,
        api_message_count=len(messages),
        from_api=False,
    )
    snap_none = engine.get_context_usage_snapshot(api_prompt_tokens=0, api_message_count=0)
    assert snap_false["used_tokens"] == snap_none["used_tokens"]
    assert snap_false["used_tokens"] != 30000


def test_calibration_learned_and_applied():
    """同覆盖时用 API 值反校准；之后即使没有 API 值，本地估算也被拉到真实量级"""
    messages = [{"role": "user", "content": "中文内容测试" * 50}]
    engine, session = _make_engine(messages)

    local_est = engine.get_context_usage_snapshot()["used_tokens"]
    assert local_est > 0

    # 真实占用只有本地估算的一半（中文模型分词更高效）
    engine.get_context_usage_snapshot(
        api_prompt_tokens=local_est // 2,
        api_message_count=len(messages),
        from_api=True,
    )
    assert session.token_calibration_model == "gpt-4"
    assert CALIBRATION_MIN <= session.token_calibration < 1.0

    # 下一轮无 API 值：本地估算必须已被校准（显著小于原始估算）
    calibrated = engine.get_context_usage_snapshot()["used_tokens"]
    assert calibrated < local_est * 0.8


def test_calibration_not_updated_when_messages_grew():
    """消息数已变化（覆盖不同）→ 比值无意义，不更新校准因子"""
    messages = [{"role": "user", "content": "hi"}]
    engine, session = _make_engine(messages)

    engine.get_context_usage_snapshot(
        api_prompt_tokens=1000,
        api_message_count=99,  # 明显大于当前消息数 → 走 delta 分支，不校准
        from_api=True,
    )
    assert session.token_calibration == 0.0
