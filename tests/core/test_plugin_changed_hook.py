# -*- coding: utf-8 -*-
"""PluginChanged hook 事件测试

覆盖：
- matcher 按子事件 action pipe 匹配（installed|enabled 等）
- trigger_plugin_changed_hook 全局触发辅助（diff 附加 + 无 backend 静默）
- PluginHostService._infer_plugin_changed_action 推断
"""

import pytest

from app.core.hooks.hook_manager import HookMatchRule, HookManager


def _rule(matcher: str) -> HookMatchRule:
    return HookMatchRule(matcher=matcher)


class TestPluginChangedMatcher:
    """matcher 按 context["action"] pipe 匹配"""

    def _ctx(self, action: str) -> dict:
        return {"event_name": "PluginChanged", "action": action}

    def test_single_action_match(self):
        assert _rule("installed").matches(self._ctx("installed")) is True
        assert _rule("installed").matches(self._ctx("updated")) is False

    def test_pipe_multi_action(self):
        rule = _rule("installed|enabled|mcp_failed")
        assert rule.matches(self._ctx("enabled")) is True
        assert rule.matches(self._ctx("mcp_failed")) is True
        assert rule.matches(self._ctx("uninstalled")) is False

    def test_empty_matcher_matches_all(self):
        assert _rule(None).matches(self._ctx("anything")) is True
        assert _rule("").matches(self._ctx("anything")) is True

    def test_empty_action_no_match(self):
        """context 缺 action（异常场景）时非空 matcher 不匹配"""
        assert _rule("installed").matches({"event_name": "PluginChanged"}) is False

    def test_sub_actions_match(self):
        """工具级子动作（sub_actions）匹配：matcher 可精确到工具增删改"""
        rule = _rule("tools_added|tools_removed|tools_updated")
        # action 未命中但 sub_actions 命中 → 触发
        assert rule.matches({"event_name": "PluginChanged", "action": "enabled", "sub_actions": ["tools_added"]}) is True
        assert rule.matches({"event_name": "PluginChanged", "action": "installed", "sub_actions": ["tools_updated"]}) is True
        # 仅 MCP 变化（子动作不在 matcher）→ 不触发
        assert rule.matches({"event_name": "PluginChanged", "action": "enabled", "sub_actions": ["mcp_added"]}) is False
        # action 与 matcher 都不相关 → 不触发
        assert rule.matches({"event_name": "PluginChanged", "action": "installed", "sub_actions": []}) is False

    def test_action_and_sub_actions_mixed(self):
        """插件级 action 与子动作混合 matcher"""
        rule = _rule("installed|tools_removed")
        assert rule.matches({"event_name": "PluginChanged", "action": "installed"}) is True
        assert rule.matches({"event_name": "PluginChanged", "action": "updated", "sub_actions": ["tools_removed"]}) is True
        assert rule.matches({"event_name": "PluginChanged", "action": "updated", "sub_actions": []}) is False


class TestTriggerPluginChangedHook:
    """全局触发辅助：无 backend / 无注册 hook 静默，diff 基线刷新"""

    def test_no_backend_silent(self, monkeypatch):
        """无活跃 backend 时不抛异常（快照基线仍刷新）"""
        from app.core.hooks import hook_manager as hm
        from app.core.conversation.backend import ChatBackend

        monkeypatch.setattr(ChatBackend, "_active_instances", [])
        hm.trigger_plugin_changed_hook({"action": "mcp_added", "server_name": "x"})
        # 首次调用建立基线
        assert hm._plugin_snapshot_tools is not None

    def test_no_registered_hook_skips_trigger(self, monkeypatch):
        """PluginChanged 无注册 hook 时不投递线程池（基线仍刷新）"""
        from app.core.hooks import hook_manager as hm
        from app.core.conversation.backend import ChatBackend

        submitted: list = []
        monkeypatch.setattr(
            hm, "_get_parallel_executor", lambda: type("E", (), {"submit": staticmethod(lambda f, *a: submitted.append(a))})()
        )
        fake_hm = type("FakeHookManager", (), {"_hooks": {}})()
        fake_backend = type("B", (), {"_hook_manager": fake_hm})()
        monkeypatch.setattr(ChatBackend, "_active_instances", [fake_backend])
        hm.trigger_plugin_changed_hook({"action": "enabled"})
        assert submitted == []


