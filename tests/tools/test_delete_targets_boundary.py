# -*- coding: utf-8 -*-
"""删除目标解析边界回归（2026-10-24 事故）

事故：AI 执行 ``cd D:/work/proj && find plugins/x -exec rm -rf {} +``，
旧 ``delete_targets`` 把命令里**全部**非 flag token 当删除目标，
``D:/work/proj`` 因存在而命中，``stash_delete_targets_to_trash`` 直接
``shutil.move`` 把整个仓库搬进垃圾箱 —— 磁盘上目录消失，后续所有命令报
WinError 267。三次恢复三次被移走。

本文件锁两层防线：
1. ``delete_targets`` 只取删除命令自身的参数（不跨 shell 连接符）
2. ``stash_delete_targets_to_trash`` 绝不 move 工作目录本身或其祖先
"""

import pytest

from app.tools.sandbox import (
    _delete_arg_tokens,
    _find_delete_command_spans,
    _is_workdir_or_ancestor,
    delete_targets,
    stash_delete_targets_to_trash,
)


# ── 第 1 层：候选提取不跨命令边界 ──


def test_delete_targets_ignores_prefix_cd_target(tmp_path):
    """`cd <工作目录> && rm -rf <子目录>`：只认子目录，不认工作目录"""
    workdir = tmp_path / "proj"
    sub = workdir / "plugins" / "jimeng"
    sub.mkdir(parents=True)
    (sub / "a.py").write_text("x", encoding="utf-8")

    cmd = f'cd "{workdir.as_posix()}" && rm -rf plugins/jimeng'
    result = delete_targets(cmd, workdir=workdir)

    assert result["existing"] == [sub], "只应命中真正的删除目标"
    assert workdir not in result["existing"], "工作目录本身绝不能被当目标"


def test_delete_targets_ignores_pipeline_head(tmp_path):
    """`find X ... | xargs rm`：管道前的路径不是删除目标"""
    workdir = tmp_path / "proj"
    victim = workdir / "tmp" / "cache.bin"
    victim.parent.mkdir(parents=True)
    victim.write_text("x", encoding="utf-8")

    cmd = f"find {workdir.as_posix()} -name cache.bin | xargs rm -f"
    result = delete_targets(cmd, workdir=workdir)

    assert workdir not in result["existing"]
    assert result["existing"] == [], "管道形态下 find 的路径不属于 rm 的参数"


def test_delete_targets_semicolon_boundary(tmp_path):
    """`;` 之后属于下一条命令，不参与删除目标"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    victim = workdir / "a.txt"
    other = workdir / "other"
    victim.write_text("x", encoding="utf-8")
    other.mkdir()

    cmd = f"rm {victim.as_posix()}; cd {other.as_posix()}"
    result = delete_targets(cmd, workdir=workdir)

    assert result["existing"] == [victim]
    assert other not in result["existing"]


def test_delete_targets_multiple_delete_commands(tmp_path):
    """一条命令里两处 rm：两处参数都算目标"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    a = workdir / "a.txt"
    b = workdir / "b.txt"
    a.write_text("x", encoding="utf-8")
    b.write_text("y", encoding="utf-8")

    cmd = f"rm {a.as_posix()} && rm {b.as_posix()}"
    result = delete_targets(cmd, workdir=workdir)

    assert set(result["existing"]) == {a, b}


def test_delete_arg_tokens_excludes_verb(tmp_path):
    """参数 token 不含删除动词本身"""
    toks = _delete_arg_tokens("rm -rf build")
    assert "build" in toks
    assert "rm" not in toks


def test_find_delete_command_spans_located():
    """能定位删除动词位置"""
    spans = _find_delete_command_spans("cd x && rm -rf build")
    assert len(spans) == 1
    assert spans[0][0] == 3  # cd / x / && / rm


def test_wrapped_shell_command_args_extracted():
    """壳命令包裹形态：参数与动词同一 token，必须就地拆出来

    `powershell -c "Remove-Item -Recurse D:/x/build"` 整条子命令是一个 token，
    后续 token 列表为空。修前会把参数当空、静默漏保护。
    """
    toks = _delete_arg_tokens('powershell -c "Remove-Item -Recurse -Force D:/x/build"')
    assert "D:/x/build" in toks
    assert not any("Remove-Item" in t for t in toks), "动词本身不能当参数"


def test_wrapped_shell_command_not_top_level_double_count():
    """包裹形态只产生一条 span，参数走内部拆解分支"""
    spans = _find_delete_command_spans('powershell -c "Remove-Item x.txt"')
    assert len(spans) == 1
    assert spans[0][1] == [-1], "参数在 token 内部用 -1 标记"


def test_git_rm_targets():
    """git rm 的路径参数被识别，--cached 等 flag 不当路径"""
    toks = _delete_arg_tokens("git rm -r --cached build")
    assert "build" in toks
    assert "--cached" in toks  # 保留在候选里，但 _looks_like_path 会过滤


