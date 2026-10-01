# -*- coding: utf-8 -*-
"""显式 workdir 参数（#6 报告发现的真实缺陷）

背景：`sandbox_check_tool` 原先只从 `_current_workdir()` 取基准，而该函数读
`BackgroundTaskManager` 单例里的全局 workdir（由每次工具调用刷新）。
**它返回的是"全局最后一次工具调用的 workdir"，不是调用者线程的 workdir** ——
多窗口 / 主对话与子智能体并发时会串味，导致路径判定用错基准（该拦的放行或反之）。

修法：加显式 `workdir` 参数（不传时行为完全不变，向后兼容）。
"""

import pytest

from app.tools import sandbox
from app.tools.sandbox import SandboxConfig, sandbox_check_tool


def _cfg():
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    return cfg


def _fake_groups(monkeypatch, write=("write",)):
    monkeypatch.setattr(
        sandbox,
        "_tools_in",
        lambda g: frozenset(write if g == sandbox.GROUP_WRITE_NAME else ()),
    )


def test_explicit_workdir_overrides_current(tmp_path, monkeypatch):
    """显式 workdir 覆盖全局单例值：同一路径因基准不同得到不同判定

    - 目标是 A 目录下的文件
    - 全局 workdir = B（不含目标）→ 不传参时判 confirm
    - 显式传 workdir = A → 判 allow
    """
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    target = dir_a / "f.txt"
    target.write_text("x", encoding="utf-8")

    _fake_groups(monkeypatch)
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: dir_b)

    # 不传：用全局基准 B → 目标在界外 → confirm
    assert sandbox_check_tool("write", {"path": str(target)}, _cfg()) == "confirm"
    # 显式传 A → 目标在界内 → allow（证明不再依赖全局单例）
    assert sandbox_check_tool("write", {"path": str(target)}, _cfg(), workdir=dir_a) == "allow"


def test_workdir_none_falls_back_to_current(tmp_path, monkeypatch):
    """不传 workdir → 行为与改造前一致（向后兼容回归）"""
    _fake_groups(monkeypatch)
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    assert sandbox_check_tool("write", {"path": str(tmp_path / "a.txt")}, _cfg()) == "allow"
    assert sandbox_check_tool("write", {"path": "D:/elsewhere/x.txt"}, _cfg()) == "confirm"
    # 显式 None 与不传等价
    assert sandbox_check_tool("write", {"path": str(tmp_path / "a.txt")}, _cfg(), workdir=None) == "allow"


def test_explicit_workdir_accepts_str_and_path(tmp_path, monkeypatch):
    """workdir 同时接受 str 与 Path（调用方两种写法都常见）"""
    _fake_groups(monkeypatch)
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    assert sandbox_check_tool("write", {"path": str(target)}, _cfg(), workdir=str(tmp_path)) == "allow"
    assert sandbox_check_tool("write", {"path": str(target)}, _cfg(), workdir=tmp_path) == "allow"


def test_explicit_workdir_ignored_when_sandbox_disabled(tmp_path, monkeypatch):
    """沙箱关闭时 workdir 参数无影响（直接放行）"""
    _fake_groups(monkeypatch)
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", False)
    assert sandbox_check_tool("write", {"path": "D:/x"}, cfg, workdir=tmp_path) == "allow"


def test_explicit_workdir_not_used_for_command_tools(tmp_path, monkeypatch):
    """命令工具走命令/网络判定，不受 workdir 影响（回归边界）"""
    _fake_groups(monkeypatch)
    assert sandbox_check_tool("bash", {"command": "git status"}, _cfg(), workdir=tmp_path) == "allow"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
