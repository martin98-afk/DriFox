# -*- coding: utf-8 -*-
"""
会话仓储模块 - 专门负责会话的持久化

从 SessionStore 中提取的会话 CRUD 逻辑。
"""

import orjson as json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from loguru import logger


class SessionRepository:
    """会话数据仓储，处理会话的 CRUD 操作"""

    TABLE_NAME = "sessions"
    DB_FILENAME = "sessions.db"

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

    def _row_to_session(self, row) -> Dict:
        """将数据库行转换为会话字典"""
        if not row:
            return {}

        if hasattr(row, 'keys'):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        # 解析 JSON 字段
        messages = []
        compaction_state = {}
        compaction_cache = {}

        try:
            msg_raw = d.get("messages", "[]")
            if isinstance(msg_raw, str):
                messages = json.loads(msg_raw)
            elif isinstance(msg_raw, list):
                messages = msg_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize session messages: {e}")

        try:
            state_raw = d.get("compaction_state", "{}")
            if isinstance(state_raw, str):
                compaction_state = json.loads(state_raw) if state_raw else {}
            elif isinstance(state_raw, dict):
                compaction_state = state_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize compaction_state: {e}")

        try:
            cache_raw = d.get("compaction_cache", "{}")
            if isinstance(cache_raw, str):
                compaction_cache = json.loads(cache_raw) if cache_raw else {}
            elif isinstance(cache_raw, dict):
                compaction_cache = cache_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize compaction_cache: {e}")

        # 统一字段名：DB 的 title 列映射到 name 和 topic_summary
        raw_title = d.get("title", "") or ""
        return {
            "session_id": d.get("session_id", ""),
            "name": raw_title,       # ChatSession.name
            "title": raw_title,      # HistoryManager 兼容
            "topic_summary": raw_title,  # ChatSession.topic_summary
            "project": d.get("project", "默认项目"),
            "messages": messages,
            "system_prompt": d.get("system_prompt", ""),
            "compaction_state": compaction_state,
            "compaction_cache": compaction_cache,
            "message_count": d.get("message_count", 0),
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
            "worktree_path": d.get("worktree_path", "") or "",
            # 添加兼容字段（HistoryManager 期望这些字段）
            # 优先使用消息列表中最后一条消息的时间
            "last_time": d.get("last_time") or (
                messages[-1].get("timestamp") if messages else None
            ) or d.get("updated_at", ""),
            "saved_at": d.get("saved_at") or d.get("created_at", ""),
            "user_edited_title": d.get("user_edited_title", False),
        }

    def save(self, session: Dict) -> bool:
        """
        原子性保存单个会话

        Args:
            session: 会话数据字典

        Returns:
            bool: 保存是否成功
        """
        if not self.is_initialized:
            logger.warning("[SessionRepository] 未初始化，无法保存")
            return False

        session_id = session.get("session_id")
        if not session_id:
            logger.warning("[SessionRepository] session_id 不能为空")
            return False

        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user_edited = 1 if session.get("user_edited_title", False) else 0
            session_data = {
                "session_id": session_id,
                # 优先使用 topic_summary（UI/Agent生成），其次 name（Gateway创建），兜底空字符串
                "title": session.get("topic_summary") or session.get("name") or session.get("title", ""),
                "project": session.get("project", "默认项目"),
                "messages": json.dumps(session.get("messages", [])).decode('utf-8'),
                "system_prompt": session.get("system_prompt", ""),
                "compaction_state": json.dumps(session.get("compaction_state", {})).decode('utf-8'),
                "compaction_cache": json.dumps(session.get("compaction_cache", {})).decode('utf-8'),
                "message_count": session.get("message_count", 0),
                "user_edited_title": user_edited,
                "worktree_path": session.get("worktree_path", "") or "",
            }

            success, result = self._execute(f'''
                INSERT OR REPLACE INTO {self.TABLE_NAME}
                (session_id, title, project, messages, system_prompt,
                 compaction_state, compaction_cache, message_count, user_edited_title,
                 worktree_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 
                    COALESCE((SELECT created_at FROM {self.TABLE_NAME} WHERE session_id = ?), ?),
                    ?)
            ''', (
                session_data["session_id"],
                session_data["title"],
                session_data["project"],
                session_data["messages"],
                session_data["system_prompt"],
                session_data["compaction_state"],
                session_data["compaction_cache"],
                session_data["message_count"],
                session_data["user_edited_title"],
                session_data["worktree_path"],
                session_id,  # for coalesce
                now,  # created_at default
                now,  # updated_at
            ))

            return success

        except Exception as e:
            logger.error(f"[SessionRepository] save_session 异常: {e}")
            return False

    def get(self, session_id: str) -> Optional[Dict]:
        """根据 ID 获取单个会话"""
        if not self.is_initialized:
            return None

        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} WHERE session_id = ?',
                (session_id,)
            )
            if success and rows and len(rows) > 0:
                return self._row_to_session(rows[0])
            return None
        except Exception as e:
            logger.error(f"[SessionRepository] get_session 异常: {e}")
            return None

    def get_all(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取所有会话（按更新时间倒序）"""
        if not self.is_initialized:
            return []

        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} ORDER BY updated_at DESC LIMIT ? OFFSET ?',
                (limit, offset)
            )
            if success:
                return [self._row_to_session(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_sessions 异常: {e}")
            return []

    def get_by_project(self, project: str, limit: int = 100) -> List[Dict]:
        """获取指定项目的会话列表"""
        if not self.is_initialized:
            return []

        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} WHERE project = ? ORDER BY updated_at DESC LIMIT ?',
                (project, limit)
            )
            if success:
                return [self._row_to_session(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_sessions_by_project 异常: {e}")
            return []

    def get_projects(self) -> List[str]:
        """获取所有项目名称列表（含无会话但有关键文档/笔记的项目）

        关键修复：与归档清理配合使用——归档时必须同时清理 sessions、
        key_documents、project_notes 三张表，否则已归档项目会从
        key_documents/project_notes "复活"。
        """
        if not self.is_initialized:
            return ["默认项目"]

        try:
            success, rows = self._execute(
                f"""
                SELECT DISTINCT project FROM (
                    SELECT project FROM {self.TABLE_NAME}
                    UNION
                    SELECT project FROM key_documents
                    UNION
                    SELECT project FROM project_notes
                ) ORDER BY project
                """
            )
            if success and rows:
                projects = []
                for row in rows:
                    p = row[0] if isinstance(row, tuple) else row.get("project", "")
                    if p and not p.startswith("__archived__"):
                        projects.append(p)
                return projects if projects else ["默认项目"]
            return ["默认项目"]
        except Exception as e:
            logger.error(f"[SessionRepository] get_projects 异常: {e}")
            return ["默认项目"]

    def delete(self, session_id: str) -> bool:
        """删除指定会话"""
        if not self.is_initialized:
            return False

        try:
            success, _ = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE session_id = ?',
                (session_id,)
            )
            return success
        except Exception as e:
            logger.error(f"[SessionRepository] delete_session 异常: {e}")
            return False

    def update_title(self, session_id: str, title: str) -> bool:
        """更新会话标题"""
        if not self.is_initialized:
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET title = ?, updated_at = ? WHERE session_id = ?',
                (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id)
            )
            return success
        except Exception as e:
            logger.error(f"[SessionRepository] update_title 异常: {e}")
            return False

    def update_project(self, session_id: str, project: str) -> bool:
        """更新会话的项目归属"""
        if not self.is_initialized:
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET project = ?, updated_at = ? WHERE session_id = ?',
                (project, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id)
            )
            return success
        except Exception as e:
            logger.error(f"[SessionRepository] update_project 异常: {e}")
            return False

    def archive_by_project(self, project: str) -> int:
        """归档指定项目的所有会话"""
        if not self.is_initialized:
            return 0

        try:
            sessions = self.get_by_project(project, limit=1000)
            count = 0
            for s in sessions:
                sid = s.get("session_id")
                if sid:
                    success, _ = self._execute(
                        f'UPDATE {self.TABLE_NAME} SET project = ? WHERE session_id = ?',
                        (f"__archived__/{project}", sid)
                    )
                    if success:
                        count += 1
            return count
        except Exception as e:
            logger.error(f"[SessionRepository] archive_sessions_by_project 异常: {e}")
            return 0

    def get_session_counts(self) -> Dict[str, int]:
        """获取所有项目（非归档）的会话数量（COUNT DISTINCT session_id 去重）"""
        if not self.is_initialized:
            return {}
        try:
            success, rows = self._execute(
                f"SELECT project, COUNT(DISTINCT session_id) as cnt FROM {self.TABLE_NAME} "
                f"WHERE project NOT LIKE '__archived__%' GROUP BY project"
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
            logger.error(f"[SessionRepository] get_session_counts 异常: {e}")
            return {}