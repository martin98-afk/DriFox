# -*- coding: utf-8 -*-
"""EU-G31：命令类工具的黑名单定向检查（方案丁）

背景：`sandbox_check_tool` 的 command 分支原**不查路径**，导致 `path.blacklist`
对命令类工具完全失效 —— AI 用 `type D:/secrets/key.pem` 可静默读取敏感目录，
而 `CONFIRM_COMMANDS` 的条目全是写类/系统类命令，一个读取类都没有。

设计（方案丁，采纳 plan 洞察）：`check_path` 的 read 模式**天然只检查黑名单**
（其余无条件 allow），故统一传 `"read"` 即可获得「黑名单严格 + 其他全放行」语义，
**无需 mode 推断**（rm=write / type=read / git、npm 无法静态判定），且 read 模式
不判 workdir 边界 → 零误报。
"""

import pytest

from app.tools.sandbox import SandboxConfig, sandbox_check_tool

SECRET = "D:/secrets"


def _cfg(blacklist=None, exempt=None, enabled=True):
    cfg = SandboxConfig(config_path=":memory:")
    cfg.set("sandbox_enabled", enabled)
    cfg.set("path.blacklist", blacklist or [])
    cfg.set("delete.exempt_paths", exempt or [])
    return cfg


# ── 命中黑名单 → confirm ──


def test_blacklist_blocks_read_command():
    """`type` 读取黑名单目录 → confirm（修复前是 allow）"""
    verdict = sandbox_check_tool("bash", {"command": f"type {SECRET}/k.pem"}, _cfg(blacklist=[SECRET]))
    assert verdict == "confirm", "读取类命令必须受黑名单约束"


def test_blacklist_blocks_cat():
    verdict = sandbox_check_tool("bash", {"command": f"cat {SECRET}/k.pem"}, _cfg(blacklist=[SECRET]))
    assert verdict == "confirm"


def test_blacklist_blocks_powershell_read():
    verdict = sandbox_check_tool(
        "bash",
        {"command": f'powershell -c "Get-Content {SECRET}/k.pem"'},
        _cfg(blacklist=[SECRET]),
    )
    assert verdict == "confirm"


def test_blacklist_blocks_write_redirect():
    """重定向写入黑名单目录 → confirm"""
    verdict = sandbox_check_tool("bash", {"command": f"echo hi > {SECRET}/x.txt"}, _cfg(blacklist=[SECRET]))
    assert verdict == "confirm"


# ── 零误报回归 ──


def test_blacklist_does_not_block_normal_command():
    """普通命令不受影响（零误报）"""
    verdict = sandbox_check_tool("bash", {"command": "git status"}, _cfg(blacklist=[SECRET]))
    assert verdict == "allow"


def test_blacklist_does_not_block_unrelated_path():
    """非黑名单路径的命令仍放行"""
    verdict = sandbox_check_tool("bash", {"command": "type D:/work/proj/readme.md"}, _cfg(blacklist=[SECRET]))
    assert verdict == "allow"


def test_blacklist_flags_ignored():
    """纯 flag token（-rf / --verbose）不得被当路径判定"""
    verdict = sandbox_check_tool("bash", {"command": "ls -la --color=auto"}, _cfg(blacklist=[SECRET]))
    assert verdict == "allow"


# ── 幂等：默认空黑名单零影响 ──


def test_blacklist_empty_no_change():
    """黑名单为空（默认）→ 行为与改造前完全一致"""
    verdict = sandbox_check_tool("bash", {"command": f"type {SECRET}/k.pem"}, _cfg(blacklist=[]))
    assert verdict == "allow", "未配置黑名单时不得改变行为"


# ── 顺序：豁免优先于黑名单 ──


def test_exempt_takes_precedence_over_blacklist():
    """同时配 delete.exempt_paths 与 path.blacklist → 豁免路径内删除仍 allow

    顺序设计：delete_exempt 判定在黑名单检查之前（用户显式豁免优先）。
    ⚠ EU-G23 起需显式开启 delete_protection，否则删除类命令先被门控降级为
    allow，测不到"豁免优先于黑名单"这一层。
    """
    cfg = _cfg(blacklist=[SECRET], exempt=[SECRET])
    cfg.set("delete_protection", True)
    verdict = sandbox_check_tool("bash", {"command": f"rm {SECRET}/old.txt"}, cfg)
    assert verdict == "allow", "显式豁免应优先于黑名单"


def test_blacklist_still_applies_outside_exempt():
    """豁免路径**外**的同类命令仍受黑名单约束"""
    cfg = _cfg(blacklist=[SECRET], exempt=["D:/other"])
    verdict = sandbox_check_tool("bash", {"command": f"type {SECRET}/k.pem"}, cfg)
    assert verdict == "confirm"


# ── 与 G28 边界修复协同（复用 _match_blacklist，无第二套逻辑）──


def test_blacklist_boundary_respected_in_command_branch():
    """黑名单边界（G28 已修）在命令分支同样生效：secret 不误命中 secret2"""
    cfg = _cfg(blacklist=["D:/work/proj/secret"])
    verdict = sandbox_check_tool("bash", {"command": "type D:/work/proj/secret2/b.txt"}, cfg)
    assert verdict == "allow", "不该因前缀相似而误命中（复用 _match_blacklist 的边界判定）"


def test_sandbox_disabled_passthrough():
    verdict = sandbox_check_tool("bash", {"command": f"type {SECRET}/k.pem"}, _cfg(blacklist=[SECRET], enabled=False))
    assert verdict == "allow"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
