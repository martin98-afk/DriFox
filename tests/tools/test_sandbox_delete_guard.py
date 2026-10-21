# -*- coding: utf-8 -*-
"""删除审批：删除命令识别 + 垃圾箱 move 行为 + 相对路径 workdir 基准回归"""
from app.tools.sandbox import is_delete_command, stash_delete_targets_to_trash


def test_is_delete_command():
    assert is_delete_command("rm -rf build/")
    assert is_delete_command("del x.txt")
    assert is_delete_command('powershell -c "Remove-Item x.txt"')
    assert is_delete_command("cmd /c deltree y")
    assert not is_delete_command("git status")
    assert not is_delete_command("mkdir build")


# ── 相对路径基准（2026-10-13 实测缺陷回归，原快照链路等价迁移到垃圾箱 move）──


def test_stash_relative_target_resolves_against_workdir(tmp_path, monkeypatch):
    """相对路径目标按 workdir 解析（修前按进程 cwd → exists() False → 静默漏移）

    用 monkeypatch 把进程 cwd 设到别处，确保命中的只可能是显式 workdir。
    """
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    workdir = tmp_path / "proj"
    sub = workdir / "subdir"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("data", encoding="utf-8")

    trash_root = tmp_path / "deletes"
    moved = stash_delete_targets_to_trash("rm -rf subdir", trash_root, workdir=workdir)
    assert len(moved) == 1, "相对目标必须按 workdir 解析，否则静默漏移"
    assert (moved[0] / "a.txt").read_text(encoding="utf-8") == "data"
    assert not sub.exists(), "原目录应已移入垃圾箱"


def test_stash_relative_target_without_workdir_falls_back(tmp_path, monkeypatch):
    """未传 workdir 回退 `_current_workdir()`（向后兼容，不崩）"""
    from app.tools import sandbox as sb

    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "a.txt").write_text("data", encoding="utf-8")
    monkeypatch.setattr(sb, "_current_workdir", lambda: tmp_path)
    moved = stash_delete_targets_to_trash("rm -rf subdir", tmp_path / "deletes")
    assert len(moved) == 1


def test_stash_missing_target_only_noop(tmp_path):
    """命令里只写了不存在的目标时不动任何文件"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    moved = stash_delete_targets_to_trash(
        f"rm {workdir / 'ghost.txt'}", tmp_path / "deletes", workdir=workdir
    )
    assert moved == []
