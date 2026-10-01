# -*- coding: utf-8 -*-
"""数组字段路径解析（EU-G4）：files/paths 数组逐元素判定

背景：原实现只读 `path` / `file_path` 单值字段，带数组字段的工具（如
`stage_files` 的 `files`）在路径检查处被静默放行。

**关键事实**（防止后人困惑"改了没效果"）：
- `multi_edit` 的 schema **已有** `path` 字段（`file_tools.py:526`），已被覆盖，不属漏判
- `stage_files` 是 `danger="safe"` + `GROUP_READ`（`file_tools.py:1180-1188`），
  走 read 分支，而 read 模式在 `check_path` 恒定 ALLOW
  → **本项对 stage_files 不产生实际拦截效果**，是**防御未来可能出现的数组写工具**
"""

import pytest

from app.tools import sandbox
from app.tools.sandbox import SandboxConfig, sandbox_check_tool


def _cfg():
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    return cfg


def _fake_groups(monkeypatch, write=(), read=()):
    """registry 分组注入（测试环境不加载插件工具注册）"""
    monkeypatch.setattr(
        sandbox,
        "_tools_in",
        lambda g: frozenset(write if g == sandbox.GROUP_WRITE_NAME else read),
    )


def test_array_field_paths_checked(tmp_path, monkeypatch):
    """锁定现状：stage_files 属 read 组 → 数组字段即使越界也是 allow

    read 模式在 check_path 中恒定 ALLOW（只有 write 才有 workdir 边界），
    所以本项对 stage_files 无实际拦截效果——这是**预期行为**，不是缺陷。
    """
    _fake_groups(monkeypatch, read=("stage_files",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    outside = str(tmp_path.parent / "elsewhere" / "x.txt")
    verdict = sandbox_check_tool("stage_files", {"files": [outside]}, _cfg())
    assert verdict == "allow"


def test_unknown_array_tool_in_write_group(tmp_path, monkeypatch):
    """写组工具传 files 数组且越界 → confirm（证明 G4 逻辑真的生效）"""
    _fake_groups(monkeypatch, write=("fake_array_writer",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    outside = str(tmp_path.parent / "elsewhere" / "x.txt")
    verdict = sandbox_check_tool("fake_array_writer", {"files": [outside]}, _cfg())
    assert verdict == "confirm", "数组字段越界写必须判 confirm"


def test_array_all_inside_workdir_allows(tmp_path, monkeypatch):
    """数组内全部路径在 workdir 内 → allow"""
    _fake_groups(monkeypatch, write=("fake_array_writer",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    verdict = sandbox_check_tool(
        "fake_array_writer",
        {"files": [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")]},
        _cfg(),
    )
    assert verdict == "allow"


def test_array_one_outside_is_enough_for_confirm(tmp_path, monkeypatch):
    """任一元素越界 → 整体 confirm（不因多数在界内而放行）"""
    _fake_groups(monkeypatch, write=("fake_array_writer",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    verdict = sandbox_check_tool(
        "fake_array_writer",
        {"files": [str(tmp_path / "ok.txt"), str(tmp_path.parent / "out.txt")]},
        _cfg(),
    )
    assert verdict == "confirm"


def test_paths_key_supported(tmp_path, monkeypatch):
    """`paths` 键同样支持（第二候选数组字段）"""
    _fake_groups(monkeypatch, write=("fake_array_writer",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    outside = str(tmp_path.parent / "out.txt")
    assert sandbox_check_tool("fake_array_writer", {"paths": [outside]}, _cfg()) == "confirm"


def test_empty_array_allows(tmp_path, monkeypatch):
    """空数组 → allow（无路径可判，不误拦）"""
    _fake_groups(monkeypatch, write=("fake_array_writer",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    assert sandbox_check_tool("fake_array_writer", {"files": []}, _cfg()) == "allow"


def test_no_path_field_allows(monkeypatch):
    """无任何路径字段 → allow（保持原行为）"""
    _fake_groups(monkeypatch, write=("fake_array_writer",))
    assert sandbox_check_tool("fake_array_writer", {}, _cfg()) == "allow"


def test_single_path_still_works(tmp_path, monkeypatch):
    """单值 path 优先于数组（回归：原有行为不变）"""
    _fake_groups(monkeypatch, write=("write",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    assert sandbox_check_tool("write", {"path": str(tmp_path / "a.txt")}, _cfg()) == "allow"
    assert sandbox_check_tool("write", {"path": "D:/elsewhere/x.txt"}, _cfg()) == "confirm"


def test_path_takes_precedence_over_files(tmp_path, monkeypatch):
    """同时存在 path 与 files 时以 path 为准（不因 files 越界而升级）"""
    _fake_groups(monkeypatch, write=("write",))
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    verdict = sandbox_check_tool(
        "write",
        {"path": str(tmp_path / "ok.txt"), "files": ["D:/elsewhere/x.txt"]},
        _cfg(),
    )
    assert verdict == "allow", "path 字段存在时不应再看 files"


def test_sandbox_disabled_passthrough(monkeypatch):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", False)
    assert sandbox_check_tool("fake_array_writer", {"files": ["D:/x"]}, cfg) == "allow"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
