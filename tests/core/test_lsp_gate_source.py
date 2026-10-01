# -*- coding: utf-8 -*-
"""EU-G22：LSP 启动门禁的 source 透传（修复「任何 LSP server 永远无法启动」）

背景：`mcp_lsp_safety.is_builtin_source(source)` 判 source 是否位于系统插件根下。
LSP 侧调用链**全程不传 source** → `is_builtin_source(None)` → 非内置 → 首启
`need_confirm`；而 LSP 侧无确认入口（`confirm_by_key` 仅 MCP 侧有调用）→
**任何 LSP server（含官方 system 插件提供的）永远无法启动**，`lsp` /
`get_diagnostics` 工具静默返回"启动被安全门禁拦截"。

修法：source 从 plugin_manager（读 .lsp.json 的位置）一路透传到 gate_server_launch。
"""

import sys
from pathlib import Path

import pytest

from app.core.tools.mcp_lsp_safety import gate_server_launch

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SYS_PLUGIN_LSP = _REPO_ROOT / "plugins" / "system-tools" / ".lsp.json"
_USER_LSP = Path("C:/Users/someone/.drifox/plugins/pyright-lsp/.lsp.json")


# ── 一、门禁本身的双向判定（不依赖 LSP 侧实现）──


def test_builtin_lsp_source_proceeds():
    """系统插件根下的 source → proceed（核心回归）"""
    verdict = gate_server_launch(
        "lsp", "system-tools", "pyright", ["pyright-langserver", "--stdio"], source=str(_SYS_PLUGIN_LSP)
    )
    assert verdict == "proceed", f"系统插件源应放行，实际 {verdict}"


def test_non_builtin_lsp_source_needs_confirm():
    """用户级插件 source → need_confirm（防修复过度）"""
    verdict = gate_server_launch(
        "lsp", "pyright-lsp", "pyright", ["pyright-langserver", "--stdio"], source=str(_USER_LSP)
    )
    assert verdict == "need_confirm", f"非内置源应要求确认，实际 {verdict}"


def test_no_source_still_needs_confirm():
    """不传 source → 仍 need_confirm（保持原语义，不因本修复而放宽）"""
    verdict = gate_server_launch("lsp", "pyright", "pyright", ["pyright-langserver", "--stdio"])
    assert verdict == "need_confirm"


# ── 二、LSP 侧确实把 source 传进门禁（链路验证）──


def test_lsp_client_passes_source_from_config(monkeypatch):
    """LspClient.start() 必须把 config.source_path 传给 gate_server_launch

    monkeypatch 门禁记录调用参数（在 mcp_lsp_safety 模块上打桩，
    因为 lsp_client 是函数内 import）。
    """
    import app.core.lsp.lsp_client as lc_mod
    import app.core.tools.mcp_lsp_safety as safety_mod

    calls = []

    def _fake_gate(kind, plugin, server, args, source=None):
        calls.append({"kind": kind, "plugin": plugin, "server": server, "source": source})
        return "need_confirm"  # 立刻返回，避免真启动

    monkeypatch.setattr(safety_mod, "gate_server_launch", _fake_gate)

    from app.core.lsp.lsp_config import LspServerConfig

    cfg = LspServerConfig.from_dict(
        "pyright",
        {"command": "pyright-langserver", "args": ["--stdio"]},
        "system-tools",
        source_path=str(_SYS_PLUGIN_LSP),
    )
    client = lc_mod.LspClient(cfg, str(_REPO_ROOT))
    # 直接调 start（不需 pygls 真装：命令可用性检查若失败会早退，
    # 故先 patch 掉 _resolve_command 保证走到门禁）
    monkeypatch.setattr(client, "_resolve_command", lambda: "pyright-langserver")
    import asyncio

    asyncio.run(client.start())

    assert calls, "门禁未被调用（start 提前退出？）"
    assert calls[0]["source"] == str(_SYS_PLUGIN_LSP), f"source 未透传: {calls[0]}"


def test_lsp_client_source_is_empty_when_not_provided(monkeypatch):
    """未提供 source_path 时传到门禁的是空串（→ 门禁判非内置，行为不变）"""
    import app.core.lsp.lsp_client as lc_mod
    import app.core.tools.mcp_lsp_safety as safety_mod

    calls = []
    monkeypatch.setattr(
        safety_mod,
        "gate_server_launch",
        lambda kind, plugin, server, args, source=None: calls.append(source) or "need_confirm",
    )

    from app.core.lsp.lsp_config import LspServerConfig

    cfg = LspServerConfig.from_dict("x", {"command": "x"}, "p")
    client = lc_mod.LspClient(cfg, str(_REPO_ROOT))
    monkeypatch.setattr(client, "_resolve_command", lambda: "x")
    import asyncio

    asyncio.run(client.start())
    assert calls == [""]


