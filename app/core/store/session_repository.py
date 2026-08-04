# -*- coding: utf-8 -*-
"""
会话仓储模块 - 专门负责会话的持久化

从 SessionStore 中提取的会话 CRUD 逻辑。
"""

from datetime import datetime
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.core.store.serde import deserialize, serialize


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
        # 内容地址缓存：session_id -> (message_count, last_msg_hash)
        # 用于跳过内容未变的重复持久化，节省全量序列化+zstd压缩开销
        self._content_hash_cache: Dict[str, tuple] = {}
        # 保护 _content_hash_cache 的读写原子性。
        # Gateway（异步写入）+ UI（同步 save）跨线程访问同一缓存时，
        # 裸 dict 的复合判断+赋值在 GIL 释放窗口内可能丢失更新或脏命中，
        # 导致两个线程都走昂贵的 serialize+INSERT OR REPLACE 路径。
        # RLock 而非 Lock：get() 可能在持锁状态下再次访问缓存。
        self._cache_lock = RLock()

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

        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        # 解析 JSON 字段（使用 serde 自动处理 zstd 压缩 + 旧数据兼容）
        messages = []
        compaction_state = {}
        compaction_cache = {}

        try:
            msg_raw = d.get("messages")
            if isinstance(msg_raw, (str, bytes)):
                messages = deserialize(msg_raw) or []
            elif isinstance(msg_raw, list):
                messages = msg_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize session messages: {e}")

        try:
            state_raw = d.get("compaction_state")
            if isinstance(state_raw, (str, bytes)):
                compaction_state = deserialize(state_raw) or {}
            elif isinstance(state_raw, dict):
                compaction_state = state_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize compaction_state: {e}")

        try:
            cache_raw = d.get("compaction_cache")
            if isinstance(cache_raw, (str, bytes)):
                compaction_cache = deserialize(cache_raw) or {}
            elif isinstance(cache_raw, dict):
                compaction_cache = cache_raw
        except Exception as e:
            logger.warning(f"Failed to deserialize compaction_cache: {e}")

        # 统一字段名：DB 的 title 列映射到 name 和 topic_summary
        raw_title = d.get("title", "") or ""
        return {
            "session_id": d.get("session_id", ""),
            "name": raw_title,  # ChatSession.name
            "title": raw_title,  # HistoryManager 兼容
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
            "context_usage": d.get("context_usage", 0),
            "last_api_prompt_tokens": d.get("last_api_prompt_tokens", 0),
            "last_api_message_count": d.get("last_api_message_count", 0),
            # 团队元数据（方案 A 团队会话恢复基础，非团队会话为空串）
            "team_run_id": d.get("team_run_id", "") or "",
            "team_name": d.get("team_name", "") or "",
            "agent_name": d.get("agent_name", "") or "",
            # 团队成员快照（F3：JSON 字符串，恢复时找回无会话记录的手动成员）
            "team_members": d.get("team_members", "") or "",
            # 添加兼容字段（HistoryManager 期望这些字段）
            # 优先使用消息列表中最后一条消息的时间
            "last_time": d.get("last_time")
            or (messages[-1].get("timestamp") if messages else None)
            or d.get("updated_at", ""),
            "saved_at": d.get("saved_at") or d.get("created_at", ""),
            "user_edited_title": d.get("user_edited_title", False),
        }

    def save(self, session: Dict) -> bool:
        """
        原子性保存单个会话（带内容去重，内容未变时跳过序列化和写盘）

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

        # 增量内容指纹：消息流式过程中只追加/修改最后一条消息，
        # 用 (len, last_msg_hash) 二元组替代 hash(str(全量)) 避免 O(n) 字符串化。
        # 对于"修改历史消息"的非常规场景（如 compaction），后续 serialize 会覆盖写入。
        messages = session.get("messages", [])
        if messages:
            last_msg = messages[-1]
            content_key = (len(messages), hash(str(last_msg)))
        else:
            content_key = (0, 0)
        # 持锁读 cache：判断是否可跳过序列化+写盘。
        # 锁粒度最小（仅 cache 读 / 写），不阻塞后续长时间的 serialize+SQL。
        with self._cache_lock:
            cached = self._content_hash_cache.get(session_id)
            if cached is not None and cached == content_key:
                return True  # 消息未变，跳过昂贵的序列化+压缩+写盘

        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user_edited = 1 if session.get("user_edited_title", False) else 0
            session_data = {
                "session_id": session_id,
                # 优先使用 topic_summary（UI/Agent生成），其次 name（Gateway创建），兜底空字符串
                "title": session.get("topic_summary") or session.get("name") or session.get("title", ""),
                "project": session.get("project", "默认项目"),
                # 使用 serde 透明压缩（zstd + 格式魔数），DB 体积减少 50-80%
                "messages": serialize(messages),
                "system_prompt": session.get("system_prompt", ""),
                "compaction_state": serialize(session.get("compaction_state", {})),
                "compaction_cache": serialize(session.get("compaction_cache", {})),
                "message_count": session.get("message_count", 0),
                "user_edited_title": user_edited,
                "worktree_path": session.get("worktree_path", "") or "",
                "preview": session.get("preview", "") or "",
                "context_usage": session.get("context_usage", 0),
                "last_api_prompt_tokens": session.get("last_api_prompt_tokens", 0),
                "last_api_message_count": session.get("last_api_message_count", 0),
                # 团队元数据透传（方案 A）：非团队会话保持空串
                "team_run_id": session.get("team_run_id", "") or "",
                "team_name": session.get("team_name", "") or "",
                "agent_name": session.get("agent_name", "") or "",
                # 团队成员快照透传（F3）：JSON 字符串，非团队会话保持空串
                "team_members": session.get("team_members", "") or "",
            }

            success, result = self._execute(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_NAME}
                (session_id, title, project, messages, system_prompt,
                 compaction_state, compaction_cache, message_count, user_edited_title,
                 worktree_path, preview, context_usage,
                 last_api_prompt_tokens, last_api_message_count,
                 team_run_id, team_name, agent_name, team_members,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM {self.TABLE_NAME} WHERE session_id = ?), ?),
                    ?)
            """,
                (
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
                    session_data["preview"],
                    session_data["context_usage"],
                    session_data["last_api_prompt_tokens"],
                    session_data["last_api_message_count"],
                    session_data["team_run_id"],
                    session_data["team_name"],
                    session_data["agent_name"],
                    session_data["team_members"],
                    session_id,  # for coalesce
                    now,  # created_at default
                    now,  # updated_at
                ),
            )

            if success:
                # 更新内容 hash 缓存（持锁写）
                with self._cache_lock:
                    self._content_hash_cache[session_id] = content_key
                # INSERT OR REPLACE = DELETE 旧行 + INSERT 新行，旧页全部进 freelist
                # 高频率 save（每次用户发消息+Agent 回复）叠加 zstd 压缩后 blob
                # 大小波动（50KB~500KB），持续制造不可复用的空闲页。
                # 此处检测 freelist 是否超过安全阈值（5000 页 ≈ 20MB），超过则
                # 增量回收 500 页（≈2MB），防止 freelist 滚雪球到 GB 级。
                self._reclaim_freelist_if_needed()

            return success

        except Exception as e:
            logger.error(f"[SessionRepository] save_session 异常: {e}")
            # 失败时不更新 cache，下次 save 会重新尝试序列化+写盘
            return False

    def _reclaim_freelist_if_needed(self, threshold_pages: int = 5000, reclaim_pages: int = 500):
        """
        空闲页超过阈值时增量回收，防止 freelist 滚雪球

        INSERT OR REPLACE 的 DELETE→INSERT 路径将旧 blob 页全部释放进
        freelist，高频率 save（每次对话回合 2 次）叠加 zstd 压缩后大小波动
        （50KB~500KB）持续制造不可复用空闲页。用户截断/子任务清理等大型
        DELETE 操作一次性释放数千页，必须及时回收。

        每次最多回收 reclaim_pages 页（≈2MB @ 4KB page），避免阻塞 UI。
        incremental_vacuum 仅在 auto_vacuum=INCREMENTAL 时实际回收，
        否则静默跳过。

        Args:
            threshold_pages: freelist 超过此页数才触发回收（默认 5000 页 ≈ 20MB）
            reclaim_pages:  单次回收最多页数（默认 500 页 ≈ 2MB）
        """
        try:
            ok, rows = self._execute("PRAGMA freelist_count")
            if not ok or not rows:
                return
            freelist = list(rows[0].values())[0] if isinstance(rows[0], dict) else rows[0]
            if freelist < threshold_pages:
                return
            self._execute(f"PRAGMA incremental_vacuum({reclaim_pages})")
        except Exception:
            pass  # auto_vacuum 未启用时静默跳过，不阻塞保存流程

    def get(self, session_id: str) -> Optional[Dict]:
        """根据 ID 获取单个会话（同时失效内容 hash 缓存）"""
        if not self.is_initialized:
            return None

        try:
            success, rows = self._execute(f"SELECT * FROM {self.TABLE_NAME} WHERE session_id = ?", (session_id,))
            if success and rows and len(rows) > 0:
                # 外部加载了最新数据，失效内容缓存，下次 save 会重建
                with self._cache_lock:
                    self._content_hash_cache.pop(session_id, None)
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
                f"SELECT * FROM {self.TABLE_NAME} ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset)
            )
            if success:
                return [self._row_to_session(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_sessions 异常: {e}")
            return []

    def get_all_lightweight(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取所有会话的轻量列表（不含 messages），用于启动加载和历史列表展示。

        避免 SELECT * 一次性反序列化全部 messages JSON（可达数十 MB），
        仅在用户点击某个会话时按需加载 messages。
        """
        if not self.is_initialized:
            return []

        try:
            # 🚀 只选轻量列表展示所需的字段，跳过 compaction_state/cache
            # 等重量级 BLOB 列，减少 SQLite I/O 和传输开销。
            success, rows = self._execute(
                f"SELECT session_id, title, project, system_prompt, "
                f"message_count, user_edited_title, worktree_path, "
                f"preview, context_usage, created_at, updated_at, "
                f"team_run_id, team_name, agent_name, team_members "
                f"FROM {self.TABLE_NAME} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            if success:
                return [self._row_to_session_lightweight(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_all_lightweight 异常: {e}")
            return []

    def _row_to_session_lightweight(self, row) -> Dict:
        """将数据库行转换为不含 messages 的轻量会话字典"""
        if not row:
            return {}

        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        # 🚀 性能优化：跳过 compaction_state/cache 的 zstd 解压+反序列化。
        # 轻量列表展示中不使用这两个字段，全量加载在 get_session() 中才做。
        # 省掉 500 条记录的 zstd 解压 + orjson.loads，可节省大量 CPU。
        raw_title = d.get("title", "") or ""
        return {
            "session_id": d.get("session_id", ""),
            "name": raw_title,
            "title": raw_title,
            "topic_summary": raw_title,
            "project": d.get("project", "默认项目"),
            "messages": [],  # 懒加载：不在启动时加载
            "system_prompt": d.get("system_prompt", ""),
            "compaction_state": {},
            "compaction_cache": {},
            "message_count": d.get("message_count", 0),
            "preview": d.get("preview", "") or "",  # 从 DB 读取预览文本
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
            "worktree_path": d.get("worktree_path", "") or "",
            "context_usage": d.get("context_usage", 0),
            # 团队元数据（方案 A 团队会话恢复基础，非团队会话为空串）
            "team_run_id": d.get("team_run_id", "") or "",
            "team_name": d.get("team_name", "") or "",
            "agent_name": d.get("agent_name", "") or "",
            # 团队成员快照（F3：JSON 字符串，恢复时找回无会话记录的手动成员）
            "team_members": d.get("team_members", "") or "",
            "last_time": d.get("updated_at", ""),
            "saved_at": d.get("created_at", ""),
            "user_edited_title": d.get("user_edited_title", False),
        }

    def get_by_project(self, project: str, limit: int = 100) -> List[Dict]:
        """获取指定项目的会话列表"""
        if not self.is_initialized:
            return []

        try:
            success, rows = self._execute(
                f"SELECT * FROM {self.TABLE_NAME} WHERE project = ? ORDER BY updated_at DESC LIMIT ?", (project, limit)
            )
            if success:
                return [self._row_to_session(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_sessions_by_project 异常: {e}")
            return []

    def get_by_team_run_id(self, run_id: str) -> List[Dict]:
        """按团队 run_id 获取全部成员会话（轻量，不含 messages）。

        恢复团队会话时使用：直接查 SQLite，绕开 HistoryManager 内存
        _history_limit=500 的截断——团队会话可能因长期运行被挤出
        内存前 500 条，导致恢复时按 run_id 收集不到部分成员。

        Returns:
            按 updated_at 倒序的轻量会话字典列表（含团队元数据字段）
        """
        if not self.is_initialized or not run_id:
            return []

        try:
            # 与 get_all_lightweight 相同的轻量字段投影，避免反序列化重量级 BLOB
            success, rows = self._execute(
                f"SELECT session_id, title, project, system_prompt, "
                f"message_count, user_edited_title, worktree_path, "
                f"preview, context_usage, created_at, updated_at, "
                f"team_run_id, team_name, agent_name, team_members "
                f"FROM {self.TABLE_NAME} WHERE team_run_id = ? "
                f"ORDER BY updated_at DESC",
                (run_id,),
            )
            if success:
                return [self._row_to_session_lightweight(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[SessionRepository] get_by_team_run_id 异常: {e}")
            return []

    def get_projects(self) -> List[str]:
        """获取所有项目名称列表（含无会话但有关键文档的项目）

        关键修复：与归档清理配合使用——归档时必须同时清理 sessions、
        key_documents 两张表，否则已归档项目会从
        key_documents "复活"。
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
        """删除指定会话（同时清除内容 hash 缓存）"""
        if not self.is_initialized:
            return False

        try:
            self._content_hash_cache.pop(session_id, None)
            success, _ = self._execute(f"DELETE FROM {self.TABLE_NAME} WHERE session_id = ?", (session_id,))
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
                f"UPDATE {self.TABLE_NAME} SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id),
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
                f"UPDATE {self.TABLE_NAME} SET project = ?, updated_at = ? WHERE session_id = ?",
                (project, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id),
            )
            return success
        except Exception as e:
            logger.error(f"[SessionRepository] update_project 异常: {e}")
            return False

    def archive_by_project(self, project: str) -> int:
        """归档指定项目的所有会话（单条 SQL，避免 N+1 查询）"""
        if not self.is_initialized:
            return 0

        try:
            archived_project = f"__archived__/{project}"
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            success, result = self._execute(
                f"UPDATE {self.TABLE_NAME} SET project = ?, updated_at = ? WHERE project = ?",
                (archived_project, now, project),
            )
            if success and result is not None:
                return int(result)
            return 0
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
