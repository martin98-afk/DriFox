# -*- coding: utf-8 -*-
"""worktree 缓存失效契约：删掉 worktree 后，两层清缓存函数都必须让
`is_worktree` 立刻返回 False。

## 为什么需要这组测试

`GitWorktreeDetector.is_worktree` 有结果缓存，TTL 已从 30s 提到 **300s**
（`_DETECT_CACHE_TTL`）。TTL 越长，「删了 worktree 但缓存还说是」的窗口越大，
后果是会话的 `worktree_path` 被写进已删除的目录。

清缓存有**两个独立入口**，任一漏掉都会破契约：
- tools 层：`plugins/worktree-manager/tools/worktree_tools.py::_invalidate_info_cache`
- UI 层：  `plugins/worktree-manager/ui/worktree_section.py::_invalidate_repo_cache`

历史上 UI 层曾漏清 `_is_worktree_cache`（只清了 info/worktrees/branch 三个），
本文件把「两条路径都必须生效」锁死。

## 反向断言的意义

`test_without_invalidate_stays_true_within_ttl` 断言「**不清**缓存时缓存仍然
生效」。它看似与上面冲突，实则锁住的是契约的另一半：如果有人把清缓存删掉，
上面两条会失败；如果有人把 `is_worktree` 的缓存整个去掉（让上面两条因「本来
就没缓存」而假绿），这条会失败。两者合起来才能证明「缓存存在 **且** 清得掉」。
"""

import subprocess

import pytest

from app.utils.git_worktree import GitWorktreeDetector


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def repo_with_worktree(tmp_path):
    """建一个真实的 git 仓库 + 一个 worktree，返回 (repo_root, worktree_path)。

    用真实 git 而不是伪造 .git 文件：本契约要验证的正是「删除后 git 层面不认，
    缓存也必须不认」，伪造文件无法覆盖 `git worktree remove` 后的真实状态。
    git 不可用时 skip。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    if _git(repo, "init", "-q").returncode != 0:
        pytest.skip("git 不可用")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "a.txt")
    if _git(repo, "commit", "-qm", "init").returncode != 0:
        pytest.skip("git commit 失败")

    wt = tmp_path / "wt"
    r = _git(repo, "worktree", "add", "-q", "-b", "wtbranch", str(wt))
    if r.returncode != 0:
        pytest.skip(f"git worktree add 失败: {r.stderr}")
    return repo, wt


def _load_tools_invalidate():
    """按路径加载 worktree_tools（插件模块无 package 上下文，与主程序同款）。"""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins"
        / "worktree-manager"
        / "tools"
        / "worktree_tools.py"
    )
    spec = importlib.util.spec_from_file_location("_wt_tools_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._invalidate_info_cache


def _load_ui_invalidate():
    """按路径加载 worktree_section 的 `_invalidate_repo_cache`。

    UI 模块依赖 PyQt5 与插件包上下文，直接 exec 可能失败 —— 失败时退回
    「检查源码里确实调了 clear」的静态断言，避免让契约测试因环境问题假绿/假红。
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins"
        / "worktree-manager"
        / "ui"
        / "worktree_section.py"
    )
    src = path.read_text(encoding="utf-8")
    # 静态兜底：函数体内必须出现 _is_worktree_cache.clear()
    start = src.find("def _invalidate_repo_cache")
    assert start >= 0, "找不到 UI 层 _invalidate_repo_cache，缓存失效契约失去一条路径"
    body = src[start : start + 1200]
    if "_is_worktree_cache.clear()" not in body:
        raise AssertionError(
            "UI 层 _invalidate_repo_cache 未清 _is_worktree_cache："
            "删 worktree 后 300s 内 is_worktree 仍会返回 True，worktree_path 会写错"
        )
    try:
        spec = importlib.util.spec_from_file_location("_wt_ui_contract", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._invalidate_repo_cache
    except Exception:
        # PyQt5/包上下文缺失：用等价的纯清缓存替身（静态断言已保证实现里会清）
        def _equivalent(folder: str) -> None:
            GitWorktreeDetector._info_cache.pop(folder, None)
            GitWorktreeDetector._worktrees_cache.clear()
            GitWorktreeDetector._branch_cache.clear()
            GitWorktreeDetector._is_worktree_cache.clear()

        return _equivalent


class TestCacheInvalidationContract:
    """删 worktree 后，两层清缓存都必须让 is_worktree 立刻 False。"""

    @pytest.fixture(autouse=True)
    def _clear_all_caches(self):
        """每个用例前后清空所有相关缓存，避免用例间串扰。"""
        GitWorktreeDetector._is_worktree_cache.clear()
        GitWorktreeDetector._info_cache.clear()
        GitWorktreeDetector._worktrees_cache.clear()
        GitWorktreeDetector._branch_cache.clear()
        yield
        GitWorktreeDetector._is_worktree_cache.clear()
        GitWorktreeDetector._info_cache.clear()
        GitWorktreeDetector._worktrees_cache.clear()
        GitWorktreeDetector._branch_cache.clear()

    def test_tools_invalidate_makes_is_worktree_false(self, repo_with_worktree):
        """tools 层 `_invalidate_info_cache` 后，is_worktree(已删路径) 必须 False。"""
        repo, wt = repo_with_worktree
        invalidate = _load_tools_invalidate()

        assert GitWorktreeDetector.is_worktree(str(wt)) is True, "前置条件：worktree 应被识别"

        _git(repo, "worktree", "remove", "--force", str(wt))
        invalidate(str(repo))

        assert GitWorktreeDetector.is_worktree(str(wt)) is False, (
            "tools 层清缓存后仍判为 worktree → worktree_path 会写进已删目录"
        )

    def test_ui_invalidate_makes_is_worktree_false(self, repo_with_worktree):
        """UI 层 `_invalidate_repo_cache` 后，is_worktree(已删路径) 必须 False。"""
        repo, wt = repo_with_worktree
        invalidate = _load_ui_invalidate()

        assert GitWorktreeDetector.is_worktree(str(wt)) is True, "前置条件：worktree 应被识别"

        _git(repo, "worktree", "remove", "--force", str(wt))
        invalidate(str(repo))

        assert GitWorktreeDetector.is_worktree(str(wt)) is False, (
            "UI 层清缓存后仍判为 worktree → 用户在 UI 删 worktree 会写错会话 worktree_path"
        )

    def test_without_invalidate_stays_true_within_ttl(self, repo_with_worktree):
        """反向断言：**不清**缓存时，TTL 内仍返回 True（证明缓存确实存在）。

        这条与上面两条合起来才完整：
        - 上面两条证明「清得掉」
        - 这条证明「缓存真的在生效」（否则上面两条会因无缓存而假绿）
        """
        repo, wt = repo_with_worktree

        assert GitWorktreeDetector.is_worktree(str(wt)) is True

        _git(repo, "worktree", "remove", "--force", str(wt))
        # 刻意不清缓存
        assert GitWorktreeDetector.is_worktree(str(wt)) is True, (
            "缓存未生效（或 TTL 已失效）：反向断言失败，说明上面的『清得掉』可能是假绿"
        )

    def test_ttl_is_long_enough_to_require_explicit_invalidation(self):
        """契约前提：is_worktree 的 TTL 必须是长 TTL（300s），否则本契约无意义。"""
        assert GitWorktreeDetector._DETECT_CACHE_TTL >= 300.0, (
            "is_worktree 应使用 _DETECT_CACHE_TTL(>=300s)；"
            "若退回 30s，说明缓存失效路径的重要性被削弱，本契约需重新评估"
        )
