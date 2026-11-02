# -*- coding: utf-8 -*-
"""项目工作目录仓储 —— 每个项目一个当前工作目录（project → path）

历史：原先工作目录寄生在 key_documents 表的 is_working_dir 标记上（该表还承担
「项目文件/URL 关联列表」的职责）。关键文档功能下线后，工作目录需要独立的
最小落点，故单独建表。

语义约定：
- 一个项目最多一行（project 主键）
- path 为空串或行不存在都表示「未设置」，读取方统一返回 None
- 不做路径存在性校验（目录可能临时不可用/在另一台机器上），由调用方判活
"""

import os
from datetime import datetime
from typing import Optional

from loguru import logger


class ProjectWorkdirRepository:
    """项目工作目录仓储（单表、单值语义）"""

    TABLE_NAME = "project_workdir"

    def __init__(self, db_manager):
        self._db = db_manager

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def _execute(self, sql: str, params: tuple = ()):
        if not self._db:
            return False, "数据库未初始化"
        return self._db.execute_sql(sql, params)

    def ensure_table(self) -> bool:
        """建表（幂等）+ 同步老 key_documents 里的工作目录

        迁移节流：同一进程内只跑一次全表同步（见 _sync_legacy_workdirs）。
        """
        if not self.is_initialized:
            return False
        try:
            success, _ = self._execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    project TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    updated_at TEXT
                )
                """
            )
            if success:
                self._sync_legacy_workdirs()
            return bool(success)
        except Exception as e:
            logger.error(f"[ProjectWorkdir] 创建表失败: {e}")
            return False

    # 进程内迁移节流标记（类级：单例仓储语义，多实例共享）
    _legacy_synced: bool = False

    def _sync_legacy_workdirs(self) -> int:
        """进程内只跑一次的老数据同步入口（见 _migrate_from_key_documents）"""
        if ProjectWorkdirRepository._legacy_synced:
            return 0
        ProjectWorkdirRepository._legacy_synced = True
        return self._migrate_from_key_documents()

    def _migrate_from_key_documents(self) -> int:
        """把老 key_documents 表里 is_working_dir=1 的记录同步进本表（增量、幂等）。

        背景：工作目录原先寄生在 key_documents 表（is_working_dir 标记）。关键文档
        功能下线后本表独立承载工作目录，**老用户已有的工作目录必须迁过来**，否则
        升级后所有项目都变成「未设置工作目录」。

        为什么是「每次 ensure_table 都增量跑」而不是「打标记只跑一次」：
        - 老版本程序可能与本版本并存（用户回退版本、多份安装），旧版本继续往
          key_documents 写工作目录；
        - 用户可能在旧版本里新建项目、切工作目录，这些数据只在 key_documents 里；
        - 一次性标记一旦置位，之后这些新数据永远进不来。

        冲突策略：ON CONFLICT(project) DO NOTHING —— 新表里的值优先。用户在
        新页面改过的目录不会被老数据覆盖。

        Returns:
            本次真正插入的条数（无老表/无新增时为 0）
        """
        if not self.is_initialized:
            return 0
        try:
            if not self._table_exists("key_documents"):
                return 0
            # 老库早期版本可能没有 is_working_dir 列（建表后才加的迁移列）
            if not self._column_exists("key_documents", "is_working_dir"):
                return 0

            ok, rows = self._execute(
                "SELECT project, file_path FROM key_documents WHERE is_working_dir = 1"
            )
            if not ok or not rows:
                return 0

            migrated = 0
            now = datetime.now().isoformat(timespec="seconds")
            for row in rows:
                if isinstance(row, tuple):
                    project, path = row[0], row[1]
                else:
                    project, path = row.get("project", ""), row.get("file_path", "")
                project = str(project or "").strip()
                path = str(path or "").strip()
                if not project or not path:
                    continue
                normalized = path.replace("\\", "/")
                ok_ins, affected = self._execute(
                    f"""
                    INSERT INTO {self.TABLE_NAME} (project, path, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(project) DO NOTHING
                    """,
                    (project, normalized, now),
                )
                # affected 为 0 表示撞主键（新表已有值）→ 不计数也不覆盖
                if ok_ins and isinstance(affected, int) and affected > 0:
                    migrated += 1
            if migrated:
                logger.info(f"[ProjectWorkdir] 已从 key_documents 同步 {migrated} 条项目工作目录")
            return migrated
        except Exception as e:
            logger.warning(f"[ProjectWorkdir] 同步老工作目录失败（不影响新数据）: {e}")
            return 0

    def _column_exists(self, table: str, column: str) -> bool:
        """列是否存在（老库可能在建表后才加 is_working_dir 列）"""
        try:
            ok, rows = self._execute(f"PRAGMA table_info({table})")
            if not ok or not rows:
                return False
            for row in rows:
                name = row[1] if isinstance(row, tuple) else row.get("name", "")
                if str(name) == column:
                    return True
            return False
        except Exception:
            return False

    def _table_exists(self, name: str) -> bool:
        """表是否存在（老库可能根本没有 key_documents）"""
        try:
            ok, rows = self._execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
            )
            return bool(ok and rows)
        except Exception:
            return False

    def get(self, project: str) -> Optional[str]:
        """读取项目工作目录；未设置返回 None"""
        if not project or not self.is_initialized:
            return None
        if not self.ensure_table():
            return None
        try:
            success, rows = self._execute(
                f"SELECT path FROM {self.TABLE_NAME} WHERE project = ?", (project,)
            )
            if not success or not rows:
                return None
            row = rows[0]
            path = row[0] if isinstance(row, tuple) else row.get("path", "")
            path = str(path or "").strip()
            return path or None
        except Exception as e:
            logger.error(f"[ProjectWorkdir] get 异常: {e}")
            return None

    def set(self, project: str, path: str) -> bool:
        """写入/覆盖项目工作目录；path 为空或 "clear" 等价于清除"""
        if not project:
            return False
        if not path or str(path).strip().lower() == "clear":
            return self.clear(project)
        if not self.ensure_table():
            return False
        try:
            normalized = str(path).replace("\\", "/")
            success, _ = self._execute(
                f"""
                INSERT INTO {self.TABLE_NAME} (project, path, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(project) DO UPDATE SET
                    path = excluded.path,
                    updated_at = excluded.updated_at
                """,
                (project, normalized, datetime.now().isoformat(timespec="seconds")),
            )
            return bool(success)
        except Exception as e:
            logger.error(f"[ProjectWorkdir] set 异常: {e}")
            return False

    def clear(self, project: str) -> bool:
        """清除项目工作目录（不删行则写空串亦可；此处直接删行，语义最简）"""
        if not project or not self.is_initialized:
            return False
        if not self.ensure_table():
            return False
        try:
            success, _ = self._execute(f"DELETE FROM {self.TABLE_NAME} WHERE project = ?", (project,))
            return bool(success)
        except Exception as e:
            logger.error(f"[ProjectWorkdir] clear 异常: {e}")
            return False

    def delete_by_project(self, project: str) -> bool:
        """项目归档时清理（与 clear 同义，保留别名供归档路径调用）"""
        return self.clear(project)

    def list_all(self) -> dict:
        """全部项目工作目录 {project: path}（tab 面板/项目列表消费）"""
        if not self.is_initialized or not self.ensure_table():
            return {}
        try:
            success, rows = self._execute(f"SELECT project, path FROM {self.TABLE_NAME}")
            if not success or not rows:
                return {}
            out = {}
            for row in rows:
                if isinstance(row, tuple):
                    proj, path = row[0], row[1]
                else:
                    proj, path = row.get("project", ""), row.get("path", "")
                if proj:
                    out[str(proj)] = str(path or "")
            return out
        except Exception as e:
            logger.error(f"[ProjectWorkdir] list_all 异常: {e}")
            return {}

    def is_workdir(self, project: str, path: str) -> bool:
        """路径是否为该项目当前工作目录（比较时归一化分隔符与大小写）"""
        current = self.get(project)
        if not current or not path:
            return False
        return os.path.normcase(current) == os.path.normcase(str(path).replace("\\", "/"))
