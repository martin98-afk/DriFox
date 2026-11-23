# -*- coding: utf-8 -*-
"""#15 步骤 1：全库扫描 base64 明文 vision 图片位置（只读，不改任何数据）。

扫描范围（按「可能存 vision 图片副本」穷举）：
1. `sessions` 表所有 TEXT/BLOB 列（messages / compaction_cache / preview / ...）
2. `session_message_extras` 表所有列（offload 剥离字段，可能含 base64）
3. 其它所有表的 TEXT/BLOB 列（兜底，防遗漏快照/草稿/搜索索引）

判定方式：对每行每列的值做 `data:image` 子串检测。
- TEXT 列直接查
- BLOB 列按 `app.core.store.serde` 的格式解析（`ZSTD\x01` + zstd frame /
  `JSON\x01` + json / 无头旧 JSON），失败再当原始 bytes 查。
  ⚠️ 不能把整个 BLOB 直接交给 zstd 解压：serde 头是 5 字节前缀，
  直接解必然失败（旧实现因此在有图库上永远报 0）。

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

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_PERF_DIR = Path(__file__).resolve().parent
if str(_PERF_DIR) not in sys.path:
    sys.path.insert(0, str(_PERF_DIR))

from vision_scan_common import inline_image_stats, load_blob  # noqa: E402

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
            "blocks": 0,
            "b64_bytes": 0,
            "sessions": 0,
            "unique_images": 0,
        },
    }
    unique: set[str] = set()
    sessions: set[str] = set()
    # 块级口径汇总（与迁移器 blocks_replaced / b64_bytes / unique_images 同口径）
    vb_unique: set[str] = set()
    vb_sessions: set[str] = set()
    vb_rows = vb_blocks = vb_bytes_count = 0

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
        # 块级口径：只统计 content[i] 中 type=image_url 的内嵌图，与迁移器一致；
        # 全文本口径（上面的 table_stat）仍保留，能发现二维码/HTML/业务字段里的副本
        vision_stat: dict = {}
        idx = {c: i for i, (c, _) in enumerate(cols)}
        for row in cur:
            for cname, ctype in cols:
                val = row[idx[cname]]
                if val is None:
                    continue
                obj = None
                if isinstance(val, str):
                    text = val
                    obj = load_blob(val)
                elif isinstance(val, (bytes, bytearray)):
                    raw = bytes(val)
                    obj = load_blob(raw)
                    text = str(obj) if obj is not None else raw.decode("utf-8", "replace")
                else:
                    continue
                vb_n, vb_bytes, vb_uniq = (
                    inline_image_stats(obj) if obj is not None else (0, 0, set())
                )
                if vb_n:
                    vs = vision_stat.setdefault(
                        cname,
                        {"rows": 0, "blocks": 0, "b64_bytes": 0, "unique": set(), "sessions": set()},
                    )
                    vs["rows"] += 1
                    vs["blocks"] += vb_n
                    vs["b64_bytes"] += vb_bytes
                    vs["unique"] |= vb_uniq
                    vb_rows += 1
                    vb_blocks += vb_n
                    vb_bytes_count += vb_bytes
                    vb_unique |= vb_uniq
                    if sid_col and row[idx[sid_col]]:
                        vs["sessions"].add(str(row[idx[sid_col]]))
                        vb_sessions.add(str(row[idx[sid_col]]))
                if "data:image" not in text:
                    continue
                imgs = extract_images(text)
                if not imgs:
                    continue
                csv = table_stat.setdefault(
                    cname,
                    {"rows": 0, "blocks": 0, "b64_bytes": 0, "unique": set(), "sessions": set()},
                )
                csv["rows"] += 1
                csv["blocks"] += len(imgs)
                for sha, blen in imgs:
                    csv["b64_bytes"] += blen
                    csv["unique"].add(sha)
                    unique.add(sha)
                if sid_col and row[idx[sid_col]]:
                    csv["sessions"].add(str(row[idx[sid_col]]))
                    sessions.add(str(row[idx[sid_col]]))
                report["totals"]["rows_with_image"] += 1
                report["totals"]["blocks"] += len(imgs)
                report["totals"]["b64_bytes"] += sum(blen for _s, blen in imgs)

        if table_stat or vision_stat:
            cols_out: dict = {}
            for c in sorted(set(table_stat) | set(vision_stat)):
                v = table_stat.get(c) or {
                    "rows": 0, "blocks": 0, "b64_bytes": 0, "unique": set(), "sessions": set()
                }
                vs = vision_stat.get(c) or {
                    "rows": 0, "blocks": 0, "b64_bytes": 0, "unique": set(), "sessions": set()
                }
                cols_out[c] = {
                    "rows": v["rows"],
                    "blocks": v["blocks"],
                    "b64_bytes": v["b64_bytes"],
                    "unique_images": len(v["unique"]),
                    "sessions": len(v["sessions"]),
                    "vision_blocks": {
                        "rows": vs["rows"],
                        "blocks": vs["blocks"],
                        "b64_bytes": vs["b64_bytes"],
                        "unique_images": len(vs["unique"]),
                        "sessions": len(vs["sessions"]),
                    },
                }
            report["tables"][table] = cols_out

    report["totals"]["unique_images"] = len(unique)
    report["totals"]["sessions"] = len(sessions)
    report["totals"]["vision_blocks"] = {
        "rows_with_image": vb_rows,
        "blocks": vb_blocks,
        "b64_bytes": vb_bytes_count,
        "unique_images": len(vb_unique),
        "sessions": len(vb_sessions),
    }
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
