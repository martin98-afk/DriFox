# -*- coding: utf-8 -*-
"""
glob_files 工具回归测试

覆盖 2026-08 修复的标准 glob 语义：
- '*' 只匹配单个目录层级，不跨路径分隔符
- '**' 匹配零个或多个目录层级
- 精确文件名模式不得误匹配子目录同名文件
- Windows 反斜杠路径/模式兼容
- 与标准 glob.glob(recursive=True) 结果一致

运行方式: python -m pytest tests/file_tools_glob_test.py -v
"""

import glob as std_glob
from pathlib import Path

import pytest

from app.tools.file_tools import FileTools, _glob_match


class _Owner:
    def __init__(self, workdir: Path):
        self.workdir = workdir


@pytest.fixture()
def tree_env(tmp_path: Path):
    """构造目录树：a.txt、b.py、sub/c.txt、sub/deep/d.py、sub/e.md"""
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.py").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("x")
    (sub / "deep").mkdir()
    (sub / "deep" / "d.py").write_text("x")
    (sub / "e.md").write_text("x")
    ft = FileTools(_Owner(tmp_path))
    return ft, tmp_path


def _norm_result(r) -> set:
    """ToolResult → 归一化匹配集合（无匹配 → 空集）"""
    if not r.success:
        return {"ERROR: " + r.error}
    if r.content.startswith("No files matched"):
        return set()
    return {ln.replace("\\", "/") for ln in r.content.splitlines()}


# ── 单元：_glob_match 段语义 ──


@pytest.mark.parametrize(
    "rel,pattern,expected",
    [
        # 精确文件名：不得匹配子目录同名文件（回归：旧 search 实现误匹配）
        ("e.md", "e.md", True),
        ("sub/e.md", "e.md", False),
        # '*' 不跨目录（回归：旧实现跨目录匹配 sub/c.txt）
        ("a.txt", "*.txt", True),
        ("sub/c.txt", "*.txt", False),
        # '**' 跨任意层级（含零级）
        ("a.txt", "**/*.txt", True),
        ("sub/c.txt", "**/*.txt", True),
        ("sub/deep/d.py", "**/*.py", True),
        ("e.md", "**", True),
        ("sub/deep/d.py", "sub/**/*.py", True),
        # '?' 单字符、不跨分隔符
        ("sub/c.txt", "sub/?.txt", True),
        ("sub/cc.txt", "sub/?.txt", False),
        ("sub/c.txt", "sub/??.txt", False),
        # Windows 反斜杠模式（用户习惯）
        ("sub/c.txt", "sub\\*.txt", True),
        ("sub/c.txt", "**\\*.txt", True),
        # 多级路径
        ("a/b/c.py", "**/c.py", True),
        ("a/b/c.py", "a/*/c.py", True),
        ("a/b/c.py", "a/*/d.py", False),
    ],
)
def test_glob_match_segments(rel: str, pattern: str, expected: bool):
    assert _glob_match(rel, pattern) is expected


# ── 集成：glob_files 与标准 glob.glob(recursive=True) 一致 ──


@pytest.mark.parametrize(
    "pattern,path",
    [
        ("*.txt", "."),
        ("**/*.txt", "."),
        ("*.py", "."),
        ("**/*.py", "."),
        ("sub/*.txt", "."),
        ("**/c.txt", "."),
        ("**", "."),
        ("e.md", "."),
        ("**/*.md", "."),
        ("**/*.py", "sub"),
        ("*.txt", "sub"),
        ("**", "sub"),
    ],
)
def test_glob_files_matches_std_glob(tree_env, pattern: str, path: str):
    ft, root = tree_env
    # 标准 glob 参考结果（相对搜索起点）
    base = (root / path).resolve()
    std = sorted(
        str(Path(p).resolve().relative_to(root)).replace("\\", "/")
        for p in std_glob.glob(str(base / pattern), recursive=True)
        if Path(p).is_file()
    )
    r = ft.glob_files(pattern=pattern, path=path)
    got = sorted(_norm_result(r))
    assert got == std, f"pattern={pattern!r} path={path!r}: got={got} std={std}"


def test_glob_files_exact_name_no_subdir_leak(tree_env):
    """精确文件名模式不得匹配子目录同名文件"""
    ft, _ = tree_env
    r = ft.glob_files(pattern="e.md", path=".")
    assert _norm_result(r) == set()
