# -*- coding: utf-8 -*-
"""BranchDetectTask 信号契约与失败标记单测（不打真实 git 子进程）。"""
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PLUGIN_UI = Path(__file__).resolve().parents[2] / "plugins" / "worktree-manager" / "ui"


@pytest.fixture(scope="module")
def branch_mod():
    """与生产同款机制：ui/ 目录插 sys.path 后按顶层模块导入（branch_chip 无相对导入，安全）。"""
    if str(_PLUGIN_UI) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_UI))
    importlib.invalidate_caches()
    return importlib.import_module("branch_chip")


def test_finished_signal_carries_workdir_and_ok(branch_mod):
    """信号必须为 (request_id, workdir, branch, ok) 四参；目录无效 → ok=False。"""
    sig = branch_mod.BranchDetectSignals()
    captured = []
    sig.finished.connect(lambda *a: captured.append(a))
    task = branch_mod.BranchDetectTask("D:/nonexistent-xyz", 7, sig)
    task.run()
    assert captured == [(7, "D:/nonexistent-xyz", "", False)]


def test_git_timeout_marks_not_ok(branch_mod):
    """git 超时/异常 → ok=False（而非空串 ok=True），缺陷1 前置契约。"""
    sig = branch_mod.BranchDetectSignals()
    captured = []
    sig.finished.connect(lambda *a: captured.append(a))
    with patch("app.utils.git_worktree.GitWorktreeDetector.detect_git", return_value="D:/fake"):
        with patch("branch_chip.subprocess.run", side_effect=TimeoutError()):
            task = branch_mod.BranchDetectTask("D:/fake", 1, sig)
            task.run()
    assert captured[0][3] is False
    assert captured[0][2] == ""
