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


def test_zero_limit_clears_all(tmp_path):
    _make_file(tmp_path / "s1" / "a.bak", 10, mtime_offset=0)
    removed = enforce_backup_limit(tmp_path, limit_mb=0)
    assert len(removed) == 1


def test_missing_dir_noop(tmp_path):
    assert enforce_backup_limit(tmp_path / "nope", limit_mb=1) == []


def test_empty_dir_noop(tmp_path):
    (tmp_path / "s1").mkdir(parents=True)
    assert enforce_backup_limit(tmp_path, limit_mb=1) == []
