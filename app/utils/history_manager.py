# -*- coding: utf-8 -*-
"""
会话历史管理器 - 解决 issue #374

从 JSON 存储迁移到 SQLite 存储，提供：
- 原子性写入
- 并发支持
- 损坏隔离
- 增量更新
"""
import os
import re
import uuid
import orjson as json

from datetime import datetime
from typing import List, Dict, Optional
from PyQt5.QtCore import QTimer
from loguru import logger

from app.core.message_content import consolidate_messages, content_to_text
from app.core.store import SessionStore
from app.utils.utils import get_app_data_dir, serialize_for_json, deserialize_from_json

# 预编译文件名清理正则
_SANITIZE_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*]')


def merge_session_messages(messages: List[Dict]) -> List[Dict]:
    return consolidate_messages(messages or [])


def sanitize_filename(name: str) -> str:
    """移除文件名中不合法的字符"""
    return _SANITIZE_FILENAME_PATTERN.sub("_", name)


class HistoryManager:
    """
    会话历史管理器（全局单例，跨窗口共享）

    使用 SQLite 进行持久化存储，同时维护内存缓存以提高读取性能。
    """

    _instance = None

    @classmethod
    def get_instance(cls) -> "HistoryManager":
        """获取全局唯一的 HistoryManager 实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.archive_dir = get_app_data_dir() / "archived"
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        self._history_limit = 100
        self._save_timer: Optional[QTimer] = None
        self._save_delay_ms = 1000

        # 延迟保存的待处理会话 ID
        self._pending_save_session_id: Optional[str] = None

        # SQLite 存储层
        self._session_store: Optional[SessionStore] = None
        self._use_sqlite = False

        # 内存缓存
        self._history_sessions: List[Dict] = []

        # 初始化存储
        self._init_storage()

    def _deduplicate_history_sessions(self):
        """去重历史会话列表，保持最新的一个"""
        seen_ids = set()
        unique_sessions = []
        # 倒序遍历，保留最新的（后面的是更新的）
        for session in reversed(self._history_sessions):
            session_id = session.get("session_id")
            if session_id not in seen_ids:
                seen_ids.add(session_id)
                unique_sessions.insert(0, session)  # 插回开头保持顺序
        removed = len(self._history_sessions) - len(unique_sessions)
        if removed > 0:
            logger.warning(f"[HistoryManager] 移除了 {removed} 个重复会话")
        self._history_sessions = unique_sessions

    def _init_storage(self):
        """初始化存储层"""
        use_sqlite = os.environ.get("LLM_SESSION_SQLITE", "1") == "1"

        if use_sqlite:
            try:
                self._session_store = SessionStore.get_instance()
                if self._session_store.is_initialized:
                    self._use_sqlite = True
                    logger.info(f"[HistoryManager] SQLite 存储已启用")

                    # 从 SQLite 加载
                    self._history_sessions = self._session_store.get_sessions(limit=500)

                    # 去重
                    self._deduplicate_history_sessions()

                    # 检查是否需要迁移旧 JSON 数据
                    self._migrate_if_needed()

                    return
                else:
                    logger.warning("[HistoryManager] SQLite 初始化失败，回退 JSON")
            except Exception as e:
                logger.warning(f"[HistoryManager] SQLite 初始化异常: {e}")

    def _migrate_if_needed(self):
        """迁移旧 JSON 数据到 SQLite（如果 SQLite 为空），迁移后删除 JSON"""
        if not self._session_store:
            return

        # 检查 SQLite 是否已有数据
        if self._session_store.get_session_count() > 0:
            return

    def _normalize_sessions(self, data: List) -> List[Dict]:
        """规范化会话数据"""
        normalized = []
        seen_ids = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            sid = item.get("session_id")
            if sid and sid in seen_ids:
                continue
            if sid:
                seen_ids.add(sid)
            fallback_ts = (
                item.get("last_time")
                or item.get("saved_at")
                or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            item["messages"] = self._ensure_message_timestamps(
                merge_session_messages(item.get("messages", [])),
                fallback_ts,
            )
            if "title" not in item:
                item["title"] = item.get("topic_summary", "新对话")
            if "last_time" not in item:
                item["last_time"] = self._extract_last_message_time(
                    item.get("messages", [])
                )
            if "message_count" not in item:
                item["message_count"] = len(item.get("messages", []))
            if "session_id" not in item:
                item["session_id"] = uuid.uuid4().hex[:8]
            item["compaction_state"] = dict(item.get("compaction_state") or {})
            item["compaction_cache"] = dict(item.get("compaction_cache") or {})
            if "project" not in item:
                item["project"] = "默认项目"
            normalized.append(item)
        return normalized

    def save_session(
        self,
        messages: List[Dict],
        title: str = None,
        session_id: str = None,
        compaction_state: Dict = None,
        compaction_cache: Dict = None,
        system_prompt: str = None,
        project: str = None,
    ):
        """保存会话"""
        if not messages:
            return

        merged_messages = merge_session_messages(messages)
        session_record = self._build_session_record(
            merged_messages,
            title,
            session_id,
            compaction_state=compaction_state,
            compaction_cache=compaction_cache,
            system_prompt=system_prompt,
            project=project,
        )
        new_session_id = session_record["session_id"]

        # 更新内存缓存
        existing_index = None
        for i, s in enumerate(self._history_sessions):
            if s.get("session_id") == new_session_id:
                existing_index = i
                break

        if existing_index is not None:
            # 更新现有会话时，移动到列表开头以保持与 SQLite ORDER BY updated_at DESC 一致
            self._history_sessions.pop(existing_index)
            self._history_sessions.insert(0, session_record)
        else:
            self._history_sessions.insert(0, session_record)

        self._history_sessions = self._history_sessions[: self._history_limit]

        # 持久化到 SQLite
        self._persist_session(session_record)

    def _persist_session(self, session_record: Dict):
        """持久化单个会话（延迟保存到 SQLite）"""
        if self._use_sqlite and self._session_store:
            self._schedule_save(session_record.get("session_id"))

    def _build_session_record(
        self,
        merged_messages: List[Dict],
        title: str = None,
        session_id: str = None,
        compaction_state: Dict = None,
        compaction_cache: Dict = None,
        system_prompt: str = None,
        project: str = None,
    ) -> Dict:
        now = datetime.now()
        saved_at = now.strftime("%Y-%m-%d %H:%M:%S")
        session_id = session_id or uuid.uuid4().hex[:8]

        merged_messages = self._ensure_message_timestamps(merged_messages, saved_at)
        last_msg_time = self._extract_last_message_time(merged_messages)
        if not title:
            for msg in merged_messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = content_to_text(content)
                    title = content[:30].strip() or "新对话"
                    break
            else:
                title = "新对话"

        return {
            "session_id": session_id,
            "saved_at": saved_at,
            "title": title,
            "project": project or "默认项目",
            "last_time": last_msg_time,
            "messages": merged_messages,
            "message_count": self._count_conversation_pairs(merged_messages),
            "compaction_state": dict(compaction_state or {}),
            "compaction_cache": dict(compaction_cache or {}),
            "system_prompt": system_prompt or "",
            "user_edited_title": False,
        }

    def get_current_title(self, index: int) -> str:
        if 0 <= index < len(self._history_sessions):
            return self._history_sessions[index].get("title", "")
        return ""

    def update_session_title(self, index: int, new_title: str):
        if 0 <= index < len(self._history_sessions):
            self._history_sessions[index]["title"] = new_title

    def set_user_edited_title(self, index: int, edited: bool = True):
        """标记会话标题已被用户编辑"""
        if 0 <= index < len(self._history_sessions):
            self._history_sessions[index]["user_edited_title"] = edited

    def get_user_edited_title(self, index: int) -> bool:
        """获取会话标题是否被用户编辑"""
        if 0 <= index < len(self._history_sessions):
            return self._history_sessions[index].get("user_edited_title", False)
        return False

    def update_topic_summary(self, index: int, summary: str):
        self.update_session_title(index, summary)

    def get_topic_summary(self, index: int) -> str:
        return self.get_current_title(index)

    def should_generate_summary(self, index: int) -> bool:
        if 0 <= index < len(self._history_sessions):
            session = self._history_sessions[index]
            messages = session.get("messages", [])
            user_count = sum(1 for msg in messages if msg.get("role") == "user")
            return user_count >= 1
        return False

    def _count_conversation_pairs(self, messages: List[Dict]) -> int:
        count = 0
        for msg in messages:
            if msg.get("role") == "user":
                count += 1
        return count

    def load_latest_session(self) -> Optional[Dict]:
        if not self._history_sessions:
            return None
        latest = self._history_sessions[0]
        if not latest.get("messages"):
            return None
        return latest

    def load_most_recently_updated_session(self) -> Optional[Dict]:
        """加载最近更新的会话"""
        if not self._history_sessions:
            return None
        most_recent = None
        most_recent_time = None
        for session in self._history_sessions:
            messages = session.get("messages", [])
            if not messages:
                continue
            last_updated = session.get("last_updated") or session.get("last_time") or ""
            if not most_recent_time or last_updated > most_recent_time:
                most_recent_time = last_updated
                most_recent = session
        return most_recent

    def get_history_list(self, project: str = None) -> List[Dict]:
        """获取历史会话列表，可选按项目过滤，按最后对话时间排序"""
        # 先去重
        self._deduplicate_history_sessions()
        
        sessions = self._history_sessions
        if project:
            sessions = [s for s in sessions if s.get("project", "默认项目") == project]
        # 按最后对话时间 last_time 降序排序
        return sorted(sessions, key=lambda x: x.get("last_time", ""), reverse=True)

    def get_projects(self) -> List[str]:
        """获取所有不重复的项目名"""
        if self._use_sqlite and self._session_store:
            return self._session_store.get_projects()
        projects = set()
        for s in self._history_sessions:
            p = s.get("project", "默认项目")
            if p and not p.startswith("__archived__/"):
                projects.add(p)
        if not projects:
            return ["默认项目"]
        return sorted(projects)

    def move_to_project(self, index: int, project: str) -> bool:
        """将会话移动到指定项目"""
        if 0 <= index < len(self._history_sessions):
            self._history_sessions[index]["project"] = project
            session = self._history_sessions[index]
            if self._use_sqlite and self._session_store:
                self._session_store.update_session_project(
                    session.get("session_id"), project
                )
            self._persist_session(session)
            return True
        return False

    def archive_sessions_by_project(self, project: str) -> int:
        """批量归档指定项目的所有会话"""
        if self._use_sqlite and self._session_store:
            count = self._session_store.archive_sessions_by_project(project)
            # 同步内存缓存
            self._history_sessions = [
                s for s in self._history_sessions
                if s.get("project", "默认项目") != project
            ]
            return count
        return 0

    def archive_project(self, project_name: str) -> int:
        """归档整个项目，归档该项目所有会话并从项目列表中移除"""
        # 获取项目下所有会话
        sessions = self.get_history_list(project_name)
        count = 0
        
        for session in sessions:
            title = session.get("title", "未命名")
            last_time = session.get("last_time", datetime.now().strftime("%Y-%m-%d"))
            session_id = session.get("session_id", "unknown")

            # 保存到归档目录 JSON 文件
            safe_title = sanitize_filename(title[:50])
            date_str = last_time[:10] if last_time else datetime.now().strftime("%Y-%m-%d")
            filename = f"{date_str}_{safe_title}_{session_id}.json"
            archive_file = self.archive_dir / filename

            try:
                with open(archive_file, "wb") as f:
                    f.write(json.dumps(serialize_for_json(session), option=json.OPT_INDENT_2))
            except Exception:
                logger.warning(f"[HistoryManager] 归档会话失败: {archive_file}")
                continue

            # 从内存缓存移除
            self._history_sessions = [s for s in self._history_sessions if s.get("session_id") != session_id]

            # 从 SQLite 删除
            if self._use_sqlite and self._session_store:
                self._session_store.delete_session(session_id)

            count += 1

        return count

    def archive_history(self, index: int) -> bool:
        """归档历史记录"""
        if 0 <= index < len(self._history_sessions):
            session = self._history_sessions[index]
            title = session.get("title", "未命名")
            last_time = session.get("last_time", datetime.now().strftime("%Y-%m-%d"))
            session_id = session.get("session_id", "unknown")

            safe_title = sanitize_filename(title[:50])
            date_str = (
                last_time[:10] if last_time else datetime.now().strftime("%Y-%m-%d")
            )
            filename = f"{date_str}_{safe_title}_{session_id}.json"

            archive_file = self.archive_dir / filename

            try:
                with open(archive_file, "wb") as f:
                    f.write(json.dumps(serialize_for_json(session), option=json.OPT_INDENT_2))
            except Exception:
                logger.warning(f"[HistoryManager] 归档失败: {archive_file}")
                return False

            # 从内存缓存移除
            self._history_sessions.pop(index)

            # 从 SQLite 删除
            if self._use_sqlite and self._session_store:
                self._session_store.delete_session(session_id)

            return True
        return False

    def import_from_json(self, file_path: str) -> Optional[Dict]:
        """
        从 JSON 文件导入会话

        Args:
            file_path: JSON 文件路径

        Returns:
            导入的会话数据，失败返回 None
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                data = deserialize_from_json(json.loads(content))

            if not isinstance(data, dict):
                logger.warning(f"[HistoryManager] 导入失败，非法的会话数据格式: {file_path}")
                return None

            # 规范化会话数据
            session = self._normalize_single_session(data)

            # 检查是否已存在相同 session_id 的会话
            existing_session_id = session.get("session_id")
            if existing_session_id:
                existing_index = self.find_index_by_session_id(existing_session_id)
                if existing_index is not None:
                    # 更新已存在的会话，移动到列表开头以保持与 SQLite ORDER BY updated_at DESC 一致
                    self._history_sessions.pop(existing_index)
                    self._history_sessions.insert(0, session)
                    self._schedule_save(existing_session_id)
                    logger.info(f"[HistoryManager] 更新已存在的会话: {existing_session_id}")
                else:
                    # 检查归档目录中是否已有该会话（避免重复导入归档文件）
                    archived_files = list(self.archive_dir.glob(f"*{existing_session_id}*.json"))
                    if archived_files:
                        logger.warning(f"[HistoryManager] 该会话已在归档目录中: {existing_session_id}")
                        # 生成新的 session_id 以避免冲突
                        session["session_id"] = uuid.uuid4().hex[:8]
                        session["title"] = f"[导入] {session.get('title', '新对话')}"

                    # 添加到内存缓存顶部
                    self._history_sessions.insert(0, session)
                    self._history_sessions = self._history_sessions[: self._history_limit]
                    self._schedule_save(session["session_id"])
                    logger.info(f"[HistoryManager] 导入新会话: {session['session_id']}")
            else:
                # 没有 session_id，生成一个新的
                session["session_id"] = uuid.uuid4().hex[:8]
                self._history_sessions.insert(0, session)
                self._history_sessions = self._history_sessions[: self._history_limit]
                self._schedule_save(session["session_id"])

            return session

        except json.JSONDecodeError as e:
            logger.error(f"[HistoryManager] JSON 解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[HistoryManager] 导入失败: {e}")
            return None

    def _normalize_single_session(self, data: Dict) -> Dict:
        """规范化单个会话数据"""
        fallback_ts = (
            data.get("last_time")
            or data.get("saved_at")
            or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        # 规范化消息
        messages = data.get("messages", [])
        if isinstance(messages, list):
            messages = self._ensure_message_timestamps(
                merge_session_messages(messages),
                fallback_ts,
            )
        else:
            messages = []

        # 构建规范化会话
        session = {
            "session_id": data.get("session_id") or uuid.uuid4().hex[:8],
            "saved_at": data.get("saved_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": data.get("title") or data.get("topic_summary") or "导入的对话",
            "last_time": self._extract_last_message_time(messages) or fallback_ts,
            "messages": messages,
            "message_count": self._count_conversation_pairs(messages),
            "compaction_state": dict(data.get("compaction_state") or {}),
            "compaction_cache": dict(data.get("compaction_cache") or {}),
            "system_prompt": data.get("system_prompt") or "",
            "project": data.get("project", "默认项目"),
        }

        return session

    def get_archived_sessions(self) -> List[Dict]:
        """
        获取归档目录中的所有会话文件列表

        Returns:
            文件信息列表 [{'path': str, 'name': str, 'session_id': str, 'title': str}]
        """
        archived_files = []
        if not self.archive_dir.exists():
            return archived_files

        try:
            for json_file in self.archive_dir.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.loads(f.read())
                    archived_files.append({
                        "path": str(json_file),
                        "name": json_file.name,
                        "session_id": data.get("session_id", ""),
                        "title": data.get("title", json_file.stem[:50]),
                    })
                except Exception:
                    logger.error(f"[HistoryManager] 读取归档文件失败: {json_file}")
                    # 跳过损坏的文件
                    continue
        except Exception:
            pass

        # 按修改时间倒序排列（最新的在前）
        archived_files.sort(
            key=lambda x: os.path.getmtime(x["path"]),
            reverse=True
        )
        return archived_files

    def get_session_by_index(self, index: int) -> Optional[List[Dict]]:
        if 0 <= index < len(self._history_sessions):
            session = self._history_sessions[index]
            fallback_ts = (
                session.get("last_time")
                or session.get("saved_at")
                or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            return self._ensure_message_timestamps(
                merge_session_messages(session.get("messages", [])),
                fallback_ts,
            )
        return None

    def get_session_id_by_index(self, index: int) -> Optional[str]:
        if 0 <= index < len(self._history_sessions):
            return self._history_sessions[index].get("session_id")
        return None

    def find_index_by_session_id(self, session_id: str) -> Optional[int]:
        """根据 session_id 查找索引"""
        if not session_id:
            return None
        for i, session in enumerate(self._history_sessions):
            if session.get("session_id") == session_id:
                return i
        return None

    def get_session_by_session_id(self, session_id: str) -> Optional[Dict]:
        """根据 session_id 获取会话（内存优先，SQLite 兜底）"""
        if not session_id:
            return None
        # 1. 先从内存缓存查找（快速路径）
        for session in self._history_sessions:
            if session.get("session_id") == session_id:
                return session
        # 2. 内存没有则直接查 SQLite（跨窗口同步最新数据）
        if self._session_store and self._session_store.is_initialized:
            return self._session_store.get_session(session_id)
        return None

    def get_session_messages(self, session_id: str) -> Optional[List[Dict]]:
        """根据 session_id 获取会话的消息列表"""
        session = self.get_session_by_session_id(session_id)
        if session:
            return session.get("messages", [])
        return None

    def update_session(
        self,
        index: int,
        messages: List[Dict],
        compaction_state: Dict = None,
        compaction_cache: Dict = None,
        system_prompt: str = None,
        project: str = None,
    ):
        """更新会话"""
        if 0 <= index < len(self._history_sessions):
            merged_messages = merge_session_messages(messages)
            existing = self._history_sessions[index]
            updated = self._build_session_record(
                merged_messages,
                title=existing.get("title"),
                session_id=existing.get("session_id"),
                compaction_state=(
                    compaction_state
                    if compaction_state is not None
                    else existing.get("compaction_state", {})
                ),
                compaction_cache=(
                    compaction_cache
                    if compaction_cache is not None
                    else existing.get("compaction_cache", {})
                ),
                system_prompt=(
                    system_prompt
                    if system_prompt is not None
                    else existing.get("system_prompt", "")
                ),
                project=project if project is not None else existing.get("project", "默认项目"),
            )
            # 移动到列表开头以保持与 SQLite ORDER BY updated_at DESC 一致
            self._history_sessions.pop(index)
            self._history_sessions.insert(0, updated)
            self._schedule_save(existing.get("session_id"))

    def _schedule_save(self, session_id: str = None):
        """
        延迟保存会话，指定 session_id 时只保存该会话。
        关键修复：合并对同一 session 的重复保存请求，防止丢失。
        """
        if not session_id:
            return

        # 如果当前已有待保存的请求，且是同一个 session，直接忽略（已被覆盖，无需重复）
        if self._pending_save_session_id == session_id and self._save_timer is not None:
            logger.debug(f"[HistoryManager] 合并保存请求: session_id={session_id[:8]}...")
            return

        # 如果有待保存的不同 session_id，先立即执行（防止丢失）
        if self._pending_save_session_id and self._pending_save_session_id != session_id:
            logger.debug(f"[HistoryManager] 立即保存被覆盖的会话: {self._pending_save_session_id[:8]}...")
            self._do_save()

        self._pending_save_session_id = session_id
        if self._save_timer is None:
            self._save_timer = QTimer.singleShot(self._save_delay_ms, self._do_save)

    def _do_save(self):
        """延迟保存会话到 SQLite"""
        self._save_timer = None

        if not (self._use_sqlite and self._session_store):
            logger.debug("[HistoryManager] SQLite 未就绪，跳过保存")
            self._pending_save_session_id = None
            return

        pending_id = getattr(self, '_pending_save_session_id', None)
        if not pending_id:
            logger.debug("[HistoryManager] 无待保存会话，跳过")
            self._pending_save_session_id = None
            return

        logger.debug(f"[HistoryManager] 保存会话: pending_id={pending_id[:8]}...")
        for session in self._history_sessions:
            if session.get("session_id") == pending_id:
                self._session_store.save_session(session)
                break

        self._pending_save_session_id = None

    def flush(self):
        """立即持久化所有待保存的会话（同步写入 SQLite）

        在应用退出或关键保存点后调用，确保数据不丢失。
        """
        if self._save_timer is not None or self._pending_save_session_id:
            self._do_save()

    def _extract_last_message_time(self, messages: List[Dict]) -> str:
        for msg in reversed(messages or []):
            timestamp = msg.get("timestamp")
            if timestamp:
                return timestamp
        return "未知"

    def _ensure_message_timestamps(
        self, messages: List[Dict], fallback_ts: str
    ) -> List[Dict]:
        normalized: List[Dict] = []
        last_seen_ts = fallback_ts
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            copied = dict(msg)
            timestamp = copied.get("timestamp") or last_seen_ts
            if timestamp:
                copied["timestamp"] = timestamp
                last_seen_ts = timestamp
            normalized.append(copied)
        return normalized

    def get_session_preview(self, index: int, max_len: int = 50) -> str:
        if 0 <= index < len(self._history_sessions):
            messages = self._history_sessions[index].get("messages", [])
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = content_to_text(content)
                    return content[:max_len].strip() + (
                        "..." if len(content) > max_len else ""
                    )
        return ""

    def get_total_storage_size(self) -> int:
        """获取总存储大小"""
        if self._use_sqlite and self._session_store:
            # 估算 SQLite 数据库大小
            from app.utils.utils import get_app_data_dir
            db_path = get_app_data_dir() / "sessions.db"
            if db_path.exists():
                return db_path.stat().st_size

    def get_memory_stats(self) -> Dict:
        total_messages = sum(s.get("message_count", 0) for s in self._history_sessions)
        total_chars = 0
        for session in self._history_sessions:
            for msg in session.get("messages", []):
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = content_to_text(content)
                total_chars += len(content)
        return {
            "session_count": len(self._history_sessions),
            "total_messages": total_messages,
            "total_chars": total_chars,
            "storage_size": self.get_total_storage_size(),
            "storage_mode": "sqlite" if self._use_sqlite else "json",
        }