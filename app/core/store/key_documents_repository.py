# -*- coding: utf-8 -*-
"""
关键文档仓储模块 - 专门负责项目关键文档路径的持久化
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import hashlib
import os

from loguru import logger


class KeyDocumentsRepository:
    """关键文档数据仓储"""

    TABLE_NAME = "key_documents"
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
                    project TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT,
                    added_at TEXT,
                    added_by TEXT DEFAULT 'manual',
                    is_working_dir INTEGER DEFAULT 0,
                    UNIQUE(project, file_path)
                )
            ''')
            return success
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] 创建表失败: {e}")
            return False

    def _migrate(self):
        """迁移：添加新列"""
        if not self.is_initialized:
            return
        try:
            self._execute(f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN is_working_dir INTEGER DEFAULT 0")
        except Exception:
            pass  # 列已存在

    def add(self, project: str, file_path: str, added_by: str = "manual") -> bool:
        """
        添加关键文档
        
        Args:
            project: 项目名称
            file_path: 文件路径
            added_by: 添加方式 ('manual' 或 'stage_files')
        
        Returns:
            bool: 添加是否成功
        """
        if not self._ensure_table():
            return False
        self._migrate()
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 统一路径分隔符
        file_path = str(file_path).replace("\\", "/")
        file_name = os.path.basename(file_path)
        doc_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{hashlib.md5(file_path.encode()).hexdigest()[:8]}"
        
        try:
            success, _ = self._execute(f'''
                INSERT OR IGNORE INTO {self.TABLE_NAME} 
                (id, project, file_path, file_name, added_at, added_by)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (doc_id, project, file_path, file_name, now, added_by))
            return success
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] add 异常: {e}")
            return False

    def set_working_directory(self, project: str, file_path: str) -> bool:
        """
        设置项目的工作目录（互斥：只会有一个工作目录）
        如果 file_path 为空或"clear"，则清除工作目录设置
        """
        if not self._ensure_table():
            return False
        self._migrate()

        try:
            # 先清除所有工作目录标记
            self._execute(
                f'UPDATE {self.TABLE_NAME} SET is_working_dir = 0 WHERE project = ?',
                (project,)
            )
            if not file_path or file_path == "clear":
                return True
            # 设置指定路径为工作目录
            file_path = str(file_path).replace("\\", "/")
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET is_working_dir = 1 WHERE project = ? AND file_path = ?',
                (project, file_path)
            )
            return success
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] set_working_directory 异常: {e}")
            return False


    def set_working_directory_only(self, project: str, file_path: str) -> bool:
        """
        仅设置指定路径为工作目录，不清除其他路径的标记。

        用于 worktree 切换场景：set_working_directory 会先清除所有 is_working_dir
        （包括原根目录的标记），导致 _load_key_documents 无法识别用户真正的根目录。
        此方法仅追加设置，不干扰已有的标记。
        """
        if not self._ensure_table():
            return False
        self._migrate()

        try:
            file_path = str(file_path).replace('\\', '/')
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET is_working_dir = 1 WHERE project = ? AND file_path = ?',
                (project, file_path)
            )
            return success
        except Exception as e:
            logger.error(f'[KeyDocumentsRepository] set_working_directory_only 异常: {e}')
            return False
    def get_working_directory(self, project: str) -> Optional[str]:
        """
        获取项目当前工作目录

        Returns:
            Optional[str]: 工作目录路径，未设置返回 None
        """
        if not self.is_initialized:
            return None
        self._migrate()

        try:
            success, rows = self._execute(
                f'SELECT file_path FROM {self.TABLE_NAME} WHERE project = ? AND is_working_dir = 1 LIMIT 1',
                (project,)
            )
            if success and rows:
                row = rows[0]
                return row[0] if isinstance(row, tuple) else row.get("file_path", "")
            return None
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] get_working_directory 异常: {e}")
            return None

    def get_by_project(self, project: str, limit: int = 9999) -> List[Dict]:
        """
        获取指定项目的所有关键文档

        Args:
            project: 项目名称
            limit: 最大返回数量

        Returns:
            List[Dict]: 关键文档列表
        """
        if not self.is_initialized:
            return []

        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} WHERE project = ? ORDER BY added_at DESC LIMIT ?',
                (project, limit)
            )
            if success and rows:
                result = []
                for row in rows:
                    d = {k: row[k] for k in row.keys()} if hasattr(row, 'keys') else dict(row)
                    result.append({
                        "id": d.get("id", ""),
                        "project": d.get("project", ""),
                        "file_path": d.get("file_path", ""),
                        "file_name": d.get("file_name", ""),
                        "added_at": d.get("added_at", ""),
                        "added_by": d.get("added_by", "manual"),
                        "is_working_dir": bool(d.get("is_working_dir", 0)),
                    })
                return result
            return []
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] get_by_project 异常: {e}")
            return []

    def remove(self, doc_id: str) -> bool:
        """
        移除关键文档
        
        Args:
            doc_id: 文档 ID
        
        Returns:
            bool: 移除是否成功
        """
        if not self.is_initialized:
            return False
        
        try:
            success, _ = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE id = ?',
                (doc_id,)
            )
            return success
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] remove 异常: {e}")
            return False

    def remove_by_path(self, project: str, file_path: str) -> bool:
        """
        根据路径移除关键文档
        
        Args:
            project: 项目名称
            file_path: 文件路径
        
        Returns:
            bool: 移除是否成功
        """
        if not self.is_initialized:
            return False
        
        file_path = str(file_path).replace("\\", "/")
        try:
            success, _ = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE project = ? AND file_path = ?',
                (project, file_path)
            )
            return success
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] remove_by_path 异常: {e}")
            return False

    def clear_by_project(self, project: str) -> int:
        """
        清空项目的所有关键文档
        
        Args:
            project: 项目名称
        
        Returns:
            int: 删除的文档数量
        """
        if not self.is_initialized:
            return 0
        
        try:
            success, result = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE project = ?',
                (project,)
            )
            if success and result:
                return result[0] if isinstance(result[0], tuple) else result
            return 0
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] clear_by_project 异常: {e}")
            return 0

    def get_all_projects(self) -> List[str]:
        """
        获取所有有关键文档的项目
        
        Returns:
            List[str]: 项目名称列表
        """
        if not self.is_initialized:
            return []
        
        try:
            success, rows = self._execute(
                f'SELECT DISTINCT project FROM {self.TABLE_NAME}'
            )
            if success and rows:
                return list(set(row[0] if isinstance(row, tuple) else row.get("project", "") for row in rows))
            return []
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] get_all_projects 异常: {e}")
            return []

    def count_by_project(self, project: str) -> int:
        """
        获取项目的关键文档数量
        
        Args:
            project: 项目名称
        
        Returns:
            int: 文档数量
        """
        if not self.is_initialized:
            return 0
        
        try:
            success, result = self._execute(
                f'SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE project = ?',
                (project,)
            )
            if success and result:
                return result[0][0] if isinstance(result[0], tuple) else result[0]
            return 0
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] count_by_project 异常: {e}")
            return 0

    def get_worktree_counts(self) -> Dict[str, int]:
        """获取所有项目的工作目录数量

        计数规则：工作目录数 = 主仓库(1 if is_working_dir=1) + 所有 git worktree 数
        - 项目有 is_working_dir=1 的根目录时，至少计 1（主仓库）
        - 加上所有 added_by='git_worktree' 的 worktree
        - 没有根目录时返回 0（不显示）
        """
        if not self.is_initialized:
            return {}
        try:
            # MAX(is_working_dir) 对非 worktree 记录取 1（主仓库），
            # SUM(worktree) 对所有 worktree 记录各计 1
            success, rows = self._execute(
                f"SELECT project, "
                f"(MAX(CASE WHEN added_by != 'git_worktree' THEN is_working_dir ELSE 0 END) + "
                f" SUM(CASE WHEN added_by = 'git_worktree' THEN 1 ELSE 0 END)) as cnt "
                f"FROM {self.TABLE_NAME} "
                f"WHERE is_working_dir = 1 OR added_by = 'git_worktree' "
                f"GROUP BY project"
            )
            if success and rows:
                result = {}
                for row in rows:
                    p = row[0] if isinstance(row, tuple) else row.get("project", "")
                    c = row[1] if isinstance(row, tuple) else row.get("cnt", 0)
                    result[p] = c
                return result
            return {}
        except Exception as e:
            logger.error(f"[KeyDocumentsRepository] get_worktree_counts 异常: {e}")
            return {}