# -*- coding: utf-8 -*-
"""
项目笔记仓储模块 - 专门负责项目 Markdown 内容的持久化
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import hashlib

from loguru import logger

from app.core.project_notes_manager import INITIAL_TEMPLATE


class ProjectNotesRepository:
    """项目笔记数据仓储"""

    TABLE_NAME = "project_notes"
    DB_FILENAME = "sessions.db"

    def __init__(self, db_manager):
        self._db = db_manager

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def _execute(self, sql: str, params: tuple = ()) -> Tuple[bool, Any]:
        """执行 SQL（内部使用）"""
        if not self._db:
            return False, "数据库未初始化"
        return self._db.execute_sql(sql, params)

    def _ensure_table(self) -> bool:
        """确保表存在"""
        if not self.is_initialized:
            return False
        try:
            success, _ = self._execute(f'''
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id TEXT PRIMARY KEY,
                    project TEXT UNIQUE NOT NULL,
                    content TEXT DEFAULT '',
                    updated_at TEXT
                )
            ''')
            return success
        except Exception as e:
            logger.error(f"[ProjectNotesRepository] 创建表失败: {e}")
            return False

    def save(self, project: str, content: str = "") -> bool:
        """
        保存或更新项目笔记
        
        Args:
            project: 项目名称
            content: Markdown 内容
        
        Returns:
            bool: 保存是否成功
        """
        if not self._ensure_table():
            return False
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 检查是否存在
        success, rows = self._execute(
            f'SELECT id FROM {self.TABLE_NAME} WHERE project = ?',
            (project,)
        )
        
        if success and rows and len(rows) > 0:
            # 更新
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET content = ?, updated_at = ? WHERE project = ?',
                (content, now, project)
            )
        else:
            # 新增
            note_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{hashlib.md5(project.encode()).hexdigest()[:8]}"
            success, _ = self._execute(f'''
                INSERT INTO {self.TABLE_NAME} (id, project, content, updated_at)
                VALUES (?, ?, ?, ?)
            ''', (note_id, project, content, now))
        
        return success

    def get(self, project: str) -> Optional[Dict]:
        """
        获取指定项目的笔记
        
        Args:
            project: 项目名称
        
        Returns:
            Optional[Dict]: 笔记数据，不存在返回 None
        """
        if not self.is_initialized:
            return None
        
        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} WHERE project = ?',
                (project,)
            )
            if success and rows and len(rows) > 0:
                row = rows[0]
                d = {k: row[k] for k in row.keys()} if hasattr(row, 'keys') else dict(row)
                return {
                    "id": d.get("id", ""),
                    "project": d.get("project", ""),
                    "content": d.get("content", ""),
                    "updated_at": d.get("updated_at", ""),
                }
            return None
        except Exception as e:
            logger.error(f"[ProjectNotesRepository] get 异常: {e}")
            return None

    def get_or_create(self, project: str) -> Dict:
        """
        获取或创建项目笔记

        Args:
            project: 项目名称

        Returns:
            Dict: 笔记数据
        """
        note = self.get(project)
        if note is None:
            # 新建项目时自动填入初始开发规范模板
            # (复用 project_notes_manager.INITIAL_TEMPLATE, 单一来源)
            initial_content = INITIAL_TEMPLATE
            self.save(project, initial_content)
            return self.get(project) or {
                "id": "",
                "project": project,
                "content": initial_content,
                "updated_at": "",
            }
        return note

    def get_all_projects(self) -> List[str]:
        """
        获取所有有笔记的项目
        
        Returns:
            List[str]: 项目名称列表
        """
        if not self.is_initialized:
            return []
        
        try:
            success, rows = self._execute(
                f'SELECT DISTINCT project FROM {self.TABLE_NAME} ORDER BY updated_at DESC'
            )
            if success and rows:
                return [row[0] if isinstance(row, tuple) else row.get("project", "") for row in rows]
            return []
        except Exception as e:
            logger.error(f"[ProjectNotesRepository] get_all_projects 异常: {e}")
            return []

    def delete(self, project: str) -> bool:
        """
        删除项目笔记
        
        Args:
            project: 项目名称
        
        Returns:
            bool: 删除是否成功
        """
        if not self.is_initialized:
            return False
        
        try:
            success, _ = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE project = ?',
                (project,)
            )
            return success
        except Exception as e:
            logger.error(f"[ProjectNotesRepository] delete 异常: {e}")
            return False