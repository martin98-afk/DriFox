# -*- coding: utf-8 -*-
"""
项目笔记路由测试的共享 fixture。

设计目标：
- 完全使用临时目录/临时 sqlite, 不污染真实 .drifox
- 提供真实可用的 MemoryManagerCore 实例(注入临时 db, 隔离 _init_storage)
- 提供 _MIGRATED 集合的自动清理, 避免跨测试污染
"""

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
from unittest.mock import MagicMock


@pytest.fixture
def tmp_workdir(tmp_path) -> Iterator[Path]:
    """临时 workdir 路径"""
    workdir = tmp_path / "project_root"
    workdir.mkdir()
    yield workdir


@pytest.fixture
def tmp_db_path(tmp_path) -> Iterator[Path]:
    """临时 sqlite db 路径"""
    yield tmp_path / "test_sessions.db"


@pytest.fixture
def patched_memory_manager(tmp_db_path):
    """
    提供一个完全初始化的 MemoryManagerCore, 所有 IO 指向临时 db。

    - _sqlite_project_notes_repo / _key_documents_repo / _entry_memories_repo 都是真实实例
    - _key_documents_repo.get_working_directory 默认返回 None(无 workdir)
    - 测试可显式设置 mock return_value 来模拟 workdir
    """
    from app.core import memory_manager
    from app.core import project_notes_manager
    from app.core.store import project_notes_repository
    from app.core.store import key_documents_repository
    from app.core.store import memory_repository

    # 0) 重置全局单例
    memory_manager.MemoryManagerCore._instance = None
    project_notes_manager._MIGRATED.clear()

    # 1) 真实 sqlite 连接(每次 fixture 创建新连接, 测试间隔离)
    conn = sqlite3.connect(str(tmp_db_path))
    conn.row_factory = sqlite3.Row

    class _FakeDb:
        is_connected = True
        def execute_sql(self, sql, params=()):
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
            conn.commit()
            return True, rows

    fake_db = _FakeDb()

    # 2) 构造真实仓储(走自己的 _ensure_table)
    notes_repo = project_notes_repository.ProjectNotesRepository(fake_db)
    docs_repo = key_documents_repository.KeyDocumentsRepository(fake_db)
    entry_repo = memory_repository.MemoryRepository(fake_db)

    # 3) 跳过 _init_storage, 直接注入依赖(避免拉起真实 SessionStore)
    mgr = memory_manager.MemoryManagerCore.__new__(memory_manager.MemoryManagerCore)
    mgr._session_store = None
    mgr._db_manager = fake_db
    mgr._entry_memories_repo = entry_repo
    mgr._sqlite_project_notes_repo = notes_repo
    mgr._key_documents_repo = docs_repo

    # 4) 默认 workdir 为 None; 测试可改成 mock return_value 来模拟设置
    mgr._key_documents_repo.get_working_directory = MagicMock(return_value=None)

    yield mgr

    # 清理
    conn.close()
    memory_manager.MemoryManagerCore._instance = None
    project_notes_manager._MIGRATED.clear()
