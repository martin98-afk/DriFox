# -*- coding: utf-8 -*-
"""会话 DB 内存构成诊断（零侵入，只读）。

用 serde.deserialize 解压 messages（zstd + orjson），统计：
- 单会话解压后体积排行（≈ 加载进内存的体积）
- 内容分类：图片 base64 / 工具结果 / 其他文本 / content 外字段
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.store.serde import deserialize  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else r"D:\work\DriFox\.drifox\sessions.db"

conn = sqlite3.connect(f"file:{Path(DB).as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT rowid, session_id, title, updated_at, messages, message_count FROM sessions"
).fetchall()
print(f"total sessions: {len(rows)}")


def classify(msgs):
    """返回 (img, tool, text, other) 体积分类，口径闭环。"""
    img = tool = text = other = 0
    for m in msgs:
        if not isinstance(m, dict):
            other += len(json.dumps(m, ensure_ascii=False))
            continue
        role = m.get("role", "?")
        full = len(json.dumps(m, ensure_ascii=False))
        content = m.get("content")
        in_content = 0
        chunks = content if isinstance(content, list) else [content]
        for c in chunks:
            if isinstance(c, dict):
                s = json.dumps(c, ensure_ascii=False)
                in_content += len(s)
                if "data:image" in s:
                    img += len(s)
                else:
                    text += len(s)
            elif isinstance(c, str):
                in_content += len(c)
                if "data:image" in c:
                    img += len(c)
                elif role == "tool":
                    tool += len(c)
                else:
                    text += len(c)
        other += max(0, full - in_content)
    return img, tool, text, other


stats = []
total_raw = total_blob = 0
for r in rows:
    blob = r["messages"]
    if not blob:
        continue
    total_blob += len(blob)
    try:
        msgs = deserialize(blob)
    except Exception as e:
        print(f"  [skip] rowid={r['rowid']} deserialize failed: {e}")
        continue
    if not isinstance(msgs, list):
        continue
    ser = json.dumps(msgs, ensure_ascii=False)
    total_raw += len(ser)
    img, tool, text, other = classify(msgs)
    stats.append((len(ser), img, tool, text, other, r["title"] or "?", r["updated_at"], r["rowid"], len(msgs)))

stats.sort(reverse=True)
print(f"\nDB blob 总计: {total_blob/1e6:.1f} MB → 解压后 JSON 总计: {total_raw/1e6:.1f} MB（内存视角）")
print("\nTop 15 最大会话（解压后 | 图片 | 工具 | 文本 | other | 条数 | 标题 | 更新时间）:")
for full, img, tool, text, other, title, ts, rid, n in stats[:15]:
    print(
        f"  {full/1e6:7.2f} MB | img {img/1e6:6.2f} | tool {tool/1e6:6.2f}"
        f" | txt {text/1e6:6.2f} | other {other/1e6:6.2f} | {n:4d} 条 | {title[:24]:24} | {ts} | rowid={rid}"
    )

agg_img = sum(s[1] for s in stats)
agg_tool = sum(s[2] for s in stats)
agg_text = sum(s[3] for s in stats)
agg_other = sum(s[4] for s in stats)
print(
    f"\n全库解压后合计: 图片 {agg_img/1e6:.1f} MB | 工具结果 {agg_tool/1e6:.1f} MB"
    f" | 其他文本 {agg_text/1e6:.1f} MB | content外字段 {agg_other/1e6:.1f} MB"
)

# Top1 会话 role 分布（tuple: full,img,tool,text,other,title,ts,rid,n）
if stats:
    rid = stats[0][7]
    blob = conn.execute("SELECT messages FROM sessions WHERE rowid=?", (rid,)).fetchone()[0]
    obj = deserialize(blob)
    roles = {}
    for m in obj:
        k = m.get("role", "?")
        roles[k] = roles.get(k, 0) + len(json.dumps(m, ensure_ascii=False))
    print(f"\nTop1 会话 rowid={rid} role 分布:")
    for k, v in sorted(roles.items(), key=lambda x: -x[1]):
        print(f"    role={k:10} {v/1e6:8.2f} MB")