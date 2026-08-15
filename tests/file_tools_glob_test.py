# -*- coding: utf-8 -*-
"""
glob 工具回归测试（工具插件化后：直接测插件 _glob_impl，避免启动后台线程）

覆盖标准 glob 语义（glob.glob recursive=True 保证）：
- '*' 只匹配单个目录层级，不跨路径分隔符
- '**' 匹配零个或多个目录层级
- 精确文件名模式不得误匹配子目录同名文件
- Windows 反斜杠路径/模式兼容
- 与标准 glob.glob(recursive=True) 结果一致

运行方式: python -m pytest tests/file_tools_glob_test.py -v
"""
import glob as std_glob
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.tools.plugin_tool_loader import load_plugin_tools
from app.tools.registry import ToolRegistry


@pytest.fixture(scope="session")
def glob_impl():
    """加载系统插件，返回 glob 工具 impl（直接调用，不创建 ToolExecutor → 不启动后台线程）"""
    ToolRegistry.reset_instance()
    load_plugin_tools()
    reg = ToolRegistry.get_instance().get("glob")
    assert reg is not None, "glob 工具未注册"
    return reg.impl


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
    return tmp_path


def _exec(glob_impl, root: Path, pattern: str, path: str = "."):
    """执行 glob impl（返回 ToolResult）"""
    return glob_impl(tool_ctx={"workdir": str(root)}, pattern=pattern, path=str(Path(path)))


def _norm_result(r, root: Path) -> set:
    """ToolResult → 归一化匹配集合（相对 root 路径，无匹配 → 空集）"""
    if not r.success:
        return {"ERROR: " + r.error}
    content = str(r.content)
    if "未找到匹配" in content:
        return set()
    lines = content.splitlines()[1:]  # 跳过 "找到 N 个文件:"
    result = set()
    for ln in lines:
        try:
            rel = str(Path(ln).resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = ln.replace("\\", "/")
        result.add(rel)
    return result


# ── 集成：glob 工具与标准 glob.glob(recursive=True) 一致 ──


@pytest.mark.parametrize(
    "pattern,path",
    [
        ("*.txt", "."),
        ("**/*.txt", "."),
        ("*.py", "."),
        ("**/*.py", "."),
        ("sub/*.txt", "."),
        ("**/c.txt", "."),
        ("e.md", "."),
        ("**/*.md", "."),
        ("**/*.py", "sub"),
        ("*.txt", "sub"),
    ],
)
def test_glob_matches_std_glob(glob_impl, tree_env, pattern: str, path: str):
    root = tree_env
    base = (root / path).resolve()
    std = sorted(
        str(Path(p).resolve().relative_to(root)).replace("\\", "/")
        for p in std_glob.glob(str(base / pattern), recursive=True)
        if Path(p).is_file()
    )
    r = _exec(glob_impl, root, pattern, path)
    got = sorted(_norm_result(r, root))
    assert got == std, f"pattern={pattern!r} path={path!r}: got={got} std={std}"


def test_glob_star_not_cross_dir(glob_impl, tree_env):
    """'*' 不跨目录：*.txt 只匹配根目录 txt，不匹配 sub/c.txt"""
    root = tree_env
    r = _exec(glob_impl, root, "*.txt", ".")
    got = _norm_result(r, root)
    assert "a.txt" in got
    assert not any("sub/c.txt" in g for g in got), f"* 跨目录误匹配: {got}"


def test_glob_exact_name_no_subdir_leak(glob_impl, tree_env):
    """精确文件名模式不得匹配子目录同名文件"""
    root = tree_env
    r = _exec(glob_impl, root, "e.md", ".")
    got = _norm_result(r, root)
    assert "sub/e.md" not in got


def test_glob_windows_backslash(glob_impl, tree_env):
    """Windows 反斜杠模式兼容：sub\\*.txt"""
    root = tree_env
    r = _exec(glob_impl, root, "sub\\*.txt", ".")
    got = _norm_result(r, root)
    assert "sub/c.txt" in got, f"反斜杠模式未匹配: {got}"


def test_glob_double_star_cross_level(glob_impl, tree_env):
    """'**' 跨层级：**/*.txt 匹配 sub/c.txt"""
    root = tree_env
    r = _exec(glob_impl, root, "**/*.txt", ".")
    got = _norm_result(r, root)
    assert "sub/c.txt" in got, f"** 未跨层级匹配: {got}"
