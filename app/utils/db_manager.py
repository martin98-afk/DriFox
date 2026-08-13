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

    # 🛡️ P4（T4-TOP6）：写路径批量事务——延迟提交攒批窗口（秒）。
    # SQLite 同步写每次 commit 是主线程卡顿源（WAL+NORMAL 已缓解未消除）。
    # 写操作挂起 commit，攒批窗口内多次写合并为一次 commit（批量事务），
    # 窗口 ≤1s 保证崩溃丢失范围合理；flush()/close()/backup_to() 同步落盘。
    COMMIT_DELAY_SECONDS = 0.5

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._conn = None
            cls._instance._db_path = None
            # 读锁（共享）和写锁（排他）分离，提升并发读性能
            cls._instance._read_lock = threading.Lock()
            cls._instance._write_lock = threading.Lock()
            cls._instance._wal_mode_enabled = False
            # P4：延迟提交状态（_pending_commit=True 表示存在未提交写；
            # _commit_timer 为攒批提交定时器，到期批量 commit）
            cls._instance._pending_commit = False
            cls._instance._commit_timer: Optional[threading.Timer] = None
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
            # [审查 #8r] 主连接写锁等待超时：后台独立连接 VACUUM 持排他锁期间，
            # 主线程写若立即失败会抛 SQLITE_BUSY（写路径有 try/except 不崩溃但丢一次
            # 保存）。5s 等待让主线程写事务在锁释放后自动重试，避免偶发丢写。
            try:
                cursor = self._conn.cursor()
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()
            except Exception as e:
                logger.debug(f"[DatabaseManager] busy_timeout 设置异常（非致命）: {e}")
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
                # 🛡️ P4：关闭前必须先同步提交挂起的写事务（flush），
                # 否则攒批窗口内的写会因连接关闭而丢失（硬约束：退出路径不丢数据）
                # 🛡️ P4-C1-R1（devil-advocate 复核）：flush 失败时不得静默关闭——
                # SQLite 关闭连接会回滚未提交事务，退出路径全部挂起写一次性丢失
                # 且无重试机会（比运行中失败更严重）。失败时保留连接（pending
                # 保留），由下次 close/退出链重试；至少显式 error 日志暴露。
                if not self.flush():
                    logger.error(
                        "[DatabaseManager] close 前 flush 失败：挂起写保留，连接未关闭（待重试）"
                    )
                    return
                # 关闭前确保 WAL checkpoint
                cursor = self._conn.cursor()
                cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                logger.error(f"[DatabaseManager] close 异常: {e}")
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

        🛡️ P4（T4-TOP6）：写路径批量事务——DML（INSERT/UPDATE/DELETE/REPLACE）
        执行后不再立即 commit，而是挂起（_pending_commit=True）并启动攒批
        timer（≤1s），攒批窗口内多次写合并为一次 commit，减少 SQLite 提交
        次数（主线程写耗时降低）。安全性：
        - 每条 DML 用 SAVEPOINT 包裹：单条失败只回滚该条，不丢攒批内已成功写
        - flush() / close() / backup_to() 同步落盘（硬约束：退出不丢数据）
        - 非 DML 写（CREATE/DROP/ALTER/VACUUM 等）保持立即 commit
          （DDL 低频且部分语句（如 VACUUM）不能在事务中执行）
        - SELECT/PRAGMA 读路径不变（同连接可读到自身未提交写，语义一致）

        result 类型取决于语句:
          - SELECT / PRAGMA: List[Dict] （fetchall）
          - INSERT / UPDATE / DELETE: int （cursor.rowcount）
        """
        if not self._conn:
            return False, "未连接数据库"

        stripped_sql = sql.strip().upper()
        is_read = stripped_sql.startswith("SELECT") or stripped_sql.startswith("PRAGMA")
        # 仅高频 DML 走延迟提交攒批；DDL/其他写立即 commit（保持原语义）
        is_deferrable_write = (
            not is_read
            and (
                stripped_sql.startswith("INSERT")
                or stripped_sql.startswith("UPDATE")
                or stripped_sql.startswith("DELETE")
                or stripped_sql.startswith("REPLACE")
            )
        )
        # 🛡️ P4：VACUUM / incremental_vacuum 等语句不能在事务内执行
        # （攒批窗口可能有未提交写）。执行前先同步提交挂起事务。
        needs_clean_tx = "VACUUM" in stripped_sql or "INCREMENTAL_VACUUM" in stripped_sql
        if needs_clean_tx:
            self.flush()
        lock = self._read_lock if is_read else self._write_lock

        with lock:
            try:
                cursor = self._conn.cursor()
                if not is_read:
                    if is_deferrable_write:
                        # SAVEPOINT：单条失败只回滚本条，不影响攒批内其他已成功写
                        cursor.execute("SAVEPOINT sp_exec")
                cursor.execute(sql, params)
                if not is_read:
                    if is_deferrable_write:
                        # 🐛 修复：必须在 RELEASE 前捕获 rowcount。RELEASE SAVEPOINT
                        # 是 DML 之外的语句，执行后会重置 cursor.rowcount 为 -1，
                        # 导致 DML 实际影响行数丢失（如 UPDATE 0 行变成 -1），
                        # 依赖 rowcount==0 做兜底判断的调用方（如
                        # KeyDocumentsRepository.set_working_directory 的自动插入）
                        # 永不触发 → 新项目默认工作目录从未写入 key_documents，
                        # 关键文档列表为空。
                        affected = int(cursor.rowcount)
                        cursor.execute("RELEASE sp_exec")
                        self._schedule_commit()
                        return True, affected
                    # DDL / VACUUM 等：立即提交（部分语句不能留在事务中）
                    self._conn.commit()
                    return True, int(cursor.rowcount)
                rows = cursor.fetchall()
                return True, [dict(row) for row in rows]
            except Exception as e:
                if not is_read:
                    if is_deferrable_write:
                        # 只回滚当前语句，保留攒批内其他已成功写
                        try:
                            cursor.execute("ROLLBACK TO sp_exec")
                            cursor.execute("RELEASE sp_exec")
                        except Exception:
                            self._conn.rollback()
                    else:
                        self._conn.rollback()
                return False, str(e)

    # ── P4：延迟提交（批量事务）────────────────────────

    def _schedule_commit(self):
        """挂起一次批量提交：标记 pending + 启动攒批 timer（若未在运行）。

        攒批窗口：COMMIT_DELAY_SECONDS（0.5s ≤ 1s）。窗口内所有写合并为
        一次 commit；timer 已运行时复用（不重置窗口），到期统一提交。
        """
        self._pending_commit = True
        timer = self._commit_timer
        if timer is not None and timer.is_alive():
            return  # 已有 timer 在跑，复用攒批窗口
        self._start_timer()

    def _start_timer(self):
        new_timer = threading.Timer(self.COMMIT_DELAY_SECONDS, self._commit_pending)
        new_timer.daemon = True
        self._commit_timer = new_timer
        new_timer.start()

    def _commit_pending(self):
        """timer 回调：持写锁批量提交挂起的写事务（幂等）"""
        try:
            with self._write_lock:
                self._commit_now()
                # 🛡️ 竞态保险：timer 提交完成瞬间又有新写挂起（schedule 因
                # timer is_alive 被跳过）时，重新启动 timer 保证这批新写
                # 也在攒批窗口内提交，不被无限挂起。
                if self._pending_commit and self._conn is not None:
                    self._start_timer()
        except Exception:
            pass

    def _commit_now(self) -> bool:
        """立即提交挂起的写事务（须持写锁调用）。

        Returns:
            bool: True=提交成功（或无 pending）；False=commit 失败，挂起写保留。

        🛡️ P4-C1（devil-advocate 挑刺）：commit 失败时**不得清除 pending**、
        不得静默丢弃——execute_sql 的 DML 路径早已返回 True，调用方无法感知
        异步落盘失败。失败保留 pending + 升级 error 日志，由下一次
        flush()/timer 重试；若重试仍失败则持续 error 暴露，绝不静默丢数据。
        """
        if self._pending_commit and self._conn is not None:
            try:
                self._conn.commit()
            except Exception as e:
                logger.error(f"[DatabaseManager] 批量提交失败，挂起写保留待重试: {e}")
                self._commit_timer = None
                return False
            self._pending_commit = False
        self._commit_timer = None
        return True

    def flush(self) -> bool:
        """立即同步提交所有挂起的写事务（幂等，无 pending 时为空操作）。

        🛡️ P4 硬约束：任何显式 flush() / 连接关闭 / 应用退出路径必须同步
        落盘——本方法保证攒批窗口内的写立即持久化，flush 后数据立即可读。

        Returns:
            bool: True=全部提交成功；False=commit 失败（挂起写保留待重试）。
            返回 False 供调用方感知落盘失败（旧调用不检查返回值，兼容）。
        """
        if self._conn is None:
            return True
        with self._write_lock:
            return self._commit_now()

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
            # 🛡️ P4：备份前先 flush——攒批窗口内的写若不提交，backup API
            # 会漏掉（备份的是已提交数据），导致备份不完整。
            # 🛡️ P4-C1-R2（devil-advocate 复核）：flush 失败时中止备份并返回
            # 失败，绝不产出"缺攒批数据的假成功备份"（破坏备份信任）。
            if not self.flush():
                return False, "批量提交失败，备份中止（挂起写未落盘）"
            backup_conn = sqlite3.connect(target_path)
            self._conn.backup(backup_conn)
            backup_conn.close()
            return True, f"备份成功: {target_path}"
        except Exception as e:
            return False, str(e)
