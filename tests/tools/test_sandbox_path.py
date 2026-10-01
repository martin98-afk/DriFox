# -*- coding: utf-8 -*-
"""check_path：workdir 内外 × 读写 × 白/黑名单 × 环境变量展开"""
from app.tools.sandbox import SandboxConfig, check_path


def _cfg(tmp_path, whitelist=None, blacklist=None, enabled=True):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", enabled)
    cfg.set("path.whitelist", whitelist or [])
    cfg.set("path.blacklist", blacklist or [])
    return cfg


def test_write_inside_workdir_allowed(tmp_path):
    assert check_path(tmp_path, str(tmp_path / "a.txt"), "write", _cfg(tmp_path)) == "allow"


def test_write_outside_workdir_confirm(tmp_path):
    outside = tmp_path.parent / "elsewhere.txt"
    assert check_path(tmp_path, str(outside), "write", _cfg(tmp_path)) == "confirm"


def test_read_outside_allowed(tmp_path):
    outside = tmp_path.parent / "elsewhere.txt"
    assert check_path(tmp_path, str(outside), "read", _cfg(tmp_path)) == "allow"


def test_whitelist_allows_outside_write(tmp_path):
    outside_dir = tmp_path.parent / "other_proj"
    cfg = _cfg(tmp_path, whitelist=[str(outside_dir)])
    assert check_path(tmp_path, str(outside_dir / "a.txt"), "write", cfg) == "allow"


def test_blacklist_forces_confirm_even_inside(tmp_path):
    cfg = _cfg(tmp_path, blacklist=[".env"])
    assert check_path(tmp_path, str(tmp_path / ".env"), "read", cfg) == "confirm"
    assert check_path(tmp_path, str(tmp_path / ".env"), "write", cfg) == "confirm"


def test_blacklist_beats_whitelist(tmp_path):
    secret_dir = tmp_path.parent / "secret"
    cfg = _cfg(tmp_path, whitelist=[str(secret_dir)], blacklist=[str(secret_dir)])
    assert check_path(tmp_path, str(secret_dir / "a.txt"), "write", cfg) == "confirm"


def test_disabled_sandbox_allows_all(tmp_path):
    outside = tmp_path.parent / "x.txt"
    assert check_path(tmp_path, str(outside), "write", _cfg(tmp_path, enabled=False)) == "allow"


def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIFOX_TEST_HOME", str(tmp_path))
    # 环境变量展开后落在 workdir 内 → allow
    verdict = check_path(tmp_path, "%DRIFOX_TEST_HOME%/a.txt", "write", _cfg(tmp_path))
    assert verdict == "allow"
