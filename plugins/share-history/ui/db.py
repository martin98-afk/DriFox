# -*- coding: utf-8 -*-
"""share-history — 基于 sessions.db 的分享记录存储层

使用 ~/.drifox/sessions.db 中的 share_records 表存储分享历史。
采用独立 sqlite3 连接（不耦合 SessionStore/DatabaseManager），
通过 WAL 模式避免与主程序写入冲突。
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_DB_PATH = Path.home() / ".drifox" / "sessions.db"


def _get_conn() -> Optional[sqlite3.Connection]:
    """获取数据库连接（自动建表 + 启用 WAL）"""
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        # WAL 模式：读写不互斥，避免与 SessionStore 并发冲突
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL CHECK(type IN ('session','project')),
                title       TEXT NOT NULL,
                format      TEXT NOT NULL,
                file_path   TEXT DEFAULT '',
                upload_url  TEXT DEFAULT '',
                ref_id      TEXT DEFAULT '',
                extra_info  TEXT DEFAULT '{}',
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_share_records_time ON share_records(created_at DESC)")
        conn.commit()
        return conn
    except Exception as e:
        logger.error(f"[ShareHistory] 连接 sessions.db 失败: {e}")
        return None


def insert_record(
    type_: str,
    title: str,
    format_: str,
    file_path: str = "",
    upload_url: str = "",
    ref_id: str = "",
    extra_info: Optional[Dict[str, Any]] = None,
) -> bool:
    """插入一条分享记录

    Args:
        type_: 'session' 或 'project'
        title: 会话标题或项目名
        format_: md/json/html（session）或 drifox_project（project）
        file_path: 本地保存路径
        upload_url: Gitee 上传链接
        ref_id: session_id（session）或 project_name（project）
        extra_info: 额外信息，如 {"msg_count": 12} 或 {"session_count": 5}
    """
    try:
        conn = _get_conn()
        if conn is None:
            return False
        conn.execute(
            "INSERT INTO share_records "
            "(type, title, format, file_path, upload_url, ref_id, extra_info) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                type_,
                title,
                format_,
                file_path or "",
                upload_url or "",
                ref_id or "",
                json.dumps(extra_info or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"[ShareHistory] 插入记录失败: {e}")
        return False


def get_records(limit: int = 500) -> List[Dict[str, Any]]:
    """获取分享记录列表，按时间倒序"""
    try:
        conn = _get_conn()
        if conn is None:
            return []
        cursor = conn.execute(
            "SELECT * FROM share_records ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        for row in rows:
            if isinstance(row.get("extra_info"), str):
                try:
                    row["extra_info"] = json.loads(row["extra_info"])
                except json.JSONDecodeError, TypeError:
                    row["extra_info"] = {}
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"[ShareHistory] 查询记录失败: {e}")
        return []
