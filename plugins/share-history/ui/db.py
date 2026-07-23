# -*- coding: utf-8 -*-
"""share-history — 基于 records.json 的分享记录存储层

目录结构:
    ~/.drifox/share/
    ├── sessions/        # 会话分享文件
    ├── projects/        # 项目导出文件
    └── records.json     # 所有分享记录 (JSON 数组)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


# ── 路径解析 ──────────────────────────────────────────────


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


def _records_path() -> Path:
    """获取分享记录文件路径"""
    return _drifox_dir() / "share" / "records.json"


# ── 内部读写 ──────────────────────────────────────────────


def _load_records() -> List[Dict[str, Any]]:
    """从 records.json 加载全部分享记录"""
    path = _records_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[ShareHistory] 读取 records.json 失败: {e}")
    return []


def _save_records(records: List[Dict[str, Any]]) -> bool:
    """保存分享记录到 records.json"""
    path = _records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        logger.error(f"[ShareHistory] 写入 records.json 失败: {e}")
        return False


# ── CRUD ──────────────────────────────────────────────────


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
    records = _load_records()
    next_id = max((r.get("id", 0) for r in records), default=0) + 1
    record = {
        "id": next_id,
        "type": type_,
        "title": title,
        "format": format_,
        "file_path": file_path or "",
        "upload_url": upload_url or "",
        "ref_id": ref_id or "",
        "extra_info": extra_info or {},
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    records.insert(0, record)
    ok = _save_records(records)
    if ok:
        logger.debug(f"[ShareHistory] 插入记录 id={record['id']}")
    return ok


def get_records(limit: int = 500) -> List[Dict[str, Any]]:
    """获取分享记录列表，按时间倒序"""
    records = _load_records()
    for row in records:
        if isinstance(row.get("extra_info"), str):
            try:
                row["extra_info"] = json.loads(row["extra_info"])
            except (json.JSONDecodeError, TypeError):
                row["extra_info"] = {}
    return records[:limit]


def delete_record(record_id: int) -> bool:
    """删除单条分享记录"""
    records = _load_records()
    new_records = [r for r in records if r.get("id") != record_id]
    if len(new_records) == len(records):
        return False
    ok = _save_records(new_records)
    if ok:
        logger.debug(f"[ShareHistory] 删除记录 id={record_id}")
    return ok


def clear_all_records() -> bool:
    """清空全部分享记录"""
    ok = _save_records([])
    if ok:
        logger.debug("[ShareHistory] 已清空所有记录")
    return ok


def update_record_file_path(record_id: int, file_path: str) -> bool:
    """更新指定记录的 file_path 字段"""
    records = _load_records()
    found = False
    for r in records:
        if r.get("id") == record_id:
            r["file_path"] = file_path
            found = True
            break
    if not found:
        return False
    ok = _save_records(records)
    if ok:
        logger.debug(f"[ShareHistory] 更新记录 id={record_id} file_path={file_path}")
    return ok
