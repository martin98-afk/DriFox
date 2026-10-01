# -*- coding: utf-8 -*-
"""备份配额接线（EU-G6）：分治配额 + `limit <= 0 = 不限` + 启动期派发

背景：`enforce_backup_limit` 定义后**全仓零生产调用**（仅测试直调），等于
备份目录无上限，UI 显示的「上限 3000 MB」是纯文案。本批把它接进启动链。

分治理由（不可改回统算）：删除保护快照是用户误删文件后**唯一的恢复手段**，
FileRecorder 备份却是高频产物（每次编辑一条）。共享一个 FIFO 配额会让日常
编辑把快照挤干净 —— 这是安全功能静默失效，不可接受。

性能依据（实测，见汇报）：扫盘 + 删除耗时随文件数线性增长
（144 文件 ≈ 87ms / 2000 文件 ≈ 800ms / 10000 文件 ≈ 11.7s），
故启动期必须后台线程执行，不得阻塞主线程。
"""

import sys
import time
from pathlib import Path

import pytest

from app.utils.file_operation_recorder import cleanup_backups_partitioned, enforce_backup_limit


def _make_file(p: Path, size: int, mtime_offset: float = 0.0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    if mtime_offset:
        stamp = time.time() - mtime_offset
        import os

        os.utime(p, (stamp, stamp))


# ── 一、分治配额 ──


def test_partitioned_cleans_file_backups_and_snapshots_separately(tmp_path):
    """两类备份各自按配额清理，互不挤占

    数据量用 MB 级：配额按整数 MB 传递（int(limit_mb)），KB 级数据配 MB 配额
    无法触发超限。
    """
    mb = 1024 * 1024
    # FileRecorder 备份：3 × 0.6MB = 1.8MB，超 1MB 上限
    for i in range(3):
        _make_file(tmp_path / f"sess{i}" / "a.bak", int(0.6 * mb), mtime_offset=300 - i * 10)
    # 删除快照：单独目录
    _make_file(tmp_path / "deleted" / "snap.bak", int(0.6 * mb), mtime_offset=200)

    # 总配额 1MB → FileRecorder 分支清理；快照分支配额下限 100MB → 不清理
    result = cleanup_backups_partitioned(tmp_path, limit_mb=1)
    assert len(result["file_backups_removed"]) >= 1, "FileRecorder 备份应被清理"
    assert result["snapshots_removed"] == []
    assert (tmp_path / "deleted" / "snap.bak").exists(), "快照不得被文件备份配额波及"


def test_snapshot_excluded_from_file_backup_quota(tmp_path):
    """关键：FileRecorder 配额不得清理 deleted/ 下的快照"""
    _make_file(tmp_path / "sess" / "a.bak", 700, mtime_offset=300)
    _make_file(tmp_path / "deleted" / "snap.bak", 700, mtime_offset=100)

    # 只清文件备份分支（快照配额设为 0 = 不限）
    removed = enforce_backup_limit(tmp_path, 1 / 1024, exclude_names=("deleted",))
    names = [Path(p).name for p in removed]
    assert "snap.bak" not in names, "快照必须被排除在 FileRecorder 配额之外"
    assert (tmp_path / "deleted" / "snap.bak").exists()


def test_snapshot_limit_is_quarter_with_floor_100mb(tmp_path):
    """快照配额派生规则：总配额 1/4，下限 100MB"""
    # 总配额 1000MB → 1/4 = 250MB
    r = cleanup_backups_partitioned(tmp_path, limit_mb=1000)
    assert r["snapshot_limit_mb"] == 250
    # 总配额 200MB → 1/4 = 50MB，低于下限 → 取 100MB
    r = cleanup_backups_partitioned(tmp_path, limit_mb=200)
    assert r["snapshot_limit_mb"] == 100
    # 总配额 40MB → 1/4 = 10MB → 下限 100MB
    r = cleanup_backups_partitioned(tmp_path, limit_mb=40)
    assert r["snapshot_limit_mb"] == 100


def test_limit_zero_means_unlimited_both_partitions(tmp_path):
    """limit=0（不限）→ 两类备份都不清理"""
    _make_file(tmp_path / "sess" / "a.bak", 5000, mtime_offset=300)
    _make_file(tmp_path / "deleted" / "snap.bak", 5000, mtime_offset=200)

    result = cleanup_backups_partitioned(tmp_path, limit_mb=0)
    assert result["file_backups_removed"] == []
    assert result["snapshots_removed"] == []
    assert (tmp_path / "sess" / "a.bak").exists()
    assert (tmp_path / "deleted" / "snap.bak").exists()
    assert result["snapshot_limit_mb"] == 0


def test_missing_dirs_noop(tmp_path):
    """目录不存在 → 空结果，不抛异常"""
    result = cleanup_backups_partitioned(tmp_path / "nope", limit_mb=100)
    assert result["file_backups_removed"] == []
    assert result["snapshots_removed"] == []


def test_returns_removed_paths_for_ui(tmp_path):
    """返回值可被 UI「立即清理」用于展示删了多少（EU-G10 复用）"""
    _make_file(tmp_path / "sess" / "a.bak", int(1.5 * 1024 * 1024), mtime_offset=300)
    result = cleanup_backups_partitioned(tmp_path, limit_mb=1)
    assert isinstance(result["file_backups_removed"], list)
    assert len(result["file_backups_removed"]) == 1, "超限文件应被清理并出现在返回值中"
    assert all(isinstance(p, Path) for p in result["file_backups_removed"])


# ── 二、启动期派发（源码级断言：线程 + 异常兜底 + 调用入口）──


def _read_main_source() -> str:
    root = Path(__file__).resolve().parent.parent.parent
    return (root / "main.py").read_text(encoding="utf-8")


def test_startup_dispatches_cleanup_in_background_thread():
    """启动期必须把清理派发到后台线程（实测万级文件 11.7s，主线程会冻结 UI）"""
    src = _read_main_source()
    assert "cleanup_backups_partitioned" in src, "启动期未接线清理入口"
    assert "_cleanup_backups" in src and "threading.Thread" in src, "清理必须走后台线程"
    # 派发点必须带 daemon=True（不能阻塞进程退出）
    idx = src.find("def _cleanup_backups")
    assert idx > 0
    seg = src[idx : idx + 1200]
    assert "daemon=True" in seg, "清理线程必须为 daemon"


def test_startup_cleanup_has_exception_guard():
    """清理失败不得中断启动（与 sync_auto_start_from_config 同款 try/except 范式）"""
    src = _read_main_source()
    idx = src.find("def _cleanup_backups")
    seg = src[idx : idx + 800]
    assert "try:" in seg and "except Exception" in seg
    assert "logger.exception" in seg


def test_startup_reads_limit_from_sandbox_config():
    """配额来自 SandboxConfig['backup_limit_mb']（单一真源）"""
    src = _read_main_source()
    idx = src.find("def _cleanup_backups")
    seg = src[idx : idx + 800]
    assert "backup_limit_mb" in seg
    assert "SandboxConfig" in seg


def test_startup_cleanup_is_deferred_not_at_import():
    """清理在 _deferred_startup 内（事件循环后），不得在模块导入期执行"""
    src = _read_main_source()
    deferred_idx = src.find("def _deferred_startup")
    cleanup_idx = src.find("def _cleanup_backups")
    assert deferred_idx > 0 and cleanup_idx > deferred_idx, "清理必须位于 _deferred_startup 内"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
