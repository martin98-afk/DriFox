# -*- coding: utf-8 -*-
"""删除动词「命令位」判定回归（2026-10-25 误搬事故第二批）

事故：垃圾桶把**参数位**出现的 rm/del 当删除命令，把这些只读命令后面的真实
路径 move 进垃圾箱：

    git rm -r --cached app          → 只脱离索引，磁盘上的 app 整个被搬走
    git commit -m "fix: rm cache"   → commit message 里的 rm，缓存目录被搬走
    grep -rn "rm" src/ tests/       → 搜索关键词，tests/ 被搬走
    echo "del /s /q app" > x.bat    → 写脚本内容，app 被搬走
    ls rm build / pip install del   → 普通参数，build 被搬走

修法：删除动词只在**命令位**（段首 / 连接符后 / 包装器后 / find -exec 后 /
git 子命令位）才成立；引号内容只有作为壳命令载荷时才当代码看。
"""

import pytest

from app.tools.sandbox import (
    _command_positions,
    _head_of,
    delete_targets,
    is_delete_command,
    is_trash_delete_command,
    stash_delete_targets_to_trash,
)


# ── 参数位动词不得当成删除命令 ──

_PARAM_POSITION_CASES = [
    'grep -rn "rm" src/ tests/',
    'grep -rn "Remove-Item" app/',
    "grep -rn 'rm -rf' tests/",
    'rg "shutil.rmtree" app/',
    'git grep -n "rm " app/',
    'git commit -m "fix: rm stale cache in app"',
    'git commit -m "chore: rm build dir"',
    'git commit -m "docs: 说明 rm/del 命令"',
    'git log --grep="del"',
    'git diff rm app',
    'ls rm build',
    'ls -la rm dist',
    'cat rm notes.txt',
    'type rm build',
    'echo rm build',
    'echo del app',
    'echo "rm -rf build" > notes.txt',
    'echo "del /s /q app" >> cleanup.bat',
    'printf "del x" > a.bat',
    'chmod +x rm build',
    'uv pip install del build',
    'where rm build',
    'which rm build',
    'Get-Command rm build',
    'pytest tests/ -k "rm or del"',
    'Get-ChildItem -Recurse | Select-String "Remove-Item"',
]


@pytest.mark.parametrize("cmd", _PARAM_POSITION_CASES)
def test_param_position_verb_not_delete(cmd):
    """参数位的 rm/del 不算删除命令（否则后面的路径会被误搬）"""
    assert is_delete_command(cmd) is False, f"参数位动词误判: {cmd}"


@pytest.mark.parametrize("cmd", _PARAM_POSITION_CASES)
def test_param_position_verb_extracts_no_targets(cmd, tmp_path):
    """参数位动词不产生任何删除目标"""
    workdir = tmp_path / "proj"
    for name in ("src", "tests", "app", "build", "dist", "notes.txt"):
        (workdir / name).mkdir(parents=True, exist_ok=True)
    targets = delete_targets(cmd, workdir)
    assert targets["existing"] == [], f"误提取删除目标: {cmd} → {targets['existing']}"


@pytest.mark.parametrize("cmd", _PARAM_POSITION_CASES)
def test_param_position_verb_moves_nothing(cmd, tmp_path):
    """参数位动词在垃圾桶层零动作（端到端）"""
    workdir = tmp_path / "proj"
    victim = workdir / "app"
    victim.mkdir(parents=True)
    (victim / "keep.py").write_text("x", encoding="utf-8")

    moved = stash_delete_targets_to_trash(cmd, tmp_path / "trash", workdir=workdir)

    assert moved == [], f"误搬: {cmd} → {moved}"
    assert victim.is_dir() and (victim / "keep.py").exists()


# ── 不落盘删除：--cached / --dry-run 绝不搬 ──


@pytest.mark.parametrize(
    "cmd",
    [
        "git rm -r --cached app",
        "git rm --cached secrets/db.json",
        "git rm --dry-run app",
    ],
)
def test_no_disk_delete_flags_never_stash(cmd, tmp_path):
    """只改索引/只预演的命令不删磁盘文件，垃圾桶绝不能搬"""
    workdir = tmp_path / "proj"
    app_dir = workdir / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "main.py").write_text("x", encoding="utf-8")
    secrets = workdir / "secrets"
    secrets.mkdir()
    (secrets / "db.json").write_text("{}", encoding="utf-8")

    moved = stash_delete_targets_to_trash(cmd, tmp_path / "trash", workdir=workdir)

    assert moved == [], f"--cached/--dry-run 不该搬: {cmd} → {moved}"
    assert app_dir.is_dir() and secrets.is_dir()


# ── 命令位动词仍必须识别（防修过头）──

