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
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import orjson as json
from loguru import logger
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from app.core.message_content import consolidate_messages, content_to_text
from app.core.token_estimator import count_messages_tokens
from app.utils.utils import deserialize_from_json, get_app_data_dir, serialize_for_json


def parse_team_members_snapshot(snap_raw: str) -> List[Dict]:
    """解析团队会话落库的 team_members 快照 JSON → 成员记录列表。

    兼容两种形态（T3 快照升级前后）：
    - 老格式 `'["build","plan"]'`（str 元素）→ {"agent_name": s, "window_id": ""}
    - 新格式 `'[{"agent_name":"build","window_id":"win_01"}]'`（dict 元素）→
      保留 window_id（同角色多成员按 window_id 区分）

    Args:
        snap_raw: 快照 JSON 字符串（可能为空串 / 非法 JSON / 非 list）

    Returns:
        去重后的成员记录列表；解析失败返回空列表
    """
    if not snap_raw:
        return []
    try:
        parsed = json.loads(snap_raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: List[Dict] = []
    for item in parsed:
        if isinstance(item, dict):
            rec = {
                "agent_name": str(item.get("agent_name") or "").strip(),
                "window_id": str(item.get("window_id") or "").strip(),
            }
        else:
            rec = {"agent_name": str(item).strip(), "window_id": ""}
        if rec["agent_name"] and rec not in out:
            out.append(rec)
    return out


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

    # 反向清理孤儿 tool 消息（结果存在，但之前的 assistant 均未声明该 id）。
    # 逻辑同 chat_worker._fix_tool_result_order 第三步，保证持久化后的历史
    # 不会触发 MiniMax 2013（tool result's tool id not found）。
    final_messages = []
    declared_ids: set = set()
    for msg in cleaned:
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id", "")
                if tc_id:
                    declared_ids.add(tc_id)
            final_messages.append(msg)
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id and tc_id not in declared_ids:
                continue
            final_messages.append(msg)
        else:
            final_messages.append(msg)

    return final_messages


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
            len(
                [
                    m
                    for m in messages
                    if m.get("role") == "user" and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
                ]
            ),
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

        # 与 SQLite 轻量懒加载上限保持一致，避免首次加载后保存任意会话又截断。
        # 5000：旧值 500 会让更旧的会话彻底不出现在列表里（UI 只读内存、不分页）。
        # 轻量投影已剔除 system_prompt，单条约 0.16KB，5000 条常驻约 0.8MB，可接受。
        self._history_limit = 5000
        self._save_timer: Optional[QTimer] = None
        self._save_delay_ms = 1000

        # 延迟保存的待处理会话 ID
        self._pending_save_session_id: Optional[str] = None

        # SQLite 存储层
        self._session_store: Optional[Any] = None
        self._use_sqlite = False

        # 内存缓存
        self._history_sessions: List[Dict] = []
        self._history_loaded = False  # 懒加载标记：首次访问 _history_sessions 时从 SQLite 加载
        # 跨进程一致性：底层 SQLite（db + -wal）的变更签名（mtime 元组）。
        # 其他 OS 进程提交后签名变化 → _reload_if_db_changed 触发重载，
        # 避免各窗口历史卡片/欢迎卡片互相陈旧、且重开也无法刷新。
        self._db_signature_stored: Optional[tuple] = None
        # 🛡️ H1（T4-TOP8）：保护懒加载"检查-加载"原子性的锁。
        # 后台预热线程与主线程首次访问可能并发进入 _ensure_history_loaded，
        # 必须串行化防止双重加载/读到半初始化 _history_sessions。
        self._history_load_lock = threading.Lock()

        # === 轻量列表缓存 ===
        # get_history_list() 的结果缓存，避免每次调用都排序+过滤+重建 dict。
        # key: project (None=全部) 或 ("merge_team", project)（合并模式独立键），
        # value: [list_of_lightweight_dicts]。
        # 当 _history_sessions 发生变化时置 _cache_dirty = True。
        self._cache_dirty: bool = True
        self._cached_lightweight: Dict[object, List[Dict]] = {}

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

        # 🛡️ H1（T4-TOP8）：后台线程预热历史会话——启动后提前加载 SQLite 历史，
        # 用户首次打开历史面板时数据已就绪（主线程 0 阻塞）。仅 SQLite 模式生效；
        # 预热失败静默（主路径 _ensure_history_loaded 兜底重试）。
        self._prewarm_history()

    def _mark_cache_dirty(self):
        """标记轻量列表缓存为脏，下次 get_history_list 时重新构建"""
        self._cache_dirty = True

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
            self._mark_cache_dirty()
        else:
            self._history_sessions = unique_sessions

    def _init_storage(self):
        """初始化存储层（经 backend 门面获取活跃引擎，行为等价 SessionStore）"""
        use_sqlite = os.environ.get("LLM_SESSION_SQLITE", "1") == "1"

        if use_sqlite:
            try:
                # 函数体内延迟 import：避免与 backend 循环导入
                from app.core.backend import get_session_storage

                engine = get_session_storage()
                # hasattr 降级：引擎无 is_initialized（第三方实现）→ 视为未启用 SQLite
                if getattr(engine, "is_initialized", False):
                    self._session_store = engine
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
        team_run_id: str = None,
        team_name: str = None,
        agent_name: str = None,
        team_members: str = None,
    ):
        """保存会话

        Args:
            team_run_id: 团队运行标识（方案 A 团队会话恢复；非团队会话留空）
                None 表示保留现值（与 update_session 语义对齐）——当会话已
                存在（内存/SQLite）时不覆盖其团队元数据，防止普通编辑把
                团队会话的清空（被 _history_limit 截断出内存的会话无法走
                update 保留路径，save 分支必须兜底）；显式传空串 "" 才清空。
            team_name: 团队名（模板名），None 保留现值
            agent_name: 产出该会话的 agent 角色名，None 保留现值
            team_members: 团队成员快照（JSON 字符串，F3），None 保留现值
        """
        if not messages:
            return

        # 保存前先完成 SQLite 懒加载，避免新会话进入待保存队列后，
        # 首次打开历史面板又用数据库快照覆盖内存记录，导致延迟保存找不到目标。
        self._ensure_history_loaded()

        # 🛡️ None→保留现值：与 update_session 语义对齐，避免 save 分支
        # （会话被挤出 _history_limit 后走 INSERT OR REPLACE）用空串覆盖
        # 团队元数据，导致团队会话从历史分组消失（数据损坏，不可逆）。
        if team_run_id is None or team_name is None or agent_name is None or team_members is None:
            existing = None
            if session_id:
                existing = self.get_session_by_session_id(session_id)
            if existing:
                if team_run_id is None:
                    team_run_id = existing.get("team_run_id", "") or ""
                if team_name is None:
                    team_name = existing.get("team_name", "") or ""
                if agent_name is None:
                    agent_name = existing.get("agent_name", "") or ""
                if team_members is None:
                    team_members = existing.get("team_members", "") or ""
            else:
                # 无现有记录（全新会话）：None 回落空串（与新会话默认一致）
                team_run_id = team_run_id or ""
                team_name = team_name or ""
                agent_name = agent_name or ""
                team_members = team_members or ""

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
            team_run_id=team_run_id,
            team_name=team_name,
            agent_name=agent_name,
            team_members=team_members,
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
        self._mark_cache_dirty()

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
        team_run_id: str = "",
        team_name: str = "",
        agent_name: str = "",
        team_members: str = "",
    ) -> Dict:
        now = datetime.now()
        saved_at = now.strftime("%Y-%m-%d %H:%M:%S")
        session_id = session_id or uuid.uuid4().hex[:8]

        merged_messages = self._ensure_message_timestamps(merged_messages, saved_at)
        last_msg_time = self._extract_last_message_time(merged_messages)
        if not title:
            for msg in merged_messages:
                if msg.get("role") == "user" and not msg.get("_hook_event"):
                    # R3 风格防御：跳过未打标的任务邮件注入路径（旧数据无
                    # _hook_event 的 "📨 **来自 [...] 的任务邮件：**" 文本），
                    # 与 get_team_first_question 的防御对齐，title 不被邮件污染。
                    if str(msg.get("content", "")).startswith("📨 **来自"):
                        continue
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
            # 团队元数据（方案 A 团队会话恢复基础；非团队会话保持空串）
            "team_run_id": team_run_id or "",
            "team_name": team_name or "",
            "agent_name": agent_name or "",
            # 团队成员快照（F3：JSON 字符串，恢复时找回无会话记录的手动成员）
            "team_members": team_members or "",
        }

    def get_current_title(self, index: int) -> str:
        if 0 <= index < len(self._history_sessions):
            return self._history_sessions[index].get("title", "")
        return ""

    def update_session_title(self, index: int, new_title: str):
        if 0 <= index < len(self._history_sessions):
            self._history_sessions[index]["title"] = new_title
            self._cache_dirty = True

    def set_user_edited_title(self, index: int, edited: bool = True):
        """标记会话标题已被用户编辑"""
        if 0 <= index < len(self._history_sessions):
            self._history_sessions[index]["user_edited_title"] = edited
            self._cache_dirty = True

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
            # 口径与 _count_conversation_pairs（L640）一致：TeamMail 视为真实 user 轮次，
            # 其他 hook 排除（R1 残余清理）。
            user_count = sum(
                1
                for msg in messages
                if msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            )
            return user_count >= 1
        return False

    def _count_conversation_pairs(self, messages: List[Dict]) -> int:
        count = 0
        for msg in messages:
            # 🛡️ P1 修复（T2）：TeamMail（_hook_event="TeamMail"）视为真实用户轮次，
            # 与 group_messages_for_display / get_user_round_ranges / batch 前缀计数
            # 口径一致——此前排除 TeamMail 导致团队会话 message_count 少计（邮件轮次
            # 不计入），合并条目 message_count 累加也随之偏小。普通会话无 TeamMail
            # 行为不变；其他 hook（SessionStart 等）仍被排除。
            if msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail"):
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
        """懒加载历史会话数据（首次访问时从 SQLite 加载）

        🛡️ H1（T4-TOP8）：持 _history_load_lock 保护"检查-加载"原子性——
        后台预热线程（_prewarm_history）与主线程首次访问可能并发进入：
        - 预热已完成：主线程检查 _history_loaded 直接返回（0 阻塞）
        - 预热进行中：主线程等待锁（至多=查询耗时，与原同步方案等价，
          不劣化；预热把耗时从"用户操作时"提前到"启动后台"）
        - 预热失败：_history_loaded 不置 True，主线程首次访问兜底重试
          （与原惰性加载行为一致，功能不降级）
        - 二次检查 + 条件赋值：SQLite 查询完成后再次检查 _history_loaded，
          且赋值前条件判断（GIL 原子）——查询/检查期间外部（测试注入/
          主线程抢先）已置 True 或写入 _history_sessions 时以外部为准，
          不覆盖（消除 H1-R3 极端竞态窗口）
        """
        if not self._use_sqlite:
            return
        with self._history_load_lock:
            if self._history_loaded:
                return
            try:
                sessions = self._session_store.get_sessions_lightweight(limit=self._history_limit)
                # 二次检查 + 条件赋值：查询期间外部可能已加载/注入，
                # 以外部数据为准，不覆盖（GIL 内条件赋值原子）
                if not self._history_loaded:
                    self._history_sessions = sessions
                    self._deduplicate_history_sessions()
                    self._migrate_if_needed()
                    self._history_loaded = True
                    # 记录初始变更签名，避免首次读取时误判为"外部变更"触发冗余重载
                    self._db_signature_stored = self._db_signature()
            except Exception as e:
                logger.warning(f"[HistoryManager] 懒加载历史会话异常: {e}")

    def _prewarm_history(self):
        """后台线程预热历史会话（T4-TOP8）：首次历史加载从主线程移到后台

        应用启动后（HistoryManager 构造完成）延迟 1s 启动后台线程执行
        _ensure_history_loaded，用户首次打开历史面板时数据已就绪，主线程
        首开历史面板 0 阻塞（预热完成后）。

        🛡️ H1-R2（devil-advocate 挑刺）：延迟 1s 而非构造后立即预热——
        避免与窗口首帧创建/布局争 IO/CPU（用户不可能在启动后 1s 内打开
        历史面板，延迟对目标零影响，消除启动首帧争抢）。
        用 threading.Timer 而非 QTimer.singleShot：回调在 timer 线程执行
        （真正的后台线程），主线程不阻塞；且不依赖 Qt 事件循环。

        🛡️ H1-R3：延迟 1s 同时消除测试注入竞态——测试构造后立即注入
        _history_loaded=True 必然早于预热启动，预热查询后二次检查命中
        直接返回，不覆盖注入数据。

        线程安全论证：
        - db_manager 读路径线程安全（P4 已确认）：WAL + 读写锁分离 +
          check_same_thread=False，SELECT 走共享读锁，可与写路径并发
        - 本线程仅调用 _ensure_history_loaded，由 _history_load_lock 与
          主线程首次访问串行化（不会双重加载/半初始化）
        - 预热失败静默：_history_loaded 不置 True，主路径兜底重试（硬约束：
          预热不能改变"用户访问时数据正确"的保证）
        - daemon 线程：退出时不等待；被杀时赋值单条原子（无半状态）、
          _history_loaded 未置 True（主路径兜底）
        """
        if not self._use_sqlite:
            return

        def _do_prewarm():
            try:
                self._ensure_history_loaded()
            except Exception as e:
                logger.warning(f"[HistoryManager] 历史预热异常: {e}")

        # threading.Timer 不支持 name/daemon 参数：用 Thread 包装实现
        # 延迟 1s + daemon（应用退出不等待）
        def _delayed_start():
            try:
                time.sleep(1.0)
                _do_prewarm()
            except Exception:
                pass

        threading.Thread(target=_delayed_start, daemon=True, name="history-prewarm").start()

    def _db_signature(self) -> Optional[tuple]:
        """底层 SQLite 变更签名（db + -wal 文件的 mtime 元组）。

        跨进程一致性用：SQLite 默认 WAL 模式，其他进程的提交写入 -wal 文件
        （主库文件 mtime 不一定变），故同时取 db 与 -wal 的 mtime 作为签名。
        任一文件不存在/不可访问时返回 None（调用方跳过重载）。
        """
        if not self._use_sqlite or self._session_store is None:
            return None
        db_path = getattr(self._session_store, "_db_path", None)
        if not db_path:
            return None
        try:
            sig = [os.path.getmtime(db_path)]
            wal_path = db_path + "-wal"
            if os.path.exists(wal_path):
                sig.append(os.path.getmtime(wal_path))
            return tuple(sig)
        except OSError:
            return None

    def _reload_if_db_changed(self):
        """跨进程一致性：检测 SQLite 被其他 OS 进程修改后，把本进程内存缓存与
        SQLite 重新对齐，避免各窗口的历史卡片/欢迎卡片互相陈旧、且重开也无法刷新。

        安全策略：重载前先 flush 本进程挂起写（确保本进程状态已落盘），再整体从
        SQLite 重载内存缓存，杜绝「重载覆盖其他进程保存」的数据丢失。
        仅应在读取路径（get_history_list 等）调用，不破坏写入路径的权威状态。
        """
        if not self._use_sqlite or self._session_store is None:
            return
        sig = self._db_signature()
        if sig is None:
            return
        if self._db_signature_stored is not None and sig == self._db_signature_stored:
            return  # 未变化，跳过（含本进程自身写入后已对齐的情况）

        # 文件变化 → 先 flush 本进程挂起写，再整体重载
        try:
            self.flush()
        except Exception:
            logger.exception("[HistoryManager] 跨进程重载前 flush 失败")

        with self._history_load_lock:
            try:
                self._history_sessions = self._session_store.get_sessions_lightweight(
                    limit=self._history_limit
                )
                self._deduplicate_history_sessions()
                self._migrate_if_needed()
                self._history_loaded = True
            except Exception as e:
                logger.warning(f"[HistoryManager] 跨进程重载失败: {e}")
                return
        self._db_signature_stored = sig
        self._mark_cache_dirty()

    def get_history_list(
        self, project: str = None, with_messages: bool = False, merge_team: bool = False
    ) -> List[Dict]:
        """获取历史会话列表，可选按项目过滤，按最后对话时间排序

        Args:
            project: 项目名过滤
            with_messages: 是否包含完整消息数组（为 False 时返回轻量列表）
            merge_team: 为 True 时，team_run_id 非空的会话按 run_id 聚合为
                单个合并条目（团队成员不再逐条出现），普通会话条目保持不变。
                仅在 with_messages=False（轻量列表）时生效。
        """
        self._ensure_history_loaded()
        # 跨进程一致性：若其他 OS 进程已提交（WAL 模式写入 -wal），重载本进程内存缓存
        self._reload_if_db_changed()
        # 先去重
        self._deduplicate_history_sessions()

        # 🚀 轻量列表缓存：避免每次调用都排序+过滤+重建 dict
        # merge_team 使用独立缓存键，避免与默认列表互相污染
        if not with_messages:
            cache_key = ("merge_team", project) if merge_team else project  # None 表示全部项目
            if not self._cache_dirty and cache_key in self._cached_lightweight:
                return list(self._cached_lightweight[cache_key])

        sessions = self._history_sessions
        if project:
            sessions = [s for s in sessions if s.get("project", "默认项目") == project]
        # 按最后对话时间 last_time 降序排序
        sessions = sorted(sessions, key=lambda x: x.get("last_time", ""), reverse=True)

        if not with_messages:
            # 轻量模式：仅保留列表展示所需字段，剔除重量级字段
            # 同时确保 preview 字段存在（兼容旧数据）
            if merge_team:
                result = self._merge_team_lightweight(sessions)
            else:
                result = [self._to_lightweight_entry(s) for s in sessions]

            # 缓存构建结果，下次直接返回
            self._cached_lightweight[cache_key] = list(result)
            self._cache_dirty = False
            return result

        return sessions

    def _to_lightweight_entry(self, s: Dict) -> Dict:
        """构建单个会话的轻量列表条目（get_history_list 轻量模式共用）。

        普通会话与团队合并条目共用同一字段结构，保证 UI 层字段访问一致。
        """
        preview = s.get("preview")
        if not preview:
            preview = extract_message_preview(s.get("messages", []), 50)
        return {
            "session_id": s.get("session_id", ""),
            "saved_at": s.get("saved_at", ""),
            "title": s.get("title", ""),
            "project": s.get("project", "默认项目"),
            "last_time": s.get("last_time", ""),
            "message_count": s.get("message_count", 0),
            "preview": preview,
            "user_edited_title": s.get("user_edited_title", False),
            "worktree_path": s.get("worktree_path", "") or "",
            # 团队元数据（方案 A 团队会话恢复基础；非团队会话为空串）
            "team_run_id": s.get("team_run_id", "") or "",
            "team_name": s.get("team_name", "") or "",
            "agent_name": s.get("agent_name", "") or "",
            # 团队成员快照（F3：JSON 字符串，透传）
            "team_members": s.get("team_members", "") or "",
        }

    def _merge_team_lightweight(self, sessions: List[Dict]) -> List[Dict]:
        """构建合并团队会话后的轻量列表（merge_team=True 路径）。

        规则：
        - team_run_id 非空的会话按 run_id 聚合为单个合并条目
        - 合并条目字段：team_run_id / team_name / agent_names（成员列表）/
          member_count / team_merged=True / session_id=组内 last_time 最新会话 /
          last_time=组内最新 / preview=团队首问（get_team_first_question）/
          members=成员轻量记录列表（供 UI 展开成员列表 / 单独进入成员会话）
        - 组内成员会话不再逐条出现；普通会话条目保持不变
        - 整体仍按 last_time 降序排序
        """
        result: List[Dict] = []
        # 聚合 key = (run_id, project)：同 run_id 跨 project 的会话按项目拆分为
        # 独立合并条目（Bug A 兜底），避免不同项目下团队条目成员/会话互相污染。
        team_groups: Dict[Tuple[str, str], Dict] = {}  # (run_id, project) -> 合并条目

        for s in sessions:
            run_id = (s.get("team_run_id") or "").strip()
            if not run_id:
                result.append(self._to_lightweight_entry(s))
                continue
            # project 归一化：与 _to_lightweight_entry 默认值保持一致
            project = (s.get("project") or "默认项目").strip()

            group = team_groups.get((run_id, project))
            if group is None:
                group = {
                    "team_run_id": run_id,
                    "team_name": (s.get("team_name") or "").strip(),
                    "agent_names": [],
                    "member_count": 0,
                    "team_merged": True,
                    "session_id": s.get("session_id", ""),
                    "saved_at": s.get("saved_at", ""),
                    "title": (s.get("team_name") or "").strip() or s.get("title", ""),
                    "project": s.get("project", "默认项目"),
                    "last_time": s.get("last_time", ""),
                    "message_count": 0,
                    "preview": "",
                    "user_edited_title": False,
                    "worktree_path": s.get("worktree_path", "") or "",
                    "agent_name": "",
                    "members": [],
                }
                team_groups[(run_id, project)] = group

            agent = (s.get("agent_name") or "").strip()
            if agent and agent not in group["agent_names"]:
                group["agent_names"].append(agent)
            group["member_count"] = len(group["agent_names"])
            # 成员轻量记录收集（UI 展开成员列表 / 单独进入成员会话用）
            # 🛡️ 按 agent 去重：agent_names/member_count 按 agent 去重，members
            # 必须对齐——同 agent 多会话（多轮 run）时展开区成员行数不得 >
            # member_count（S-A 修复）。同 agent 多条保留 last_time 最新一条
            # （与 by_agent 恢复去重语义一致）。
            member_entry = self._to_lightweight_entry(s)
            member_agent = member_entry.get("agent_name") or ""
            replaced = False
            for i, existing in enumerate(group["members"]):
                if (existing.get("agent_name") or "") == member_agent:
                    # 同 agent：保留 last_time 最新一条
                    if (member_entry.get("last_time") or "") >= (existing.get("last_time") or ""):
                        group["members"][i] = member_entry
                    replaced = True
                    break
            if not replaced:
                group["members"].append(member_entry)
            # 组内 last_time 最新会话 → 同步 session_id / last_time / saved_at / worktree_path
            if (s.get("last_time") or "") >= (group.get("last_time") or ""):
                group["session_id"] = s.get("session_id", "")
                group["last_time"] = s.get("last_time", "")
                group["saved_at"] = s.get("saved_at", "")
                group["worktree_path"] = s.get("worktree_path", "") or ""
            group["message_count"] += s.get("message_count", 0) or 0
            # 🛡️ F3（T2-P3 第 1/2 层）：合并条目收集成员会话的 team_members 快照
            # （JSON 字符串：手动加入但无会话记录的成员），最后并入成员列表——
            # 手动成员在历史合并条目中可见（不依赖当前 team.json，历史 run 的
            # team.json 会被新 run 覆盖，快照随会话落库根治）。
            # T3：改用 parse_team_members_snapshot 收集含 window_id 的记录——
            # 同角色多成员（两个 build 异 wid）各保留一条，不再按 agent 名覆盖。
            snap = (s.get("team_members") or "").strip()
            if snap:
                group.setdefault("_snapshot_members", [])
                for rec in parse_team_members_snapshot(snap):
                    if rec not in group["_snapshot_members"]:
                        group["_snapshot_members"].append(rec)

        # 为每个团队组填充 preview（团队首问，仍按 run_id 查，跨 project 组共享）
        for (run_id, _project), group in team_groups.items():
            group["preview"] = self.get_team_first_question(run_id)
            # 🛡️ F3/T3：快照成员并入成员列表。
            # 1) 有 window_id 的记录（T3 新格式）：同 agent 多成员各保留一条——
            #    若 members 已有同 agent 无 wid 的会话行则补上 wid；否则追加
            #    独立成员行（无会话记录 → session_id=""，UI 空窗口语义）。
            # 2) 无 wid 老格式（纯名字快照）：仍走 agent_names 去重补充（兼容
            #    老数据）；同时若 members 无同 agent 行则追加成员行，保证
            #    member_count 与展开区行数一致（F3 手动成员可见）。
            # member_count 语义 = 成员数：同角色异 wid 各算 1（T3 决策）。
            snap_members = group.pop("_snapshot_members", [])
            wid_records = [r for r in snap_members if r.get("window_id")]
            plain_names = [r["agent_name"] for r in snap_members if not r.get("window_id")]
            for rec in wid_records:
                agent = rec["agent_name"]
                replaced = False
                for i, existing in enumerate(group["members"]):
                    if (existing.get("agent_name") or "") == agent and not (existing.get("window_id") or ""):
                        merged_row = dict(existing)
                        merged_row["window_id"] = rec["window_id"]
                        group["members"][i] = merged_row
                        replaced = True
                        break
                if not replaced:
                    group["members"].append(
                        {
                            "agent_name": agent,
                            "window_id": rec["window_id"],
                            "session_id": "",
                            "title": "",
                            "last_time": "",
                            "saved_at": "",
                        }
                    )
            for name in plain_names:
                if name and name not in group["agent_names"]:
                    group["agent_names"].append(name)
                if name and not any((m.get("agent_name") or "") == name for m in group["members"]):
                    group["members"].append(
                        {
                            "agent_name": name,
                            "window_id": "",
                            "session_id": "",
                            "title": "",
                            "last_time": "",
                            "saved_at": "",
                        }
                    )
            group["member_count"] = len(group["members"])
            result.append(group)

        # 整体按 last_time 降序（合并条目参与排序）
        result.sort(key=lambda x: x.get("last_time", ""), reverse=True)
        return result

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
            self._mark_cache_dirty()
            return True
        return False

    def archive_sessions_by_project(self, project: str) -> int:
        """批量归档指定项目的所有会话"""
        if self._use_sqlite and self._session_store:
            count = self._session_store.archive_sessions_by_project(project)
            # 同步内存缓存
            self._history_sessions = [s for s in self._history_sessions if s.get("project", "默认项目") != project]
            self._mark_cache_dirty()
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

        if count > 0:
            self._mark_cache_dirty()

        return count

    def _archive_session_record(self, session: Dict) -> bool:
        """归档单个会话记录：写归档 JSON + 从内存缓存移除 + 从 SQLite 删除。

        由 archive_history 与 archive_sessions_by_run_id 复用，保证归档流程单点定义。
        """
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
        # 🛡️ 守卫 session_id="unknown"（轻量记录缺失 id 时的兜底值）：
        # 避免误删多条无 id 会话的内存记录。
        if session_id != "unknown":
            self._history_sessions = [s for s in self._history_sessions if s.get("session_id") != session_id]
        self._mark_cache_dirty()

        # 从 SQLite 删除
        if self._use_sqlite and self._session_store:
            self._session_store.delete_session(session_id)

        return True

    def archive_history(self, index: int) -> bool:
        """归档历史记录"""
        if 0 <= index < len(self._history_sessions):
            return self._archive_session_record(self._history_sessions[index])
        return False

    def remove_session(self, session_id: str, release_messages_only: bool = True) -> bool:
        """从内存缓存移除会话记录（或仅释放其消息体）。

        内存泄漏修复（P0-1）：HistoryManager 全局单例 _history_sessions 在
        删除会话/关闭标签时不清理，导致已删除会话的全量消息驻留内存。

        Args:
            session_id: 目标会话 ID
            release_messages_only: True 仅置空 messages（保留记录元数据，
                供历史列表展示；get_session_by_session_id 已有「messages 空
                → SQLite 懒加载回填」机制，置空不影响恢复）；
                False 完全移除该记录并标记缓存脏。

        Returns:
            是否找到并处理了该会话
        """
        if not session_id:
            return False
        self._ensure_history_loaded()
        for i, s in enumerate(self._history_sessions):
            if s.get("session_id") == session_id:
                if release_messages_only:
                    if s.get("messages"):
                        s["messages"] = []
                else:
                    self._history_sessions.pop(i)
                    self._mark_cache_dirty()
                return True
        return False

    def archive_sessions_by_run_id(self, run_id: str) -> int:
        """按团队 run_id 归档该 run 下全部成员会话。

        复用 _archive_session_record 的归档流程（写归档 JSON + 从内存/SQLite 删除），
        与归档区"逐条显示成员会话"保持一致（归档区不做合并）。

        Args:
            run_id: 团队运行标识

        Returns:
            成功归档的会话数量
        """
        if not run_id:
            return 0
        sessions = self.get_team_sessions_by_run_id(run_id)
        count = 0
        for session in sessions:
            if self._archive_session_record(session):
                count += 1
        return count

    def import_from_json(self, file_path: str) -> Optional[Dict]:
        """
        从 JSON 文件导入会话

        Args:
            file_path: JSON 文件路径

        Returns:
            导入的会话数据，失败返回 None
        """
        try:
            # 导入后会立即刷新历史面板，因此必须先完成懒加载，防止刷新时覆盖尚未落盘的导入记录。
            self._ensure_history_loaded()

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
                    self._mark_cache_dirty()
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
                    self._mark_cache_dirty()
                    self._schedule_save(session["session_id"])
                    logger.info(f"[HistoryManager] 导入新会话: {session['session_id']}")
            else:
                # 没有 session_id，生成一个新的
                session["session_id"] = uuid.uuid4().hex[:8]
                self._history_sessions.insert(0, session)
                self._history_sessions = self._history_sessions[: self._history_limit]
                self._mark_cache_dirty()
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
                        # system_prompt 轻量列表不再加载，借这次全量查询回填
                        # （full 已含该字段，零额外 I/O）
                        if session.get("system_prompt") is None:
                            session["system_prompt"] = full.get("system_prompt", "")
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

    def get_team_sessions_by_run_id(self, run_id: str) -> List[Dict]:
        """按团队 run_id 获取全部成员会话（轻量，不含 messages）。

        🛡️ 恢复团队会话专用：直接查 SQLite，绕开 _history_limit=500
        截断——团队会话长期运行可能被挤出内存前 500 条，若用
        get_history_list() 收集会漏成员（恢复成员不全的根因之一）。
        """
        if self._use_sqlite and self._session_store:
            return self._session_store.get_sessions_by_team_run_id(run_id)
        # 非 SQLite 模式（JSON 存储）：退化为内存列表过滤
        sessions = self.get_history_list(with_messages=False)
        return [s for s in sessions if (s.get("team_run_id") or "").strip() == run_id]

    def _lookup_team_first_question(self, run_id: str, max_len: int = 50) -> Optional[str]:
        """从落库列 first_user_msg / first_user_ts 直接求团队首问（零反序列化）。

        候选按 updated_at DESC 返回，与 get_by_team_run_id 的遍历顺序一致，从而
        复现旧实现「时间戳并列时保留首个」的语义：首个候选无条件成为 best（即使
        ts 为空），后续候选仅在 ts 非空且严格小于 best.ts 时覆盖。

        Args:
            run_id: 团队运行标识
            max_len: 预览截断长度

        Returns:
            首问预览文本；落库列缺失（未迁移 / 老数据 / 存储未就绪）时返回 None，
            调用方据此退回全量扫描路径，功能零退化。
        """
        if not (self._use_sqlite and self._session_store):
            return None
        getter = getattr(self._session_store, "get_team_first_question_candidates", None)
        if getter is None:
            return None
        candidates = getter(run_id) or []
        best: Optional[Dict] = None
        for ts, content in candidates:
            if not content:
                continue
            if best is None or (ts and (not best["ts"] or ts < best["ts"])):
                best = {"ts": ts, "content": content}
        if best is None:
            return None
        content = best["content"]
        return content[:max_len].strip() + ("..." if len(content) > max_len else "")

    def get_team_first_question(self, run_id: str, max_len: int = 50) -> str:
        """获取团队首问：该 run_id 下所有会话中时间戳最早的真实 user 消息文本。

        供团队合并条目（merge_team=True）的 preview 使用。

        🛡️ P2 修复（T2）：此前实现"先选 earliest 会话（消息最后时间最早）再在该
        会话内找首条 user"，存在顺序缺陷——若最早的会话首条消息是 TeamMail/邮件
        注入（真实首问在另一个较晚结束的成员会话里），会返回空串或邮件文本。
        改为**全局遍历**该 run 下所有会话的所有消息，取时间戳最早的真实 user
        消息（跨会话比较），保证首问不依赖"最早会话"的选型。

        跳过 _hook_event 系统消息；跳过 "📨 **来自" 开头的任务邮件文本（兼容
        旧数据无 _hook_event 标记的注入路径）；跳过空 content；content 为 list
        时用 content_to_text 转换。时间戳并列时保留首个（结果稳定可复现）。

        Args:
            run_id: 团队运行标识
            max_len: 预览截断长度

        Returns:
            首条 user 消息文本（截断后加省略号），无则空串
        """
        if not run_id:
            return ""
        # 🚀 T4 快路径（内存治理）：首问已随会话落库时直接读字符串列，零反序列化。
        # 旧路径为此反序列化该 run 下全部成员会话的完整 messages —— 实测 246 条
        # 约 285MB 常驻、放大 9.9 倍、耗时 1.0s；落库后退化为一列字符串读取。
        _quick = self._lookup_team_first_question(run_id, max_len)
        if _quick is not None:
            return _quick
        sessions = self.get_team_sessions_by_run_id(run_id)
        if not sessions:
            return ""
        # 🛡️ P2：全局遍历所有成员会话的所有消息，跨会话比较消息时间戳。
        # 不依赖"最早结束的会话"选型（该会话可能只有邮件/assistant 消息，
        # 真实首问在另一个成员的会话里）。
        best: Optional[Dict] = None  # {"ts": str, "content": str}
        for s in sessions:
            session_id = s.get("session_id", "")
            if not session_id:
                continue
            full = self.get_session_by_session_id(session_id)
            if full is None:
                continue
            for msg in full.get("messages", []):
                if msg.get("_hook_event"):
                    continue
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = content_to_text(content)
                if not content:
                    continue
                # R3 防御：跳过未打标的任务邮件注入路径（如旧记录中无 _hook_event 的
                # "📨 **来自 [...] 的任务邮件：**" 文本），确保首问取真实用户问题。
                if str(content).startswith("📨 **来自"):
                    continue
                ts = msg.get("timestamp") or ""
                if best is None or (ts and (not best["ts"] or ts < best["ts"])):
                    best = {"ts": ts, "content": content}
                # 时间戳并列（ts == best["ts"]）或 best 已更新：保留首个，不覆盖
        if best is None:
            return ""
        content = best["content"]
        return content[:max_len].strip() + ("..." if len(content) > max_len else "")

    def _resolve_existing_system_prompt(self, existing: Dict) -> str:
        """取会话既有 system_prompt；未加载（哨兵 None）时回查 SQLite。

        背景：轻量列表不再 SELECT system_prompt，内存里的值为 None。
        若 update_session 直接拿 None 当空串写回，会把 DB 中真实的
        system_prompt 清空 —— 故此处必须回查兜底。
        """
        val = existing.get("system_prompt")
        if val is not None:
            return val
        sid = existing.get("session_id") or ""
        if not sid or not self._session_store or not self._session_store.is_initialized:
            return ""
        try:
            repo = getattr(self._session_store, "_session_repo", None)
            if repo is not None and hasattr(repo, "get_system_prompt"):
                return repo.get_system_prompt(sid)
        except Exception as e:
            logger.warning(f"[HistoryManager] system_prompt 回查失败 {sid}: {e}")
        return ""

    def update_session(
        self,
        index: int,
        messages: List[Dict],
        compaction_state: Dict = None,
        compaction_cache: Dict = None,
        system_prompt: str = None,
        project: str = None,
        worktree_path: str = None,
        team_run_id: str = None,
        team_name: str = None,
        agent_name: str = None,
        team_members: str = None,
    ):
        """更新会话

        team_run_id/team_name/agent_name/team_members 为 None 时保留现有值
        （最小侵入，不传则不触碰团队元数据），传入非 None 才更新——避免
        update 链路把团队会话的元数据覆盖成空。
        """
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
                system_prompt=(
                    system_prompt
                    if system_prompt is not None
                    else self._resolve_existing_system_prompt(existing)
                ),
                project=project if project is not None else existing.get("project", "默认项目"),
                worktree_path=worktree_path if worktree_path is not None else existing.get("worktree_path", ""),
                team_run_id=team_run_id if team_run_id is not None else existing.get("team_run_id", ""),
                team_name=team_name if team_name is not None else existing.get("team_name", ""),
                agent_name=agent_name if agent_name is not None else existing.get("agent_name", ""),
                team_members=team_members if team_members is not None else existing.get("team_members", ""),
            )
            # 移动到列表开头以保持与 SQLite ORDER BY updated_at DESC 一致
            self._history_sessions.pop(index)
            self._history_sessions.insert(0, updated)
            self._mark_cache_dirty()
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
                # 🛡️ 空消息守卫：内存记录 messages 已被释放置空
                # （_release_inactive_session_messages / remove_session
                # release_messages_only）时跳过写库。若照常保存会用
                # serialize([]) 覆盖 DB 中已有完整消息的会话（preview/
                # message_count 保留、messages 变空 → 历史会话加载后
                # 消息列表为空且无任何报错，根因）。
                if not session.get("messages"):
                    logger.warning(f"[HistoryManager] 跳过空消息会话保存（内存已释放）: session_id={pending_id[:8]}...")
                    break
                self._session_store.save_session(session)
                break

        self._pending_save_session_id = None

    def flush(self):
        """立即持久化所有待保存的会话（同步写入 SQLite）

        在应用退出或关键保存点后调用，确保数据不丢失。

        🛡️ P4（T4-TOP6）：db_manager 写路径改为延迟提交后，_do_save 写入的
        SQL 只是执行未提交；本方法必须穿透到 DatabaseManager.flush() 同步
        提交挂起事务，否则 flush 语义失效（退出路径丢数据）。调用方
        （main_widget.py 退出/保存点）行为不变。
        """
        if self._save_timer is not None or self._pending_save_session_id:
            self._do_save()
        # 穿透到 db 层：同步提交 SQLite 挂起事务（攒批窗口内的写立即落盘）
        # 🛡️ P4-C1-Y1（devil-advocate 复核）：flush 失败透传 error 日志
        # （退出链经本方法最终落 db.close()，由 close 的失败保留逻辑兜底）。
        try:
            store = self._session_store
            db = getattr(store, "_db", None) if store is not None else None
            if db is not None and getattr(db, "is_connected", False):
                if not db.flush():
                    logger.error(
                        f"[HistoryManager] flush 穿透失败：SQLite 挂起事务未落盘 "
                        f"(pending_save={self._pending_save_session_id})"
                    )
        except Exception as e:
            logger.error(f"[HistoryManager] flush 穿透异常: {e}")

    def _extract_last_message_time(self, messages: List[Dict]) -> str:
        for msg in reversed(messages or []):
            timestamp = msg.get("timestamp")
            if timestamp:
                return timestamp
        return "未知"

    def _ensure_message_timestamps(self, messages: List[Dict], fallback_ts: str) -> List[Dict]:
        """确保所有消息都有时间戳，缺失时用 fallback_ts 填充。

        优化：所有消息已有有效时间戳时直接返回原列表（避免 O(n) dict 拷贝）。
        """
        if not messages:
            return messages

        # 快速路径：所有消息已有有效时间戳
        all_have_ts = True
        last_seen_ts = fallback_ts
        for msg in messages:
            if not isinstance(msg, dict):
                all_have_ts = False
                break
            ts = msg.get("timestamp")
            if ts:
                last_seen_ts = ts
            else:
                all_have_ts = False
                break

        if all_have_ts:
            return messages

        # 慢路径：需要填充缺失的时间戳
        normalized: List[Dict] = []
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

    # ============================================================
    # 项目归档导出/导入（ZIP 压缩包）
    # ============================================================

    def export_project_archive(self, project_name: str, root_dir: str = "") -> Optional[str]:
        """将项目完整导出为 ZIP 压缩包

        包含：
        - 项目所有会话的 JSON 文件（`sessions/` 目录）
        - Git 仓库信息（`git_info.json`）
        - Git 跟踪的文件快照（`git_files/` 目录，仅当 root_dir 为 git 仓库时）

        Args:
            project_name: 项目名
            root_dir: 项目根目录（用于获取 git 仓库信息）

        Returns:
            ZIP 文件路径，失败返回 None
        """
        # 确保会话已加载
        self._ensure_history_loaded()

        # 获取项目下所有会话（轻量列表）
        sessions_light = self.get_history_list(project_name, with_messages=False)
        if not sessions_light:
            logger.warning(f"[HistoryManager] 项目「{project_name}」无会话，无法导出")
            return None

        # 🚀 T4：不再先把整个项目的完整会话攒进内存。
        # 旧实现此处对每条会话调 get_session_by_session_id()，会把完整 messages
        # 回填进 _history_sessions（无淘汰常驻），且 sessions 列表本身也持有全部
        # 完整消息 —— 导出 1283 条会话的项目实测 1035 MB。改为在写 ZIP 的循环里
        # 逐条直查 SQLite、写完即弃（见下方流式写入），峰值降到单条会话。

        # 构建 ZIP 文件名
        safe_name = sanitize_filename(project_name[:50])
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"{safe_name}_{timestamp}.drifox_project"
        from app.utils.share_records import ensure_dirs, get_projects_dir

        ensure_dirs()
        zip_dir = get_projects_dir()
        zip_dir.mkdir(parents=True, exist_ok=True)
        zip_path = zip_dir / zip_filename

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # ── 写入项目元信息 ──
                meta = {
                    "project_name": project_name,
                    "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "session_count": len(sessions_light),
                    "version": 1,
                }
                zf.writestr("project.json", json.dumps(serialize_for_json(meta), option=json.OPT_INDENT_2))

                # ── 流式写入会话 JSON（🚀 T4 内存治理）──
                # 逐条「直查 SQLite（不经过 _history_sessions，因此不产生回填常驻）
                # → 立即写 ZIP → 下一条覆盖引用后即可回收」。峰值 = 单条会话，
                # 而非旧实现的整个项目（1283 条 ≈ 1035 MB → 约 17 MB）。
                # 基底仍是轻量条目，只补 messages / system_prompt，保持产物字段不变。
                exported = 0
                for s in sessions_light:
                    session = s
                    sid = s.get("session_id", "")
                    if sid and self._use_sqlite and self._session_store:
                        try:
                            # 直查 SQLite 取完整行：不经过 _history_sessions，因此不
                            # 产生回填常驻；写入 ZIP 后引用即被下一条覆盖回收。
                            # 用完整行而非轻量条目，保证导出产物字段与旧实现一致
                            # （含 created_at / updated_at / name / topic_summary /
                            # compaction_state 等），导入侧无差异。
                            full = self._session_store.get_session(sid)
                            if full:
                                session = full
                        except Exception as e:
                            logger.debug(f"[HistoryManager] 导出读取完整会话失败 {sid[:8]}: {e}")
                    session_id = session.get("session_id", uuid.uuid4().hex[:8])
                    title = session.get("title", "未命名")
                    safe_title = sanitize_filename(title[:50])
                    last_time = session.get("last_time", datetime.now().strftime("%Y-%m-%d"))
                    date_str = last_time[:10]
                    session_filename = f"sessions/{date_str}_{safe_title}_{session_id}.json"
                    zf.writestr(
                        session_filename,
                        json.dumps(serialize_for_json(session), option=json.OPT_INDENT_2),
                    )
                    exported += 1
                if exported == 0:
                    raise RuntimeError("无有效会话可写入")

                # ── 写入 Git 仓库信息（如果支持） ──
                git_info = self._collect_git_info(root_dir)
                if git_info:
                    zf.writestr("git_info.json", json.dumps(serialize_for_json(git_info), option=json.OPT_INDENT_2))

                    # ── 写入 Git 跟踪文件快照 ──
                    if git_info.get("root"):
                        self._add_git_files_to_zip(zf, git_info["root"])

            logger.info(f"[HistoryManager] 项目导出成功: {zip_path}")
            return str(zip_path)

        except Exception as e:
            logger.error(f"[HistoryManager] 项目导出失败: {e}", exc_info=True)
            # 清理不完整的 ZIP
            if zip_path.exists():
                try:
                    zip_path.unlink()
                except Exception:
                    pass
            return None

    def _collect_git_info(self, root_dir: str) -> Optional[Dict]:
        """收集 Git 仓库信息"""
        if not root_dir or not os.path.isdir(root_dir):
            return None

        try:
            # 检查是否在 git 仓库中
            git_dir = self._find_git_root(root_dir)
            if not git_dir:
                return None

            result = {
                "root": git_dir,
                "remote": "",
                "branch": "",
                "last_commit_hash": "",
                "last_commit_message": "",
                "last_commit_author": "",
                "last_commit_date": "",
            }

            # 获取 remote
            try:
                r = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=git_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                if r.returncode == 0:
                    result["remote"] = r.stdout.strip()
            except Exception:
                pass

            # 获取当前分支
            try:
                r = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=git_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                if r.returncode == 0:
                    result["branch"] = r.stdout.strip()
            except Exception:
                pass

            # 获取最新提交信息
            try:
                r = subprocess.run(
                    ["git", "log", "-1", "--format=%H%n%s%n%an%n%ci"],
                    cwd=git_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                if r.returncode == 0:
                    lines = r.stdout.strip().split("\n")
                    if len(lines) >= 4:
                        result["last_commit_hash"] = lines[0]
                        result["last_commit_message"] = lines[1]
                        result["last_commit_author"] = lines[2]
                        result["last_commit_date"] = lines[3]
            except Exception:
                pass

            return result

        except Exception as e:
            logger.debug(f"[HistoryManager] 收集 git 信息失败: {e}")
            return None

    def _find_git_root(self, path: str) -> Optional[str]:
        """从指定路径向上查找 git 根目录"""
        current = os.path.abspath(path)
        while current and os.path.isdir(current):
            if os.path.isdir(os.path.join(current, ".git")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return None

    def _add_git_files_to_zip(self, zf, git_root: str):
        """将 Git 跟踪的文件添加到 ZIP（git_files/ 目录）"""
        try:
            # git_root 统一用 Posix 路径分隔符，避免 Windows 反斜杠问题
            git_root = git_root.replace("\\", "/")
            # 获取 git 跟踪的文件列表
            r = subprocess.run(
                ["git", "-c", "core.quotepath=false", "ls-files"],
                cwd=git_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if r.returncode != 0:
                logger.debug(f"[HistoryManager] git ls-files 返回非零: {r.stderr}")
                return

            tracked_files = [line.strip() for line in r.stdout.strip().split("\n") if line.strip()]
            if not tracked_files:
                logger.debug("[HistoryManager] git ls-files 无输出，跳过 git 文件")
                return

            max_files = 500  # 限制最大文件数，避免 ZIP 过大
            added_count = 0

            for i, rel_path in enumerate(tracked_files):
                if i >= max_files:
                    break
                # 统一用正斜杠作为 ZIP 内路径
                arc_path = f"git_files/{rel_path.replace('\\', '/')}"
                abs_path = os.path.join(git_root, rel_path)
                if os.path.isfile(abs_path) and os.path.getsize(abs_path) < 50 * 1024 * 1024:
                    try:
                        # 用 arcname 显式指定归档内路径，避免 zipfile 自动处理带来的问题
                        zf.write(abs_path, arcname=arc_path)
                        added_count += 1
                    except Exception as e:
                        logger.debug(f"[HistoryManager] 跳过文件 {rel_path}: {e}")

            logger.info(f"[HistoryManager] 已添加 {added_count}/{min(len(tracked_files), max_files)} 个 git 文件到 ZIP")

        except Exception as e:
            logger.debug(f"[HistoryManager] 添加 git 文件到 ZIP 失败: {e}")

    def import_project_archive(self, zip_path: str) -> Optional[Dict]:
        """从 ZIP 压缩包导入完整项目

        导入内容：
        - 项目会话（还原到该项目下）
        - 项目元信息（项目名、git 信息）

        Args:
            zip_path: .drifox_project ZIP 文件路径

        Returns:
            导入结果字典 {project_name, session_count, git_info}，失败返回 None
        """
        if not os.path.isfile(zip_path):
            logger.error(f"[HistoryManager] 导入文件不存在: {zip_path}")
            return None

        # 确保会话已加载
        self._ensure_history_loaded()

        result = {
            "project_name": "导入项目",
            "session_count": 0,
            "git_info": None,
        }

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # ── 读取项目元信息 ──
                if "project.json" in zf.namelist():
                    meta_raw = zf.read("project.json")
                    meta = deserialize_from_json(json.loads(meta_raw))
                    result["project_name"] = meta.get("project_name", "导入项目")

                # ── 读取并导入所有会话 ──
                session_files = sorted([n for n in zf.namelist() if n.startswith("sessions/") and n.endswith(".json")])
                imported_count = 0
                for session_file in session_files:
                    try:
                        raw = zf.read(session_file)
                        data = deserialize_from_json(json.loads(raw))
                        session = self._normalize_single_session(data)
                        # 设置项目名
                        session["project"] = result["project_name"]

                        # 检查是否已存在
                        existing_id = session.get("session_id", "")
                        if existing_id:
                            existing_index = self.find_index_by_session_id(existing_id)
                            if existing_index is not None:
                                # 更新已存在会话
                                self._history_sessions[existing_index] = session
                                self._schedule_save(existing_id)
                                imported_count += 1
                                continue

                        # 新会话
                        if not existing_id:
                            session["session_id"] = uuid.uuid4().hex[:8]
                        self._history_sessions.insert(0, session)
                        self._schedule_save(session["session_id"])
                        imported_count += 1
                    except Exception as e:
                        logger.warning(f"[HistoryManager] 导入会话文件失败 {session_file}: {e}")

                result["session_count"] = imported_count

                # ── 读取 Git 信息（含原始根路径，用于文件恢复） ──
                if "git_info.json" in zf.namelist():
                    try:
                        git_raw = zf.read("git_info.json")
                        git_info = deserialize_from_json(json.loads(git_raw))
                        result["git_info"] = git_info
                        # 保存原始 git 根路径，用于后续文件恢复
                        if git_info.get("root"):
                            result["original_root"] = git_info["root"]
                    except Exception as e:
                        logger.warning(f"[HistoryManager] 读取 git_info.json 失败: {e}")

                # ── 提取 git_files 到临时目录 ──
                git_file_entries = [n for n in zf.namelist() if n.startswith("git_files/")]
                if git_file_entries:
                    safe_proj = sanitize_filename(result["project_name"][:30])
                    extract_dir = Path.home() / ".drifox" / "project_imports" / safe_proj
                    # 清空旧目录防止残留
                    if extract_dir.exists():
                        shutil.rmtree(str(extract_dir))
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    for gf_name in git_file_entries:
                        try:
                            zf.extract(gf_name, str(extract_dir))
                        except Exception as e:
                            logger.warning(f"[HistoryManager] 提取 git_files 失败 {gf_name}: {e}")
                    # 将 git_files/ 子目录的内容提到 extract_dir 根目录
                    git_files_subdir = extract_dir / "git_files"
                    if git_files_subdir.exists():
                        for item in git_files_subdir.iterdir():
                            target = extract_dir / item.name
                            if item.is_dir():
                                shutil.copytree(str(item), str(target), dirs_exist_ok=True)
                            else:
                                shutil.copy2(str(item), str(target))
                        shutil.rmtree(str(git_files_subdir))
                    result["extract_dir"] = str(extract_dir)

            logger.info(f"[HistoryManager] 项目导入成功: {result['project_name']}, {imported_count} 会话")
            return result

        except zipfile.BadZipFile as e:
            logger.error(f"[HistoryManager] ZIP 文件损坏: {e}")
            return None
        except Exception as e:
            logger.error(f"[HistoryManager] 项目导入异常: {e}", exc_info=True)
            return None