class TestInferAction:
    """PluginHostService._infer_plugin_changed_action 推断"""

    def test_sentinel_is_installed(self):
        from app.core.services.plugin_host_service import PluginHostService

        svc = PluginHostService.__new__(PluginHostService)
        assert svc._infer_plugin_changed_action(PluginHostService._NEW_PLUGIN_SENTINEL) == "installed"

    def test_empty_name_is_updated(self):
        from app.core.services.plugin_host_service import PluginHostService

        svc = PluginHostService.__new__(PluginHostService)
        assert svc._infer_plugin_changed_action("") == "updated"

    def test_missing_plugin_is_uninstalled(self, monkeypatch):
        from app.core.services.plugin_host_service import PluginHostService

        svc = PluginHostService.__new__(PluginHostService)
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.get_instance",
            staticmethod(lambda: type("PM", (), {"has_plugin": staticmethod(lambda n: False)})()),
        )
        assert svc._infer_plugin_changed_action("gone") == "uninstalled"


class TestFormatToolChangesSchema:
    """注入 hook 的 schema 获取：本地工具必须注入完整 schema，禁止误标 MCP 工具

    回归背景：hook 经 caps_for 走 manifest 声明裁剪，system-hooks 一旦漏声明
    tool.read，所有 tools_added 的 schema 都查不到，全部退化成「MCP 工具」回退文案。
    """

    @pytest.fixture()
    def hook_module(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "plugins" / "system-hooks" / "hooks" / "format_tool_changes.py"
        spec = importlib.util.spec_from_file_location("format_tool_changes_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @pytest.fixture()
    def real_manifest_plugin_manager(self):
        """PluginManager 返回 system-hooks 的真实 manifest（模拟生产声明裁剪路径）"""
        import json
        from pathlib import Path
        from types import SimpleNamespace

        import app.plugins.managers.plugin_manager as pm_mod

        manifest_path = (
            Path(__file__).resolve().parents[2] / "plugins" / "system-hooks" / ".drifox-plugin" / "plugin.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        fake_info = SimpleNamespace(manifest=manifest, path=str(manifest_path.parents[1]))
        real_get = pm_mod.PluginManager.get_instance
        pm_mod.PluginManager.get_instance = staticmethod(
            lambda: SimpleNamespace(get_plugin=lambda name: fake_info if name == "system-hooks" else None)
        )
        yield manifest
        pm_mod.PluginManager.get_instance = staticmethod(real_get)

    def test_local_tool_injects_full_schema_not_mcp_fallback(
        self, monkeypatch, hook_module, real_manifest_plugin_manager
    ):
        """manifest 声明了 tool.read 时，新增本地工具必须带完整 schema 注入"""
        from app.tools.registry import ToolRegistry

        tr = ToolRegistry.get_instance()
        probe = "___test_probe_local_tool"
        schema = {
            "type": "function",
            "function": {
                "name": probe,
                "description": "回归探针",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        assert tr.register(name=probe, schema=schema, impl=lambda: None, danger="safe", source="plugin:test")
        try:
            # manifest 必须声明 tool.read（否则 hook 拿不到注册表，全部误标 MCP）
            assert "tool.read" in real_manifest_plugin_manager.get("capabilities", []), (
                "system-hooks manifest 必须声明 tool.read，否则工具变更注入全部误标为 MCP 工具"
            )
            out = hook_module.hook(
                "PluginChanged",
                {"action": "enabled", "plugin_name": "some-plugin", "diff": {"tools_added": [probe]}},
            )
            assert probe in out
            assert "完整 schema" in out
            assert "MCP 工具" not in out
        finally:
            tr.unregister(probe)
