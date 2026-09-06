# -*- coding: utf-8 -*-
"""P1-3：MCP/LSP 子进程启动安全门禁（T8.1 行 9 口径）。

七用例：mcp 审计 / lsp 审计 / args 含 ; 拒启 / lsp 同 / 非内置源 NEED_CONFIRM /
确认后放行且二次免确认 / 内置源免确认。
monkeypatch 拦 subprocess 语义：本组用例只测门禁判定函数（gate_server_launch），
不触达真实子进程创建。
"""
import pytest
from loguru import logger

from app.core import mcp_lsp_safety
from app.core.mcp_lsp_safety import (
    _SYSTEM_PLUGIN_ROOT,
    confirm_plugin_server,
    gate_server_launch,
)


@pytest.fixture()
def log_capture():
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


@pytest.fixture()
def builtin_source():
    """系统插件根下的 .mcp.json 路径（内置源判定）。"""
    return str(_SYSTEM_PLUGIN_ROOT / "system" / ".mcp.json")


def test_mcp_builtin_audit_logged(builtin_source, log_capture):
    """mcp 内置源放行 + [MCPAudit] 审计日志（插件名+server+args 摘要）。"""
    verdict = gate_server_launch(
        "mcp", "system", "fetch", ["npx", "-y", "fetch-mcp"], source=builtin_source
    )
    assert verdict == "proceed"
    audits = [r for r in log_capture if "[MCPAudit]" in r]
    assert any("plugin=system" in r and "server=fetch" in r and "npx" in r for r in audits)


def test_lsp_builtin_audit_logged(builtin_source, log_capture):
    """lsp 内置源放行 + [LSPAudit] 审计日志。"""
    verdict = gate_server_launch(
        "lsp", "system", "pyright-langserver", ["pyright-langserver", "--stdio"],
        source=builtin_source,
    )
    assert verdict == "proceed"
    audits = [r for r in log_capture if "[LSPAudit]" in r]
    assert any("server=pyright-langserver" in r and "--stdio" in r for r in audits)


def test_mcp_args_with_semicolon_denied(builtin_source, log_capture):
    """args 含 shell 元字符（;）→ 拒启 + 原因可见（即使内置源）。"""
    verdict = gate_server_launch(
        "mcp", "system", "evil", ["npx", "-y", "x; rm -rf /"], source=builtin_source
    )
    assert verdict == "denied"
    assert any("shell 元字符" in r and "拒绝启动" in r for r in log_capture)


def test_lsp_args_metachar_denied(builtin_source, log_capture):
    """lsp 同：args 含 ` 与 $() 注入形态 → 拒启。"""
    for bad_args in (["pyright", "`calc`"], ["pyright", "$(curl evil)"], ["py", "-c", "a|b"]):
        verdict = gate_server_launch("lsp", "system", "evil-lsp", bad_args, source=builtin_source)
        assert verdict == "denied", bad_args


def test_non_builtin_source_need_confirm(tmp_path, log_capture):
    """非内置源（用户目录 .mcp.json）首次启动 → need_confirm。"""
    src = str(tmp_path / "my-plug" / ".mcp.json")
    verdict = gate_server_launch("mcp", "my-plug", "fetch", ["npx", "fetch"], source=src)
    assert verdict == "need_confirm"
    assert any("非内置源首次启动" in r and "需用户确认" in r for r in log_capture)
    # 用户拒绝 → 本次会话禁用（denied）
    confirm_plugin_server("mcp", "my-plug", "fetch", allow=False)
    try:
        assert gate_server_launch("mcp", "my-plug", "fetch", ["npx", "fetch"], source=src) == "denied"
    finally:
        mcp_lsp_safety._SESSION_DENIED.discard("mcp:my-plug:fetch")


def test_confirmed_then_proceed_twice(tmp_path, log_capture):
    """确认后放行，二次启动免确认（确认白名单命中）。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    key = "mcp:my-plug:fetch"
    saved = list(cfg.confirmed_plugin_servers.value or [])
    src = str(tmp_path / "my-plug" / ".mcp.json")
    try:
        assert gate_server_launch("mcp", "my-plug", "fetch", ["npx", "fetch"], source=src) == "need_confirm"
        cfg.confirmed_plugin_servers.value = saved + [key]
        assert gate_server_launch("mcp", "my-plug", "fetch", ["npx", "fetch"], source=src) == "proceed"
        # 二次免确认：不再出 need_confirm
        assert gate_server_launch("mcp", "my-plug", "fetch", ["npx", "fetch"], source=src) == "proceed"
        assert sum(1 for r in log_capture if "非内置源首次启动" in r) == 1
    finally:
        cfg.confirmed_plugin_servers.value = saved


def test_builtin_source_skips_confirmation(builtin_source, log_capture):
    """内置源免确认：确认白名单为空也直接放行。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = list(cfg.confirmed_plugin_servers.value or [])
    cfg.confirmed_plugin_servers.value = []
    try:
        verdict = gate_server_launch(
            "mcp", "system", "fetch", ["npx", "-y", "fetch-mcp"], source=builtin_source
        )
        assert verdict == "proceed"
        assert not any("需用户确认" in r for r in log_capture)
    finally:
        cfg.confirmed_plugin_servers.value = saved
