# -*- coding: utf-8 -*-
"""一次性幂等补丁：把 SQLite mmap 上限从 256MB 降到 64MB。

背景
----
`db_manager.py:84` 设 `PRAGMA mmap_size=268435456`（256MB）。sessions.db 实测
236MB → 几乎整个数据文件被映射进进程 RSS。对宿主进程 20:11 快照实测：
`sessions.db` 独占 **235.8 MB**，是当时 USS 788MB 中的最大单项。

这是「会话多 → 内存高」的第二大来源（第一是已修的团队首问 285MB）：它不随
所选项目变化，但随 DB 体积增长，封顶 256MB。

影响评估
--------
- mmap 的 clean 文件页在内存压力下可被系统回收，所以这部分属于「可回收的
  工作集」而非泄漏；但它确实体现在任务管理器的读数里。
- 降到 64MB 后，超出部分改走普通 read() + SQLite page cache。而 page cache
  仍是 62.5MB（`session_store.py:371` 的 `cache_size=-64000` 覆盖了
  db_manager 的 -65536），热数据依然有缓存，日常交互几乎无感。
- 仅在全表扫描类查询（导出/统计）上会多出一些 I/O。

用法
----
    python tools/apply_sqlite_mmap_patch.py            # 应用（256MB → 64MB）
    python tools/apply_sqlite_mmap_patch.py --revert   # 回滚
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.eol_guard import write_text_keep_eol  # noqa: E402

DBM = "app/utils/db_manager.py"

OLD = """            # 启用 mmap 读取（减少系统调用）
            cursor.execute("PRAGMA mmap_size=268435456")"""

NEW = """            # 启用 mmap 读取（减少系统调用）
            # 🔧 T5 内存治理：256MB(268435456) → 64MB(67108864)。
            # sessions.db 实测 236MB 时，256MB 上限会让整个数据文件映射进 RSS
            # （实测独占 235.8MB，是当时进程 USS 788MB 的最大单项）。超出 64MB
            # 的部分改走 read() + page cache（62.5MB，见 session_store 的
            # cache_size=-64000），热数据仍有缓存，仅全表扫描类查询略增 I/O。
            cursor.execute("PRAGMA mmap_size=67108864")"""

EDITS = [(DBM, OLD, NEW, 1)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    pairs = [(p, new, old, n) for (p, old, new, n) in EDITS[::-1]] if args.revert else EDITS

    loaded: dict = {}
    for path, old, new, expect in pairs:
        full = ROOT / path
        if path not in loaded:
            loaded[path] = full.read_text(encoding="utf-8")
        got = loaded[path].count(old)
        if got != expect:
            print(f"[FAIL] {path}: 期望命中 {expect} 次，实际 {got} 次")
            print(repr(old[:120]))
            return 1

    for path, old, new, _ in pairs:
        loaded[path] = loaded[path].replace(old, new, 1)

    for path, text in loaded.items():
        used = write_text_keep_eol(str(ROOT / path), text)
        print(f"[OK] {path}  (行尾 {used!r})")

    print("\n已回滚" if args.revert else "\n已应用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
