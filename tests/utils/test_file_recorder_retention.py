# -*- coding: utf-8 -*-
"""备份容量 FIFO：超限删最旧、限额内不动、零限额全清"""
import os
import time
from pathlib import Path

from app.utils.file_operation_recorder import enforce_backup_limit


def _make_file(p: Path, size: int, mtime_offset: float):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    stamp = time.time() - mtime_offset
    os.utime(p, (stamp, stamp))


def test_under_limit_untouched(tmp_path):
    _make_file(tmp_path / "s1" / "a.bak", 100, mtime_offset=100)
    _make_file(tmp_path / "s2" / "b.bak", 100, mtime_offset=50)
    removed = enforce_backup_limit(tmp_path, limit_mb=1)
    assert removed == []


def test_over_limit_removes_oldest_first(tmp_path):
    _make_file(tmp_path / "s1" / "old.bak", 600, mtime_offset=200)  # 最旧
    _make_file(tmp_path / "s2" / "mid.bak", 600, mtime_offset=100)
    _make_file(tmp_path / "s3" / "new.bak", 600, mtime_offset=10)
    # 上限 1KB：总量 1800B 超限，删 old(600)+mid(600)=1200 后剩 600
    removed = enforce_backup_limit(tmp_path, limit_mb=1 / 1024)
    names = [Path(p).name for p in removed]
    assert names == ["old.bak", "mid.bak"]


def test_zero_limit_means_unlimited(tmp_path):
    """limit_mb=0 → 不清理任何文件（语义变更：旧行为是清空全部）

    变更理由：旧实现 `limit_mb <= 0` 会跳过上限判断 → 全部进删除列表 → 清空备份。
    对用户是陷阱（想表达"不限制"，结果备份全丢）。现统一为"0 = 不限"。
    """
    _make_file(tmp_path / "s1" / "a.bak", 10, mtime_offset=0)
    _make_file(tmp_path / "s2" / "b.bak", 10, mtime_offset=0)
    removed = enforce_backup_limit(tmp_path, limit_mb=0)
    assert removed == [], "0 应表示不限，不得删除任何文件"
    assert (tmp_path / "s1" / "a.bak").exists()
    assert (tmp_path / "s2" / "b.bak").exists()


def test_negative_limit_also_unlimited(tmp_path):
    """负数同样按"不限"处理（不因符号意外而全删）"""
    _make_file(tmp_path / "s1" / "a.bak", 10, mtime_offset=0)
    assert enforce_backup_limit(tmp_path, limit_mb=-5) == []


def test_missing_dir_noop(tmp_path):
    assert enforce_backup_limit(tmp_path / "nope", limit_mb=1) == []


def test_empty_dir_noop(tmp_path):
    (tmp_path / "s1").mkdir(parents=True)
    assert enforce_backup_limit(tmp_path, limit_mb=1) == []


# ── exclude_names：分治配额（FileRecorder 备份清理时排除删除快照）──


def test_exclude_names_skips_deleted(tmp_path):
    """exclude_names=("deleted",) → 只清理非快照文件，快照目录整体保留

    文件 2000B × 2 = 4000B，上限 1024B（1/1024 MB）：删一个后仍超限，
    故若快照未被排除，两个都会被删（可区分行为）。
    """
    _make_file(tmp_path / "sess1" / "a.bak", 2000, mtime_offset=200)
    _make_file(tmp_path / "deleted" / "b.bak", 2000, mtime_offset=100)

    removed = enforce_backup_limit(tmp_path, limit_mb=1 / 1024, exclude_names=("deleted",))
    names = [Path(p).name for p in removed]
    assert names == ["a.bak"], f"只应清理非快照文件，实际 {names}"
    assert (tmp_path / "deleted" / "b.bak").exists(), "删除快照不得被 FileRecorder 配额清理"


def test_exclude_names_empty_keeps_old_behavior(tmp_path):
    """不传 exclude_names → 行为与改造前一致（全量参与配额，两个都清）"""
    _make_file(tmp_path / "sess1" / "a.bak", 2000, mtime_offset=200)
    _make_file(tmp_path / "deleted" / "b.bak", 2000, mtime_offset=100)
    removed = enforce_backup_limit(tmp_path, limit_mb=1 / 1024)
    names = sorted(Path(p).name for p in removed)
    assert names == ["a.bak", "b.bak"]


def test_exclude_names_only_matches_top_level(tmp_path):
    """只排除顶层同名目录：深层的 deleted/ 不误排除"""
    _make_file(tmp_path / "sess1" / "deleted" / "deep.bak", 2000, mtime_offset=200)
    removed = enforce_backup_limit(tmp_path, limit_mb=1 / 1024, exclude_names=("deleted",))
    names = [Path(p).name for p in removed]
    assert names == ["deep.bak"], "深层同名目录不应被排除（只匹配顶层段）"
