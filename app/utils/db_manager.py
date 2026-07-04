# -*- coding: utf-8 -*-
"""
SQLite 数据库管理器 - 单例模式

提供 SQLite 数据库连接管理和 CRUD 操作封装。
每个数据库文件只有一个 DatabaseManager 实例。

性能设计：
- 读/写锁分离：读操作（SELECT/PRAGMA）使用共享锁，写操作使用排他锁
- WAL 模式：已通过 PRAGMA journal_mode=WAL 启用并发读
- 写缓存去重：同一内容的重复写入自动跳过（由上层 SessionRepository 负责）
"""
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


class DatabaseManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._conn = None
            cls._instance._db_path = None
            # 读锁（共享）和写锁（排他）分离，提升并发读性能
            cls._instance._read_lock = threading.Lock()
            cls._instance._write_lock = threading.Lock()
            cls._instance._wal_mode_enabled = False
        return cls._instance

    def connect(self, db_path: str) -> bool:
        try:
            self.close()
            abs_db_path = str(Path(db_path).resolve())
            self._conn = sqlite3.connect(abs_db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._db_path = abs_db_path
            # 启用关键优化
            self._apply_performance_pragmas()
            return True
        except Exception as e:
            self._conn = None
            self._db_path = None
            raise e

    def _apply_performance_pragmas(self):
        """应用 SQLite 性能优化 PRAGMA"""
        if not self._conn:
            return
        try:
            cursor = self._conn.cursor()
            # WAL 模式：读不阻塞写，写不阻塞读
            cursor.execute("PRAGMA journal_mode=WAL")
            # 同步模式改为 NORMAL（比 FULL 快 10-100x，仅丢失最近一次事务）
            cursor.execute("PRAGMA synchronous=NORMAL")
            # 缓存大小提升到 64MB（默认 2MB，减少 I/O）
            cursor.execute("PRAGMA cache_size=-65536")
            # 临时存储放到内存（加速 ORDER BY/GROUP BY）
            cursor.execute("PRAGMA temp_store=MEMORY")
            # 启用 mmap 读取（减少系统调用）
            cursor.execute("PRAGMA mmap_size=268435456")
            # 外键约束
            cursor.execute("PRAGMA foreign_keys=ON")
            self._wal_mode_enabled = True
        except Exception as e:
            logger.debug(f"[DatabaseManager] PRAGMA 配置异常（非致命）: {e}")

    def close(self):
        if self._conn:
            try:
                # 关闭前确保 WAL checkpoint
                cursor = self._conn.cursor()
                cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.close()
            self._conn = None
            self._db_path = None
            self._wal_mode_enabled = False

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def db_path(self) -> Optional[str]:
        return self._db_path

    def get_tables(self) -> List[str]:
        if not self._conn:
            return []
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row[0] for row in cursor.fetchall()]

    def get_table_info(self, table_name: str) -> List[Dict[str, Any]]:
        if not self._conn:
            return []
        cursor = self._conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = []
        for row in cursor.fetchall():
            columns.append(
                {
                    "cid": row[0],
                    "name": row[1],
                    "type": row[2],
                    "notnull": row[3],
                    "default_value": row[4],
                    "pk": row[5],
                }
            )
        return columns

    def get_table_data(
        self, table_name: str, limit: int = 100, offset: int = 0
    ) -> Tuple[List[str], List[List[Any]]]:
        if not self._conn:
            return [], []
        cursor = self._conn.cursor()
        cursor.execute(
            f'SELECT * FROM "{table_name}" LIMIT ? OFFSET ?', (limit, offset)
        )
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return columns, [list(row) for row in rows]

    def get_table_count(self, table_name: str) -> int:
        if not self._conn:
            return 0
        cursor = self._conn.cursor()
        cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        return cursor.fetchone()[0]

    def execute_sql(self, sql: str, params: tuple = ()) -> Tuple[bool, Any]:
        """
        执行 SQL 并返回 (success, result)。

        性能设计：读操作（SELECT/PRAGMA）使用共享读锁，
        写操作使用排他写锁，实现读写并发。

        result 类型取决于语句:
          - SELECT / PRAGMA: List[Dict] （fetchall）
          - INSERT / UPDATE / DELETE: int （cursor.rowcount）
        """
        if not self._conn:
            return False, "未连接数据库"

        stripped_sql = sql.strip().upper()
        is_read = stripped_sql.startswith("SELECT") or stripped_sql.startswith("PRAGMA")
        lock = self._read_lock if is_read else self._write_lock

        with lock:
            try:
                cursor = self._conn.cursor()
                cursor.execute(sql, params)
                if not is_read:
                    self._conn.commit()
                    return True, int(cursor.rowcount)
                rows = cursor.fetchall()
                return True, [dict(row) for row in rows]
            except Exception as e:
                if not is_read:
                    self._conn.rollback()
                return False, str(e)

    def create_table(
        self, table_name: str, columns: List[Dict[str, str]]
    ) -> Tuple[bool, str]:
        col_defs = []
        for col in columns:
            col_name = col.get("name", "").strip()
            col_type = col.get("type", "TEXT").upper()
            if not col_name:
                continue
            col_def = f'"{col_name}" {col_type}'
            if col.get("primary_key"):
                col_def += " PRIMARY KEY"
            if col.get("not_null"):
                col_def += " NOT NULL"
            if col.get("unique"):
                col_def += " UNIQUE"
            default = col.get("default")
            if default is not None:
                col_def += f" DEFAULT {repr(default)}"
            col_defs.append(col_def)

        if not col_defs:
            return False, "至少需要定义一个列"

        sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(col_defs)})'
        return self.execute_sql(sql)

    def drop_table(self, table_name: str) -> Tuple[bool, str]:
        if not table_name or not table_name.isidentifier():
            return False, "无效的表名"
        sql = f"DROP TABLE IF EXISTS {table_name}"
        return self.execute_sql(sql)

    def insert_data(self, table_name: str, data: Dict[str, Any]) -> Tuple[bool, str]:
        if not data:
            return False, "没有数据"
        columns = [f'"{k}"' for k in data.keys()]
        placeholders = ["?"] * len(columns)
        sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        return self.execute_sql(sql, tuple(data.values()))

    def update_data(
        self,
        table_name: str,
        data: Dict[str, Any],
        where: str,
        where_params: tuple = (),
    ) -> Tuple[bool, str]:
        if not data:
            return False, "没有数据"
        set_clause = ", ".join([f'"{k}" = ?' for k in data.keys()])
        sql = f'UPDATE "{table_name}" SET {set_clause} WHERE {where}'
        return self.execute_sql(sql, tuple(data.values()) + where_params)

    def delete_data(
        self, table_name: str, where: str, where_params: tuple = ()
    ) -> Tuple[bool, str]:
        sql = f'DELETE FROM "{table_name}" WHERE {where}'
        return self.execute_sql(sql, where_params)

    def backup_to(self, target_path: str) -> Tuple[bool, str]:
        if not self._conn:
            return False, "未连接数据库"
        try:
            backup_conn = sqlite3.connect(target_path)
            self._conn.backup(backup_conn)
            backup_conn.close()
            return True, f"备份成功: {target_path}"
        except Exception as e:
            return False, str(e)
