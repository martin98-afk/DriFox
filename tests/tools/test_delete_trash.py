# -*- coding: utf-8 -*-
"""垃圾桶软删除：delete.trash_enabled + stash_delete_targets_to_trash"""

from app.tools.sandbox import SandboxConfig, stash_delete_targets_to_trash


def _cfg(tmp_path):
    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=str(tmp_path / "cfg.json"))
    cfg.set("sandbox_enabled", True)
    cfg.set("delete_protection", True)
    return cfg


def test_trash_enabled_default_false(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.get("delete.trash_enabled") is False


def test_stash_moves_targets_to_trash(tmp_path):
    """垃圾桶开启：删除目标 move 进垃圾桶目录，原路径消失"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    doomed_dir = workdir / "sdd"
    doomed_dir.mkdir()
    (doomed_dir / "keep.txt").write_text("x", encoding="utf-8")
    doomed_file = workdir / "log.txt"
    doomed_file.write_text("y", encoding="utf-8")

    trash_root = tmp_path / "backups" / "trash"
    cmd = f"rm -rf {doomed_dir.as_posix()} {doomed_file.as_posix()}"
    moved = stash_delete_targets_to_trash(cmd, trash_root, workdir=workdir)

    assert len(moved) == 2, f"两个目标都应被移入垃圾桶: {moved}"
    assert not doomed_dir.exists(), "原目录应已移走"
    assert not doomed_file.exists(), "原文件应已移走"
    for dest in moved:
        assert dest.exists() and trash_root in dest.parents


def test_stash_noop_for_non_delete(tmp_path):
    """非删除命令不动任何文件"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    f = workdir / "a.txt"
    f.write_text("x", encoding="utf-8")
    moved = stash_delete_targets_to_trash("echo hi", tmp_path / "trash", workdir=workdir)
    assert moved == []
    assert f.exists()


def test_stash_survives_failure(tmp_path, monkeypatch):
    """单个目标移动失败不阻断其余目标（快照同款语义）"""
    import app.tools.sandbox as sandbox_mod

    workdir = tmp_path / "proj"
    workdir.mkdir()
    a = workdir / "a.txt"
    a.write_text("a", encoding="utf-8")
    b = workdir / "b.txt"
    b.write_text("b", encoding="utf-8")

    real_move = sandbox_mod.shutil.move
    calls = {"n": 0}

    def flaky_move(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("boom")
        real_move(src, dst)

    monkeypatch.setattr(sandbox_mod.shutil, "move", flaky_move)
    trash_root = tmp_path / "trash"
    moved = stash_delete_targets_to_trash(f"rm {a.as_posix()} {b.as_posix()}", trash_root, workdir=workdir)
    assert len(moved) == 1, "第一个失败后第二个仍应成功"
    assert b.exists() is False or moved, "不阻断"
