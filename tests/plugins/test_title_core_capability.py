# -*- coding: utf-8 -*-
"""标题能力池：事件常量 / catalog 登记 / commit provider 契约。"""


def test_title_event_constant():
    from app.core.infra.ui_event_bus import EV_TITLE_REQUEST

    assert EV_TITLE_REQUEST == "title.request"


def test_only_one_title_capability_registered():
    from app.plugins.capabilities.catalog import CATALOG

    assert "session.title.commit" in CATALOG
    assert CATALOG["session.title.commit"].group == "session"
    assert "ui" in CATALOG["session.title.commit"].surfaces
    assert CATALOG["session.title.commit"].value_style == "factory"
    assert "session.title.snapshot" not in CATALOG
    assert "session.title.cancel" not in CATALOG


class _RecordingHistory:
    def __init__(self):
        self.saved = []
        self.updated = []

    def find_index_by_session_id(self, sid):
        return 1 if sid == "s-1" else None

    def save_session(self, messages, **kw):
        self.saved.append(kw)

    def update_topic_summary(self, idx, title):
        self.updated.append((idx, title))


class _TitleEdit:
    def __init__(self):
        self.text = ""

    def setText(self, value):
        self.text = value


class _CommitSession:
    session_id = "s-1"
    messages = [{"role": "user", "content": "x"}]
    compaction_state = {}
    compaction_cache = {}

    def __init__(self, user_edited, is_temp):
        self.user_edited_title = user_edited
        self.is_temp = is_temp
        self.topic_summary = ""

    def set_topic_summary(self, value):
        self.topic_summary = value


class _CommitBackend:
    def __init__(self, user_edited, is_temp):
        self._session = _CommitSession(user_edited, is_temp)

    def get_current_session(self):
        return self._session


class _CommitHost:
    def __init__(self, *, user_edited=False, is_temp=False, session_id="s-1"):
        self.history_manager = _RecordingHistory()
        self._current_session_id = session_id
        self._current_project = "proj-a"
        self._team_run_id = "run-7"
        self._team_name = "组A"
        self._team_agent_name = "alice"
        self.title_edit = _TitleEdit()
        self._synced = []
        self.backend = _CommitBackend(user_edited, is_temp)

    def _get_current_worktree_path(self):
        return "/repo/.worktrees/wt-a"

    def _get_team_members_snapshot_json(self):
        return "[]"

    def _resolve_session_project_fallback(self, sid, project, session=None):
        return "proj-origin"

    def _api_usage_kwargs(self, session):
        return {"prompt_tokens": 10}

    def _sync_dialog_title(self):
        self._synced.append(True)


def _commit(host, title="新标题", monkeypatch=None):
    from app.plugins.capabilities.providers import title_core

    if monkeypatch is not None:
        monkeypatch.setattr(title_core, "_resolve_window_by_id", lambda wid: host)
    return title_core._session_title_commit(host)("w-1", title)


def test_commit_user_edited_skips(monkeypatch):
    host = _CommitHost(user_edited=True)
    assert _commit(host, monkeypatch=monkeypatch) is False
    assert host.title_edit.text == ""
    assert host.history_manager.saved == []


def test_commit_temp_session_ui_only(monkeypatch):
    host = _CommitHost(is_temp=True)
    assert _commit(host, monkeypatch=monkeypatch) is True
    assert host.backend._session.topic_summary == "新标题"
    assert host.title_edit.text == "新标题"
    assert host._synced == [True]
    assert host.history_manager.saved == []


def test_commit_over_50_chars_skips(monkeypatch):
    host = _CommitHost()
    assert _commit(host, "标" * 51, monkeypatch) is False
    assert host.title_edit.text == ""


def test_commit_new_session_saves_team_metadata(monkeypatch):
    host = _CommitHost(session_id=None)
    assert _commit(host, monkeypatch=monkeypatch) is True
    kw = host.history_manager.saved[0]
    assert kw["title"] == "新标题"
    assert kw["project"] == "proj-origin"
    assert kw["team_run_id"] == "run-7"
    assert kw["team_name"] == "组A"
    assert kw["agent_name"] == "alice"
    assert "team_members" not in kw
    assert kw["worktree_path"] == "/repo/.worktrees/wt-a"
    assert host.history_manager.updated == [(1, "新标题")]


def test_commit_dead_window_returns_false(monkeypatch):
    from app.plugins.capabilities.providers import title_core

    monkeypatch.setattr(title_core, "_resolve_window_by_id", lambda wid: None)
    assert title_core._session_title_commit(_CommitHost())("gone", "新标题") is False
