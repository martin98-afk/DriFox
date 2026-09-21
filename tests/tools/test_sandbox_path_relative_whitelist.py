# -*- coding: utf-8 -*-
"""白名单相对路径（EU-G5）：相对条目按 workdir 解析 + 环境变量展开

背景：原实现 `_norm(item)` 对相对条目按**进程 cwd** 解析（不是 workdir），
用户在 UI 白名单填 `other_proj` 会静默失效。
坑：`%USERPROFILE%\\docs` 这类含环境变量的条目 `os.path.isabs()` 返回 False，
会被误当相对路径拼 workdir —— 必须先 `expandvars` 再判绝对。
"""

import pytest

from app.tools import sandbox
from app.tools.sandbox import SandboxConfig, check_path


def _cfg(whitelist=None):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("path.whitelist", whitelist or [])
    return cfg


def test_relative_whitelist_resolves_against_workdir(tmp_path):
    """相对白名单条目按 workdir 解析（不是进程 cwd）"""
    workdir = tmp_path / "proj"
    target_dir = workdir / "other_proj"
    target_dir.mkdir(parents=True)
    target = target_dir / "x.txt"
    target.write_text("x", encoding="utf-8")

    cfg = _cfg(["other_proj"])
    # 目标在 workdir 之外（用绝对路径传入），但命中相对白名单
    assert check_path(workdir, str(target), "write", cfg) == "allow"


def test_relative_whitelist_does_not_leak_outside(tmp_path):
    """workdir 之外的路径不因相对白名单而放行（边界不被放宽）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    cfg = _cfg(["other_proj"])
    outside = str(tmp_path / "elsewhere" / "y.txt")
    assert check_path(workdir, outside, "write", cfg) == "confirm"


def test_absolute_whitelist_still_works(tmp_path):
    """绝对白名单条目行为不变（回归）"""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    cfg = _cfg([str(allowed)])
    assert check_path(tmp_path / "proj", str(allowed / "f.txt"), "write", cfg) == "allow"


def test_env_var_whitelist_expanded(tmp_path, monkeypatch):
    """含环境变量的条目：先 expandvars 再判绝对（不被误当相对路径）"""
    fake_home = tmp_path / "home"
    mine = fake_home / "mine"
    mine.mkdir(parents=True)
    monkeypatch.setenv("DRIFOX_TEST_HOME", str(fake_home))

    cfg = _cfg(["%DRIFOX_TEST_HOME%\\mine"])
    # workdir 刻意设为别处：若实现漏了 expandvars，会被当相对路径拼到 workdir 下而失效
    workdir = tmp_path / "proj"
    workdir.mkdir()
    assert check_path(workdir, str(mine / "a.txt"), "write", cfg) == "allow"


def test_env_var_whitelist_posix_style(tmp_path, monkeypatch):
    """$VAR 形态（posix 风格）同样展开"""
    fake = tmp_path / "envroot"
    sub = fake / "sub"
    sub.mkdir(parents=True)
    monkeypatch.setenv("DRIFOX_TEST_ROOT", str(fake))
    cfg = _cfg(["$DRIFOX_TEST_ROOT/sub"])
    workdir = tmp_path / "proj"
    workdir.mkdir()
    assert check_path(workdir, str(sub / "b.txt"), "write", cfg) == "allow"


def test_whitelist_allows_outside_write_only(tmp_path):
    """白名单只影响越界写判定；read 模式本就恒 ALLOW（回归）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    cfg = _cfg(["other"])
    assert check_path(workdir, str(tmp_path / "out.txt"), "read", cfg) == "allow"


def test_blacklist_still_beats_whitelist(tmp_path):
    """黑名单优先级不因白名单改造而变化（回归）"""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    secrets = workdir / "secrets"
    secrets.mkdir()
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("path.whitelist", ["secrets"])
    cfg.set("path.blacklist", [str(secrets)])
    assert check_path(workdir, str(secrets / "token"), "write", cfg) == "confirm"


def test_relative_whitelist_multiple_entries(tmp_path):
    """多条相对条目任一命中即放行"""
    workdir = tmp_path / "proj"
    a = workdir / "a"
    a.mkdir(parents=True)
    cfg = _cfg(["nope", "a"])
    assert check_path(workdir, str(a / "f.txt"), "write", cfg) == "allow"


def test_sandbox_disabled_ignores_whitelist(tmp_path):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", False)
    cfg.set("path.whitelist", [])
    assert check_path(tmp_path, "D:/anywhere/x", "write", cfg) == "allow"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
