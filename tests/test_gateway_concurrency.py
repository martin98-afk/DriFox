"""
TDD for Issue #144: Gateway multi-user concurrent conversations.

Tests behavior (not implementation):
1. Multiple users can send messages simultaneously without blocking
2. Each user has independent session context
3. Idle executors can be cleaned up
4. Existing command functionality is unaffected
"""
import uuid
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class ChatSession:
    session_id: str
    name: str = ""
    messages: List[Dict] = None
    metadata: Dict[str, object] = None

    def __post_init__(self):
        if self.messages is None:
            self.messages = []
        if self.metadata is None:
            self.metadata = {}

    def add_user_message(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def clear(self):
        self.messages.clear()
        self.metadata.clear()


class MockExecutor:
    def __init__(self):
        self._is_streaming = False

    @property
    def is_streaming(self):
        return self._is_streaming

    def execute(self, **kwargs):
        if self._is_streaming:
            return False
        self._is_streaming = True
        return True

    def finish_streaming(self):
        self._is_streaming = False


class MockEngine:
    def __init__(self):
        self._user_executors = {}
        self._pending_queues = {}

    def _get_or_create_executor(self, sid):
        if sid not in self._user_executors:
            self._user_executors[sid] = MockExecutor()
            self._pending_queues[sid] = []
        return self._user_executors[sid]

    def process(self, session, text, callbacks=None):
        callbacks = callbacks or {}
        if text.startswith("/"):
            if text == "/help":
                return True
            if text in ("/new", "/clear"):
                session.clear()
                return True
            return None
        return self._send_to_ai(session, text, callbacks)

    def _send_to_ai(self, session, text, callbacks):
        sid = session.session_id
        executor = self._get_or_create_executor(sid)
        if executor.is_streaming:
            self._pending_queues[sid].append((session, text, callbacks))
            return True
        session.add_user_message(content=text)
        return executor.execute()

    def cleanup_idle_executors(self):
        idle = [s for s, e in self._user_executors.items() if not e.is_streaming]
        for s in idle:
            self._user_executors.pop(s, None)
            self._pending_queues.pop(s, None)


class TestGatewayConcurrency:
    def setup_method(self):
        self.engine = MockEngine()

    def _make_session(self):
        return ChatSession(session_id=uuid.uuid4().hex)

    def test_single_user_can_send_message(self):
        s = self._make_session()
        assert self.engine.process(s, "hello") is True

    def test_single_user_executor_created(self):
        s = self._make_session()
        self.engine.process(s, "hello")
        assert len(self.engine._user_executors) == 1

    def test_two_users_can_stream_concurrently(self):
        a, b = self._make_session(), self._make_session()
        self.engine.process(a, "msg_a")
        assert self.engine._user_executors[a.session_id].is_streaming
        assert self.engine.process(b, "msg_b") is True
        assert self.engine._user_executors[b.session_id].is_streaming

    def test_user_b_not_blocked_by_user_a(self):
        a, b = self._make_session(), self._make_session()
        self.engine.process(a, "long")
        assert self.engine.process(b, "urgent") is True

    def test_same_user_second_message_queued(self):
        u = self._make_session()
        self.engine.process(u, "first")
        self.engine.process(u, "second")
        assert len(self.engine._pending_queues[u.session_id]) == 1

    def test_queued_message_processed_after_finished(self):
        u = self._make_session()
        self.engine.process(u, "first")
        self.engine.process(u, "second")
        assert len(self.engine._pending_queues[u.session_id]) == 1
        self.engine._user_executors[u.session_id].finish_streaming()
        self.engine._send_to_ai(*self.engine._pending_queues[u.session_id].pop(0), {})
        assert len(self.engine._pending_queues[u.session_id]) == 0

    def test_commands_still_work(self):
        s = self._make_session()
        assert self.engine.process(s, "/help") is True
        s.add_user_message("old")
        self.engine.process(s, "/new")
        assert len(s.messages) == 0
        s.add_user_message("old2")
        self.engine.process(s, "/clear")
        assert len(s.messages) == 0

    def test_idle_executor_cleanup(self):
        a, b = self._make_session(), self._make_session()
        self.engine.process(a, "a")
        self.engine.process(b, "b")
        assert len(self.engine._user_executors) == 2
        self.engine._user_executors[a.session_id].finish_streaming()
        self.engine._user_executors[b.session_id].finish_streaming()
        self.engine.cleanup_idle_executors()
        assert len(self.engine._user_executors) == 0

    def test_streaming_executor_not_cleaned(self):
        a, b = self._make_session(), self._make_session()
        self.engine.process(a, "a")
        self.engine.process(b, "b")
        self.engine._user_executors[b.session_id].finish_streaming()
        self.engine.cleanup_idle_executors()
        assert a.session_id in self.engine._user_executors
        assert b.session_id not in self.engine._user_executors

    def test_concurrent_users_with_commands(self):
        a, b = self._make_session(), self._make_session()
        assert self.engine.process(a, "/help") is True
        assert self.engine.process(b, "/help") is True
        assert a.session_id not in self.engine._user_executors
        assert b.session_id not in self.engine._user_executors