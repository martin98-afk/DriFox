# -*- coding: utf-8 -*-
"""命令写边界（重定向 / tee / mkdir / touch：写目标过 check_path(write)）

背景：命令分支原只做「黑名单读取检查」，workdir 写边界对命令类写入零约束 ——
`echo x > D:/out.txt` / `mkdir D:/new` / `tee` / `touch` 全部静默放行，
AI 可经 bash 写工作目录外任意位置，绕开「写只在工作目录内」的预期。

设计：write_targets 提取明确写语义的目标（引号感知扫描，不用正则/shex：
要处理无空格重定向、引号目标、Windows 反斜杠），过 check_path(write) 判定。
"""

import pytest

from app.tools.sandbox import SandboxConfig, sandbox_check_tool, write_targets


def _cfg(whitelist=None, blacklist=None, enabled=True):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", enabled)
    cfg.set("path.whitelist", whitelist or [])
    cfg.set("path.blacklist", blacklist or [])
    return cfg


# ── write_targets 提取 ──


def test_targets_redirect_with_space():
    assert write_targets("echo x > D:/a.txt") == ["D:/a.txt"]


def test_targets_redirect_no_space():
    """无空格重定向 `x>f`（正则方案会漏，扫描器覆盖）"""
    assert write_targets("echo x>D:/a.txt") == ["D:/a.txt"]


def test_targets_append_redirect():
    assert write_targets("echo x >> D:/a.txt") == ["D:/a.txt"]


def test_targets_fd_redirect():
    assert write_targets("cmd /c echo x 2> D:/err.txt") == ["D:/err.txt"]


def test_targets_fd_dup_ignored():
    """`2>&1` 是 fd 复制不是文件 → 不提取"""
    assert write_targets("echo x 2>&1") == []
    assert write_targets("echo x > D:/a.txt 2>&1") == ["D:/a.txt"]


def test_targets_quoted_with_space():
    assert write_targets('echo x > "D:/a b.txt"') == ["D:/a b.txt"]


def test_targets_quoted_angle_ignored():
    """引号内的 `>` 不是重定向"""
    assert write_targets('echo "a > b" > D:/real.txt') == ["D:/real.txt"]


def test_targets_chain_segments():
    assert write_targets("echo y & echo x > D:/a.txt") == ["D:/a.txt"]


def test_targets_pipe_tee():
    assert write_targets("type a.txt | tee D:/b.txt") == ["D:/b.txt"]


def test_targets_mkdir_touch():
    assert write_targets("mkdir D:/n") == ["D:/n"]
    assert write_targets("touch D:/t.txt") == ["D:/t.txt"]


def test_targets_mkdir_flags_skipped():
    assert write_targets("mkdir -p D:/deep/new") == ["D:/deep/new"]


def test_targets_shell_payload():
    """cmd /c mkdir / bash -c 'echo > f' 解壳提取"""
    assert write_targets("cmd /c mkdir D:/n2") == ["D:/n2"]
    assert write_targets("bash -c 'echo x > D:/q.txt'") == ["D:/q.txt"]


def test_targets_windows_backslash_preserved():
    """反斜杠路径不被 shlex 吞掉（C:\\tmp → C:tmp 是已知坑）"""
    assert write_targets("echo x > C:\\tmp\\a.txt") == ["C:\\tmp\\a.txt"]


def test_targets_non_write_commands_empty():
    """非写形态命令提取为空（幂等前提）"""
    assert write_targets("git status") == []
    assert write_targets("rm -rf build/") == []
    assert write_targets("python -m pytest tests/") == []


# ── 端到端判定 ──


def test_redirect_outside_confirm(tmp_path):
    v = sandbox_check_tool("bash", {"command": f"echo x > {tmp_path.parent}/out.txt"}, _cfg(), workdir=tmp_path)
    assert v == "confirm"


def test_redirect_inside_allow(tmp_path):
    v = sandbox_check_tool("bash", {"command": f"echo x > {tmp_path}/in.txt"}, _cfg(), workdir=tmp_path)
    assert v == "allow"


def test_relative_redirect_resolved_against_workdir(tmp_path):
    """相对写目标按 workdir 解析（bash 进程 cwd = workdir）"""
    assert sandbox_check_tool("bash", {"command": "echo x > out.txt"}, _cfg(), workdir=tmp_path) == "allow"


def test_mkdir_outside_confirm(tmp_path):
    v = sandbox_check_tool("bash", {"command": f"mkdir {tmp_path.parent}/newdir"}, _cfg(), workdir=tmp_path)
    assert v == "confirm"


def test_tee_outside_confirm(tmp_path):
    v = sandbox_check_tool("bash", {"command": f"type a | tee {tmp_path.parent}/b.txt"}, _cfg(), workdir=tmp_path)
    assert v == "confirm"


def test_whitelist_allows_outside_write(tmp_path):
    outside = tmp_path.parent / "allowed"
    cfg = _cfg(whitelist=[str(outside)])
    v = sandbox_check_tool("bash", {"command": f"echo x > {outside}/f.txt"}, cfg, workdir=tmp_path)
    assert v == "allow"


def test_blacklist_blocks_write(tmp_path):
    cfg = _cfg(blacklist=[str(tmp_path.parent / "secret")])
    v = sandbox_check_tool("bash", {"command": f"echo x > {tmp_path.parent}/secret/f.txt"}, cfg, workdir=tmp_path)
    assert v == "confirm"


# ── 关键回归：不许降级既有判定 ──


def test_deny_not_downgraded_by_redirect():
    """DENY 命令附带重定向不得被写检查降级为 CONFIRM（block 不受影响）"""
    v = sandbox_check_tool("bash", {"command": "cacls C:/ /g user:F > D:/out.txt"}, _cfg())
    assert v == "deny"


def test_safe_command_still_allow():
    assert sandbox_check_tool("bash", {"command": "git status"}, _cfg()) == "allow"


def test_fd_dup_only_command_untouched():
    assert sandbox_check_tool("bash", {"command": "echo x 2>&1"}, _cfg()) == "allow"


def test_sandbox_disabled_passthrough():
    v = sandbox_check_tool("bash", {"command": "echo x > D:/outside.txt"}, _cfg(enabled=False))
    assert v == "allow"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
