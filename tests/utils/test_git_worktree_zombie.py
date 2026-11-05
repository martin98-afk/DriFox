# -*- coding: utf-8 -*-
"""Zombie worktree 回归：.git 指向的 gitdir 消失后不再被识别为 worktree

背景：主仓库 .git 被删除/重建后，历史 worktree 目录仍带着 .git 文件
（指向已不存在的 <主仓库>/.git/worktrees/<名>），但 git 层面已不认
（git worktree list 不再列出）。旧判据只查「.git 是文件」→ 会话绑定与
侧栏树把 zombie 当真 worktree，而工作树插件页（git worktree list 数据源）
不显示它，两处 UI 矛盾（症状：会话绑定到不存在的工作树 / 不在插件页显示）。

修复口径：.git 是文件时必须校验其 gitdir 指向真实存在（纯文件系统，零子进程）。
"""

import pytest

from app.utils.git_worktree import GitWorktreeDetector


def _make_worktree(tmp_path, name, main_root):
    """伪造一个 gitdir 真实存在的 worktree"""
    wt = tmp_path / name
    wt.mkdir()
    gitdir = main_root / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(f"gitdir: {gitdir.as_posix()}", encoding="utf-8")
    return wt


def _make_zombie(tmp_path, name, main_root):
    """伪造一个 zombie worktree：.git 文件在，但 gitdir 已被删（主仓库 .git 重建）"""
    wt = tmp_path / name
    wt.mkdir()
    dead_gitdir = main_root / ".git" / "worktrees" / name
    (wt / ".git").write_text(f"gitdir: {dead_gitdir.as_posix()}", encoding="utf-8")
    return wt


@pytest.fixture
def main_root(tmp_path):
    root = tmp_path / "main"
    (root / ".git" / "worktrees").mkdir(parents=True)
    return root


class TestIsValidWorktreeLink:
    def test_valid_link(self, tmp_path, main_root):
        wt = _make_worktree(tmp_path, "feat", main_root)
        assert GitWorktreeDetector.is_valid_worktree_link(str(wt)) is True

    def test_zombie_link(self, tmp_path, main_root):
        wt = _make_zombie(tmp_path, "gone", main_root)
        assert GitWorktreeDetector.is_valid_worktree_link(str(wt)) is False

    def test_relative_gitdir_exists(self, tmp_path, main_root):
        wt = tmp_path / "wt-rel"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: ../main/.git/worktrees/rel", encoding="utf-8")
        (main_root / ".git" / "worktrees" / "rel").mkdir(parents=True)
        assert GitWorktreeDetector.is_valid_worktree_link(str(wt)) is True

    def test_relative_gitdir_missing(self, tmp_path, main_root):
        wt = tmp_path / "wt-rel-dead"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: ../main/.git/worktrees/gone", encoding="utf-8")
        assert GitWorktreeDetector.is_valid_worktree_link(str(wt)) is False

    def test_git_is_dir_not_file(self, tmp_path):
        wt = tmp_path / "wt-dir"
        (wt / ".git").mkdir(parents=True)
        assert GitWorktreeDetector.is_valid_worktree_link(str(wt)) is False

    def test_no_git(self, tmp_path):
        wt = tmp_path / "plain"
        wt.mkdir()
        assert GitWorktreeDetector.is_valid_worktree_link(str(wt)) is False

    def test_unreadable_git_file_falls_back_true(self, tmp_path):
        """读取失败保守维持旧判据：.git 文件存在本身是强信号"""
        wt = tmp_path / "wt-bad"
        wt.mkdir()
        (wt / ".git").write_bytes(b"\xff\xfe\x00\x01")
        assert GitWorktreeDetector.is_valid_worktree_link(str(wt)) is True


class TestIsWorktreeZombie:
    def test_zombie_not_worktree(self, tmp_path, main_root):
        wt = _make_zombie(tmp_path, "zomb", main_root)
        assert GitWorktreeDetector.is_worktree(str(wt)) is False

    def test_real_worktree(self, tmp_path, main_root):
        wt = _make_worktree(tmp_path, "real", main_root)
        assert GitWorktreeDetector.is_worktree(str(wt)) is True


