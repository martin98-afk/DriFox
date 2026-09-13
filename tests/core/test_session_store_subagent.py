# -*- coding: utf-8 -*-
"""SessionStore 子智能体日志清理测试：clear_old_subagent_tasks 返回值契约 + 真删验证

回归背景：clear_old_subagent_tasks 曾为空实现（隐式返回 None），
调用方 main_widget._do_clean_subagent_logs 的 `deleted > 0` 抛
TypeError: '>' not supported between instances of 'NoneType' and 'int'。
"""

import pytest


def _fresh_store(tmp_path):
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    store = SessionStore(str(tmp_path))
    assert store.is_initialized
    return store


@pytest.fixture
def store(tmp_path):
    s = _fresh_store(tmp_path)
    yield s
    try:
        s.close()
    except Exception:
        pass
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    SessionStore._instance = None
    DatabaseManager._instance = None


def test_clear_old_subagent_tasks_returns_int(store):
    """清理方法必须返回 int（委托 SubAgentLogRepository.clear_old_tasks），不能返回 None。"""
    deleted = store.clear_old_subagent_tasks(14)
    assert isinstance(deleted, int)
    assert deleted >= 0


def test_clear_old_subagent_tasks_deletes_expired(store):
    """过期任务被真删并返回删除行数，未过期任务保留。"""
    repo = store._subagent_log_repo
    assert repo.save_task("t_old", "explore", "旧任务", status="finished")
    assert repo.save_task("t_new", "explore", "新任务", status="running")
    # 人为把 t_old 的 updated_at 拨到 30 天前
    success, _ = repo._execute(
        f'UPDATE "{repo.TABLE_NAME}" SET updated_at = ? WHERE task_id = ?',
        ("2026-08-01T00:00:00", "t_old"),
    )
    assert success

    deleted = store.clear_old_subagent_tasks(14)

    assert deleted == 1
    assert repo.get_task("t_old") is None
    assert repo.get_task("t_new") is not None