# ── 三、配置结构确实带 source_path ──


def test_lsp_config_carries_source_path():
    """LspServerConfig 新增 source_path 字段并可由 from_dict 填充"""
    from app.core.lsp.lsp_config import LspServerConfig

    cfg = LspServerConfig.from_dict("n", {"command": "c"}, "plug", source_path="/x/.lsp.json")
    assert cfg.source_path == "/x/.lsp.json"
    # 默认空串（向后兼容）
    assert LspServerConfig.from_dict("n", {"command": "c"}, "plug").source_path == ""


def test_lsp_manager_passes_source_to_from_dict(monkeypatch):
    """LspManager.initialize 必须把 entry["source"] 传给 from_dict"""
    from app.core.lsp import lsp_config as cfg_mod
    from app.core.lsp import lsp_manager as mgr_mod

    captured = []
    orig = cfg_mod.LspServerConfig.from_dict

    def _spy(cls, name, data, plugin_name="", source_path=""):
        captured.append(source_path)
        return orig(name, data, plugin_name, source_path)

    monkeypatch.setattr(cfg_mod.LspServerConfig, "from_dict", classmethod(_spy))

    mgr = mgr_mod.LspManager.__new__(mgr_mod.LspManager)
    mgr._clients = {}
    mgr._ext_map = {}
    mgr._initialized = False
    mgr._loop = None
    mgr._workspace_root = str(_REPO_ROOT)

    mgr.initialize(
        str(_REPO_ROOT),
        [{"plugin": "system-tools", "config": {"s1": {"command": "c"}}, "source": "/p/.lsp.json"}],
    )
    assert captured == ["/p/.lsp.json"], f"source 未传入 from_dict: {captured}"


def test_lsp_manager_add_plugin_servers_passes_source(monkeypatch):
    """增量路径 add_plugin_servers 同样透传 source（热重载场景）"""
    from app.core.lsp import lsp_config as cfg_mod
    from app.core.lsp import lsp_manager as mgr_mod

    captured = []
    orig = cfg_mod.LspServerConfig.from_dict

    def _spy(cls, name, data, plugin_name="", source_path=""):
        captured.append(source_path)
        return orig(name, data, plugin_name, source_path)

    monkeypatch.setattr(cfg_mod.LspServerConfig, "from_dict", classmethod(_spy))

    mgr = mgr_mod.LspManager.__new__(mgr_mod.LspManager)
    mgr._clients = {}
    mgr._ext_map = {}
    mgr._workspace_root = str(_REPO_ROOT)
    # 事件循环由桩提供（真 _ensure_loop 会起 daemon 线程，测试无需）
    mgr._loop = object()
    monkeypatch.setattr(mgr, "_ensure_loop", lambda: None)
    # 后台启动走 run_coroutine_threadsafe(client.start(), loop)，loop 是桩会抛异常
    # → 由实现内 try/except 吞掉，不影响 from_dict 已被调用的断言
    monkeypatch.setattr(
        mgr_mod, "asyncio", type("A", (), {"run_coroutine_threadsafe": staticmethod(lambda *a, **k: None)})
    )

    mgr.add_plugin_servers("plug", {"s2": {"command": "c"}}, source_path="/q/.lsp.json")
    assert captured == ["/q/.lsp.json"]


# ── 四、plugin_manager 返回结构带 source（含热重载路径）──


def test_plugin_manager_source_fields_present():
    """源码级：两处 get_lsp_configs append + get_plugin_lsp_config return 均含 source"""
    src = (_REPO_ROOT / "app" / "plugins" / "managers" / "plugin_manager.py").read_text(encoding="utf-8")
    assert src.count('"source": str(lsp_file)') >= 3, "三处（主扫描/兜底扫描/单插件）都应带 source"


# ── 五、CLI fallback 也透传（此处原为「双重失效」：plugin_name 空 + 无 source）──


def test_cli_fallback_passes_source():
    """源码级：CLI fallback 三处门禁调用都必须带 source=source_path"""
    src = (_REPO_ROOT / "app" / "core" / "lsp" / "lsp_manager.py").read_text(encoding="utf-8")
    assert src.count("source=source_path") >= 3, "CLI fallback 三处都应透传 source"
    assert 'source_path: str = ""' in src, "CLI 函数应带 source_path 参数"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专用探针")
def test_lsp_setting_card_has_confirm_entry():
    """源码级：LSP 设置卡必须有 confirm_by_key 调用（此前零命中 → 用户插件永远启不了）"""
    src = (_REPO_ROOT / "app" / "widgets" / "cards" / "settings" / "lsp_setting_card.py").read_text(encoding="utf-8")
    assert "confirm_by_key" in src, "LSP 确认入口缺失"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
