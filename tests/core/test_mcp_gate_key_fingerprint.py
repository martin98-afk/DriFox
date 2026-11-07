# -*- coding: utf-8 -*-
"""回归测试：门禁指纹 key 与卡片 _mcp_gate_key 必须同口径（含 stdio 命令回落）。

根因（2026-11-05 browser 插件连接风暴）：.mcp.json 声明 command=python3，
_lifespan_stdio 在门禁之前先 _resolve_stdio_command 回落（python3→python）再算
指纹；卡片 _mcp_gate_key 用原始声明算指纹 → 两侧 key 错位，三处全坏：
1. refresh_connections 的 is_pending_confirm_by_key 守卫永不命中 → 每次热重载
   广播（watchfiles 400ms 防抖）都对同一台服务器发起注定被拦的连接，ERROR 刷屏；
2. 确认弹窗「允许启动」confirm_by_key 写入错位 key → 门禁校验永不命中，确认死路；
3. 「本次拒绝」写错 key → 拒绝无效。

修复：卡片 _mcp_gate_key 复刻同一回落后再算指纹。本组用例锁定：
- 回落场景下守卫命中（真实 gate_server_launch 置位，非手工 add legacy key）；
- 无回落场景同样命中；
- 用卡片 key 确认后门禁放行（确认闭环）。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.tools import mcp_lsp_safety
from app.core.tools.mcp_lsp_safety import (
    confirm_by_key,
    gate_server_launch,
    is_pending_confirm_by_key,
)
from app.tools.mcp_tools import _resolve_stdio_command
from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard


@pytest.fixture(autouse=True)
def _no_settings_disk_write(monkeypatch):
    """确认白名单写入禁止落盘，只改内存态（防污染真实 app.config）。"""
    from app.utils.config import Settings

    monkeypatch.setattr(Settings, "set", lambda self, attr, value, save=False: setattr(attr, "value", value))


class _FakeCfg:
    def __init__(self, enabled: bool):
        self.mcp_enabled = SimpleNamespace(value=enabled)


class _FakeMgr:
    def __init__(self, status_list):
        self._status_list = status_list
        self._connect_calls = []

    def get_status(self):
        return list(self._status_list)

    def connect_server_background(self, name, config, on_done=None):
        self._connect_calls.append((name, config))


def _make_card(enabled=True, servers=None):
    """构造不初始化 Qt 的 MCPListSettingCard（与 test_mcp_hot_reload_connect 同手法）"""
    card = MCPListSettingCard.__new__(MCPListSettingCard)
    card.cfg = _FakeCfg(enabled)
    card._get_servers = lambda: list(servers or [])
    card._get_mcp_manager = lambda: _FakeMgr([])
    card._confirm_bars = {}
    return card


def _user_source(tmp_path):
    return str(tmp_path / "browser" / ".mcp.json")


def _gate_launch_like_lifespan(source, command, args):
    """复刻 _lifespan_stdio 的门禁调用口径：先回落再传实际执行命令。"""
    cmd, resolved_args = _resolve_stdio_command(command, list(args or []))
    return (
        gate_server_launch(
            "mcp",
            "browser",
            "browser",
            [cmd] + resolved_args,
            source=source,
            command=cmd,
        ),
        cmd,
    )


def test_fallback_scenario_guard_hits(tmp_path):
    """command=python3 触发回落时：门禁置位后卡片守卫 key 必须命中。"""
    src = _user_source(tmp_path)
    srv = {
        "name": "browser",
        "type": "stdio",
        "command": "python3",
        "args": ["mcp/server.py"],
        "enabled": True,
        "_source": src,
    }
    verdict, _cmd = _gate_launch_like_lifespan(src, "python3", ["mcp/server.py"])
    assert verdict == "need_confirm"
    card = _make_card(servers=[srv])
    # 修复点：卡片 key 必须与门禁置位 key 同指纹（回落对齐）
    assert is_pending_confirm_by_key(card._mcp_gate_key("browser")) is True


def test_no_fallback_guard_hits(tmp_path):
    """无回落场景（echo 不在回落表）：守卫同样命中。"""
    src = _user_source(tmp_path)
    srv = {
        "name": "browser",
        "type": "stdio",
        "command": "echo",
        "args": ["server.py"],
        "enabled": True,
        "_source": src,
    }
    verdict, _cmd = _gate_launch_like_lifespan(src, "echo", ["server.py"])
    assert verdict == "need_confirm"
    card = _make_card(servers=[srv])
    assert is_pending_confirm_by_key(card._mcp_gate_key("browser")) is True


def test_confirm_via_card_key_unblocks_gate(tmp_path):
    """确认闭环：用卡片 key 提交 allow 后，门禁按实际执行命令校验放行。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = list(cfg.confirmed_plugin_servers.value or [])
    src = _user_source(tmp_path)
    srv = {
        "name": "browser",
        "type": "stdio",
        "command": "python3",
        "args": ["mcp/server.py"],
        "enabled": True,
        "_source": src,
    }
    try:
        verdict, cmd = _gate_launch_like_lifespan(src, "python3", ["mcp/server.py"])
        assert verdict == "need_confirm"
        card = _make_card(servers=[srv])
        key = card._mcp_gate_key("browser")
        confirm_by_key(key, allow=True)
        # 门禁二次判定（_lifespan_stdio 同口径）→ proceed，确认不再死路
        verdict2, _ = _gate_launch_like_lifespan(src, "python3", ["mcp/server.py"])
        assert verdict2 == "proceed"
    finally:
        cfg.confirmed_plugin_servers.value = saved


def test_deny_via_card_key_blocks_session(tmp_path):
    """拒绝闭环：用卡片 key 提交 deny 后，门禁同会话判 denied 而非再次 need_confirm。"""
    src = _user_source(tmp_path)
    srv = {
        "name": "browser",
        "type": "stdio",
        "command": "python3",
        "args": ["mcp/server.py"],
        "enabled": True,
        "_source": src,
    }
    try:
        verdict, _ = _gate_launch_like_lifespan(src, "python3", ["mcp/server.py"])
        assert verdict == "need_confirm"
        card = _make_card(servers=[srv])
        confirm_by_key(card._mcp_gate_key("browser"), allow=False)
        verdict2, _ = _gate_launch_like_lifespan(src, "python3", ["mcp/server.py"])
        assert verdict2 == "denied"
    finally:
        mcp_lsp_safety._SESSION_DENIED.discard("mcp:browser:browser")
        mcp_lsp_safety._PENDING_CONFIRM.discard("mcp:browser:browser")
