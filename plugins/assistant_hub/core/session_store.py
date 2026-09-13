# -*- coding: utf-8 -*-
"""session_store.py — 会话库只读访问（收编自 openhanako-adapter/tools/sessions.py 底层）。

供记忆传送带（compile/ticker）读取 <app_data>/sessions.db 的对话内容：
- 连接只读（mode=ro），绝不写主程序会话库。
- messages 反序列化对齐 app/core/store/serde.py（ZSTD\\x01 / JSON\\x01 魔数）。
- clean_messages 只留 user/assistant 文本轮次，剥 <think>、滤 <system-reminder>。
- logical_day：逻辑日边界 04:00（对齐 openhanako time-utils 的 DAY_BOUNDARY_HOUR）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

ROLES = ("user", "assistant")

# ── 数据库定位 ────────────────────────────────────────────


def _candidate_db_paths() -> List[Path]:
    """sessions.db 常见位置：主程序 app_data（开发=.drifox / 打包=~/.drifox）→ APPDATA → home。"""
    out: List[Path] = []
    try:
        # 主程序同源定位（开发环境 get_app_data_dir 返回相对 .drifox，依赖 cwd；resolve 兜底）
        from app.utils.utils import get_app_data_dir

        out.append(Path(get_app_data_dir()).resolve() / "sessions.db")
    except Exception:
        pass
    appdata = None
    try:
        import os

        appdata = os.getenv("APPDATA")
    except Exception:
        appdata = None
    if appdata:
        out.append(Path(appdata) / "DriFox" / "sessions.db")
        out.append(Path(appdata) / "drifox" / "sessions.db")
    out.append(Path.home() / ".drifox" / "sessions.db")
    return out


def find_db() -> Optional[Path]:
    for p in _candidate_db_paths():
        if p.exists():
            return p
    return None


def connect_ro(db_path: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    """只读连接；找不到库返回 None。"""
    path = db_path or find_db()
    if path is None or not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


# ── messages 反序列化（对齐主程序 serde.py）──────────────

_MAGIC_ZSTD = b"ZSTD"
_MAGIC_JSON = b"JSON"
_VERSION_V1 = b"\x01"


def deserialize_messages(data) -> Optional[list]:
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data:
        return None
    try:
        if data.startswith(_MAGIC_ZSTD):
            if data[4:5] != _VERSION_V1:
                return None
            from compression import zstd  # PEP 784, Python 3.14+

            return json.loads(zstd.ZstdDecompressor().decompress(data[5:]))
        if data.startswith(_MAGIC_JSON):
            if data[4:5] != _VERSION_V1:
                return None
            return json.loads(data[5:])
        return json.loads(data)
    except Exception:
        return None


def _content_text(msg: dict) -> str:
    """提取消息纯文本；content 为分片列表时拼接 text 部分。"""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for seg in c:
            if isinstance(seg, dict):
                if seg.get("type") == "text" and isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
                elif isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
            elif isinstance(seg, str):
                parts.append(seg)
        return "\n".join(parts)
    if c is None:
        return ""
    return str(c)


def _strip_think(text: str) -> str:
    """剥掉 <think>...</think> 推理块，只留正文。"""
    if "<think>" not in text:
        return text
    out, rest = [], text
    while True:
        head, sep, rest = rest.partition("<think>")
        out.append(head)
        if not sep:
            break
        _, sep2, rest = rest.partition("</think>")
        if not sep2:  # 未闭合：后面的内容全部视为推理，丢弃
            break
    return "".join(out)


def clean_messages(raw) -> List[dict]:
    """只留 user/assistant 文本轮次，跳过 <system-reminder> 注入块与空消息。"""
    out: List[dict] = []
    if not isinstance(raw, list):
        return out
    for msg in raw:
        if not isinstance(msg, dict) or msg.get("role") not in ROLES:
            continue
        text = _strip_think(_content_text(msg)).strip()
        if not text or "<system-reminder>" in text[:200]:
            continue
        out.append({"role": msg["role"], "text": text, "ts": str(msg.get("timestamp") or "")})
    return out


# ── 查询 ────────────────────────────────────────────────


def _fetch_clean(conn: sqlite3.Connection, where: str, params: tuple) -> List[dict]:
    sql = (
        "SELECT session_id, title, project, message_count, created_at, updated_at, preview, messages "
        f"FROM sessions {where} ORDER BY updated_at DESC"
    )
    out: List[dict] = []
    for row in conn.execute(sql, params):
        d = dict(row)
        d["msgs"] = clean_messages(deserialize_messages(d.pop("messages")))
        out.append(d)
    return out


def sessions_between(conn: sqlite3.Connection, start: str, end: str) -> List[dict]:
    """updated_at ∈ [start, end) 的会话（含清洗后的消息）。"""
    return _fetch_clean(
        conn,
        "WHERE updated_at >= ? AND updated_at < ?",
        (start, end),
    )


def turns_text(sessions: List[dict], max_chars: int = 24000) -> str:
    """把会话消息拼成「[MM-DD HH:MM] user:/assistant:」对话文本；超出尾部截断。"""
    lines: List[str] = []
    total = 0
    for s in sessions:
        for msg in s.get("msgs") or []:
            ts = (msg.get("ts") or "")[:16].replace("T", " ")
            prefix = f"[{ts}] " if ts else ""
            line = f"{prefix}{msg['role']}: {msg['text']}"
            if total + len(line) > max_chars:
                lines.append("…（已截断）")
                return "\n".join(lines)
            lines.append(line)
            total += len(line)
    return "\n".join(lines)


# ── 逻辑日（04:00 边界，对齐 openhanako）────────────────


def logical_day(dt: Optional[datetime] = None) -> str:
    """返回逻辑日 YYYY-MM-DD：04:00 前属于前一天。"""
    dt = dt or datetime.now()
    if dt.hour < 4:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")
