# -*- coding: utf-8 -*-
"""ChatBackend 交接注入链路：pending 传递、消费标记、信号"""

from unittest.mock import MagicMock

from app.core.context.handoff import dump_front, load
from app.core.conversation.backend import ChatBackend


def _make_backend() -> ChatBackend:
    backend = ChatBackend(window_id="win_handoff_test")
    backend._tool_executor = MagicMock()
    backend._tool_executor.get_workdir.return_value = ""
    backend._session_manager = MagicMock()
    return backend


def _write_handoff(tmp_path, sid="s1"):
    path = tmp_path / f"{sid}.md"
    meta = {"session_id": sid, "updated_at": "2026-10-27 17:00:00", "injected_into": []}
    path.write_text(dump_front(meta) + "\n## 任务目标\nX\n", encoding="utf-8")
    return path


def test_build_session_context_carries_pending(tmp_path):
    backend = _make_backend()
    backend.set_pending_handoff(
        {"path": str(_write_handoff(tmp_path)), "content": "## 任务目标\nX", "source_session_id": "s1"}
    )
    ctx = backend._build_session_context("startup")
    assert ctx["handoff"]["content"] == "## 任务目标\nX"
    assert ctx["handoff"]["source_session_id"] == "s1"


def test_build_session_context_without_pending():
    backend = _make_backend()
    ctx = backend._build_session_context("startup")
    assert "handoff" not in ctx


def test_finalize_clears_pending_marks_file_and_emits(tmp_path):
    backend = _make_backend()
    path = _write_handoff(tmp_path)
    received = []
    backend.handoff_injected.connect(received.append)

    backend.set_pending_handoff({"path": str(path), "content": "X", "source_session_id": "s1"})
    session = MagicMock()
    session.session_id = "new-1"
    backend._finalize_pending_handoff(session)

    assert backend._pending_handoff is None
    assert received == ["new-1"]
    assert load(path)["meta"]["injected_into"] == ["new-1"]


def test_finalize_without_pending_is_noop(tmp_path):
    backend = _make_backend()
    received = []
    backend.handoff_injected.connect(received.append)
    backend._finalize_pending_handoff(MagicMock(session_id="new-2"))
    assert received == []


def test_set_pending_handoff_none_clears():
    backend = _make_backend()
    backend.set_pending_handoff({"path": "x", "content": "y", "source_session_id": "s1"})
    backend.set_pending_handoff(None)
    assert backend._pending_handoff is None
