# -*- coding: utf-8 -*-
"""OS 级沙箱（macOS Seatbelt / sandbox-exec）回归测试

覆盖面：
- DEFAULT_CONFIG 默认值（os_sandbox 节、enabled=False、level="write"）
- os_sandbox_active 的平台与开关门控
- wrap_os_sandbox 的直通与包装形态（Path A argv / Path B bash -c）
- profile 生成（deny default + 白名单 allow 模式）
- macOS 真沙箱集成：白名单内写、越界拒、/dev/null 放行、两档网络差异

集成用例仅 darwin + 存在 sandbox-exec 时运行；其余平台 skip。
注意：测试只改 SandboxConfig 单例的内存值，不调 save()，不落盘用户配置。
"""

import os
import sys
from pathlib import Path

import pytest

import app.tools.sandbox as sandbox_mod
from app.tools.sandbox import (
    SandboxConfig,
    _seatbelt_profile_text,
    os_sandbox_active,
    wrap_os_sandbox,
)

SEATBELT = Path("/usr/bin/sandbox-exec")


# ============================================================
# 配置默认值
# ============================================================


def test_default_config_has_os_sandbox():
    cfg = SandboxConfig(":memory:")
    node = cfg.get("os_sandbox")
    assert isinstance(node, dict)
    assert node.get("enabled") is False
    assert node.get("level") == "write"


def test_active_gates_on_platform_and_config(monkeypatch):
    # 非 darwin：一律 False
    monkeypatch.setattr(sandbox_mod.sys, "platform", "win32")
    assert os_sandbox_active() is False

    # darwin 但未启用：False
    monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
    cfg = SandboxConfig(":memory:")
    monkeypatch.setattr(SandboxConfig, "get_instance", classmethod(lambda cls: cfg))
    assert os_sandbox_active() is False

    # darwin + 启用：取决于 sandbox-exec 是否存在（测试机为 mac 时为 True）
    cfg._data["os_sandbox"]["enabled"] = True
    assert os_sandbox_active() is SEATBELT.exists()


# ============================================================
# wrap_os_sandbox
# ============================================================


def test_wrap_passthrough_when_disabled(monkeypatch):
    cfg = SandboxConfig(":memory:")
    monkeypatch.setattr(SandboxConfig, "get_instance", classmethod(lambda cls: cfg))
    monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
    assert wrap_os_sandbox("git status", "/tmp/x") == "git status"
    assert wrap_os_sandbox("echo hi | wc", "/tmp/x", use_shell=True) == "echo hi | wc"


def test_wrap_path_a_argv(monkeypatch):
    cfg = SandboxConfig(":memory:")
    cfg._data["os_sandbox"]["enabled"] = True
    monkeypatch.setattr(SandboxConfig, "get_instance", classmethod(lambda cls: cfg))
    if not (sys.platform == "darwin" and SEATBELT.exists()):
        pytest.skip("仅 macOS + sandbox-exec")

    wrapped = wrap_os_sandbox("git status", "/tmp", use_shell=False)
    assert wrapped.startswith("/usr/bin/sandbox-exec -f ")
    assert "/tmp/sbx" not in wrapped
    assert "git status" in wrapped
    # Path A 不引入 shell 解释
    assert "/bin/bash" not in wrapped and "-c" not in wrapped


def test_wrap_path_b_bash_c(monkeypatch):
    cfg = SandboxConfig(":memory:")
    cfg._data["os_sandbox"]["enabled"] = True
    monkeypatch.setattr(SandboxConfig, "get_instance", classmethod(lambda cls: cfg))
    if not (sys.platform == "darwin" and SEATBELT.exists()):
        pytest.skip("仅 macOS + sandbox-exec")

    inner = "echo hi | grep hi > /tmp/x.txt"
    wrapped = wrap_os_sandbox(inner, "/tmp", use_shell=True)
    assert "/bin/bash -c " in wrapped
    # 原命令整体被 quote，防二次解释
    assert shlex_quote(inner) in wrapped


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


# ============================================================
# profile 生成
# ============================================================


def test_profile_deny_default_whitelist(tmp_path):
    text = _seatbelt_profile_text(tmp_path, allow_network=True)
    assert "(deny default)" in text
    # 工作区原始 + resolve 双形态都在写白名单
    assert f'(subpath "{tmp_path}")' in text
    assert f'(subpath "{tmp_path.resolve()}")' in text
    # 系统临时区与 /dev 放行（devfs 必须 allow，见实现注释的实测坑）
    for p in ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "/dev"):
        assert f'(subpath "{p}")' in text
    # write 档放行网络
    assert "(allow network*)" in text


def test_profile_no_network_level(tmp_path):
    text = _seatbelt_profile_text(tmp_path, allow_network=False)
    assert "(deny default)" in text
    # write+net 档：deny default 下网络默认已禁，无 allow network*
    assert "(allow network*)" not in text


# ============================================================
# 真沙箱集成（darwin only）
# ============================================================

pytestmark_darwin = pytest.mark.skipif(
    not (sys.platform == "darwin" and SEATBELT.exists()),
    reason="仅 macOS + sandbox-exec",
)


@pytest.fixture()
def os_sb_enabled(monkeypatch):
    cfg = SandboxConfig(":memory:")
    cfg._data["os_sandbox"]["enabled"] = True
    cfg._data["os_sandbox"]["level"] = "write"
    monkeypatch.setattr(SandboxConfig, "get_instance", classmethod(lambda cls: cfg))
    return cfg


def _run(command: str, cwd: Path, shell: bool = False):
    import subprocess

    from app.tools.command_safety import run_safe, run_with_shell

    fn = run_with_shell if shell else run_safe
    proc = fn(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(cwd))
    out = proc.communicate(timeout=30)[0].decode("utf-8", "replace").strip()
    return proc.returncode, out


@pytest.mark.skipif(
    not (sys.platform == "darwin" and SEATBELT.exists()),
    reason="仅 macOS + sandbox-exec",
)
class TestOsSandboxIntegration:
    def test_whitelist_write_allowed(self, os_sb_enabled, tmp_path):
        rc, out = _run("touch inside.txt", tmp_path)
        assert rc == 0, out
        assert (tmp_path / "inside.txt").exists()

    def test_home_write_denied(self, os_sb_enabled, tmp_path):
        target = Path.home() / "drifox_os_sb_escape_probe.txt"
        rc, out = _run(f"touch {target}", tmp_path)
        assert rc != 0
        assert not target.exists()

    def test_dev_null_write_allowed(self, os_sb_enabled, tmp_path):
        rc, out = _run("sh -c 'echo x > /dev/null'", tmp_path, shell=True)
        assert rc == 0, out

    def test_shell_pipe_in_whitelist(self, os_sb_enabled, tmp_path):
        rc, out = _run("echo hi | tr a-z A-Z > piped.txt && cat piped.txt", tmp_path, shell=True)
        assert rc == 0 and out == "HI", out

    def test_write_net_level_blocks_network(self, os_sb_enabled, tmp_path):
        os_sb_enabled._data["os_sandbox"]["level"] = "write+net"
        # socket() 在内核层被拒，不依赖外网可达性
        rc, out = _run(
            "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\"",
            tmp_path,
        )
        assert rc != 0

    def test_write_level_allows_local_shell(self, os_sb_enabled, tmp_path):
        rc, out = _run("python3 -c 'print(6*7)'", tmp_path)
        assert rc == 0 and out == "42", out