def test_rd_and_del_forms():
    """Windows rd / del 形态"""
    assert "build" in _delete_arg_tokens("rd /s /q build")
    assert "a.txt" in _delete_arg_tokens("del /f a.txt")


def test_dotnet_delete_falls_back_to_all_tokens():
    """无删除动词 token（仅 .NET Delete 命中）时退化为全局候选"""
    cmd = '[System.IO.Directory]::Delete("D:/x/y")'
    toks = _delete_arg_tokens(cmd)
    assert any("D:/x/y" in t for t in toks), "变量/引号形态目标必须仍能提取"


# ── 第 2 层：stash 硬保护 ──


def test_stash_refuses_workdir_itself(tmp_path):
    """即使命令直接写 `rm -rf <workdir>`，也不 move 工作目录"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    (workdir / "keep.txt").write_text("x", encoding="utf-8")

    moved = stash_delete_targets_to_trash(
        f"rm -rf {workdir.as_posix()}", tmp_path / "trash", workdir=workdir
    )

    assert moved == [], "工作目录不允许被移入垃圾桶"
    assert workdir.is_dir(), "工作目录必须原样保留"
    assert (workdir / "keep.txt").exists()


def test_stash_refuses_workdir_ancestor(tmp_path):
    """工作目录的祖先目录同样拒绝"""
    parent = tmp_path / "work"
    workdir = parent / "proj"
    workdir.mkdir(parents=True)

    moved = stash_delete_targets_to_trash(
        f"rm -rf {parent.as_posix()}", tmp_path / "trash", workdir=workdir
    )

    assert moved == []
    assert parent.is_dir()
    assert workdir.is_dir()


def test_stash_still_moves_subdir(tmp_path):
    """向下删除子目录照常工作（保护只拦向上）"""
    workdir = tmp_path / "proj"
    sub = workdir / "build"
    sub.mkdir(parents=True)
    (sub / "out.o").write_text("x", encoding="utf-8")

    moved = stash_delete_targets_to_trash(
        "rm -rf build", tmp_path / "trash", workdir=workdir
    )

    assert len(moved) == 1
    assert not sub.exists()


def test_stash_still_moves_sibling_file(tmp_path):
    """workdir 内的普通文件照常移入垃圾桶"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    f = workdir / "log.txt"
    f.write_text("x", encoding="utf-8")

    moved = stash_delete_targets_to_trash(
        f"rm {f.as_posix()}", tmp_path / "trash", workdir=workdir
    )

    assert len(moved) == 1
    assert not f.exists()


def test_stash_explicit_workdir_outside_target_still_moves(tmp_path):
    """目标与 workdir 无关（workdir 之外）时正常 move"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    outside = tmp_path / "elsewhere" / "old.bin"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")

    moved = stash_delete_targets_to_trash(
        f"rm {outside.as_posix()}", tmp_path / "trash", workdir=workdir
    )

    assert len(moved) == 1
    assert not outside.exists()


def test_stash_refuses_drive_root(tmp_path):
    """盘符根绝不 move：跨盘 move = copytree + rmtree（2026-10-25 把盘根搬残）"""
    import sys
    from pathlib import Path

    root = Path("C:/") if sys.platform == "win32" else Path("/")
    moved = stash_delete_targets_to_trash(f"rm -rf {root.as_posix()}", tmp_path / "trash")

    assert moved == [], "盘符根不允许被移入垃圾桶"
    assert root.exists()


def test_stash_refuses_system_root_dir(tmp_path):
    """盘根一级系统目录（C:/Windows 等）同样拒绝（与 workdir 无关，按目标本身判定）"""
    import sys
    from pathlib import Path

    if sys.platform != "win32":
        pytest.skip("系统目录清单为 Windows 语义")
    win = Path("C:/Windows")
    if not win.exists():
        pytest.skip("测试机无 C:\\Windows")

    moved = stash_delete_targets_to_trash("rm -rf C:/Windows", tmp_path / "trash")

    assert moved == []
    assert win.is_dir()


# ── 守卫函数本身 ──


@pytest.mark.parametrize(
    "target_rel, expected",
    [
        ("", True),             # 就是 workdir
        ("..", True),           # 父目录
        ("../..", True),        # 祖父目录
        ("sub", False),         # 子目录
        ("sub/deep", False),    # 深层子目录
    ],
)
def test_is_workdir_or_ancestor(tmp_path, target_rel, expected):
    workdir = tmp_path / "work" / "proj"
    workdir.mkdir(parents=True)
    target = (workdir / target_rel) if target_rel else workdir
    assert _is_workdir_or_ancestor(target, workdir) is expected


def test_is_workdir_or_ancestor_no_workdir(monkeypatch):
    """取不到 workdir 时不拦（fail-open，避免误阻断正常删除）"""
    from app.tools import sandbox as sb

    monkeypatch.setattr(sb, "_current_workdir", lambda: None)
    assert _is_workdir_or_ancestor(__import__("pathlib").Path("."), None) is False
