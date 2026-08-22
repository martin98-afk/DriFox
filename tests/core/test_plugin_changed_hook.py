# -*- coding: utf-8 -*-
"""PluginChanged hook 事件测试

覆盖：
- matcher 按子事件 action pipe 匹配（installed|enabled 等）
- trigger_plugin_changed_hook 全局触发辅助（diff 附加 + 无 backend 静默）
- ChatBackend._infer_plugin_changed_action 推断
"""

import pytest

from app.core.hook_manager import HookMatchRule, HookManager


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
        from app.core import hook_manager as hm
        from app.core.backend import ChatBackend

        monkeypatch.setattr(ChatBackend, "_active_instances", [])
        hm.trigger_plugin_changed_hook({"action": "mcp_added", "server_name": "x"})
        # 首次调用建立基线
        assert hm._plugin_snapshot_tools is not None

    def test_no_registered_hook_skips_trigger(self, monkeypatch):
        """PluginChanged 无注册 hook 时不投递线程池（基线仍刷新）"""
        from app.core import hook_manager as hm
        from app.core.backend import ChatBackend

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
    """ChatBackend._infer_plugin_changed_action 推断"""

    def test_sentinel_is_installed(self):
        from app.core.backend import ChatBackend

        backend = ChatBackend.__new__(ChatBackend)
        assert backend._infer_plugin_changed_action(ChatBackend._NEW_PLUGIN_SENTINEL) == "installed"

    def test_empty_name_is_updated(self):
        from app.core.backend import ChatBackend

        backend = ChatBackend.__new__(ChatBackend)
        assert backend._infer_plugin_changed_action("") == "updated"

    def test_missing_plugin_is_uninstalled(self, monkeypatch):
        from app.core.backend import ChatBackend

        backend = ChatBackend.__new__(ChatBackend)
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.get_instance",
            staticmethod(lambda: type("PM", (), {"has_plugin": staticmethod(lambda n: False)})()),
        )
        assert backend._infer_plugin_changed_action("gone") == "uninstalled"
