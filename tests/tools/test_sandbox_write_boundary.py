# -*- coding: utf-8 -*-
"""文件工具写边界：相对路径必须按 workdir 解析（2026-11-03 安全中心误审批根因）

背景：`sandbox_check_tool` 文件分支把 path 原样传 `check_path`，`_norm` 内
`Path.resolve()` 对相对路径按**进程 cwd** 解析；而文件工具执行层（file_tools
`_resolve`）相对路径按 **workdir** 解析。两侧基准不一致 → AI 用相对路径写
workdir 内文件（如 `skills/x/SKILL.md`）被判越界 → 全部触发审批，即用户报的
「写到当前工作目录也触发审批」。

命令分支早已踩过同一坑并修复（sandbox.py 命令写边界块注释②：相对目标必须按
workdir 解析，走 `_resolve_cmd_path`）；本组测试把文件分支对齐到同一语义。

防护语义保持：
- workdir 内相对路径 → allow（本组核心回归）
- 相对路径逃出 workdir（`../`）→ confirm
- 黑名单对相对路径读取同样生效（修复前按进程 cwd 解析会漏判）
"""

from pathlib import Path

import pytest

from app.tools.sandbox import SandboxConfig, sandbox_check_tool


def _cfg(blacklist=None, enabled=True):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", enabled)
    cfg.set("path.blacklist", blacklist or [])
    return cfg


@pytest.fixture
def write_tools(monkeypatch):
    """注册表不可用的测试环境里直接声明 write/read 分组成员"""
    from app.tools import sandbox

    def fake_tools_in(group_name):
        if group_name == sandbox.GROUP_WRITE_NAME:
            return {"write", "edit", "multi_edit"}
        if group_name == sandbox.GROUP_READ_NAME:
            return {"read"}
        return frozenset()

    monkeypatch.setattr(sandbox, "_tools_in", fake_tools_in)


# ── 核心：workdir 内相对路径不得触发审批 ──

def test_write_relative_path_inside_workdir_allowed(write_tools):
    """相对路径写 workdir 内 → allow（修复前按进程 cwd 解析误报 confirm）"""
    cfg = _cfg()
    wd = Path(tmp_root()).resolve()
    verdict = sandbox_check_tool("write", {"path": "skills/demo/SKILL.md"}, cfg, workdir=wd)
    assert verdict == "allow", "workdir 内相对路径写入不得触发审批"

def test_edit_relative_path_inside_workdir_allowed(write_tools):
    cfg = _cfg()
    wd = Path(tmp_root()).resolve()
    verdict = sandbox_check_tool("edit", {"path": "src/app.py"}, cfg, workdir=wd)
    assert verdict == "allow"

def test_multi_edit_paths_array_inside_workdir_allowed(write_tools):
    cfg = _cfg()
    wd = Path(tmp_root()).resolve()
    verdict = sandbox_check_tool("multi_edit", {"paths": ["a.py", "sub/b.py"]}, cfg, workdir=wd)
    assert verdict == "allow"


# ── 防护语义保持：逃出 workdir 仍拦截 ──

def test_write_relative_path_escape_workdir_confirmed(write_tools):
    cfg = _cfg()
    wd = Path(tmp_root()).resolve()
    verdict = sandbox_check_tool("write", {"path": "../outside/evil.txt"}, cfg, workdir=wd)
    assert verdict == "confirm", "相对路径逃出 workdir 必须继续审批"

def test_write_absolute_path_outside_workdir_confirmed(write_tools):
    """绝对路径越界（现状正确行为）不回归"""
    cfg = _cfg()
    wd = Path(tmp_root()).resolve()
    verdict = sandbox_check_tool("write", {"path": "D:/elsewhere/x.txt"}, cfg, workdir=wd)
    assert verdict == "confirm"


# ── 黑名单 × 相对路径（read 分支同源缺陷）──

def test_read_relative_path_hit_blacklist_confirmed(write_tools, tmp_path):
    """相对路径读取 workdir 内黑名单目录 → confirm（修复前按进程 cwd 解析漏判）"""
    cfg = _cfg(blacklist=[str(tmp_path / "secret")])
    verdict = sandbox_check_tool("read", {"path": "secret/k.pem"}, cfg, workdir=tmp_path)
    assert verdict == "confirm", "黑名单对相对路径读取必须生效"


# ── 环境变量 / ~ 形态 ──

def test_write_env_var_path_inside_workdir_allowed(write_tools, tmp_path, monkeypatch):
    """%VAR% 展开后落在 workdir 内 → allow（expandvars 先于基准判定）"""
    monkeypatch.setenv("FOO_WD", str(tmp_path))
    cfg = _cfg()
    verdict = sandbox_check_tool("write", {"path": "%FOO_WD%/a.txt"}, cfg, workdir=tmp_path)
    assert verdict == "allow"


def tmp_root() -> str:
    """稳定测试根：进程 cwd 下不存在歧义的临时目录（只用于拼相对路径，不落盘）"""
    import tempfile

    return tempfile.mkdtemp(prefix="sandbox_wd_test_")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
