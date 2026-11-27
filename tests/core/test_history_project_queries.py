"""真实 SQLite 验证历史项目清单和按项目计数的兼容性。"""
import sqlite3
from contextlib import closing

from app.core.store.session_repository import SessionRepository


class _Db:
    is_connected = True

    def __init__(self, conn):
        self.conn = conn

    def execute_sql(self, sql, params):
        return True, self.conn.execute(sql, params).fetchall()


def test_project_queries_preserve_archives_empty_projects_and_null_ids():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.executescript('''
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project TEXT);
            CREATE INDEX idx_project ON sessions(project);
            CREATE TABLE project_workdir (project TEXT PRIMARY KEY);
        ''')
        repo = SessionRepository(_Db(conn))
        assert repo.get_projects() == ['默认项目']
        assert repo.get_session_counts() == {}
        conn.executemany('INSERT INTO sessions VALUES (?,?)', [
            ('a', '项目甲'), ('b', '项目甲'), (None, '项目甲'),
            ('c', '__archived__旧项目'), ('d', None), (None, '只有空编号'),
        ])
        conn.executemany('INSERT INTO project_workdir VALUES (?)', [('项目甲',), ('未开始',)])
        assert repo.get_projects() == sorted(['项目甲', '未开始', '只有空编号'])
        assert repo.get_session_counts() == {'项目甲': 2, '只有空编号': 0}
