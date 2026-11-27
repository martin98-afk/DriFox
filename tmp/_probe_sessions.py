# -*- coding: utf-8 -*-
"""临时探针：最近会话里工具调用的路径参数，判断工具卡显示的是文件还是目录。"""
import json
import os
import sys

sys.path.insert(0, r"D:\work\DriFox")
import sqlite3

from app.core.store.serde import deserialize

db = sqlite3.connect(r"D:\work\DriFox\.drifox\sessions.db")
db.row_factory = sqlite3.Row
rows = list(
    db.execute(
        "select session_id, title, messages, updated_at from sessions order by updated_at desc limit 2"
    )
)
for r in rows:
    print("=" * 70)
    print("session:", r["session_id"], "| updated:", r["updated_at"], "|", r["title"])
    try:
        msgs = deserialize(r["messages"]) or []
    except Exception as e:
        print("  messages 解析失败:", e)
        continue
    print("  msgs:", len(msgs))
    shown = 0
    for m in reversed(msgs):
        calls = m.get("tool_calls") or []
        for c in calls:
            fn = (c.get("function") or {}) if isinstance(c, dict) else {}
            name = fn.get("name", "")
            args = fn.get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            path = ""
            if isinstance(args, dict):
                path = args.get("file_path") or args.get("path") or ""
            print(f"  tool={name} path={path!r} exists={os.path.exists(path) if path else '-'}")
            shown += 1
            if shown >= 15:
                break
        if shown >= 15:
            break