_COMMAND_POSITION_CASES = [
    "rm -rf build",
    "rm -f logs/a.log",
    "del /q dist",
    "erase x.txt",
    "rd /s /q build",
    "unlink a.txt",
    "deltree old",
    "Remove-Item build",
    "ri build",
    "git rm -rf build",
    "git -C D:/repo rm x.txt",
    "cd app && rm -rf __pycache__",
    "echo done; rm -rf build",
    "echo start && rm -rf build && echo end",
    "sudo rm -rf build",
    "sudo -u root rm -rf build",
    "xargs rm -f",
    "busybox rm -rf build",
    "wsl rm -rf /mnt/c/x",
    'cmd /c del x.txt',
    'cmd /c "del /s /q build"',
    'powershell -c "Remove-Item -Recurse -Force D:/x/build"',
    'pwsh -Command "Remove-Item app"',
    'bash -c "rm -rf build"',
    'find . -name "*.tmp" -exec rm -rf {} +',
]


@pytest.mark.parametrize("cmd", _COMMAND_POSITION_CASES)
def test_command_position_verb_detected(cmd):
    """命令位的删除动词必须仍被识别（修过头会漏保护真删除）"""
    assert is_delete_command(cmd) is True, f"命令位动词漏判: {cmd}"


def test_command_position_extracts_targets(tmp_path):
    """命令位动词照常提取目标（真删除仍受保护）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    victim = workdir / "build"
    victim.mkdir()

    targets = delete_targets("rm -rf build", workdir)

    assert victim in targets["existing"]


def test_git_rm_still_stashes(tmp_path):
    """git rm（不带 --cached）真删工作区文件，仍要搬"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    victim = workdir / "build"
    victim.mkdir()
    (victim / "a.o").write_text("x", encoding="utf-8")

    moved = stash_delete_targets_to_trash("git rm -rf build", tmp_path / "trash", workdir=workdir)

    assert len(moved) == 1
    assert not victim.exists()


# ── 命令位判定函数本身 ──


def test_command_positions_simple():
    toks = ["rm", "-rf", "build"]
    assert _command_positions(toks) == {0}


def test_command_positions_after_separator():
    toks = ["cd", "app", "&&", "rm", "-rf", "build"]
    assert _command_positions(toks) == {0, 3}


def test_command_positions_wrapper_payload():
    toks = ["sudo", "-u", "root", "rm", "-rf", "build"]
    assert 3 in _command_positions(toks)


def test_command_positions_git_subcommand():
    toks = ["git", "-C", "D:/repo", "rm", "x.txt"]
    positions = _command_positions(toks)
    assert 3 in positions, f"git 子命令位应在集合内: {positions}"
    assert 0 in positions


def test_head_of_normalizes():
    assert _head_of("C:/Windows/System32/rm.exe") == "rm"
    assert _head_of('"del"') == "del"
    assert _head_of("RM") == "rm"


# ── 垃圾桶自包含与 .NET 兜底收窄 ──


def test_stash_refuses_dir_containing_trash(tmp_path):
    """含垃圾桶的目录不得被搬（等于把它搬进它自己内部）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    data_dir = workdir / ".drifox"
    trash_root = data_dir / "backups" / "deletes"
    trash_root.mkdir(parents=True)
    (data_dir / "app.config").write_text("cfg", encoding="utf-8")

    moved = stash_delete_targets_to_trash(
        f'rm -rf "{data_dir.as_posix()}"', trash_root, workdir=workdir
    )

    assert moved == [], "含垃圾桶的目录不能被搬"
    assert data_dir.is_dir() and (data_dir / "app.config").exists()


def test_dotnet_delete_skips_following_segments(tmp_path):
    """.NET Delete 之后语句里的路径不是删除目标"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    doomed = workdir / "doomed"
    doomed.mkdir()
    unrelated = workdir / "unrelated"
    unrelated.mkdir()

    cmd = (
        f"$p='{doomed.as_posix()}'; [System.IO.Directory]::Delete($p, $true); "
        f"Get-ChildItem '{unrelated.as_posix()}'"
    )
    targets = delete_targets(cmd, workdir)

    existing = [str(p) for p in targets["existing"]]
    assert str(doomed) in existing, "赋值变量里的删除目标应被提取"
    assert str(unrelated) not in existing, "Delete 之后的语句路径不该算目标"


# ── 两级判定分工：审批宽松 / 垃圾桶严格 ──

# 真删除但目标无法静态定位（glob / 变量 / 管道 / 索引）：
# 审批层必须认（否则静默执行），垃圾桶层必须不碰（否则搬错东西）
_APPROVAL_ONLY_CASES = [
    'find . -name "*.pyc" -delete',
    'find build -name "*.o" -delete',
    "git clean -fdx",
    "git clean -fd",
    'python -c "import shutil; shutil.rmtree(\'build\')"',
    'python -c "import os; os.remove(\'a.txt\')"',
    'python3 -c "from pathlib import Path; Path(\'a\').unlink()"',
    'node -e "require(\'fs\').rmSync(\'build\')"',
    'for f in *.txt; do rm "$f"; done',
    "truncate -s 0 a.txt",
    "shred -u a.txt",
    "srm a.txt",
    "[System.IO.Directory]::Delete($p, $true)",
]


