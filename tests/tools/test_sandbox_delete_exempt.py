# -*- coding: utf-8 -*-
"""删除豁免（delete.exempt_paths）与删除目标解析（delete_targets）测试

覆盖：豁免判定（全在豁免内→True / 部分在外→False / 无豁免配置→False /
无目标可解析→False）、相对豁免条目按 workdir 解析、sandbox_check_tool
端到端降级（CONFIRM→ALLOW）、豁免外删除仍审批、删除目标解析明细。
"""

from app.tools.sandbox import SandboxConfig, delete_exempt, delete_targets, sandbox_check_tool


def _make_cfg(tmp_path, exempt):
    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("delete.exempt_paths", exempt)
    return cfg


def test_delete_targets_splits_existing_and_missing(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    ghost = tmp_path / "ghost.txt"
    result = delete_targets(f"rm {target} {ghost}")
    assert result["existing"] == [target]
    assert result["missing"] == [str(ghost)]


def test_delete_targets_non_delete_command_empty(tmp_path):
    assert delete_targets("git status") == {"existing": [], "missing": []}


def test_exempt_all_targets_inside(tmp_path):
    """删除目标全部在豁免路径内 → 豁免成立"""
    tests_dir = tmp_path / "tests"
    (tests_dir / "sub").mkdir(parents=True)
    cfg = _make_cfg(tmp_path, [str(tests_dir)])
    assert delete_exempt(f"rm {tests_dir / 'sub' / 'old.py'} {tests_dir / 'x.txt'}", cfg)


def test_exempt_partial_outside_not_exempt(tmp_path):
    """任一目标在豁免外 → 不豁免（宁多问一次）"""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    outside = tmp_path / "app" / "main.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("x", encoding="utf-8")
    cfg = _make_cfg(tmp_path, [str(tests_dir)])
    assert not delete_exempt(f"rm {tests_dir / 'old.py'} {outside}", cfg)


def test_exempt_no_config_false(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    cfg = _make_cfg(tmp_path, [])
    assert not delete_exempt(f"rm {f}", cfg)


def test_exempt_unresolvable_targets_false(tmp_path):
    """命令里解析不出任何目标（如纯 flag 异常形态）→ 不豁免"""
    cfg = _make_cfg(tmp_path, [str(tmp_path)])
    assert not delete_exempt("rm -rf", cfg)


def test_sandbox_check_tool_downgrades_confirmed_delete(tmp_path):
    """端到端：豁免路径内的 rm 在 worker 入口降级为 allow"""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("delete.exempt_paths", [str(tmp_path)])

    verdict = sandbox_check_tool(
        "terminal",
        {"command": f"rm {tests_dir / 'old_case.py'}"},
        cfg,
    )
    assert verdict == "allow"


def test_sandbox_check_tool_still_confirms_outside_delete(tmp_path):
    """豁免路径外的删除仍走审批

    ⚠ EU-G23 起必须显式开启 delete_protection：删除类命令的 confirm 由该开关
    门控（关闭时仅取消"因删除命令而 confirm"这一层，降级为 allow）。
    本用例验证的是"豁免路径外"，与开关无关，故显式置 True 以隔离关注点。
    """
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    outside_dir = tmp_path / "app"
    outside_dir.mkdir()
    SandboxConfig.reset_instance()
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("delete_protection", True)  # EU-G23：删除保护开关门控删除类 confirm
    cfg.set("delete.exempt_paths", [str(tests_dir)])

    verdict = sandbox_check_tool(
        "terminal",
        {"command": f"rm {outside_dir / 'keep.py'}"},
        cfg,
    )
    assert verdict == "confirm"


def test_relative_exempt_entry_resolved_against_workdir(tmp_path, monkeypatch):
    """相对豁免条目（tests/）按当前有效 workdir 解析"""
    from app.tools import sandbox as sb

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "old.py").write_text("x", encoding="utf-8")
    cfg = _make_cfg(tmp_path, ["tests/"])
    monkeypatch.setattr(sb, "_current_workdir", lambda: tmp_path)
    assert delete_exempt(f"rm {tests_dir / 'old.py'}", cfg)
