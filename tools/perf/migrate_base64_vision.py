# -*- coding: utf-8 -*-
"""存量 vision base64 迁移器：sessions.messages 内嵌 data:image → CAS 文件引用。

格式完全复用生产链（不另写一套）：
- 解析/序列化：``app.core.store.serde``（zstd + 5 字节 magic，兼容旧 JSON）
- 落盘 + 改写：``app.core.store.session_repository._persist_vision_image_blocks_impl``
  → ``<cas-dir>/screenshots/persisted/<session_id>/<sha1(raw)[:16]><ext>``
  → 块替换为 ``{"type": "image_ref", "image_ref": {"file", "meta", "session_id"}}``
- 备份：``app.utils.db_manager.DatabaseManager.backup_to()``

安全约束：
- ``--db`` 必需；指向真实数据目录（app data / 路径含 .drifox）时必须显式
  加 ``--i-understand-real-db``，否则直接拒绝。
- 迁移前先备份；备份失败立即中止，不改库。
- 单会话单事务（BEGIN IMMEDIATE）；UPDATE 带旧 blob 做 CAS，防覆盖并发写。
- 每 ``--batch-size`` 个会话做一次 WAL checkpoint + 30ms 让锁，避免长占写锁。
- 幂等：image_ref 块跳过；重序列化后字节不变则不 UPDATE，重复执行 0 变更。

用法：
    python tools/perf/migrate_base64_vision.py --db <副本.db> --cas-dir <临时目录> --dry-run
    python tools/perf/migrate_base64_vision.py --db <副本.db> --cas-dir <临时目录>
    python tools/perf/migrate_base64_vision.py --db <副本.db> --cas-dir <临时目录> --verify 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_PERF_DIR = Path(__file__).resolve().parent
if str(_PERF_DIR) not in sys.path:
    sys.path.insert(0, str(_PERF_DIR))

from vision_scan_common import count_image_refs, inline_image_stats  # noqa: E402

from app.core.store.serde import deserialize, serialize  # noqa: E402
from app.core.store.session_repository import (  # noqa: E402
    _persist_vision_image_blocks_impl,
    revive_vision_image_refs,
)
from app.utils.app_paths import get_app_data_dir  # noqa: E402

_SAFE_SID = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA1_16 = re.compile(r"^[0-9a-f]{16}$")


# ── 数据访问 ──────────────────────────────────────────────────────────────


def open_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:" + Path(db_path).resolve().as_posix() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def open_rw(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(str(Path(db_path).resolve()), timeout=30, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _iter_sessions(con: sqlite3.Connection, limit: int | None):
    sql = "SELECT session_id, messages FROM sessions WHERE messages IS NOT NULL ORDER BY rowid"
    for n, row in enumerate(con.execute(sql)):
        if limit is not None and n >= limit:
            break
        yield row[0], row[1]


# ── 块级扫描（只读，用于 dry-run 与迁移前基线）──────────────────────────


# 实现与 scan_base64_vision.py 共用 vision_scan_common，避免格式/口径漂移
_inline_image_stats = inline_image_stats
_count_image_refs = count_image_refs

# ── 安全闸 ────────────────────────────────────────────────────────────────


def is_real_data_path(db_path: str) -> bool:
    """库文件是否位于真实数据目录（默认 app data 或任何 .drifox 目录）。"""
    p = Path(db_path).resolve()
    try:
        if p.is_relative_to(Path(get_app_data_dir()).resolve()):
            return True
    except (OSError, ValueError):
        pass
    return any(part.lower() == ".drifox" for part in p.parts)


# ── 备份 / 统计 ───────────────────────────────────────────────────────────


def backup_db(db_path: str, target: str) -> tuple[bool, str]:
    """SQLite 在线备份（与 ``DatabaseManager.backup_to`` 同 API），源库只读零副作用。

    不用 ``db_manager.backup_to`` 的原因：它先 ``connect()`` 再备份，connect 会
    执行 ``PRAGMA journal_mode=WAL`` 等，**备份失败时源库已被改写**（实测 rollback
    journal 库的主库文件 sha256 变化）。本函数从只读连接发起 backup，失败时源库
    字节不变，满足「备份失败立即中止且不改库」。
    """
    try:
        src = open_ro(db_path)
        try:
            dst = sqlite3.connect(str(Path(target).resolve()))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return True, f"备份成功: {target}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _db_bytes(db_path: str) -> int:
    total = 0
    for suffix in ("", "-wal"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


# ── 迁移 ──────────────────────────────────────────────────────────────────


def migrate(args) -> dict:
    db_path = str(Path(args.db).resolve())
    cas_root = Path(args.cas_dir).resolve()
    stats: dict = {
        "db": db_path,
        "cas_dir": str(cas_root),
        "dry_run": bool(args.dry_run),
        "limit": args.limit,
        "sessions_scanned": 0,
        "sessions_with_images": 0,
        "sessions_migrated": 0,
        "sessions_already_ref": 0,
        "blocks_replaced": 0,
        "b64_bytes": 0,
        "unique_images": 0,
        "failed_sessions": [],
        "backup_path": None,
        "db_bytes_before": _db_bytes(db_path),
        "db_bytes_after": None,
        "elapsed_s": None,
    }
    uniq_all: set[str] = set()
    t0 = time.monotonic()

    if args.dry_run:
        con = open_ro(db_path)
        try:
            for sid, blob in _iter_sessions(con, args.limit):
                stats["sessions_scanned"] += 1
                try:
                    data = deserialize(blob)
                except Exception as e:  # noqa: BLE001
                    stats["failed_sessions"].append({"session_id": sid, "error": repr(e)})
                    continue
                n, nbytes, uniq = _inline_image_stats(data)
                if n:
                    stats["sessions_with_images"] += 1
                    stats["blocks_replaced"] += n
                    stats["b64_bytes"] += nbytes
                    uniq_all |= uniq
                elif _count_image_refs(data):
                    stats["sessions_already_ref"] += 1
        finally:
            con.close()
        stats["unique_images"] = len(uniq_all)
        stats["elapsed_s"] = round(time.monotonic() - t0, 2)
        return stats

    # 真实迁移：先备份，失败立即中止
    backup_path = args.backup_to or (
        f"{db_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    ok, msg = backup_db(db_path, backup_path)
    if not ok:
        raise SystemExit(f"[ABORT] 备份失败，未做任何修改: {msg}")
    stats["backup_path"] = backup_path
    print(f"[backup] {msg}", file=sys.stderr)

    con = open_rw(db_path)
    done_since_checkpoint = 0
    try:
        for sid, blob in _iter_sessions(con, args.limit):
            stats["sessions_scanned"] += 1
            if not sid or not _SAFE_SID.match(str(sid)):
                stats["failed_sessions"].append(
                    {"session_id": sid, "error": "unsafe session_id"}
                )
                continue
            try:
                data = deserialize(blob)
            except Exception as e:  # noqa: BLE001
                stats["failed_sessions"].append({"session_id": sid, "error": repr(e)})
                continue
            n_before, nbytes, uniq = _inline_image_stats(data)
            if not n_before:
                if _count_image_refs(data):
                    stats["sessions_already_ref"] += 1
                continue
            stats["sessions_with_images"] += 1
            stats["b64_bytes"] += nbytes
            uniq_all |= uniq

            try:
                _persist_vision_image_blocks_impl(data, str(sid), cas_root)
                new_blob = serialize(data)
            except Exception as e:  # noqa: BLE001 — 单会话失败只记录
                stats["failed_sessions"].append({"session_id": sid, "error": repr(e)})
                continue
            if new_blob == blob:
                continue  # 全部块落盘失败（原块保留）→ 本轮无变更

            try:
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute(
                    "UPDATE sessions SET messages = ? "
                    "WHERE session_id = ? AND messages = ?",
                    (new_blob, sid, blob),
                )
                if cur.rowcount == 1:
                    con.execute("COMMIT")
                    stats["sessions_migrated"] += 1
                    stats["blocks_replaced"] += n_before
                else:
                    con.execute("ROLLBACK")  # blob 被并发覆盖：放弃本轮
                    stats["failed_sessions"].append(
                        {"session_id": sid, "error": "blob changed concurrently"}
                    )
            except Exception as e:  # noqa: BLE001
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                stats["failed_sessions"].append({"session_id": sid, "error": repr(e)})
                continue

            done_since_checkpoint += 1
            if done_since_checkpoint >= args.batch_size:
                # 会话事务已提交（写锁已释放）；checkpoint 把 WAL 回写主库，
                # sleep 让同库其它连接（DriFox/其它进程）拿到执行机会。
                try:
                    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
                time.sleep(0.03)
                done_since_checkpoint = 0
    finally:
        con.close()

    # SQLite 删大 blob 只把页放回 freelist，文件不缩；--vacuum 才真正回收磁盘
    if args.vacuum:
        con = open_rw(db_path)
        try:
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
            free_pages = con.execute("PRAGMA freelist_count").fetchone()[0]
            stats["freelist_bytes"] = int(page_size) * int(free_pages)
            t_v = time.monotonic()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            stats["vacuum_s"] = round(time.monotonic() - t_v, 2)
        finally:
            con.close()

    stats["unique_images"] = len(uniq_all)
    stats["db_bytes_after"] = _db_bytes(db_path)
    stats["elapsed_s"] = round(time.monotonic() - t0, 2)
    return stats


# ── 迁移后校验 ────────────────────────────────────────────────────────────


def verify(args) -> dict:
    """抽 N 个会话：image_ref → revive → data:image，校验还原内容与 CAS 文件一致。"""
    db_path = str(Path(args.db).resolve())
    cas_root = Path(args.cas_dir).resolve()
    out: dict = {
        "db": db_path,
        "cas_dir": str(cas_root),
        "sessions_checked": 0,
        "refs": 0,
        "revived": 0,
        "sha_matched": 0,
        "missing_files": [],
        "mismatched": [],
    }
    con = open_ro(db_path)
    try:
        for sid, blob in _iter_sessions(con, args.limit):
            if out["sessions_checked"] >= args.verify:
                break
            try:
                data = deserialize(blob)
            except Exception:  # noqa: BLE001
                continue
            n_ref = _count_image_refs(data)
            if not n_ref:
                continue
            out["sessions_checked"] += 1
            out["refs"] += n_ref
            revived = revive_vision_image_refs(data, root_dir=cas_root)
            for msg in revived:
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "image_url":
                        continue
                    img = block.get("image_url")
                    url = img.get("url") if isinstance(img, dict) else None
                    if not isinstance(url, str) or not url.startswith("data:image"):
                        continue
                    out["revived"] += 1
                    comma = url.find(",")
                    try:
                        import base64

                        raw = base64.b64decode(url[comma + 1 :])
                    except Exception:  # noqa: BLE001
                        out["mismatched"].append({"session_id": sid, "error": "bad b64"})
                        continue
                    digest = hashlib.sha1(raw).hexdigest()[:16]
                    # 该块对应的 CAS 文件：按 session 目录 + digest 找
                    d = cas_root / "screenshots" / "persisted" / str(sid)
                    hits = list(d.glob(digest + ".*")) if d.is_dir() else []
                    if not hits:
                        out["missing_files"].append(
                            {"session_id": sid, "digest": digest}
                        )
                    else:
                        out["sha_matched"] += 1
    finally:
        con.close()
    return out


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="存量 vision base64 → CAS 文件引用迁移（默认拒绝真实库）"
    )
    ap.add_argument("--db", required=True, help="sessions.db 路径（必需，显式指定）")
    ap.add_argument(
        "--cas-dir",
        default=None,
        help="app data 根目录（内部拼 screenshots/persisted/<sid>）；默认 get_app_data_dir()",
    )
    ap.add_argument("--dry-run", action="store_true", help="只统计不写（不备份、不改库）")
    ap.add_argument("--limit", type=int, default=None, help="最多扫描 N 个会话")
    ap.add_argument("--batch-size", type=int, default=20, help="每 N 个会话 checkpoint 一次")
    ap.add_argument("--backup-to", default=None, help="备份目标路径（默认 <db>.bak-<ts>）")
    ap.add_argument(
        "--vacuum",
        action="store_true",
        help="迁移后 VACUUM 回收空闲页（需独占锁、临时空间≈库大小；DriFox 运行中勿用）",
    )
    ap.add_argument("--verify", type=int, default=0, help="只校验 N 个已迁移会话后退出")
    ap.add_argument(
        "--i-understand-real-db",
        action="store_true",
        help="允许对真实数据目录的库执行（危险，先确认 DriFox 已退出）",
    )
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        raise SystemExit(f"[ABORT] 库不存在: {db}")
    if args.cas_dir is None:
        args.cas_dir = str(get_app_data_dir())
    args.cas_dir = str(Path(args.cas_dir).resolve())

    if is_real_data_path(str(db)) and not args.i_understand_real_db:
        raise SystemExit(
            "[ABORT] 拒绝在真实数据目录上运行：\n"
            f"  db     = {db.resolve()}\n"
            f"  appdata= {Path(get_app_data_dir()).resolve()}\n"
            "  先复制副本，在副本上迁移；确需操作真实库请加 --i-understand-real-db"
        )

    if args.verify:
        result = verify(args)
    else:
        result = migrate(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
