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
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import orjson as json
from loguru import logger
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from app.core.message_content import consolidate_messages, content_to_text
from app.core.store import SessionStore
from app.core.token_estimator import count_messages_tokens
from app.utils.utils import deserialize_from_json, get_app_data_dir, serialize_for_json


def _clean_orphan_tool_calls(messages: List[Dict]) -> List[Dict]:
    """清理消息列表中的孤立 tool_calls（没有对应 tool 结果的 tool_call）

    这是持久化前的守门员：无论内部数据流经过多少层变换，
    在落地前统一移除 orphan，保证下一轮加载时消息天然干净。

    逻辑同 chat_worker._fix_tool_result_order，但作为独立函数
    放在持久化层，不依赖 worker 上下文。
    """
    # 收集所有有效的 tool_call_id（来自 tool 消息）
    valid_ids = {
        msg.get("tool_call_id", "") for msg in messages if msg.get("role") == "tool" and msg.get("tool_call_id")
    }

    if not valid_ids:
        # 没有任何 tool 消息时，检查是否存在带 tool_calls 的 assistant 消息
        has_orphan = any(msg.get("role") == "assistant" and msg.get("tool_calls") for msg in messages)
        if has_orphan:
            cleaned = []
            for msg in messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    msg = dict(msg)
                    msg.pop("tool_calls", None)
                    if msg.get("content") is None:
                        msg["content"] = ""
                cleaned.append(msg)
            return cleaned
        return messages

    # 清理每个 assistant 消息中的孤立 tool_calls
    cleaned = []
    for msg in messages:
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            cleaned.append(msg)
            continue

        tool_calls = msg["tool_calls"]
        # 只保留有对应结果的 tool_call
        kept = [tc for tc in tool_calls if tc.get("id") in valid_ids]

        if not kept:
            # 全被移除了，去掉 tool_calls 字段
            msg = dict(msg)
            msg.pop("tool_calls", None)
            if msg.get("content") is None:
                msg["content"] = ""
        else:
            # 部分保留：只保留有对应结果的 tool_call
            msg = dict(msg)
            msg["tool_calls"] = kept

        cleaned.append(msg)

    return cleaned


# 预编译文件名清理正则
_SANITIZE_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*]')


def merge_session_messages(messages: List[Dict]) -> List[Dict]:
    return consolidate_messages(messages or [])


def extract_message_preview(messages: List[Dict], max_len: int = 50) -> str:
    """从消息列表中提取预览文本（用于历史列表展示，避免遍历完整消息）"""
    if not messages:
        return ""
    for msg in reversed(messages):
        # 跳过 hook 消息（role=user 但带 _hook_event），避免预览显示 hook 内容
        if msg.get("_hook_event"):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user" and content:
            if isinstance(content, list):
                from app.core.message_content import content_to_text

                content = content_to_text(content)
            return content[:max_len].strip() + ("..." if len(content) > max_len else "")
    return ""


def sanitize_filename(name: str) -> str:
    """移除文件名中不合法的字符"""
    return _SANITIZE_FILENAME_PATTERN.sub("_", name)


# ============================================================
# 异步归档扫描（性能优化）
# ============================================================
#
# 历史问题：归档页（"项目一多"）卡顿的根因是主线程同步读取+解析 N 个
# JSON 文件。即便 N=200 也会在慢盘/网络盘/AV 扫描场景下卡 0.5-2s。
# 优化：把 glob + stat + 读 + 解析全部搬到后台 QRunnable，缓存按 mtime 失效，
# 主线程仅接收最终 enriched_list 一次性渲染。
#
# request_id 机制：每次扫描自增 ID，主线程对比当前 ID 丢弃过期结果，
# 防止快速来回切换 tab 导致旧数据覆盖新数据。


