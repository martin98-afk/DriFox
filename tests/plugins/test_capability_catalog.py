# -*- coding: utf-8 -*-
"""能力目录：53 条定义完整性 / 命名空间 / 别名 / 通配展开 / 旧键映射。"""
import re

import pytest


def test_catalog_size_and_ids():
    from app.plugins.capabilities import catalog

    assert len(catalog.CATALOG) == 58
    for cid, spec in catalog.CATALOG.items():
        assert spec.id == cid
        assert re.fullmatch(r"[a-z]+(\.[a-z][a-z_]*)+", cid), cid
        assert spec.group == cid.split(".")[0], cid
        assert spec.summary.strip(), cid


def test_every_surface_valid():
    from app.plugins.capabilities import catalog

    for cid, spec in catalog.CATALOG.items():
        assert spec.surfaces, cid
        assert spec.surfaces <= {"ui", "tool"}, cid


def test_high_level_set_is_stable():
    """高危能力的集合变更必须显式改测试（防止悄悄降级）。"""
    from app.plugins.capabilities import catalog
    from app.plugins.contracts.capabilities import CapabilityLevel

    high = {cid for cid, s in catalog.CATALOG.items() if s.level is CapabilityLevel.HIGH}
    assert high == {
        "model.config",
        "session.manage",
        "tool.inject",
        "tool.execute",
        "subagent.run",
        "engine.session",
        "engine.stack",
        "platform.send",
        "mcp.call",
        "net.upload",
        "sync.register",
        "plugin.control",
        "host.raw",
        "process.start",
    }


def test_legacy_map_targets_exist():
    from app.plugins.capabilities import catalog

    for old, cap in catalog.LEGACY_MAP.items():
        assert cap in catalog.CATALOG, f"{old} → {cap} 不在 catalog"


def test_legacy_map_covers_engine_host_keys():
    """EngineHost 契约的 20 个服务键全部有旧键映射（P3 投影的前提）。"""
    from app.plugins.capabilities import catalog

    expected = {
        "get_model_config",
        "get_provider_config",
        "get_tool_executor",
        "get_agent_manager",
        "get_agent_prompt",
        "get_tools_schema",
        "set_workdir",
        "get_workdir",
        "get_compactor",
        "conversation_stack",
        "create_engine_session",
        "save_messages_to_session",
        "enter_exclusive_ui_mode",
        "exit_exclusive_ui_mode",
        "hide_card",
        "sync_working_directory",
        "notify",
        "send_to_platform",
        "list_platform_sessions",
        "list_platforms",
    }
    assert expected <= set(catalog.LEGACY_MAP)


def test_expand_wildcards_and_aliases():
    from app.plugins.capabilities import catalog

    assert catalog.expand(["tool.*"]) == {
        "tool.read",
        "tool.inject",
        "tool.execute",
    }
    assert catalog.expand(["*"]) == set(catalog.CATALOG)
    # 旧扁平名当别名：存量插件写 ["lsp"] 仍可用
    assert catalog.expand(["lsp"]) == {"lsp.query"}
    assert catalog.expand([]) == frozenset()
    assert catalog.expand(["session.read", "session.read"]) == {"session.read"}


def test_get_unknown_returns_none():
    from app.plugins.capabilities import catalog

    assert catalog.get("nope.nope") is None
    assert catalog.get("session.read").group == "session"


def test_unknown_declared_flags_typos_and_stale_ids():
    """加载期语义校验：typo / 已删除能力 / 不存在的组通配被挑出。"""
    from app.plugins.capabilities import catalog

    # 正常 ID + 别名 + 通配 + 组通配全部放行
    assert catalog.unknown_declared(["model.config", "lsp", "*", "ui.*"]) == []
    # typo 与已删除能力（ui.control 已从 catalog 移除）
    assert catalog.unknown_declared(["session.Read", "ui.control"]) == [
        "session.Read",
        "ui.control",
    ]
    # 不存在的组
    assert catalog.unknown_declared(["badgroup.*"]) == ["badgroup.*"]
    # 非法类型 / 空值安全
    assert catalog.unknown_declared(None) == []
    assert catalog.unknown_declared("model.config") == []
    assert catalog.unknown_declared([]) == []
