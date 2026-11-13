# -*- coding: utf-8 -*-
"""IM 内交互提问：文本协议与 GatewayService 状态机单测。

覆盖：
- ``app.gateway.interaction``：问题渲染、答案解析（序号/label/多选/自定义）
- ``GatewayService`` 待答状态：多题推进、超时跳过、命令打断、答案回填 worker
"""
import asyncio

import pytest

from app.gateway.interaction import (
    build_answer_payload,
    format_question,
    parse_answer,
)


# ==================== 文本协议 ====================

Q_SINGLE = {
    "question": "用哪个方案？",
    "options": [
        {"label": "方案A", "description": "快但不稳"},
        {"label": "方案B", "description": "稳但慢"},
    ],
}


def test_format_question_with_options():
    text = format_question([Q_SINGLE], 0, 1)
    assert "用哪个方案？" in text
    assert "1. 方案A — 快但不稳" in text
    assert "2. 方案B — 稳但慢" in text
    assert "回复选项序号" in text


def test_format_question_progress_for_multi():
    text = format_question([Q_SINGLE, Q_SINGLE], 1, 2)
    assert "（2/2）" in text


def test_format_question_without_options():
    text = format_question([{"question": "项目叫什么？"}], 0, 1)
    assert "请直接回复你的答案" in text
    assert "1." not in text


def test_format_question_caps_options_at_four():
    q = {"question": "选一个", "options": [{"label": f"O{i}"} for i in range(6)]}
    text = format_question([q], 0, 1)
    assert "4. O3" in text
    assert "5. O4" not in text


def test_parse_answer_by_number():
    assert parse_answer(Q_SINGLE, "2") == "【方案B】"


def test_parse_answer_by_label():
    assert parse_answer(Q_SINGLE, "方案A") == "【方案A】"


def test_parse_answer_custom_text():
    assert parse_answer(Q_SINGLE, "都不行，用方案C") == "都不行，用方案C"


def test_parse_answer_out_of_range_falls_back_to_custom():
    assert parse_answer(Q_SINGLE, "9") == "9"


def test_parse_answer_multiple():
    q = {
        "question": "要哪些？",
        "multiple": True,
        "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
    }
    assert parse_answer(q, "1,3") == "【A】；【C】"
    assert parse_answer(q, "A，自己加一个") == "【A】；自己加一个"


def test_parse_answer_no_options_returns_raw():
    assert parse_answer({"question": "名字？"}, "DriFox") == "DriFox"


def test_build_answer_payload_matches_ui_format():
    """worker 侧 _parse_question_result 依赖这个格式，改格式会静默丢答案。"""
    payload = build_answer_payload([Q_SINGLE, {"question": "第二个?"}], ["【方案A】", "随便"])
    assert "问题「用哪个方案？」的回答：\n【方案A】" in payload
    assert "\n---\n" in payload
    assert "问题「第二个?」的回答：\n随便" in payload


# ==================== GatewayService 状态机 ====================

class _FakeWorker:
    def __init__(self):
        self.answers = []

    def provide_answer(self, answer):
        self.answers.append(answer)


class _FakeExecutor:
    def __init__(self, worker):
        self._current_worker = worker


class _FakeEngine:
    def __init__(self, worker):
        self._conversation_executor = _FakeExecutor(worker)

    def provide_question_answer(self, answer):
        self._conversation_executor._current_worker.provide_answer(answer)
        return True


@pytest.fixture
def service(qapp_stub=None, monkeypatch=None):
    """构造最小 GatewayService 桩：只留待答状态相关方法所需字段。"""
    from app.core.services import gateway_service as mod

    svc = mod.GatewayService.__new__(mod.GatewayService)
    from PyQt5.QtCore import QObject

    QObject.__init__(svc)
    svc.gateway_input_received = mod.GatewayService.gateway_input_received
    svc.question_asked_received = mod.GatewayService.question_asked_received
    svc.question_state_cleared = mod.GatewayService.question_state_cleared
    svc._manager = None  # 消息发送在测试里被拦截
    svc._pending_questions = {}
    svc._engine = None
    # 拦截平台发送：只记录文本
    svc.sent = []
    svc._send_question_text = lambda platform, chat_id, text: svc.sent.append(text)
    return svc


def test_question_asked_registers_state(service):
    from app.gateway.base import Platform

    engine_worker = _FakeWorker()
    service._engine = _FakeEngine(engine_worker)
    service._on_question_asked(
        {
            "platform": Platform.FEISHU,
            "chat_id": "chat1",
            "payload": {"tool_call_id": "t1", "questions": [Q_SINGLE]},
        }
    )
    key = service._question_key(Platform.FEISHU, "chat1")
    assert key in service._pending_questions
    assert service.sent and "用哪个方案？" in service.sent[0]


def test_single_question_answer_fills_worker(service):
    from app.gateway.base import Platform

    worker = _FakeWorker()
    service._engine = _FakeEngine(worker)
    service._on_question_asked(
        {
            "platform": Platform.FEISHU,
            "chat_id": "chat1",
            "payload": {"tool_call_id": "t1", "questions": [Q_SINGLE]},
        }
    )
    consumed = service._consume_pending_question(Platform.FEISHU, "chat1", "u1", "1")
    assert consumed is True
    assert worker.answers == ["问题「用哪个方案？」的回答：\n【方案A】"]
    assert service._pending_questions == {}


