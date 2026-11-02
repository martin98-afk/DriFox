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
        """建表（幂等）"""
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
            return bool(success)
        except Exception as e:
            logger.error(f"[ProjectWorkdir] 创建表失败: {e}")
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