@pytest.mark.parametrize("cmd", _APPROVAL_ONLY_CASES)
def test_approval_layer_recognizes_soft_deletes(cmd):
    """审批层必须认出这些真删除（漏认 = 不弹审批直接执行）"""
    assert is_delete_command(cmd) is True, f"审批层漏判真删除: {cmd}"


@pytest.mark.parametrize("cmd", _APPROVAL_ONLY_CASES)
def test_trash_layer_refuses_unresolvable_targets(cmd, tmp_path):
    """垃圾桶层不碰目标无法静态定位的命令（目标藏在 glob/变量/管道里）"""
    assert is_trash_delete_command(cmd) is False, f"垃圾桶不该处理: {cmd}"

    workdir = tmp_path / "proj"
    build = workdir / "build"
    build.mkdir(parents=True)
    (build / "keep.o").write_text("x", encoding="utf-8")
    a_txt = workdir / "a.txt"
    a_txt.write_text("a", encoding="utf-8")

    moved = stash_delete_targets_to_trash(cmd, tmp_path / "trash", workdir=workdir)

    assert moved == [], f"垃圾桶误搬: {cmd} → {moved}"
    assert build.is_dir() and a_txt.exists()


@pytest.mark.parametrize(
    "cmd",
    ["rm -rf build", "git rm -rf build", 'powershell -c "Remove-Item build"', "rm a.txt&&rm b.txt"],
)
def test_both_layers_agree_on_resolvable_deletes(cmd):
    """目标可静态定位的真删除：两层都认（审批弹窗 + 垃圾桶兜底）"""
    assert is_delete_command(cmd) is True
    assert is_trash_delete_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    ['eval "rm -rf build"', 'iex "Remove-Item build"', 'Invoke-Expression "Remove-Item -Recurse build"'],
)
def test_literal_targets_reachable_through_evaluators(cmd):
    """求值器包裹但目标是字面量：垃圾桶照常搬（判据看目标可解析性，不看求值器）"""
    assert is_delete_command(cmd) is True
    assert is_trash_delete_command(cmd) is True


def test_glob_targets_expanded_and_moved(tmp_path):
    """`rm *.pyc`：glob 展开成实际文件后搬走（不因通配就放弃保护）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    victims = [workdir / f"m{i}.pyc" for i in range(3)]
    for v in victims:
        v.write_text("x", encoding="utf-8")
    keep = workdir / "keep.py"
    keep.write_text("k", encoding="utf-8")

    moved = stash_delete_targets_to_trash("rm *.pyc", tmp_path / "trash", workdir=workdir)

    assert len(moved) == 3, f"三个 pyc 都应搬: {moved}"
    assert all(not v.exists() for v in victims)
    assert keep.exists(), "不匹配 glob 的文件不能动"


def test_variable_target_not_moved(tmp_path):
    """`rm "$f"`：变量目标无法静态解析，垃圾桶不搬（否则搬错东西）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    f = workdir / "f"
    f.write_text("x", encoding="utf-8")

    assert is_trash_delete_command('rm "$f"') is False
    moved = stash_delete_targets_to_trash('rm "$f"', tmp_path / "trash", workdir=workdir)

    assert moved == []
    assert f.exists()


def test_git_clean_still_gated_by_command_safety():
    """git clean -fdx 的 confirm 来自 command_safety，不得被 delete_protection 门控降级

    2026-10-25 回归：宽松判据接入 is_delete_command 后，`git clean -fdx` 走了
    「删除命令 → 门控」分支，delete_protection 关闭时被静默降为 allow。
    """
    from app.tools.sandbox import SandboxConfig, sandbox_check_tool

    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("delete_protection", False)

    assert sandbox_check_tool("bash", {"command": "git clean -fdx"}, cfg) == "confirm"
    assert sandbox_check_tool("bash", {"command": "git push --force"}, cfg) == "confirm"


def test_delete_gate_still_lifts_plain_rm():
    """普通 rm 仍受 delete_protection 门控（开关关闭时不弹审批）"""
    from app.tools.sandbox import SandboxConfig, sandbox_check_tool

    off = SandboxConfig(config_path=":memory:")
    off.set("sandbox_enabled", True)
    off.set("delete_protection", False)
    assert sandbox_check_tool("bash", {"command": "rm x.txt"}, off) == "allow"

    on = SandboxConfig(config_path=":memory:")
    on.set("sandbox_enabled", True)
    on.set("delete_protection", True)
    assert sandbox_check_tool("bash", {"command": "rm x.txt"}, on) == "confirm"


def test_adjacent_separators_both_targets_moved(tmp_path):
    """`rm a.txt&&rm b.txt`：紧贴连接符不得丢目标（旧实现只搬到一个）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    a = workdir / "a.txt"
    b = workdir / "b.txt"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")

    moved = stash_delete_targets_to_trash("rm a.txt&&rm b.txt", tmp_path / "trash", workdir=workdir)

    assert len(moved) == 2, f"两个目标都应搬: {moved}"
    assert not a.exists() and not b.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
