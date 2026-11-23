# -*- coding: utf-8 -*-
"""test_project_notes_git_signals.py — git 采集双信号回归测试。

覆盖 #27 的三条缺陷修复：
1. `reset --soft` 只移动 ref 不写 refs 目录 → logs/HEAD 必须进 head_sig
2. status 超时/失败不得压制同时成功的 log 结果（commits 陈旧 300s）
3. log 真失败不得把 commits 写成空；空仓库（无提交）仍要显示「(无)」

用真实临时仓库（git 需在 PATH），不 mock git 输出，保证信号来自文件系统。
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "hooks" / "project_notes.py"

spec = importlib.util.spec_from_file_location("test_pn_git_signals_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_pn_git_signals_mod", m)
spec.loader.exec_module(m)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8"
    )
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    for i in range(1, 4):
        (root / "f.txt").write_text(f"line{i}\n", encoding="utf-8")
        _git(root, "add", "f.txt")
        _git(root, "commit", "-m", f"c{i}")
    m._GIT_SIG_CACHE.clear()
    yield root
    m._GIT_SIG_CACHE.clear()


def test_reset_soft_changes_head_sig_and_commits(repo):
    """reset --soft 移动分支 ref：head_sig 必须变，commits 立即反映新 HEAD。"""
    sig_before = m._git_head_sig(str(repo))
    r1 = m._collect_all_git(str(repo))
    assert [c.split(" ", 1)[1] for c in r1["commits"]] == ["c3", "c2", "c1"]

    _git(repo, "reset", "--soft", "HEAD~1")

    assert m._git_head_sig(str(repo)) != sig_before
    r2 = m._collect_all_git(str(repo))
    assert [c.split(" ", 1)[1] for c in r2["commits"]] == ["c2", "c1"]


def test_status_timeout_does_not_suppress_new_commits(repo, monkeypatch):
    """status 超时只影响文件侧；同时成功的 log 必须更新 commits。"""
    r0 = m._collect_all_git(str(repo))
    (repo / "f.txt").write_text("c4\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "c4")

    orig = m._run_git

    def fail_status(cwd, *args):
        if "status" in args:
            return "", "timeout", -1
        return orig(cwd, *args)

    monkeypatch.setattr(m, "_run_git", fail_status)
    r1 = m._collect_all_git(str(repo))
    assert r1["commits"][0].endswith(" c4")
    assert r1["files"] == r0["files"]  # 文件侧沿用上次，不被清空


def test_log_failure_keeps_previous_commits(repo, monkeypatch):
    """log 真失败：commits 保持上一轮值，不写成 []。"""
    r0 = m._collect_all_git(str(repo))
    (repo / "f.txt").write_text("c4\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "c4")

    orig = m._run_git

    def fail_log(cwd, *args):
        if "log" in args:
            return "", "timeout", -1
        return orig(cwd, *args)

    monkeypatch.setattr(m, "_run_git", fail_log)
    r1 = m._collect_all_git(str(repo))
    assert r1["commits"] == r0["commits"]


def test_empty_repo_renders_none(tmp_path):
    """空仓库（无任何提交）是合法空态：commits=[] 且渲染「(无)」。"""
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-b", "main")
    m._GIT_SIG_CACHE.clear()
    r = m._collect_all_git(str(root))
    assert r["commits"] == []
    assert m._format_recent_commits(r["commits"]) == "**最近 commits**: (无)"
    m._GIT_SIG_CACHE.clear()


def test_steady_state_spawns_no_git(repo, monkeypatch):
    """签名不变 → 0 个子进程（冷启动首轮 status 会回写 index，故先跑两轮）。"""
    m._collect_all_git(str(repo))
    m._collect_all_git(str(repo))

    orig = m._run_git
    calls = []

    def counting(cwd, *args):
        calls.append(args)
        return orig(cwd, *args)

    monkeypatch.setattr(m, "_run_git", counting)
    m._collect_all_git(str(repo))
    assert calls == []
