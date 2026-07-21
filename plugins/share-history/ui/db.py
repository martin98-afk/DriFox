# -*- coding: utf-8 -*-
"""share-history — 基于 sessions.db 的分享记录存储层"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


# ── 路径解析（与 plugin-marketplace 一致） ──


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）

    开发环境: 当前目录/.drifox
    PyInstaller打包: ~/.drifox
    macOS .app: ~/Library/Application Support/Drifox/.drifox
    """
    import sys as _sys

    if not hasattr(_sys, "_MEIPASS") and not getattr(_sys, "frozen", False):
        return Path(".drifox")
    if _sys.platform == "darwin":
        try:
            from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask

            paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
                NSApplicationSupportDirectory, NSUserDomainMask
            )
            if paths:
                app_support = Path(paths[0].fileSystemRepresentation().decode("utf-8")) / "Drifox"
                app_support.mkdir(parents=True, exist_ok=True)
                return app_support / ".drifox"
        except Exception:
            pass
    return Path.home() / ".drifox"


# ── 数据库连接 ──


def _get_conn() -> Optional[sqlite3.Connection]:
    """获取 sessions.db 连接（自动建表 + WAL）"""
    try:
        db_path = _drifox_dir() / "sessions.db"
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
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


# ── CRUD ──


def insert_record(
    type_: str,
    title: str,
    format_: str,
    file_path: str = "",
    upload_url: str = "",
    ref_id: str = "",
    extra_info: Optional[Dict[str, Any]] = None,
) -> bool:
    """插入一条分享记录"""
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


def delete_record(record_id: int) -> bool:
    """删除单条分享记录"""
    try:
        conn = _get_conn()
        if conn is None:
            return False
        conn.execute("DELETE FROM share_records WHERE id = ?", (record_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"[ShareHistory] 删除记录失败: {e}")
        return False


def clear_all_records() -> bool:
    """清空全部分享记录"""
    try:
        conn = _get_conn()
        if conn is None:
            return False
        conn.execute("DELETE FROM share_records")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"[ShareHistory] 清空记录失败: {e}")
        return False