def _build_archive_preview(messages: List[Dict], max_len: int = 50) -> str:
    """从消息列表中提取归档预览文本（与 history_card.get_message_preview 等价，
    保持单点定义以避免循环依赖）。"""
    if not messages:
        return ""
    for msg in reversed(messages):
        # 跳过 hook 消息（role=user 但带 _hook_event），避免预览显示 hook 内容
        if msg.get("_hook_event"):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not content:
            continue
        if isinstance(content, list):
            content = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        return content[:max_len].strip() + ("..." if len(content) > max_len else "")
    return ""


class _ArchiveScanSignals(QObject):
    """归档扫描 worker 信号：用于从后台线程回到主线程交付结果。"""

    finished = pyqtSignal(int, list)  # request_id, enriched_list


class _ArchiveScanTask(QRunnable):
    """异步扫描归档目录的 QRunnable。

    工作流程（在后台线程）：
    1. listdir + stat 拿到所有归档文件的 mtime（不读内容）
    2. 对比 self._cache，mtime 未变 → 复用缓存
    3. mtime 变了或无缓存 → 打开 + json.loads + 提取 preview
    4. 按 mtime 倒序排序后通过 signals.finished 发回主线程
    """

    def __init__(
        self,
        archive_dir: Path,
        cache: Dict[str, Tuple[float, Dict]],
        cache_lock: threading.Lock,
        request_id: int,
        signals: _ArchiveScanSignals,
    ) -> None:
        super().__init__()
        self._archive_dir = archive_dir
        self._cache = cache
        self._cache_lock = cache_lock
        self._request_id = request_id
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            if not self._archive_dir.exists():
                self._signals.finished.emit(self._request_id, [])
                return

            try:
                files = list(self._archive_dir.glob("*.json"))
            except Exception as e:
                logger.warning(f"[ArchiveScan] 列出归档目录失败: {e}")
                self._signals.finished.emit(self._request_id, [])
                return

            if not files:
                self._signals.finished.emit(self._request_id, [])
                return

            # 阶段 1：批量取 mtime（只 stat，不读内容，AV 通常不拦）
            mtimes: Dict[str, float] = {}
            for fp in files:
                fp_str = str(fp)
                try:
                    mtimes[fp_str] = os.path.getmtime(fp)
                except OSError:
                    mtimes[fp_str] = 0.0

            enriched: List[Dict[str, Any]] = []
            # 阶段 2：在锁内依次判断：命中缓存则复用，否则读+解析
            for fp_str, mtime in mtimes.items():
                fp_path = Path(fp_str)
                info: Optional[Dict[str, Any]] = None
                need_read = True
                with self._cache_lock:
                    cached = self._cache.get(fp_str)
                    if cached and cached[0] == mtime:
                        info = cached[1]
                        need_read = False

                if not need_read and info is not None:
                    enriched.append(self._enrich_from_info(fp_str, fp_path, mtime, info))
                    continue

                # 缓存未命中或过期 → 读+解析（这是慢的部分，发生在后台线程）
                parsed = self._read_and_parse(fp_path)
                if parsed is None:
                    continue
                info = {
                    "session_id": parsed.get("session_id", ""),
                    "title": parsed.get("title", fp_path.stem[:50]),
                    "last_time": parsed.get("last_time", ""),
                    "message_count": parsed.get("message_count", 0),
                    "preview": parsed.get("preview", ""),
                    "project": parsed.get("project", ""),
                }
                with self._cache_lock:
                    self._cache[fp_str] = (mtime, info)
                enriched.append(self._enrich_from_info(fp_str, fp_path, mtime, info))

            # 按修改时间倒序
            enriched.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
            self._signals.finished.emit(self._request_id, enriched)
        except Exception as e:
            logger.exception(f"[ArchiveScan] 扫描异常: {e}")
            self._signals.finished.emit(self._request_id, [])

    @staticmethod
    def _enrich_from_info(fp_str: str, fp_path: Path, mtime: float, info: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "path": fp_str,
            "name": fp_path.name,
            "session_id": info.get("session_id", ""),
            "title": info.get("title", fp_path.stem[:50]),
            "last_time": info.get("last_time", ""),
            "message_count": info.get("message_count", 0),
            "preview": info.get("preview", ""),
            "project": info.get("project", ""),
            "mtime": mtime,
        }

    @staticmethod
    def _read_and_parse(fp_path: Path) -> Optional[Dict[str, Any]]:
        """读取单个归档 JSON，提取卡片所需的轻量字段。"""
        try:
            with open(fp_path, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
        except Exception as e:
            logger.warning(f"[ArchiveScan] 读取失败: {fp_path}: {e}")
            return None

        if not isinstance(data, dict):
            return None

        messages = data.get("messages", []) or []
        msg_count = data.get(
            "message_count",
            len([m for m in messages if m.get("role") == "user" and not m.get("_hook_event")]),
        )
        last_time = data.get("last_time") or data.get("saved_at", "")
        return {
            "session_id": data.get("session_id", ""),
            "title": data.get("title", fp_path.stem[:50]),
            "last_time": last_time,
            "message_count": msg_count,
            "preview": _build_archive_preview(messages, 50),
            "project": data.get("project", ""),
        }


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
        self._history_loaded = False  # 懒加载标记：首次访问 _history_sessions 时从 SQLite 加载

        # === 异步归档扫描（性能优化）===
        # _archive_meta_cache：file_path → (mtime, info_dict)
        # 仅在后台 _ArchiveScanTask 线程内访问，访问路径都用 _archive_cache_lock 保护。
        self._archive_meta_cache: Dict[str, Tuple[float, Dict]] = {}
        self._archive_cache_lock = threading.Lock()
        # 每次发起扫描自增；主线程接收结果时对比 _current_scan_request_id 丢弃过期数据。
        self._current_scan_request_id: int = 0
        # signals 必须在主线程创建（Qt 要求 QObject 的线程亲和性）。
        self._archive_scan_signals = _ArchiveScanSignals()
        self._archive_scan_signals.finished.connect(self._on_archive_scan_finished)
        # 当前扫描对应的回调（仅主线程访问，无需锁）。
        self._pending_archive_callback: Optional[Callable[[List[Dict]], None]] = None

        # 初始化存储
        self._init_storage()

    def _deduplicate_history_sessions(self):
        """去重历史会话列表，保持最新的一个

        原实现使用 list.insert(0, ...) 在 reversed 循环里反复前插，是 O(n²)。
        改为基于 dict 保持插入顺序的单次遍历，O(n)。
        列表本身已按 updated_at DESC 排序（最新在索引 0），
        因此正向遍历首次出现的即为最新，保留即可。
        """
        seen_ids = set()
        unique_sessions = []
        for session in self._history_sessions:
            session_id = session.get("session_id")
            if session_id and session_id not in seen_ids:
                seen_ids.add(session_id)
                unique_sessions.append(session)
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
                    logger.info("[HistoryManager] SQLite 存储已启用")

                    # 懒加载：启动时不查 SQLite，首次访问 _history_sessions 时再加载
                    # 避免启动时 SELECT 500 条会话拖慢 UI 首帧
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
            fallback_ts = item.get("last_time") or item.get("saved_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            item["messages"] = self._ensure_message_timestamps(
                merge_session_messages(item.get("messages", [])),
                fallback_ts,
            )
            if "title" not in item:
                item["title"] = item.get("topic_summary", "新对话")
            if "last_time" not in item:
                item["last_time"] = self._extract_last_message_time(item.get("messages", []))
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
        worktree_path: str = None,
        last_api_prompt_tokens: int = 0,
        last_api_message_count: int = 0,
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
            worktree_path=worktree_path,
            last_api_prompt_tokens=last_api_prompt_tokens,
            last_api_message_count=last_api_message_count,
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
        worktree_path: str = None,
        last_api_prompt_tokens: int = 0,
        last_api_message_count: int = 0,
    ) -> Dict:
        now = datetime.now()
        saved_at = now.strftime("%Y-%m-%d %H:%M:%S")
        session_id = session_id or uuid.uuid4().hex[:8]

        merged_messages = self._ensure_message_timestamps(merged_messages, saved_at)
        last_msg_time = self._extract_last_message_time(merged_messages)
        if not title:
            for msg in merged_messages:
                if msg.get("role") == "user" and not msg.get("_hook_event"):
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
            "preview": extract_message_preview(merged_messages),
            "compaction_state": dict(compaction_state or {}),
            "compaction_cache": dict(compaction_cache or {}),
            "system_prompt": system_prompt or "",
            "user_edited_title": False,
            "worktree_path": worktree_path or "",
            "context_usage": count_messages_tokens(merged_messages),
            "last_api_prompt_tokens": last_api_prompt_tokens,
            "last_api_message_count": last_api_message_count,
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
            user_count = sum(1 for msg in messages if msg.get("role") == "user" and not msg.get("_hook_event"))
            return user_count >= 1
        return False

    def _count_conversation_pairs(self, messages: List[Dict]) -> int:
        count = 0
        for msg in messages:
            if msg.get("role") == "user" and not msg.get("_hook_event"):
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

    def _ensure_history_loaded(self):
        """懒加载历史会话数据（首次访问时从 SQLite 加载）"""
        if self._history_loaded or not self._use_sqlite:
            return
        self._history_loaded = True
        try:
            self._history_sessions = self._session_store.get_sessions_lightweight(limit=500)
            self._deduplicate_history_sessions()
            self._migrate_if_needed()
        except Exception as e:
            logger.warning(f"[HistoryManager] 懒加载历史会话异常: {e}")

    def get_history_list(self, project: str = None, with_messages: bool = False) -> List[Dict]:
        """获取历史会话列表，可选按项目过滤，按最后对话时间排序

        Args:
            project: 项目名过滤
            with_messages: 是否包含完整消息数组（为 False 时返回轻量列表）
        """
        self._ensure_history_loaded()
        # 先去重
        self._deduplicate_history_sessions()

        sessions = self._history_sessions
        if project:
            sessions = [s for s in sessions if s.get("project", "默认项目") == project]
        # 按最后对话时间 last_time 降序排序
        sessions = sorted(sessions, key=lambda x: x.get("last_time", ""), reverse=True)

        if not with_messages:
            # 轻量模式：仅保留列表展示所需字段，剔除重量级字段
            # 同时确保 preview 字段存在（兼容旧数据）
            result = []
            for s in sessions:
                preview = s.get("preview")
                if not preview:
                    preview = extract_message_preview(s.get("messages", []), 50)
                result.append(
                    {
                        "session_id": s.get("session_id", ""),
                        "saved_at": s.get("saved_at", ""),
                        "title": s.get("title", ""),
                        "project": s.get("project", "默认项目"),
                        "last_time": s.get("last_time", ""),
                        "message_count": s.get("message_count", 0),
                        "preview": preview,
                        "user_edited_title": s.get("user_edited_title", False),
                        "worktree_path": s.get("worktree_path", "") or "",
                    }
                )
            return result

        return sessions

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
                self._session_store.update_session_project(session.get("session_id"), project)
            self._persist_session(session)
            return True
        return False

    def archive_sessions_by_project(self, project: str) -> int:
        """批量归档指定项目的所有会话"""
        if self._use_sqlite and self._session_store:
            count = self._session_store.archive_sessions_by_project(project)
            # 同步内存缓存
            self._history_sessions = [s for s in self._history_sessions if s.get("project", "默认项目") != project]
            return count
        return 0

    def archive_project(self, project_name: str) -> int:
        """归档整个项目，归档该项目所有会话并从项目列表中移除"""
        # 获取项目下所有会话
        sessions = self.get_history_list(project_name)
        count = 0

        for session in sessions:
            session_id = session.get("session_id", "")

            # 🛡️ 如果 messages 为空（轻量模式），通过 session_id 懒加载完整数据
            if not session.get("messages") and session_id:
                full_session = self.get_session_by_session_id(session_id)
                if full_session and full_session.get("messages"):
                    session = full_session

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

            # 🛡️ 如果 messages 为空（轻量模式），通过 session_id 懒加载完整数据
            if not session.get("messages"):
                session_id = session.get("session_id", "")
                if session_id:
                    full_session = self.get_session_by_session_id(session_id)
                    if full_session and full_session.get("messages"):
                        session = full_session

            title = session.get("title", "未命名")
            last_time = session.get("last_time", datetime.now().strftime("%Y-%m-%d"))
            session_id = session.get("session_id", "unknown")

            safe_title = sanitize_filename(title[:50])
            date_str = last_time[:10] if last_time else datetime.now().strftime("%Y-%m-%d")
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
                    # 🛡️ 排除当前正在导入的文件自身，避免「自己的影子」触发 session_id 重生成
                    archived_files = [
                        f for f in self.archive_dir.glob(f"*{existing_session_id}*.json") if str(f) != file_path
                    ]
                    if archived_files:
                        logger.warning(
                            f"[HistoryManager] 该会话已在归档目录中: {existing_session_id} ({len(archived_files)} 个其他文件)"
                        )
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
        fallback_ts = data.get("last_time") or data.get("saved_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
            "context_usage": count_messages_tokens(messages),
        }

        return session

    # ============================================================
    # 归档扫描 API（异步，线程安全）
    # ============================================================

    def get_archived_session_count(self) -> int:
        """快速获取归档文件数量（仅 glob，不读内容）。

        用于在异步扫描期间显示骨架屏占位的初始数量。
        """
        if not self.archive_dir.exists():
            return 0
        try:
            return sum(1 for _ in self.archive_dir.glob("*.json"))
        except Exception:
            return 0

    def scan_archives_async(self, callback: Callable[[List[Dict]], None]) -> int:
        """异步扫描归档目录，结果通过 callback 在主线程交付。

        - 全部 I/O + JSON 解析在后台 QRunnable 线程完成
        - 已读取过的文件按 mtime 命中缓存，不会被重复打开
        - 若在结果返回前再次调用，旧回调被新回调覆盖、request_id 自增，
          旧结果会通过 request_id 校验自动丢弃

        Args:
            callback: 接收 enriched_list 的主线程回调

        Returns:
            request_id（用于测试/调试追踪）
        """
        with self._archive_cache_lock:
            self._current_scan_request_id += 1
            request_id = self._current_scan_request_id

        # 主线程内赋值，与 _on_archive_scan_finished 都在主线程执行，无竞争。
        self._pending_archive_callback = callback

        task = _ArchiveScanTask(
            archive_dir=self.archive_dir,
            cache=self._archive_meta_cache,
            cache_lock=self._archive_cache_lock,
            request_id=request_id,
            signals=self._archive_scan_signals,
        )
        QThreadPool.globalInstance().start(task)
        return request_id

    def _on_archive_scan_finished(self, request_id: int, enriched: List[Dict]) -> None:
        """后台扫描完成的主线程回调（Qt signal handler）。"""
        with self._archive_cache_lock:
            current_id = self._current_scan_request_id
        if request_id != current_id:
            # 过期结果：用户已切换 tab 或重新触发扫描
            logger.debug(f"[HistoryManager] 丢弃过期归档扫描结果: req={request_id} current={current_id}")
            return
        callback = self._pending_archive_callback
        self._pending_archive_callback = None
        if callback is None:
            return
        try:
            callback(enriched)
        except Exception as e:
            logger.exception(f"[HistoryManager] 归档扫描回调异常: {e}")

    def invalidate_archive_cache(self, file_path: Optional[str] = None) -> None:
        """失效归档元数据缓存。

        Args:
            file_path: 仅失效该路径；传 None 清空全部。
        """
        with self._archive_cache_lock:
            if file_path is None:
                self._archive_meta_cache.clear()
            else:
                self._archive_meta_cache.pop(file_path, None)

    def get_archived_sessions(self) -> List[Dict]:
        """同步获取归档列表（兼容旧调用方，UI 层请优先使用 scan_archives_async）。

        该方法阻塞主线程：先调用 get_archived_session_count 提示归零，
        实际读取仍走 N 次文件 I/O。仅在插件/测试场景下使用。
        """
        archived_files: List[Dict] = []
        if not self.archive_dir.exists():
            return archived_files

        try:
            for json_file in self.archive_dir.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.loads(f.read())
                    archived_files.append(
                        {
                            "path": str(json_file),
                            "name": json_file.name,
                            "session_id": data.get("session_id", ""),
                            "title": data.get("title", json_file.stem[:50]),
                        }
                    )
                except Exception:
                    logger.error(f"[HistoryManager] 读取归档文件失败: {json_file}")
                    continue
        except Exception:
            pass

        archived_files.sort(
            key=lambda x: os.path.getmtime(x["path"]),
            reverse=True,
        )
        return archived_files

    def get_session_by_index(self, index: int) -> Optional[List[Dict]]:
        if 0 <= index < len(self._history_sessions):
            session = self._history_sessions[index]
            # 💡 如果 messages 为空（轻量模式），通过 session_id 懒加载
            if not session.get("messages"):
                session_id = session.get("session_id")
                if session_id:
                    full_session = self.get_session_by_session_id(session_id)
                    if full_session:
                        session = full_session
            fallback_ts = (
                session.get("last_time") or session.get("saved_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        """根据 session_id 获取会话（内存优先，SQLite 兜底）

        💡 内存优化：如果内存缓存的 messages 为空（轻量模式），
        自动从 SQLite 按需加载并回填，避免启动时一次性反序列化所有消息。
        """
        if not session_id:
            return None
        # 1. 先从内存缓存查找（快速路径）
        for session in self._history_sessions:
            if session.get("session_id") == session_id:
                # 如果 messages 为空（轻量模式），从 SQLite 懒加载
                if not session.get("messages") and self._session_store and self._session_store.is_initialized:
                    full = self._session_store.get_session(session_id)
                    if full:
                        session["messages"] = full.get("messages", [])
                        session["message_count"] = full.get("message_count", len(session["messages"]))
                return session
        # 2. 内存没有则直接查 SQLite（跨窗口同步最新数据）
        if self._session_store and self._session_store.is_initialized:
            return self._session_store.get_session(session_id)
        return None

    def get_session_messages(self, session_id: str) -> Optional[List[Dict]]:
        """根据 session_id 获取会话的消息列表

        💡 内存优化：委托 get_session_by_session_id 处理懒加载，
        避免启动时一次性反序列化所有消息。
        """
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
        worktree_path: str = None,
    ):
        """更新会话"""
        if 0 <= index < len(self._history_sessions):
            merged_messages = merge_session_messages(messages)
            # 🛡️ 落地前清理孤立 tool_calls，保证下一轮加载时消息天然干净
            merged_messages = _clean_orphan_tool_calls(merged_messages)
            existing = self._history_sessions[index]
            updated = self._build_session_record(
                merged_messages,
                title=existing.get("title"),
                session_id=existing.get("session_id"),
                compaction_state=(
                    compaction_state if compaction_state is not None else existing.get("compaction_state", {})
                ),
                compaction_cache=(
                    compaction_cache if compaction_cache is not None else existing.get("compaction_cache", {})
                ),
                system_prompt=(system_prompt if system_prompt is not None else existing.get("system_prompt", "")),
                project=project if project is not None else existing.get("project", "默认项目"),
                worktree_path=worktree_path if worktree_path is not None else existing.get("worktree_path", ""),
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

        pending_id = getattr(self, "_pending_save_session_id", None)
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

    def _ensure_message_timestamps(self, messages: List[Dict], fallback_ts: str) -> List[Dict]:
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
                if msg.get("_hook_event"):
                    continue
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = content_to_text(content)
                    return content[:max_len].strip() + ("..." if len(content) > max_len else "")
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
