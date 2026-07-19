# -*- coding: utf-8 -*-
"""数据层 — SQLite 读取 + Token 估算 + 异步 Worker

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 sqlite3/stdlib 完成
- 基于 .drifox/sessions.db 文件直接读取数据
"""

import sqlite3
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger


# ── 路径常量 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DB_PATH = _PROJECT_ROOT / ".drifox" / "sessions.db"
_USER_DB_PATH = Path.home() / ".drifox" / "sessions.db"


def _find_db() -> Optional[Path]:
    """查找 sessions.db 文件路径（开发环境 → 用户目录兜底）"""
    if _DEV_DB_PATH.exists():
        return _DEV_DB_PATH
    if _USER_DB_PATH.exists():
        return _USER_DB_PATH
    return None


# ── Token 快速估算 ────────────────────────────────────────


def _fast_estimate_tokens(text: str) -> int:
    """快速估算文本的 token 数（无需 tiktoken 依赖）

    经验公式（cl100k_base 类分词器近似，覆盖 GPT/DeepSeek/Qwen/Claude）：
    - 中文约 1.2 token/字
    - 英文/代码约 1 token / 4 字符
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    non_chinese = len(text) - chinese
    estimated = int(chinese * 1.2 + non_chinese / 4.0)
    return max(1, estimated)


def _estimate_messages_tokens(messages_json: str) -> int:
    """估算整条会话消息 JSON 的 token 数（快速但不精确）"""
    if not messages_json:
        return 0
    if isinstance(messages_json, str) and len(messages_json) > 10:
        return _fast_estimate_tokens(messages_json)
    return 0


# ── SQLite 数据读取 ──────────────────────────────────────


def _get_db_connection() -> Optional[sqlite3.Connection]:
    """获取 SQLite 数据库连接（只读模式）"""
    db_path = _find_db()
    if db_path is None:
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"[ContextUsageStats] 无法打开数据库: {e}")
        return None


def _fetch_session_stats() -> dict:
    """从数据库中读取会话统计数据

    Returns:
        dict with keys:
        - total_sessions: int
        - total_messages: int
        - total_tokens: int
        - avg_daily_tokens: int
        - total_compacted: int (已压缩会话数)
        - daily_sessions: List[Tuple[str, int]] (日期, 会话数), 最近 14 天
        - daily_messages: List[Tuple[str, int]] (日期, 消息数), 最近 14 天
        - daily_tokens: List[Tuple[str, int]] (日期, 估算 token 数), 最近 14 天
        - sessions_per_project: Dict[str, int]
        - avg_messages_per_session: float
        - compaction_rate: float
        - error: Optional[str]
    """
    result = {
        "total_sessions": 0,
        "total_messages": 0,
        "total_tokens": 0,
        "avg_daily_tokens": 0,
        "total_compacted": 0,
        "daily_sessions": [],
        "daily_messages": [],
        "daily_tokens": [],
        "sessions_per_project": {},
        "avg_messages_per_session": 0.0,
        "compaction_rate": 0.0,
        "error": None,
    }

    conn = _get_db_connection()
    if conn is None:
        result["error"] = "无法连接到数据库"
        return result

    try:
        cursor = conn.cursor()

        # 1. ═══ 基础统计 ═══
        cursor.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(message_count), 0) as msgs, "
            "COALESCE(SUM(context_usage), 0) as tokens "
            "FROM sessions WHERE project NOT LIKE '__archived__%'"
        )
        row = cursor.fetchone()
        total_sessions = row["cnt"] if row else 0
        total_messages = row["msgs"] if row else 0
        total_tokens_known = row["tokens"] if row else 0
        result["total_sessions"] = total_sessions
        result["total_messages"] = total_messages

        # 1b. ═══ Token 总量（全部会话，context_usage=0 的旧会话用 messages 估算） ═══
        total_tokens_estimated = 0
        cursor.execute(
            "SELECT messages FROM sessions "
            "WHERE project NOT LIKE '__archived__%' "
            "AND (context_usage IS NULL OR context_usage = 0) "
            "AND messages IS NOT NULL AND messages != ''"
        )
        for row in cursor.fetchall():
            msg_data = row["messages"]
            if isinstance(msg_data, (str, bytes)):
                total_tokens_estimated += _fast_estimate_tokens(str(msg_data)[:100000])
        result["total_tokens"] = total_tokens_known + total_tokens_estimated

        # 2. ═══ 压缩统计（精确检查 compaction_state 中 active:true） ═══
        cursor.execute(
            "SELECT compaction_state FROM sessions "
            "WHERE project NOT LIKE '__archived__%' "
            "AND instr(compaction_state, 'true') > 0"
        )
        compacted_count = 0
        for row in cursor.fetchall():
            raw = row["compaction_state"]
            if isinstance(raw, str) and '"active":true' in raw:
                compacted_count += 1
        result["total_compacted"] = compacted_count
        result["compaction_rate"] = compacted_count / total_sessions if total_sessions > 0 else 0.0

        # 3. ═══ 按项目统计 ═══
        cursor.execute(
            "SELECT project, COUNT(*) as cnt FROM sessions "
            "WHERE project NOT LIKE '__archived__%' "
            "GROUP BY project ORDER BY cnt DESC"
        )
        for row in cursor.fetchall():
            result["sessions_per_project"][row["project"]] = row["cnt"]

        # 4. ═══ 最近 14 天按日统计 ═══
        today = datetime.now()
        date_labels = [(today - timedelta(days=i)).strftime("%m-%d") for i in range(13, -1, -1)]

        daily_sessions_map: Dict[str, int] = {dl: 0 for dl in date_labels}
        daily_messages_map: Dict[str, int] = {dl: 0 for dl in date_labels}
        daily_tokens_map: Dict[str, int] = {dl: 0 for dl in date_labels}

        cursor.execute(
            "SELECT DATE(created_at) as day, COUNT(*) as cnt, "
            "COALESCE(SUM(message_count), 0) as msgs "
            "FROM sessions "
            "WHERE created_at >= date('now', '-13 days') "
            "AND project NOT LIKE '__archived__%' "
            "GROUP BY DATE(created_at) ORDER BY day"
        )
        for row in cursor.fetchall():
            day_str = row["day"]
            if not day_str:
                continue
            try:
                label = datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
                daily_sessions_map[label] = row["cnt"]
                daily_messages_map[label] = row["msgs"]
            except ValueError, TypeError:
                pass

        # 5. ═══ Token 用量 ═══
        cursor.execute(
            "SELECT DATE(created_at) as day, COALESCE(SUM(context_usage), 0) as total_tokens "
            "FROM sessions "
            "WHERE created_at >= date('now', '-13 days') "
            "AND project NOT LIKE '__archived__%' "
            "AND context_usage > 0 "
            "GROUP BY DATE(created_at) ORDER BY day"
        )
        for row in cursor.fetchall():
            day_str = row["day"]
            if not day_str:
                continue
            try:
                label = datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
                daily_tokens_map[label] = row["total_tokens"]
            except ValueError, TypeError:
                pass

        # 回退：对 context_usage=0 的旧会话，从 messages 估算 token
        cursor.execute(
            "SELECT DATE(created_at) as day, messages "
            "FROM sessions "
            "WHERE created_at >= date('now', '-13 days') "
            "AND project NOT LIKE '__archived__%' "
            "AND (context_usage IS NULL OR context_usage = 0) "
            "AND messages IS NOT NULL AND messages != '' "
            "ORDER BY created_at DESC"
        )
        for row in cursor.fetchall():
            day_str = row["day"]
            if not day_str:
                continue
            try:
                label = datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
                msg_data = row["messages"]
                if isinstance(msg_data, (str, bytes)):
                    tokens = _fast_estimate_tokens(str(msg_data)[:100000])
                    daily_tokens_map[label] = daily_tokens_map.get(label, 0) + tokens
            except Exception:
                pass

        for dl in date_labels:
            result["daily_sessions"].append((dl, daily_sessions_map[dl]))
            result["daily_messages"].append((dl, daily_messages_map[dl]))
            result["daily_tokens"].append((dl, daily_tokens_map[dl]))

        # 6. ═══ 平均消息数 / 日均 token ═══
        result["avg_messages_per_session"] = round(total_messages / total_sessions, 1) if total_sessions > 0 else 0.0

        total_in_window = sum(v for _, v in result["daily_tokens"])
        result["avg_daily_tokens"] = int(round(total_in_window / 14)) if total_in_window > 0 else 0

        conn.close()
    except Exception as e:
        result["error"] = f"{e}"
        logger.error(f"[ContextUsageStats] 数据读取失败: {e}\n{traceback.format_exc()}")
        try:
            conn.close()
        except Exception:
            pass

    return result


# ── 异步工作器 ───────────────────────────────────────────


class _DataWorker(QObject):
    """后台线程执行数据库读取"""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()

    def run(self):
        try:
            data = _fetch_session_stats()
            self.finished.emit(data)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")
