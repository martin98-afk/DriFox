# -*- coding: utf-8 -*-
"""
文件操作记录仓储模块 - 专门负责文件操作的持久化（用于撤销功能）

从 SessionStore 中提取的文件操作记录 CRUD 逻辑。
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from loguru import logger


class FileOperationRepository:
    """文件操作记录数据仓储，处理文件操作的 CRUD 操作"""

    TABLE_NAME = "file_operations"

    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager 实例
        """
        self._db = db_manager

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def _execute(self, sql: str, params: tuple = ()) -> Tuple[bool, Any]:
        """执行 SQL（内部使用）"""
        if not self._db:
            return False, "数据库未初始化"
        return self._db.execute_sql(sql, params)

    def _row_to_operation(self, row) -> Dict:
        """将数据库行转换为操作字典"""
        if not row:
            return {}

        if hasattr(row, 'keys'):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        return {
            "id": d.get("id"),
            "session_id": d.get("session_id", ""),
            "call_id": d.get("call_id", ""),
            "tool_name": d.get("tool_name", ""),
            "file_path": d.get("file_path", ""),
            "backup_path": d.get("backup_path", ""),
            "created_at": d.get("created_at", ""),
        }

    def record(self, session_id: str, call_id: str,
               tool_name: str, file_path: str,
               backup_path: str) -> bool:
        """
        记录文件操作

        Args:
            session_id: 会话 ID
            call_id: 工具调用 ID
            tool_name: 工具名称
            file_path: 操作的文件路径
            backup_path: 备份文件路径

        Returns:
            bool: 记录是否成功
        """
        if not self.is_initialized:
            return False

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        success, _ = self._execute('''
            INSERT INTO file_operations
            (session_id, call_id, tool_name, file_path, backup_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session_id, call_id, tool_name, file_path, backup_path, now))

        return success

    def get_after_call(self, session_id: str, call_id: str) -> List[Dict]:
        """
        获取某 call_id 之后的所有文件操作（用于撤销）

        Args:
            session_id: 会话 ID
            call_id: 目标 call_id，获取该 call_id 之后的所有操作

        Returns:
            List[Dict]: 按时间正序排列的操作列表
        """
        if not self.is_initialized:
            logger.warning("[FileOperationRepository] 未初始化")
            return []

        # 获取目标 call_id 的 id（用于比较）
        success, rows = self._execute('''
            SELECT id FROM file_operations
            WHERE session_id = ? AND call_id = ?
            LIMIT 1
        ''', (session_id, call_id))

        if not success or not rows:
            return []

        target_id = rows[0][0] if isinstance(rows[0], tuple) else rows[0].get('id')

        # 获取该 id 之后的所有操作（正序，用于逆序回滚）
        success, ops = self._execute('''
            SELECT * FROM file_operations
            WHERE session_id = ? AND id > ?
            ORDER BY id ASC
        ''', (session_id, target_id))

        if not success:
            return []

        result = []
        for row in ops:
            if hasattr(row, 'keys'):
                op = {k: row[k] for k in row.keys()}
            else:
                op = dict(row) if isinstance(row, dict) else {}
            result.append(op)

        return result

    def get_by_call_id(self, session_id: str, call_id: str) -> List[Dict]:
        """
        根据 call_id 获取文件操作记录

        Args:
            session_id: 会话 ID
            call_id: 工具调用 ID

        Returns:
            List[Dict]: 操作列表
        """
        if not self.is_initialized:
            return []

        success, ops = self._execute('''
            SELECT * FROM file_operations
            WHERE session_id = ? AND call_id = ?
            ORDER BY id ASC
        ''', (session_id, call_id))

        if not success:
            return []

        result = []
        for row in ops:
            if hasattr(row, 'keys'):
                op = {k: row[k] for k in row.keys()}
            else:
                op = dict(row) if isinstance(row, dict) else {}
            result.append(op)

        return result

    def get_all(self, session_id: str) -> List[Dict]:
        """获取指定会话的所有文件操作"""
        if not self.is_initialized:
            return []

        success, ops = self._execute('''
            SELECT * FROM file_operations
            WHERE session_id = ?
            ORDER BY id ASC
        ''', (session_id,))

        if not success:
            return []

        result = []
        for row in ops:
            if hasattr(row, 'keys'):
                op = {k: row[k] for k in row.keys()}
            else:
                op = dict(row) if isinstance(row, dict) else {}
            result.append(op)

        logger.info(f"[FileOperationRepository] 返回 {len(result)} 个操作")
        return result

    def delete_after_id(self, session_id: str, after_id: int) -> int:
        """
        删除指定 session 中 id 大于 after_id 的所有操作记录

        Args:
            session_id: 会话 ID
            after_id: 起始 ID

        Returns:
            int: 删除的记录数量
        """
        if not self.is_initialized:
            return 0

        success, result = self._execute('''
            DELETE FROM file_operations
            WHERE session_id = ? AND id > ?
        ''', (session_id, after_id))

        if success and result:
            return int(result)
        return 0

    def delete_by_call_id(self, session_id: str, call_id: str) -> int:
        """
        根据 session_id 和 call_id 删除文件操作记录

        Args:
            session_id: 会话 ID
            call_id: 工具调用 ID

        Returns:
            int: 删除的记录数量
        """
        if not self.is_initialized:
            return 0

        success, result = self._execute('''
            DELETE FROM file_operations
            WHERE session_id = ? AND call_id = ?
        ''', (session_id, call_id))

        if success and result:
            return int(result)
        return 0

    def clear_session(self, session_id: str) -> Tuple[int, List[str]]:
        """
        清空会话的所有文件操作记录，返回被删除的备份文件路径列表

        Args:
            session_id: 会话 ID

        Returns:
            Tuple[int, List[str]]: (删除的记录数, 备份文件路径列表)
        """
        if not self.is_initialized:
            return 0, []

        # 先获取所有备份文件路径
        success, rows = self._execute(
            'SELECT backup_path FROM file_operations WHERE session_id = ?',
            (session_id,)
        )

        backup_paths = []
        if success and rows:
            for row in rows:
                path = row[0] if isinstance(row, tuple) else row.get('backup_path', '')
                if path:
                    backup_paths.append(path)

        # 删除记录
        success, result = self._execute(
            'DELETE FROM file_operations WHERE session_id = ?',
            (session_id,)
        )

        deleted_count = 0
        if success and result:
            deleted_count = int(result)

        return deleted_count, backup_paths