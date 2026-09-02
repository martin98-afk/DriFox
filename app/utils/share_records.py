# -*- coding: utf-8 -*-
"""统一的分享记录存储层 — 基于 JSON 文件，取代 SQLite

目录结构:
    ~/.drifox/share/
    ├── sessions/        # 会话分享文件
    ├── projects/        # 项目导出文件
    └── records.json     # 所有分享记录 (JSON 数组)

每条记录格式:
    {
        "id": 1,
        "type": "session" | "project",
        "title": "...",
        "format": "md" | "json" | "html" | "drifox_project",
        "file_path": "...",
        "upload_url": "...",
        "ref_id": "...",
        "extra_info": {...},
        "created_at": "2026-07-23 22:41:43"
    }
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


# ── 路径解析 ──────────────────────────────────────────────


def _drifox_dir() -> Path:
    """获取应用数据目录"""
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


def get_share_dir() -> Path:
    """获取分享根目录 ~/.drifox/share/"""
    return _drifox_dir() / "share"


def get_sessions_dir() -> Path:
    """获取会话分享目录 ~/.drifox/share/sessions/"""
    return get_share_dir() / "sessions"


def get_projects_dir() -> Path:
    """获取项目导出目录 ~/.drifox/share/projects/"""
    return get_share_dir() / "projects"


def get_records_path() -> Path:
    """获取分享记录文件路径 ~/.drifox/share/records.json"""
    return get_share_dir() / "records.json"


def ensure_dirs() -> None:
    """确保分享相关目录存在"""
    get_sessions_dir().mkdir(parents=True, exist_ok=True)
    get_projects_dir().mkdir(parents=True, exist_ok=True)


# ── 线程安全锁 ────────────────────────────────────────────

_lock = threading.Lock()


# ── 内部读写 ──────────────────────────────────────────────


def _load_records() -> List[Dict[str, Any]]:
    """从 records.json 加载全部分享记录"""
    path = get_records_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[ShareRecords] 读取 records.json 失败: {e}")
    return []


def _save_records(records: List[Dict[str, Any]]) -> bool:
    """保存分享记录到 records.json"""
    path = get_records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        logger.error(f"[ShareRecords] 写入 records.json 失败: {e}")
        return False


def _next_id(records: List[Dict[str, Any]]) -> int:
    """获取下一个自增 ID"""
    if not records:
        return 1
    return max(r.get("id", 0) for r in records) + 1


# ── CRUD API ──────────────────────────────────────────────


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
    with _lock:
        records = _load_records()
        record = {
            "id": _next_id(records),
            "type": type_,
            "title": title,
            "format": format_,
            "file_path": file_path or "",
            "upload_url": upload_url or "",
            "ref_id": ref_id or "",
            "extra_info": extra_info or {},
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        records.insert(0, record)  # 最新在最前面
        ok = _save_records(records)
        if ok:
            logger.debug(f"[ShareRecords] 插入记录 id={record['id']} type={type_} title={title}")
        return ok


def get_records(limit: int = 500) -> List[Dict[str, Any]]:
    """获取分享记录列表，按时间倒序"""
    records = _load_records()
    return records[:limit]


def delete_record(record_id: int) -> bool:
    """删除单条分享记录"""
    with _lock:
        records = _load_records()
        new_records = [r for r in records if r.get("id") != record_id]
        if len(new_records) == len(records):
            return False  # 未找到
        ok = _save_records(new_records)
        if ok:
            logger.debug(f"[ShareRecords] 删除记录 id={record_id}")
        return ok


def clear_all_records() -> bool:
    """清空全部分享记录"""
    with _lock:
        ok = _save_records([])
        if ok:
            logger.debug("[ShareRecords] 已清空所有记录")
        return ok


def get_records_path_for_sync() -> str:
    """返回 records.json 的绝对路径（供 ConfigSyncService 同步用）"""
    return str(get_records_path().resolve())
