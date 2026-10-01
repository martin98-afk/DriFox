# -*- coding: utf-8 -*-
"""删除保护：删除命令识别 + 存在路径快照 + 非删除命令不动作"""
from app.tools.sandbox import is_delete_command, snapshot_paths_before_delete


def test_is_delete_command():
    assert is_delete_command("rm -rf build/")
    assert is_delete_command("del x.txt")
    assert is_delete_command('powershell -c "Remove-Item x.txt"')
    assert is_delete_command("cmd /c deltree y")
    assert not is_delete_command("git status")
    assert not is_delete_command("mkdir build")


def test_snapshot_existing_paths(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("data", encoding="utf-8")
    backup_root = tmp_path / "snapshots"
    copied = snapshot_paths_before_delete(f"rm {target}", backup_root)
    assert len(copied) == 1
    assert copied[0].read_text(encoding="utf-8") == "data"


def test_snapshot_skips_missing_paths(tmp_path):
    backup_root = tmp_path / "snapshots"
    copied = snapshot_paths_before_delete(f"rm {tmp_path / 'ghost.txt'}", backup_root)
    assert copied == []


def test_snapshot_ignores_flags(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    copied = snapshot_paths_before_delete(f"rm -rf {target}", tmp_path / "snap")
    assert len(copied) == 1


def test_snapshot_non_delete_command_noop(tmp_path):
    assert snapshot_paths_before_delete("git status", tmp_path / "snap") == []


def test_snapshot_directory(tmp_path):
    src = tmp_path / "tree"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("deep", encoding="utf-8")
    copied = snapshot_paths_before_delete(f"rm -rf {src}", tmp_path / "snap")
    assert len(copied) == 1
    assert (copied[0] / "sub" / "f.txt").read_text(encoding="utf-8") == "deep"
