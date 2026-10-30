# -*- coding: utf-8 -*-
"""InputHistoryRepository.get_all 查询失败语义回归测试。

根因背景（2026-10-31 启动后输入历史空、发消息才恢复）：启动瞬时 disk I/O
error 时 get_all 把失败静默转成空列表 []，宿主把空列表装进输入框，上下键
历史为空；直到用户发消息触发重拉才恢复。修复后契约：失败返回 None，合法
空历史返回 []。
"""

from unittest.mock import MagicMock

def _make_repo():
    from app.core.store.input_history_repo import InputHistoryRepository

    db = MagicMock()
    db.is_connected = True
    # 绕过构造期迁移（ALTER TABLE 对 MagicMock 返回 (False, ...) 且吞异常，无需处理）
    return InputHistoryRepository(db), db

def test_get_all_sql_failure_returns_none():
    """SQL 失败必须返回 None，不得伪装成空历史。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (False, "disk I/O error")

    assert repo.get_all(limit=50) is None

def test_get_all_exception_returns_none():
    """查询抛异常必须返回 None（上层 try 会捕获，但契约上不返回空列表）。"""
    repo, db = _make_repo()
    db.execute_sql.side_effect = RuntimeError("database is locked")

    try:
        result = repo.get_all(limit=50)
    except RuntimeError:
        return  # 允许异常穿透（上层有 try）；关键是不得静默返回 []
    assert result is None

def test_get_all_empty_table_returns_empty_list():
    """合法空历史必须返回 []（与失败 None 区分）。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (True, [])

    assert repo.get_all(limit=50) == []

def test_get_all_parses_attachments():
    """正常行解析：content → text，attachments JSON 解析，非法 JSON 回落空列表。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (
        True,
        [
            {"content": "带附件", "attachments": '["a.png"]'},
            {"content": "坏附件", "attachments": "not-json"},
            {"content": "无附件", "attachments": None},
        ],
    )

    entries = repo.get_all(limit=50)
    assert entries[0] == {"text": "带附件", "attachments": ["a.png"]}
    assert entries[1] == {"text": "坏附件", "attachments": []}
    assert entries[2] == {"text": "无附件", "attachments": []}