class TestResolveWorktreeZombie:
    """侧栏树 _resolve_worktree：zombie 回落主仓库，真 worktree 原样保留"""

    @staticmethod
    def _resolve():
        from app.widgets.tab_panel import TabPanel

        return TabPanel._resolve_worktree

    def test_zombie_falls_back_to_main(self, tmp_path, main_root):
        wt = _make_zombie(tmp_path, "zomb", main_root)
        assert self._resolve()(str(wt), []) == ""

    def test_valid_worktree_kept(self, tmp_path, main_root):
        wt = _make_worktree(tmp_path, "feat", main_root)
        assert self._resolve()(str(wt), []) == str(wt)

    def test_deleted_dir_falls_back_to_main(self, tmp_path):
        assert self._resolve()(str(tmp_path / "never-existed"), []) == ""

    def test_registered_match_wins(self, tmp_path, main_root):
        """命中已登记 worktree（含子目录归组）原样返回，优先级不变"""
        wt = _make_worktree(tmp_path, "feat", main_root)
        registered_norm = _registered_norm([str(wt)])
        assert self._resolve()(str(wt), registered_norm) == str(wt)
        sub = wt / "sub"
        sub.mkdir()
        assert self._resolve()(str(sub), registered_norm) == str(wt)


def _registered_norm(paths):
    from app.widgets.tab_panel import TabPanel

    return TabPanel._norm_registered(paths)

class TestMainRepoFromWorktreePath:
    """已删工作树的路径 → 主仓库根（纯字符串，无需目录存在）"""

    @staticmethod
    def _f():
        from app.utils.git_worktree import GitWorktreeDetector

        return GitWorktreeDetector.main_repo_from_worktree_path

    def test_posix_style(self):
        assert self._f()("/work/app/.worktrees/feat-x") == "/work/app"

    def test_windows_style(self):
        assert self._f()("D:/work/app/.worktrees/baseline-pre-cleanup") == "D:/work/app"

    def test_backslash_input(self):
        assert self._f()("D:\\work\\app\\.worktrees\\feat") == "D:/work/app"

    def test_trailing_slash(self):
        assert self._f()("/work/app/.worktrees/feat/") == "/work/app"

    def test_main_repo_itself_returns_none(self):
        assert self._f()("/work/app") is None

    def test_nested_deeper_returns_none(self):
        """<主>/.worktrees/<名>/sub 不是工作树根，不猜"""
        assert self._f()("/work/app/.worktrees/feat/sub") is None

    def test_empty_and_relative(self):
        assert self._f()("") is None
        assert self._f()(".worktrees/feat") is None

class TestResolveWorkdirOrMain:
    """失效工作目录回落：活着原样返回，死了回主树，认不出返回 None"""

    @staticmethod
    def _f():
        from app.utils.git_worktree import GitWorktreeDetector

        return GitWorktreeDetector.resolve_workdir_or_main

    def test_alive_dir_kept_as_is(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert self._f()(str(plain)) == str(plain)

    def test_dead_worktree_falls_back_to_main(self, tmp_path):
        import os

        main = tmp_path / "app"
        main.mkdir()
        dead = main / ".worktrees" / "gone"
        # 返回值按项目口径归一化为正斜杠（与 project_workdir 表存储一致）
        assert os.path.normcase(self._f()(str(dead))) == os.path.normcase(str(main).replace("\\", "/"))

    def test_dead_worktree_missing_main_returns_none(self, tmp_path):
        dead = tmp_path / "nope" / ".worktrees" / "gone"
        assert self._f()(str(dead)) is None

    def test_dead_plain_dir_returns_none(self, tmp_path):
        assert self._f()(str(tmp_path / "never-existed")) is None

    def test_empty_returns_none(self):
        assert self._f()("") is None

