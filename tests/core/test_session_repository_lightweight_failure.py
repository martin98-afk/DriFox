# -*- coding: utf-8 -*-
"""SessionRepository.get_all_lightweight 查询失败语义回归测试。

根因背景（2026-10-31 启动空历史）：启动瞬时 disk I/O error 时
get_all_lightweight 把失败静默转成空列表 []，上层 HistoryManager 无法区分
「查询失败」与「数据库真的没有会话」，把空列表固化成已加载态，导致整个
会话期历史列表为空。修复后契约：失败返回 None，合法空库返回 []。
"""

from unittest.mock import MagicMock

def _make_repo():
    from app.core.store.session_repository import SessionRepository

    db = MagicMock()
    db.is_connected = True
    return SessionRepository(db), db

def test_get_all_lightweight_sql_failure_returns_none():
    """SQL 执行失败（execute_sql 返回 False）必须返回 None，不得伪装成空列表。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (False, "disk I/O error")

    assert repo.get_all_lightweight(limit=100) is None

def test_get_all_lightweight_exception_returns_none():
    """查询抛异常必须返回 None，不得伪装成空列表。"""
    repo, db = _make_repo()
    db.execute_sql.side_effect = RuntimeError("database is locked")

    assert repo.get_all_lightweight(limit=100) is None

def test_get_all_lightweight_uninitialized_returns_none():
    """连接未就绪必须返回 None（与「合法空库」区分）。"""
    repo, db = _make_repo()
    db.is_connected = False

    assert repo.get_all_lightweight(limit=100) is None

def test_get_all_lightweight_empty_db_returns_empty_list():
    """真实空库（查询成功、零行）必须返回 []，保持空列表 = 无数据的语义。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (True, [])

    assert repo.get_all_lightweight(limit=100) == []
