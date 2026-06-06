# -*- coding: utf-8 -*-
"""
子智能体日志仓储模块 - 专门负责子智能体任务日志的持久化

从 SubAgentLogStore 提取的 CRUD 逻辑，集成到 SessionStore 统一管理。
"""

import orjson as json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from loguru import logger


class SubAgentLogRepository:
    """子智能体日志数据仓储，处理任务日志的 CRUD 操作"""

    TABLE_NAME = "sub_agent_logs"

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

    def _ensure_table(self) -> bool:
        """确保表存在"""
        if not self.is_initialized:
            return False
        try:
            self._db.create_table(self.TABLE_NAME, [
                {"name": "task_id", "type": "TEXT", "primary_key": True},
                {"name": "agent_name", "type": "TEXT"},
                {"name": "task_description", "type": "TEXT"},
                {"name": "session_id", "type": "TEXT"},  # 所属会话 ID（会话隔离）
                {"name": "status", "type": "TEXT"},  # running, finished, error
                {"name": "result", "type": "TEXT"},
                {"name": "error", "type": "TEXT"},
                {"name": "logs", "type": "TEXT"},  # JSON 存储
                {"name": "summary", "type": "TEXT"},  # JSON 存储摘要信息
                {"name": "created_at", "type": "TEXT"},
                {"name": "updated_at", "type": "TEXT"},
            ])
            # 兼容旧表：尝试添加 session_id 列（表已存在时，create_table 不会自动加新列）
            self._db.execute_sql(
                f'ALTER TABLE "{self.TABLE_NAME}" ADD COLUMN session_id TEXT DEFAULT ""'
            )
            return True
        except Exception as e:
            # ALTER TABLE ADD COLUMN 在列已存在时会抛异常，这是正常的
            err_msg = str(e).lower()
            if "duplicate column" in err_msg or "already exists" in err_msg:
                return True
            logger.error(f"[SubAgentLogRepository] _ensure_table 异常: {e}")
            return False

    def _row_to_task(self, row) -> Dict:
        """将数据库行转换为任务字典"""
        if not row:
            return {}

        if hasattr(row, 'keys'):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        try:
            logs = json.loads(d.get("logs", "[]"))
        except Exception:
            logs = []

        try:
            summary = json.loads(d.get("summary", "{}"))
        except Exception:
            summary = {}

        return {
            "task_id": d.get("task_id", ""),
            "agent_name": d.get("agent_name", ""),
            "task_description": d.get("task_description", ""),
            "session_id": d.get("session_id", ""),
            "status": d.get("status", ""),
            "result": d.get("result", ""),
            "error": d.get("error", ""),
            "logs": logs,
            "summary": summary,
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
        }

    def save_task(self, task_id: str, agent_name: str, task_description: str,
                  status: str = "running", result: str = None, error: str = None,
                  logs: List[Dict] = None, summary: Dict = None,
                  session_id: str = "") -> bool:
        """
        保存或更新任务

        Args:
            task_id: 任务 ID
            agent_name: Agent 名称
            task_description: 任务描述
            status: 状态 (running, finished, error)
            result: 结果
            error: 错误信息
            logs: 日志列表
            summary: 摘要信息
            session_id: 所属会话 ID

        Returns:
            bool: 保存是否成功
        """
        if not self._ensure_table():
            return False

        now = datetime.now().isoformat()
        logs_json = json.dumps(logs or []).decode('utf-8')
        summary_json = json.dumps(summary or {}).decode('utf-8')

        # 检查是否存在
        success, rows = self._execute(
            f'SELECT 1 FROM "{self.TABLE_NAME}" WHERE task_id = ?', (task_id,)
        )

        if success and rows and len(rows) > 0:
            # 更新
            success, _ = self._execute(f'''
                UPDATE "{self.TABLE_NAME}"
                SET agent_name = ?, task_description = ?, status = ?,
                    result = ?, error = ?, logs = ?, summary = ?, updated_at = ?
                WHERE task_id = ?
            ''', (agent_name or "", task_description or "", status,
                  result or "", error or "", logs_json, summary_json, now, task_id))
        else:
            # 插入
            success, _ = self._execute(f'''
                INSERT INTO "{self.TABLE_NAME}"
                (task_id, agent_name, task_description, session_id, status, result, error, logs, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (task_id, agent_name or "", task_description or "", session_id or "", status,
                  result or "", error or "", logs_json, summary_json, now, now))

        return success

    def update_status(self, task_id: str, status: str, result: str = None,
                      error: str = None, logs: List[Dict] = None, summary: Dict = None) -> bool:
        """更新任务状态"""
        if not self._ensure_table():
            return False

        now = datetime.now().isoformat()
        data = {"status": status, "updated_at": now}
        params = []

        if result is not None:
            data["result"] = result
        if error is not None:
            data["error"] = error
        if logs is not None:
            data["logs"] = json.dumps(logs).decode('utf-8')
        if summary is not None:
            data["summary"] = json.dumps(summary).decode('utf-8')

        for v in data.values():
            params.append(v)
        params.append(task_id)

        set_clause = ", ".join([f'"{k}" = ?' for k in data.keys()])
        success, _ = self._execute(
            f'UPDATE "{self.TABLE_NAME}" SET {set_clause} WHERE task_id = ?',
            tuple(params)
        )
        return success

    def get_task(self, task_id: str) -> Optional[Dict]:
        """获取单个任务"""
        if not self._ensure_table():
            return None

        success, rows = self._execute(
            f'SELECT * FROM "{self.TABLE_NAME}" WHERE task_id = ?', (task_id,)
        )
        if success and rows and len(rows) > 0:
            return self._row_to_task(rows[0])
        return None

    def get_tasks(self, task_ids: List[str]) -> List[Dict]:
        """获取多个任务"""
        if not self._ensure_table() or not task_ids:
            return []

        placeholders = ",".join(["?"] * len(task_ids))
        sql = f'SELECT * FROM "{self.TABLE_NAME}" WHERE task_id IN ({placeholders})'
        success, rows = self._execute(sql, tuple(task_ids))

        if success and rows:
            return [self._row_to_task(row) for row in rows]
        return []

    def get_tasks_by_session(self, session_id: str, limit: int = 100) -> List[Dict]:
        """获取指定会话的所有任务（按创建时间倒序）"""
        if not self._ensure_table():
            return []

        success, rows = self._execute(
            f'SELECT * FROM "{self.TABLE_NAME}" WHERE session_id = ? ORDER BY created_at DESC LIMIT ?',
            (session_id, limit)
        )
        if success and rows:
            return [self._row_to_task(row) for row in rows]
        return []

    def get_all_tasks(self, limit: int = 100) -> List[Dict]:
        """获取所有任务（按创建时间倒序）"""
        if not self._ensure_table():
            return []

        success, rows = self._execute(
            f'SELECT * FROM "{self.TABLE_NAME}" ORDER BY created_at DESC LIMIT ?',
            (limit,)
        )
        if success and rows:
            return [self._row_to_task(row) for row in rows]
        return []

    def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        if not self._ensure_table():
            return False

        success, _ = self._execute(
            f'DELETE FROM "{self.TABLE_NAME}" WHERE task_id = ?', (task_id,)
        )
        return success

    def clear_old_tasks(self, days: int = 7) -> int:
        """清理旧任务（默认保留7天）"""
        if not self._ensure_table():
            return 0

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        success, result = self._execute(
            f'DELETE FROM "{self.TABLE_NAME}" WHERE updated_at < ?', (cutoff,)
        )
        if success and result:
            return int(result)
        return 0