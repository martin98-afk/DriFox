# -*- coding: utf-8 -*-
"""能力目录：74 条定义完整性 / 命名空间 / 别名 / 通配展开 / 旧键映射。"""
import re

import pytest


def test_catalog_size_and_ids():
    from app.plugins.capabilities import catalog

    assert len(catalog.CATALOG) == 92
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
        "engine.session.resume",
        # 既有漏登（HIGH 但先前未进守护集合），随本次能力补件一并登记
        "session.title.commit",
        "engine.stack",
        "platform.send",
        "mcp.call",
        "net.upload",
        "sync.register",
        "plugin.control",
        "host.raw",
        "process.start",
        # 2026-10-29 历史/项目/团队会话面（一次性收敛 history-manager 的宿主耦合）
        "session.history.write",
        "project.write",
        "team.session",
        # 2026-11-01 子智能体插件抽离：模型解析与通用 DB 执行面
        "model.config_for",
        "storage.db",
        # 2026-11-09 能力池 model.set：改窗口模型实例态，高危需显式登记
        "model.set",
        # 2026-11-09 补登记：此前 catalog 已有但守护测试漏登的既有高危项
        "team.manage",
        "team.spawn",
        # 2026-11-12 能力池补件：通用 HTTP 请求（数据外发），及此前漏登的 reload_components
        "net.http",
        "plugin.reload_components",
        # 2026-11-12 运行时工具注册：等于让 LLM 能调插件代码
        "tool.register",
        # 2026-11-13 IM 网关插件化：服务/平台连接控制面
        "platform.control",
    }


def test_legacy_map_targets_exist():
    from app.plugins.capabilities import catalog

    for old, cap in catalog.LEGACY_MAP.items():
        assert cap in catalog.CATALOG, f"{old} → {cap} 不在 catalog"


def test_legacy_map_covers_engine_host_keys():
    """EngineHost 契约仍受支持的服务键全部有旧键映射（P3 投影的前提）。

    get_model_config / get_provider_config / get_tools_schema 已退役：自家插件
    已全部迁移到命名空间能力（model.config / model.schema），投影不再提供。
    """
    from app.plugins.capabilities import catalog

    expected = {
        "get_tool_executor",
        "get_agent_manager",
        "get_agent_prompt",
        "set_workdir",
        "get_workdir",
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
        "tool.register",
        "tool.unregister",
    }
    assert catalog.expand(["*"]) == set(catalog.CATALOG)
    # A3 时代扁平名别名已退役：不再自动展开，原样保留（池构建时不可用）
    assert catalog.expand(["lsp"]) == {"lsp"}
    assert catalog.expand([]) == frozenset()
    assert catalog.expand(["session.read", "session.read"]) == {"session.read"}


def test_get_unknown_returns_none():
    from app.plugins.capabilities import catalog

    assert catalog.get("nope.nope") is None
    assert catalog.get("session.read").group == "session"


def test_unknown_declared_flags_typos_and_stale_ids():
    """加载期语义校验：typo / 已删除能力 / 不存在的组通配被挑出。"""
    from app.plugins.capabilities import catalog

    # 正常 ID + 通配 + 组通配全部放行（A3 扁平名别名已退役，lsp 不再放行）
    assert catalog.unknown_declared(["model.config", "*", "ui.*"]) == []
    # typo、已删除能力（ui.control 已从 catalog 移除）与已退役扁平名
    assert catalog.unknown_declared(["session.Read", "ui.control", "lsp"]) == [
        "session.Read",
        "ui.control",
        "lsp",
    ]
    # 不存在的组
    assert catalog.unknown_declared(["badgroup.*"]) == ["badgroup.*"]
    # 非法类型 / 空值安全
    assert catalog.unknown_declared(None) == []
    assert catalog.unknown_declared("model.config") == []
    assert catalog.unknown_declared([]) == []
