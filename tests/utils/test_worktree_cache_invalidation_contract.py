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

import os
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
    """按路径加载 worktree_section 的 `_invalidate_repo_cache`（**真实实现**）。

    ⚠️ 这里**不允许**在手写替身上自证。历史版本曾在 import 失败时返回一个
    手写的等价函数（自己清 4 个缓存），测试随后验证的是那个替身，真实实现
    根本没被执行 —— 典型的假绿：真实实现把 `.clear()` 删掉，测试照样绿。

    原则：**宁可不跑，不要替身自证**。import 失败一律 skip，让「未验证」
    如实暴露为 skipped 而不是伪装成 passed。
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
    try:
        spec = importlib.util.spec_from_file_location("_wt_ui_contract", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "_invalidate_repo_cache", None)
    except Exception as exc:  # noqa: BLE001 — 环境问题（缺 PyQt5/包上下文）
        pytest.skip(f"UI 模块不可导入（{exc!r}），无法验证真实实现 → 跳过而非替身自证")
    if fn is None:
        pytest.skip("worktree_section 未暴露 _invalidate_repo_cache")
    return fn


# ── AST 级静态断言：真实实现必须真的清这些缓存 ──────────────────────
# 名单依据：直接读 worktree_section.py::_invalidate_repo_cache 函数体确认。
# 前 4 项是本次性能改动引入 `_is_worktree_cache` 时必须一并清的集合，
# `_detect_cache` 用循环 + abspath 匹配逐键 pop（负缓存也在此失效）。
_UI_EXPECTED_CLEARS = (
    ("_info_cache", "clear"),  # GitWorktreeDetector._info_cache.clear()
    ("_worktrees_cache", "clear"),  # .clear()
    ("_branch_cache", "clear"),  # .clear()
    ("_is_worktree_cache", "clear"),  # .clear() ← 本契约的核心
    ("_detect_cache", "pop"),  # 循环内 .pop(key, None)
)


def _ui_section_func_node():
    """解析 worktree_section.py，返回 `_invalidate_repo_cache` 的 AST 节点。"""
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins"
        / "worktree-manager"
        / "ui"
        / "worktree_section.py"
    )
    # 该文件带 UTF-8 BOM，必须用 utf-8-sig 读，否则 ast.parse 报
    # invalid non-printable character U+FEFF
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_invalidate_repo_cache":
            return node
    pytest.fail("worktree_section.py 里找不到 _invalidate_repo_cache")


def _collect_reachable_stmts(func_node):
    """收集函数体内**可达的叶子语句**（排除 `if False` / `if 0` 常量死分支）。

    关键设计：**只返回叶子语句**（Expr/Assign/Return/...），
    **不返回** If/While/For/With/Try 这些复合节点本身。

    原因：调用方会用 `ast.walk(stmt)` 展开每条返回的语句来找 `.clear()` 调用。
    若把整个 `If` 节点返回，`ast.walk` 会连它的死分支 body 一起展开 →
    `if False: cache.clear()` 照样被搜到，断言失效（本函数首版正是这么错的，
    被 `test_reachable_helper_rejects_if_false` 元测试抓出）。

    复合语句只用于向下递归，进入哪个分支由 `_is_const_false` 决定。
    """
    import ast

    _COMPOUND = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.Match)
    out = []

    def _is_const_false(test_node) -> bool:
        # `False` / `0` / `None` / `""` 字面量
        if isinstance(test_node, ast.Constant):
            return not test_node.value
        # `not True` 形式
        if isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not):
            inner = test_node.operand
            if isinstance(inner, ast.Constant):
                return bool(inner.value)
        return False

    def _walk(stmts):
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                if _is_const_false(stmt.test):
                    _walk(stmt.orelse)  # 死分支：只走 else
                else:
                    _walk(stmt.body)
                    _walk(stmt.orelse)
            elif isinstance(stmt, ast.While):
                if _is_const_false(stmt.test):
                    _walk(stmt.orelse)  # while False 的 body 不可达
                else:
                    _walk(stmt.body)
                    _walk(stmt.orelse)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                # For 的 body 是可达的（迭代次数未知，保守按可达处理）
                _walk(stmt.body)
                _walk(stmt.orelse)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                _walk(stmt.body)
            elif isinstance(stmt, ast.Try):
                _walk(stmt.body)
                for handler in getattr(stmt, "handlers", []):
                    _walk(handler.body)
                _walk(getattr(stmt, "orelse", []))
                _walk(getattr(stmt, "finalbody", []))
            elif isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    _walk(case.body)
            else:
                # 叶子语句：直接收（不含死分支子树）
                out.append(stmt)

    _walk(func_node.body)
    return out


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

    def test_invalidate_clears_subtree_keyed_info_cache(self, repo_with_worktree):
        """会话位于子工作树时写入的 `(子工作树路径, counts)` 条目也必须失效。

        回归：`_info_cache` 键是元组，且以子工作树为 key 探测时 `info.root`
        指向子工作树自身（不是主仓库根）。两层清缓存若按 `info.root == repo_root`
        精确匹配会漏掉这类条目 → 主仓库新建工作树后，`get_repo_info(子工作树)`
        在 30s TTL 内返回旧列表，表现为「新建的工作树刷新不出来」。
        """
        repo, wt = repo_with_worktree

        # 模拟「会话工作目录在子工作树里」：以子工作树为 key 探测并写缓存
        info_in_wt = GitWorktreeDetector.get_repo_info(str(wt))
        assert info_in_wt is not None
        assert os.path.normcase(info_in_wt.root) == os.path.normcase(str(wt)), (
            "前置条件变了：以子工作树探测时 info.root 应指向子工作树自身"
        )
        before = len(info_in_wt.worktrees)

        # 主仓库里再新建一个工作树
        wt2 = repo.parent / "wt2"
        r = _git(repo, "worktree", "add", "-q", "-b", "wtbranch2", str(wt2))
        if r.returncode != 0:
            pytest.skip(f"第二个 worktree 创建失败: {r.stderr}")

        # 两层清缓存都必须让「子工作树视角」的旧条目失效
        for loader in (_load_tools_invalidate, _load_ui_invalidate):
            invalidate = loader()
            GitWorktreeDetector._info_cache.clear()
            GitWorktreeDetector.get_repo_info(str(wt))  # 重新写入子工作树键条目
            invalidate(str(repo))
            assert not GitWorktreeDetector._info_cache, (
                f"{loader.__name__} 清缓存后仍残留 _info_cache 条目："
                f"{list(GitWorktreeDetector._info_cache)}"
            )
            after = len(GitWorktreeDetector.get_repo_info(str(wt)).worktrees)
            assert after == before + 1, (
                f"{loader.__name__} 后重新探测仍拿到 {after} 个工作树（应 {before + 1}）"
                "→ 新建的工作树会刷不出来"
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


class TestUiInvalidateStaticContract:
    """AST 级静态断言：`_invalidate_repo_cache` **可达代码**里必须真的清这些缓存。

    这组与上面的动态测试互补：
    - 动态测试（`test_ui_invalidate_makes_is_worktree_false`）跑真实函数，
      但它依赖 import 成功；且只验证「`_is_worktree_cache` 这一条」的最终效果，
      其余 4 个缓存清了没清它看不出来。
    - 静态测试逐个确认 5 个缓存都有对应清理调用，且**不在 `if False` 死分支里**。

    历史假绿的教训：旧实现用「源码字符串窗口内出现 `.clear()`」判定，
    只要把 `.clear()` 移出 1200 字符窗口、或包进 `if False:`，断言仍然绿。
    改 AST 后这两种作弊都会被抓到。
    """

    def test_all_expected_caches_are_cleared_in_reachable_code(self):
        """5 个缓存的清理调用都必须出现在可达代码里。"""
        import ast

        node = _ui_section_func_node()
        stmts = _collect_reachable_stmts(node)

        # 收集可达语句里出现的 "X.clear()" / "X.pop(...)" 调用：{(cache_attr, op)}
        found: set[tuple[str, str]] = set()
        for stmt in stmts:
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if not isinstance(func, ast.Attribute):
                    continue
                op = func.attr  # clear / pop
                if op not in ("clear", "pop"):
                    continue
                target = func.value
                # 形如 GitWorktreeDetector._xxx_cache.clear()
                if isinstance(target, ast.Attribute) and target.attr.startswith("_"):
                    found.add((target.attr, op))
                # 形如 for key in list(GitWorktreeDetector._detect_cache) 内 pop：
                # 遍历的那个循环本身也在可达语句里，pop 的 target 是 _detect_cache

        missing = []
        for cache_attr, expected_op in _UI_EXPECTED_CLEARS:
            if (cache_attr, expected_op) not in found:
                # 放宽：pop/clear 任一都算清（实现可能换形态）
                alt = "clear" if expected_op == "pop" else "pop"
                if (cache_attr, alt) not in found:
                    missing.append(f"{cache_attr}.{expected_op}()")

        assert not missing, (
            f"UI 层 _invalidate_repo_cache 的可达代码里缺少这些清理调用：{missing}。"
            f"实际找到：{sorted(found)}"
        )

    def test_is_worktree_cache_clear_is_reachable_specifically(self):
        """核心断言：`_is_worktree_cache.clear()` 必须在可达代码里（单独锁一条）。"""
        import ast

        node = _ui_section_func_node()
        stmts = _collect_reachable_stmts(node)

        hit = False
        for stmt in stmts:
            for sub in ast.walk(stmt):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "clear"
                    and isinstance(sub.func.value, ast.Attribute)
                    and sub.func.value.attr == "_is_worktree_cache"
                ):
                    hit = True
                    break
            if hit:
                break

        assert hit, (
            "_is_worktree_cache.clear() 不在 _invalidate_repo_cache 的可达代码里："
            "删 worktree 后 300s 内 is_worktree 仍返回 True → worktree_path 会写进已删目录"
        )

    def test_detect_cache_pop_is_reachable(self):
        """`_detect_cache` 的失效（循环内 pop）也必须在可达代码里。"""
        node = _ui_section_func_node()
        stmts = _collect_reachable_stmts(node)
        import ast

        hit = False
        for stmt in stmts:
            for sub in ast.walk(stmt):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "pop"
                    and isinstance(sub.func.value, ast.Attribute)
                    and sub.func.value.attr == "_detect_cache"
                ):
                    hit = True
                    break
            if hit:
                break

        assert hit, "_detect_cache.pop(...) 不在可达代码里：负缓存不会失效"

    def test_reachable_helper_rejects_if_false(self):
        """元测试：`_collect_reachable_stmts` 自身必须能识破 `if False:` 死分支。

        这条防止「断言工具本身失效导致全组假绿」——不验证工具就没法信任它。
        """
        import ast

        code = """
def demo():
    GitWorktreeDetector._a_cache.clear()
    if False:
        GitWorktreeDetector._is_worktree_cache.clear()
"""
        tree = ast.parse(code)
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        stmts = _collect_reachable_stmts(func)

        names = set()
        for stmt in stmts:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Attribute):
                    names.add(sub.attr)

        assert "_a_cache" in names, "可达分支的 clear 应被收集"
        assert "_is_worktree_cache" not in names, (
            "_collect_reachable_stmts 未能排除 `if False:` 死分支 → 本组断言全部不可信"
        )

