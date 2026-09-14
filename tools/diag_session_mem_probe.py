# -*- coding: utf-8 -*-
"""会话数据层内存探针 —— 量化「N 个会话的消息体常驻内存」的真实成本。

用途：判断 SessionManager 缓存多少会话是内存可接受的。与
``tools/diag_webengine_mem_probe.py``（测 WebEngine 渲染页成本）互补。

用法（项目根目录，用项目 .venv）：

    python tools/diag_session_mem_probe.py
    python tools/diag_session_mem_probe.py --top 5
    python tools/diag_session_mem_probe.py --db C:/Users/<你>/.drifox/sessions.db

输出：
  - 每个会话：blob 大小 / 消息条数 / 正文字符数 / RSS 增量 / tracemalloc 增量
  - 合计：blob 总量、RSS 总增量、膨胀系数（RSSΔ / blob）
  - consolidate_messages 的一次性成本与「重复调用是否叠加副本」

判据：
  - 膨胀系数 1.5-2.0× 属正常（zstd blob → Python 对象的固有开销）
  - 「重复 consolidate 20 轮」的 RSSΔ 应接近 0：LRU 条目之间是 list 浅拷贝，
    共享同一批 dict，不应出现 N 份全量副本
  - 单会话 deserialize 耗时 = 会话切换时按需重载的代价（实测 22-56ms）

本脚本只读业务数据，不修改任何业务代码。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import psutil  # noqa: E402

from app.core.store.serde import deserialize  # noqa: E402

DEFAULT_DB = os.path.join(".drifox", "sessions.db")


def _rss_mb(proc) -> float:
    return proc.memory_info().rss / 1024 / 1024


def _char_total(messages) -> int:
    """消息正文字符总数（含 multimodal 的 text 分片）。"""
    total = 0
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    total += len(p.get("text", "") or "")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="DriFox 会话数据层内存探针")
    ap.add_argument("--top", type=int, default=15, help="取样最大的多少个会话（默认 15）")
    ap.add_argument("--db", default=DEFAULT_DB, help="sessions.db 路径")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}")
        return

    proc = psutil.Process()
    con = sqlite3.connect(args.db)
    cur = con.cursor()
    cur.execute(
        "select session_id, messages, message_count from sessions "
        "order by length(messages) desc limit ?",
        (args.top,),
    )
    rows = cur.fetchall()
    if not rows:
        print("无数据")
        return

    print(f"数据库: {args.db}  ({os.path.getsize(args.db) / 1024 / 1024:.1f} MB)")
    print(f"取样会话数: {len(rows)}")
    header = (
        f"{'session_id':<14}{'blobKB':>9}{'msgs':>7}{'chars':>12}"
        f"{'RSSΔ(MB)':>10}{'tracedΔ(MB)':>12}{'累计RSS(MB)':>12}"
    )
    print(header)

    base_rss = _rss_mb(proc)
    tracemalloc.start()
    base_traced = tracemalloc.get_traced_memory()[0]

    held: list = []
    cumulative_blob = 0
    for sid, blob, _mcount in rows:
        blob_kb = len(blob) // 1024 if blob else 0
        cumulative_blob += blob_kb
        before_rss = _rss_mb(proc)
        before_traced = tracemalloc.get_traced_memory()[0]

        msgs = deserialize(blob) or []
        held.append(msgs)  # 保持引用，模拟 SessionManager 常驻
        chars = _char_total(msgs)

        after_rss = _rss_mb(proc)
        after_traced = tracemalloc.get_traced_memory()[0]
        print(
            f"{sid[:12]:<14}{blob_kb:>9}{len(msgs):>7}{chars:>12}"
            f"{after_rss - before_rss:>10.1f}{(after_traced - before_traced) / 1024 / 1024:>12.1f}"
            f"{after_rss - base_rss:>12.1f}"
        )

    cur_traced, peak_traced = tracemalloc.get_traced_memory()
    total_rss = _rss_mb(proc)
    print("-" * 76)
    print(f"blob 合计（压缩后）: {cumulative_blob / 1024:.1f} MB")
    print(f"RSS: {base_rss:.1f} -> {total_rss:.1f} MB  (Δ {total_rss - base_rss:.1f} MB)")
    print(f"tracemalloc: 当前 {cur_traced / 1024 / 1024:.1f} MB / 峰值 {peak_traced / 1024 / 1024:.1f} MB")
    if cumulative_blob:
        print(f"膨胀系数（RSSΔ / blob）: {(total_rss - base_rss) / (cumulative_blob / 1024):.2f}x")

    from app.core.message_content import consolidate_messages

    before = _rss_mb(proc)
    t0 = time.perf_counter()
    norm = [consolidate_messages(m) for m in held]
    dt = time.perf_counter() - t0
    after = _rss_mb(proc)
    print(f"consolidate_messages × {len(held)}: RSS Δ {after - before:.1f} MB, 耗时 {dt * 1000:.0f} ms")

    before2 = _rss_mb(proc)
    for _ in range(20):
        for m in held:
            consolidate_messages(m)
    after2 = _rss_mb(proc)
    print(f"重复 consolidate 20 轮: RSS Δ {after2 - before2:.1f} MB（检验 LRU 副本是否叠加）")
    print(f"norm 列表数: {len(norm)}, 首条消息数: {len(norm[0])}")
    tracemalloc.stop()


if __name__ == "__main__":
    main()
