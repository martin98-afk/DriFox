# -*- coding: utf-8 -*-
"""content 外字段构成诊断：统计消息 dict 中除 content 外各字段的体积分布。"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.store.serde import deserialize  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else r"D:\work\DriFox\.drifox\sessions.db"
conn = sqlite3.connect(f"file:{Path(DB).as_posix()}?mode=ro", uri=True)

field_bytes = {}

for (blob,) in conn.execute("SELECT messages FROM sessions WHERE messages IS NOT NULL"):
    try:
        msgs = deserialize(blob)
    except Exception:
        continue
    if not isinstance(msgs, list):
        continue
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        for k, v in m.items():
            if k == "content":
                continue
            s = len(json.dumps(v, ensure_ascii=False))
            field_bytes[(k, role)] = field_bytes.get((k, role), 0) + s

rank = sorted(field_bytes.items(), key=lambda x: -x[1])
print("字段体积排行（content 之外，全库累计）:")
for (k, role), v in rank[:20]:
    print(f"  {k:24} role={role:10} {v / 1e6:10.2f} MB")
