# -*- coding: utf-8 -*-
"""project_workdir 仓储契约测试

背景：工作目录原先寄生在 key_documents 表（is_working_dir 标记）。关键文档功能
下线后由 project_workdir 独立承载，**老用户已有的工作目录必须迁移过来**，否则
升级后所有项目都显示「未设置工作目录」。

覆盖：
- 老库迁移：is_working_dir=1 的记录搬进新表
- 幂等：重复 ensure_table 不重复搬运、不覆盖用户改动
- 全新库（无 key_documents）不报错
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _make_legacy_db(db_dir: str, rows) -> None:
    """造一个老库：只有 key_documents 表（含 is_working_dir 列）"""
    con = sqlite3.connect(os.path.join(db_dir, "sessions.db"))
    con.execute(
        """
        CREATE TABLE key_documents (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_name TEXT,
            added_at TEXT,
            added_by TEXT DEFAULT 'manual',
            is_working_dir INTEGER DEFAULT 0,
            UNIQUE(project, file_path)
        )
        """
    )
    for idx, (project, path, is_wd) in enumerate(rows):
        con.execute(
            "INSERT INTO key_documents (id, project, file_path, file_name, is_working_dir)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"id{idx}", project, path, os.path.basename(path), is_wd),
        )
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def reset_legacy_flag():
    """迁移是进程内一次性的（类级标记），测试间必须重置否则互相污染"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    ProjectWorkdirRepository._legacy_synced = False
    yield
    ProjectWorkdirRepository._legacy_synced = False


@pytest.fixture
def store(tmp_path):
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    s = SessionStore(str(tmp_path))
    assert s.is_initialized
    yield s
    try:
        s.close()
    except Exception:
        pass


def test_migrates_legacy_working_dirs(tmp_path):
    """老库 is_working_dir=1 的记录迁到 project_workdir"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository
    from app.core.store.session_store import SessionStore

    db_dir = str(tmp_path / "db")
    os.makedirs(db_dir, exist_ok=True)
    _make_legacy_db(
        db_dir,
        [
            ("项目A", "D:/work/a", 1),
            ("项目B", "D:/work/b", 1),
            ("项目C", "D:/work/c", 0),  # 非工作目录，不该迁移
        ],
    )

    SessionStore._instance = None
    s = SessionStore(db_dir)
    assert s.is_initialized
    try:
        repo = ProjectWorkdirRepository(s._db)
        assert repo.ensure_table() is True
        data = repo.list_all()
        assert data.get("项目A") == "D:/work/a"
        assert data.get("项目B") == "D:/work/b"
        assert "项目C" not in data, "is_working_dir=0 的条目不该被当成工作目录"
    finally:
        try:
            s.close()
        except Exception:
            pass


def test_migration_is_idempotent_and_respects_user_edits(store):
    """重复 ensure_table 不重复搬运，也不覆盖用户后来改的值"""
    import shutil
    import tempfile

    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    # 直接用当前 store 的库造老表（store fixture 已初始化 sessions.db）
    store._db.execute_sql(
        """
        CREATE TABLE IF NOT EXISTS key_documents (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, file_path TEXT NOT NULL,
            file_name TEXT, added_at TEXT, added_by TEXT DEFAULT 'manual',
            is_working_dir INTEGER DEFAULT 0, UNIQUE(project, file_path)
        )
        """
    )
    store._db.execute_sql(
        "INSERT OR REPLACE INTO key_documents (id, project, file_path, is_working_dir)"
        " VALUES ('x1', 'P1', 'D:/orig', 1)"
    )

    repo = ProjectWorkdirRepository(store._db)
    assert repo.ensure_table() is True
    assert repo.get("P1") == "D:/orig"

    # 用户在新页面改成别的目录
    assert repo.set("P1", "D:/changed") is True
    assert repo.get("P1") == "D:/changed"

    # 再 ensure 一次（重置进程内标记，模拟下次启动）：同步重跑，但老值不得覆盖
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    ProjectWorkdirRepository._legacy_synced = False
    assert repo.ensure_table() is True
    assert repo.get("P1") == "D:/changed", "重复同步不得覆盖用户改动"


def test_late_legacy_rows_are_picked_up_on_next_start(store):
    """用户回退旧版本新建了项目 → 下次启动新版本要能把它带过来

    这正是「打一次性标记」方案会漏掉的情况：老版本继续往 key_documents 写，
    新表却没有。增量同步（ON CONFLICT DO NOTHING）能补上且不覆盖已有值。
    """
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    store._db.execute_sql(
        """
        CREATE TABLE IF NOT EXISTS key_documents (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, file_path TEXT NOT NULL,
            file_name TEXT, added_at TEXT, added_by TEXT DEFAULT 'manual',
            is_working_dir INTEGER DEFAULT 0, UNIQUE(project, file_path)
        )
        """
    )
    repo = ProjectWorkdirRepository(store._db)
    repo.ensure_table()
    assert repo.get("老项目") is None

    # 模拟旧版本程序又写了一条工作目录
    store._db.execute_sql(
        "INSERT OR REPLACE INTO key_documents (id, project, file_path, is_working_dir)"
        " VALUES ('x9', '老项目', 'D:/old/late', 1)"
    )

    # 下次启动（重置进程内标记）应能同步进来
    ProjectWorkdirRepository._legacy_synced = False
    repo.ensure_table()
    assert repo.get("老项目") == "D:/old/late"

    # 而已有值仍不被覆盖
    repo.set("老项目", "D:/new")
    ProjectWorkdirRepository._legacy_synced = False
    repo.ensure_table()
    assert repo.get("老项目") == "D:/new"


def test_legacy_table_without_is_working_dir_column(store):
    """极老的库 key_documents 没有 is_working_dir 列 → 静默跳过，不抛异常"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    store._db.execute_sql(
        """
        CREATE TABLE IF NOT EXISTS key_documents (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, file_path TEXT NOT NULL,
            file_name TEXT, added_at TEXT, added_by TEXT DEFAULT 'manual'
        )
        """
    )
    store._db.execute_sql(
        "INSERT INTO key_documents (id, project, file_path) VALUES ('y1', 'P', 'D:/x')"
    )
    repo = ProjectWorkdirRepository(store._db)
    assert repo.ensure_table() is True
    assert repo.list_all() == {}


def test_fresh_db_without_key_documents(store):
    """全新用户库里没有 key_documents 表：迁移静默跳过，不报错"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    repo = ProjectWorkdirRepository(store._db)
    assert repo.ensure_table() is True
    assert repo.list_all() == {}
    # 正常读写不受影响
    assert repo.set("P", "D:/x") is True
    assert repo.get("P") == "D:/x"


def test_basic_crud(store):
    """set / get / clear / is_workdir 基本语义"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    repo = ProjectWorkdirRepository(store._db)
    assert repo.get("P") is None
    assert repo.set("P", "D:/a/b") is True
    assert repo.get("P") == "D:/a/b"
    assert repo.is_workdir("P", "D:\\a\\b") is True, "反斜杠应归一化后比较相等"
    assert repo.is_workdir("P", "D:/other") is False
    assert repo.clear("P") is True
    assert repo.get("P") is None


def test_set_clear_keyword(store):
    """path 传 "clear" 等价于清除（老调用点依赖这个约定）"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    repo = ProjectWorkdirRepository(store._db)
    repo.set("P", "D:/a")
    assert repo.get("P") == "D:/a"
    repo.set("P", "clear")
    assert repo.get("P") is None