def test_multi_question_advances_then_fills_once(service):
    from app.gateway.base import Platform

    worker = _FakeWorker()
    service._engine = _FakeEngine(worker)
    q2 = {"question": "第二个问题？", "options": [{"label": "X"}, {"label": "Y"}]}
    service._on_question_asked(
        {
            "platform": Platform.FEISHU,
            "chat_id": "chat1",
            "payload": {"tool_call_id": "t1", "questions": [Q_SINGLE, q2]},
        }
    )
    assert service._consume_pending_question(Platform.FEISHU, "chat1", "u1", "2") is True
    assert worker.answers == []  # 还有一题，先不交
    assert "第二个问题？" in service.sent[-1]
    assert service._consume_pending_question(Platform.FEISHU, "chat1", "u1", "X") is True
    assert len(worker.answers) == 1
    assert "【方案B】" in worker.answers[0] and "【X】" in worker.answers[0]


def test_unknown_chat_not_consumed(service):
    from app.gateway.base import Platform

    assert service._consume_pending_question(Platform.FEISHU, "other", "u1", "1") is False


def test_slash_command_not_consumed(service):
    from app.gateway.base import Platform

    service._engine = _FakeEngine(_FakeWorker())
    service._on_question_asked(
        {
            "platform": Platform.FEISHU,
            "chat_id": "chat1",
            "payload": {"tool_call_id": "t1", "questions": [Q_SINGLE]},
        }
    )
    assert service._consume_pending_question(Platform.FEISHU, "chat1", "u1", "/stop") is False


def test_timeout_skips_remaining(service, monkeypatch):
    from app.gateway.base import Platform

    worker = _FakeWorker()
    service._engine = _FakeEngine(worker)
    service._QUESTION_TIMEOUT_SEC = 300
    q2 = {"question": "第二个？"}
    service._on_question_asked(
        {
            "platform": Platform.FEISHU,
            "chat_id": "chat1",
            "payload": {"tool_call_id": "t1", "questions": [Q_SINGLE, q2]},
        }
    )
    service._consume_pending_question(Platform.FEISHU, "chat1", "u1", "1")
    key = service._question_key(Platform.FEISHU, "chat1")
    service._finish_question(key, service._pending_questions[key], skipped=True)
    assert len(worker.answers) == 1
    assert "第二个？" in worker.answers[0]
    assert "未回答" in worker.answers[0]


def test_stale_timer_ignored(service):
    """多题换表后旧 timer 到期不应提前收尾。"""
    from app.gateway.base import Platform

    worker = _FakeWorker()
    service._engine = _FakeEngine(worker)
    service._on_question_asked(
        {
            "platform": Platform.FEISHU,
            "chat_id": "chat1",
            "payload": {"tool_call_id": "t1", "questions": [Q_SINGLE]},
        }
    )
    key = service._question_key(Platform.FEISHU, "chat1")
    old_timer = service._pending_questions[key]["timer"]
    service._arm_question_timeout(key)  # 换表
    service._on_question_timeout(key, old_timer)
    assert key in service._pending_questions  # 旧 timer 不能收掉状态
    assert worker.answers == []


# ==================== 引擎侧工具过滤 ====================

def test_gateway_tools_keep_question_block_others():
    """Gateway 必须把 question 工具留给模型，同时挡住子智能体/待办工具。"""
    from app.core.conversation.config import INTERACTIVE_ONLY_TOOLS

    blocked = INTERACTIVE_ONLY_TOOLS - {"question"}
    names = ["question", "subagent_para", "subagent_status", "todowrite", "read", "bash"]
    tools = [{"type": "function", "function": {"name": n}} for n in names]
    kept = [
        t["function"]["name"]
        for t in tools
        if t["function"]["name"] not in blocked
    ]
    assert "question" in kept
    assert "read" in kept and "bash" in kept
    for n in ("subagent_para", "subagent_status", "todowrite"):
        assert n not in kept


def test_engine_forwards_question_asked():
    """engine 的 callback 包装层要把 question_asked 透传给 GatewayService。"""
    from PyQt5.QtCore import QObject

    from app.core.engines.gateway import engine as engine_mod

    # 只验证纯映射逻辑：直接构造 _make_gateway_callbacks 的输出字典
    class _Stub(engine_mod.GatewayEngine):
        def __init__(self):
            QObject.__init__(self)
            self._pending_queue = []
            self._saved = False

        def _save_to_store(self, session):
            self._saved = True

        def _process_next(self):
            pass

    stub = _Stub()
    got = []
    callbacks = {"question_asked": lambda payload: got.append(payload)}
    wrapped = stub._make_gateway_callbacks(None, callbacks, {})
    wrapped["question_asked"]("tc1", [{"question": "q?"}], {})
    assert got == [{"tool_call_id": "tc1", "questions": [{"question": "q?"}]}]
