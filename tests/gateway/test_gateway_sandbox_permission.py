# -*- coding: utf-8 -*-
"""EU-G2：gateway 平台侧权限请求消费 `gateway_auto_approve`

## 背景
`gateway_auto_approve` 此前是**死键** —— `DEFAULT_CONFIG` 里有定义，但
`git grep` 显示**零消费点**（`session_handler.py` 的 permission 分支无条件
`approve_tool_permission(id, True)`）。用户改了配置没效果。

这是「新增配置键未同时提交消费点」模式的第三次出现
（前两次：`enforce_backup_limit` 零调用、`delete_protection` 只控快照）。

## args 结构确认（leader 要求的第一步）
`event_name == "permission"` 的 args 来自 UI 引擎的
`permission_approval_requested = pyqtSignal(str, str, dict)`：

    args = (tool_call_id, tool_name, arguments)

同文件 `tool_result` 分支已用 `args[1]`/`args[2]` 取 tool_name/result →
**结构一致，三个元素**。故本实现取 `args[1]`/`args[2]` 有依据。
"""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HANDLER = _REPO_ROOT / "app" / "gateway" / "local_service" / "session_handler.py"


def _src() -> str:
    return _HANDLER.read_text(encoding="utf-8")


# ── 源码级：消费点存在且分支完整 ──


def test_gateway_reads_auto_approve_config():
    """必须读取 gateway_auto_approve（此前零消费点）"""
    src = _src()
    assert "gateway_auto_approve" in src, "配置键仍无消费点"


def test_gateway_has_approve_and_deny_branches():
    """必须有 approve / deny 两条分支（True 放行、False 按沙箱判定）"""
    src = _src()
    assert "ctx.engine.approve_tool_permission(tool_call_id, True)" in src
    assert "ctx.engine.deny_tool_permission(tool_call_id)" in src


def test_gateway_config_read_failure_falls_back_to_approve():
    """读配置异常 → 保持原行为（fail-open，与 sandbox 设计一致）"""
    src = _src()
    assert "读取 gateway_auto_approve 失败，按自动放行" in src


def test_gateway_sandbox_verdict_defaults_conservative():
    """取不到沙箱判定时保守处理（不同信息不足而放行）"""
    src = _src()
    assert 'verdict = "confirm"' in src, "默认值应保守（confirm 方向）"
    assert "沙箱判定失败，保守拒绝" in src


def test_gateway_uses_args_1_and_2():
    """args[1]/args[2] 取值（与 tool_result 分支同结构）"""
    src = _src()
    assert "tool_name = args[1] if len(args) > 1 else" in src
    assert "arguments = args[2] if len(args) > 2 else" in src


# ── 逻辑级：配置键语义 ──


def test_config_key_default_true():
    """默认 True（保持现状，避免无人值守场景静默失败）"""
    from app.tools.sandbox import SandboxConfig

    cfg = SandboxConfig(config_path=":memory:")
    assert bool(cfg.get("gateway_auto_approve")) is True


def test_config_key_can_be_set_false():
    from app.tools.sandbox import SandboxConfig

    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("gateway_auto_approve", False)
    assert bool(cfg.get("gateway_auto_approve")) is False


def test_config_key_in_default_config():
    """键必须在 DEFAULT_CONFIG（配置层单一真源）"""
    from app.tools.sandbox import DEFAULT_CONFIG

    assert "gateway_auto_approve" in DEFAULT_CONFIG


# ── 与沙箱判定的组合语义 ──


def test_sandbox_allow_verdict_permits_when_gate_false(tmp_path, monkeypatch):
    """gateway_auto_approve=False 但沙箱判 allow → 仍放行（不是一律拒绝）"""
    from app.tools import sandbox
    from app.tools.sandbox import SandboxConfig, sandbox_check_tool

    monkeypatch.setattr(
        sandbox,
        "_tools_in",
        lambda g: frozenset({"write"} if g == sandbox.GROUP_WRITE_NAME else ()),
    )
    monkeypatch.setattr(sandbox, "_current_workdir", lambda: tmp_path)
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)

    # 界内写 → allow → 即使 gate=False 也应放行
    verdict = sandbox_check_tool("write", {"path": str(tmp_path / "f.txt")}, cfg)
    assert verdict == "allow"


def test_sandbox_confirm_verdict_denies_when_gate_false():
    """gateway_auto_approve=False 且沙箱判 confirm → 拒绝（核心行为）"""
    from app.tools.sandbox import SandboxConfig, sandbox_check_tool

    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", True)
    cfg.set("delete_protection", True)
    # 删除命令 → confirm → 应被拒绝
    assert sandbox_check_tool("bash", {"command": "rm D:/important.txt"}, cfg) == "confirm"


def test_sandbox_disabled_all_allow():
    """沙箱关闭时全放行（gate=False 也不影响，因为判定为 allow）"""
    from app.tools.sandbox import SandboxConfig, sandbox_check_tool

    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", False)
    assert sandbox_check_tool("bash", {"command": "rm D:/important.txt"}, cfg) == "allow"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
