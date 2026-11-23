# -*- coding: utf-8 -*-
"""#15 步骤 1：全库扫描 base64 明文 vision 图片位置（只读，不改任何数据）。

扫描范围（按「可能存 vision 图片副本」穷举）：
1. `sessions` 表所有 TEXT/BLOB 列（messages / compaction_cache / preview / ...）
2. `session_message_extras` 表所有列（offload 剥离字段，可能含 base64）
3. 其它所有表的 TEXT/BLOB 列（兜底，防遗漏快照/草稿/搜索索引）

判定方式：对每行每列的值做 `data:image` 子串检测。
- TEXT 列直接查
- BLOB 列先尝试 zstd 解压（serialize 用 zstd），失败再当原始 bytes 查

输出：表/列/行数/字节数/会话数/去重后唯一图片数（按 sha1 去重）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DATA_IMAGE_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,")
# base64 body 允许的字符（宽松：含 padding）
B64_BODY_RE = re.compile(r"[A-Za-z0-9+/=\s]+")


def open_ro(db_path: str) -> sqlite3.Connection:
    """只读打开（不允许任何写入，防止误伤真实库）。"""
    uri = "file:" + Path(db_path).as_posix() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def list_tables(con: sqlite3.Connection) -> list[str]:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def table_columns(con: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    cur = con.execute(f'PRAGMA table_info("{table}")')
    return [(r[1], (r[2] or "").upper()) for r in cur.fetchall()]


def try_decompress(blob: bytes) -> bytes | None:
    """尝试 zstd 解压（serialize 走 zstd）；失败返回 None。"""
    try:
        import zstandard as zstd

        return zstd.ZstdDecompressor().decompress(blob, max_output_size=512 * 1024 * 1024)
    except Exception:
        return None


def extract_images(text: str) -> list[tuple[str, int]]:
    """从文本里抽取所有 data:image base64 片段，返回 [(sha1_16, b64_len)]。"""
    out = []
    for m in DATA_IMAGE_RE.finditer(text):
        start = m.end()
        bm = B64_BODY_RE.match(text, start)
        if not bm:
            continue
        b64 = bm.group(0).strip()
        if len(b64) < 64:  # 太短的不是真图片
            continue
        out.append((hashlib.sha1(b64.encode("ascii", "ignore")).hexdigest()[:16], len(b64)))
    return out


def scan(db_path: str) -> dict:
    con = open_ro(db_path)
    report: dict = {
        "db": str(db_path),
        "db_bytes": os.path.getsize(db_path),
        "tables": {},
        "totals": {
            "rows_with_image": 0,
            "b64_bytes": 0,
            "sessions": 0,
            "unique_images": 0,
        },
    }
    unique: set[str] = set()
    sessions: set[str] = set()

    for table in list_tables(con):
        cols = table_columns(con, table)
        if not cols:
            continue
        # 找会话主键（sessions 用 session_id，extras 用 session_id）
        col_names = [c[0] for c in cols]
        sid_col = "session_id" if "session_id" in col_names else None

        sel_cols = ", ".join(f'"{c}"' for c, _ in cols)
        try:
            cur = con.execute(f'SELECT {sel_cols} FROM "{table}"')
        except sqlite3.Error as e:
            report["tables"][table] = {"error": str(e)}
            continue

        table_stat: dict = {}
        idx = {c: i for i, (c, _) in enumerate(cols)}
        for row in cur:
            for cname, ctype in cols:
                val = row[idx[cname]]
                if val is None:
                    continue
                text = None
                if isinstance(val, str):
                    text = val
                elif isinstance(val, (bytes, bytearray)):
                    b = bytes(val)
                    dec = try_decompress(b)
                    text = (dec if dec is not None else b).decode("utf-8", "replace")
                else:
                    continue
                if "data:image" not in text:
                    continue
                imgs = extract_images(text)
                if not imgs:
                    continue
                csv = table_stat.setdefault(
                    cname, {"rows": 0, "b64_bytes": 0, "unique": set(), "sessions": set()}
                )
                csv["rows"] += 1
                for sha, blen in imgs:
                    csv["b64_bytes"] += blen
                    csv["unique"].add(sha)
                    unique.add(sha)
                if sid_col and row[idx[sid_col]]:
                    csv["sessions"].add(str(row[idx[sid_col]]))
                    sessions.add(str(row[idx[sid_col]]))
                report["totals"]["rows_with_image"] += 1
                report["totals"]["b64_bytes"] += sum(blen for _s, blen in imgs)

        if table_stat:
            report["tables"][table] = {
                c: {
                    "rows": v["rows"],
                    "b64_bytes": v["b64_bytes"],
                    "unique_images": len(v["unique"]),
                    "sessions": len(v["sessions"]),
                }
                for c, v in table_stat.items()
            }

    report["totals"]["unique_images"] = len(unique)
    report["totals"]["sessions"] = len(sessions)
    # 估算可回收：base64 比原始字节大约 4/3，回收的是 b64 文本 → 原字节的差额
    report["totals"]["b64_mb"] = round(report["totals"]["b64_bytes"] / 1048576, 2)
    con.close()
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="扫描 sessions.db 里的 base64 明文 vision 图片（只读）")
    ap.add_argument("--db", default=str(Path(r"D:\work\DriFox\.drifox\sessions.db")))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rep = scan(args.db)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[saved] {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
