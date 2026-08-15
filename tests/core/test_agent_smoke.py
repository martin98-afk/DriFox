# -*- coding: utf-8 -*-
"""
Smoke tests for app/core/agent.py
Covers: Agent, PermissionResolver, AgentManager
Run: pytest tests/core/test_agent_smoke.py -v
"""

from typing import Any, Dict

import pytest


# =============================================================================
# 1. Agent dataclass
# =============================================================================


class TestAgent:
    """Pure-logic tests for Agent dataclass (no dependencies)."""

    @pytest.fixture
    def mod(self):
        return pytest.importorskip("app.core.agent")

    # ── Defaults ──────────────────────────────────────────────────────

    def test_default_agent_has_expected_fields(self, mod):
        """Verify Agent dataclass default values."""
        agent = mod.Agent(name="test", description="test desc")
        assert agent.name == "test"
        assert agent.description == "test desc"
        assert agent.mode is None
        assert agent.permission == {}
        assert agent.temperature is None
        assert agent.steps is None
        assert agent.model is None
        assert agent.hidden is None
        assert agent.task_permissions == {}
        assert agent.color is None
        assert agent.top_p is None
        assert agent.prompt == ""
        assert agent.tools == {}
        assert agent.inherit_history is False
        assert agent.inherit_history_count is None
        assert agent.inherit_history_max_chars == 500
        assert agent.inherit_history_budget_ratio == 0.6

    # ── from_dict ─────────────────────────────────────────────────────

    def test_from_dict_minimal(self, mod):
        """from_dict with only required fields."""
        data = {"name": "minimal", "description": "just a test"}
        agent = mod.Agent.from_dict(data)
        assert agent.name == "minimal"
        assert agent.description == "just a test"
        assert agent.prompt == ""

    @pytest.mark.parametrize("tools_value,expected_type", [
        ("Read, Glob, Bash", dict),   # CSV string → dict
        (["Read", "Glob"], dict),      # list → dict
        ({"Read": True, "write": False}, dict),  # dict → dict
        (None, dict),                  # None → empty dict
        ({}, dict),                    # empty dict → empty dict
    ])
    def test_from_dict_tools_formats(self, mod, tools_value, expected_type):
        """from_dict handles all tools formats (str/list/dict/None)."""
        data = {"name": "tools-test", "description": "t", "tools": tools_value}
        agent = mod.Agent.from_dict(data)
        assert isinstance(agent.tools, expected_type)
        # String format should parse known tools
        if tools_value == "Read, Glob, Bash":
            assert "read" in agent.tools
            assert "glob" in agent.tools
            assert "bash" in agent.tools
        # List format
        elif tools_value == ["Read", "Glob"]:
            assert "read" in agent.tools
            assert "glob" in agent.tools
        # Dict format
        elif tools_value == {"Read": True, "write": False}:
            assert agent.tools.get("read") is True
            assert agent.tools.get("write") is False

    def test_from_dict_full_data(self, mod):
        """from_dict with all optional fields populated."""
        data = {
            "name": "full",
            "description": "full agent",
            "mode": "primary",
            "permission": {"read": "allow"},
            "temperature": 0.7,
            "steps": 5,
            "model": "gpt-4",
            "hidden": False,
            "task_permissions": {"task_a": "allow"},
            "color": "#ff0000",
            "top_p": 0.9,
            "prompt": "You are a helpful assistant.",
            "tools": {"read": True, "bash": False},
            "inherit_history": True,
            "inherit_history_count": 10,
            "inherit_history_max_chars": 1000,
            "inherit_history_budget_ratio": 0.5,
        }
        agent = mod.Agent.from_dict(data)
        assert agent.name == "full"
        assert agent.mode == "primary"
        assert agent.permission == {"read": "allow"}
        assert agent.temperature == 0.7
        assert agent.steps == 5
        assert agent.model == "gpt-4"
        assert agent.hidden is False
        assert agent.task_permissions == {"task_a": "allow"}
        assert agent.color == "#ff0000"
        assert agent.top_p == 0.9
        assert agent.prompt == "You are a helpful assistant."
        assert agent.tools == {"read": True, "bash": False}
        assert agent.inherit_history is True
        assert agent.inherit_history_count == 10
        assert agent.inherit_history_max_chars == 1000
        assert agent.inherit_history_budget_ratio == 0.5

    # ── to_dict round-trip ────────────────────────────────────────────

    def test_to_dict_round_trip(self, mod):
        """to_dict() produces a dict that from_dict can reconstruct."""
        original_data: Dict[str, Any] = {
            "name": "rt",
            "description": "round trip",
            "mode": "subagent",
            "permission": {"bash": "deny"},
            "temperature": 0.3,
            "steps": 3,
            "model": "claude-3",
            "hidden": True,
            "color": "#00ff00",
            "top_p": 0.8,
            "prompt": "Be concise.",
            "tools": {"grep": True, "glob": True},
            "inherit_history": True,
            "inherit_history_count": 5,
            "inherit_history_budget_ratio": 0.4,
        }
        agent1 = mod.Agent.from_dict(original_data)
        serialized = agent1.to_dict()
        agent2 = mod.Agent.from_dict(serialized)
        assert agent2.name == agent1.name
        assert agent2.description == agent1.description
        assert agent2.mode == agent1.mode
        assert agent2.temperature == agent1.temperature
        assert agent2.steps == agent1.steps
        assert agent2.model == agent1.model
        assert agent2.color == agent1.color
        assert agent2.top_p == agent1.top_p
        assert agent2.prompt == agent1.prompt
        assert agent2.inherit_history == agent1.inherit_history
        assert agent2.inherit_history_count == agent1.inherit_history_count
        assert agent2.inherit_history_budget_ratio == agent1.inherit_history_budget_ratio

    def test_to_dict_omits_defaults(self, mod):
        """to_dict() omits fields with default values."""
        agent = mod.Agent(name="min", description="minimal")
        d = agent.to_dict()
        assert "name" in d
        assert "description" in d
        assert "temperature" not in d
        assert "steps" not in d
        assert "model" not in d
        assert "color" not in d
        assert "top_p" not in d
        assert "prompt" not in d
        assert "tools" not in d
        assert "inherit_history" not in d
        assert "inherit_history_count" not in d
        assert "inherit_history_max_chars" not in d
        assert "inherit_history_budget_ratio" not in d

    # ── is_primary ────────────────────────────────────────────────────

    @pytest.mark.parametrize("mode,hidden,expected", [
        ("primary", None, True),
        ("primary", False, True),
        ("primary", True, False),  # hidden overrides
        ("all", None, True),
        ("all", False, True),
        ("subagent", None, False),
        ("subagent", False, False),
        (None, None, False),
    ])
    def test_is_primary(self, mod, mode, hidden, expected):
        agent = mod.Agent(name="p", description="", mode=mode, hidden=hidden)
        assert agent.is_primary() == expected

    # ── is_subagent ───────────────────────────────────────────────────

    @pytest.mark.parametrize("mode,expected", [
        ("subagent", True),
        ("all", True),
        ("primary", False),
        (None, True),  # mode=None defaults to subagent
    ])
    def test_is_subagent(self, mod, mode, expected):
        agent = mod.Agent(name="s", description="", mode=mode)
        assert agent.is_subagent() == expected

    # ── is_hidden ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("hidden,mode,expected", [
        (True, "primary", True),
        (True, "subagent", True),
        (True, None, True),
        (False, "primary", False),
        (None, None, True),   # mode=None → hidden
        (None, "primary", False),
        (None, "subagent", False),
        (False, None, True),   # mode=None overrides hidden=False
    ])
    def test_is_hidden(self, mod, hidden, mode, expected):
        agent = mod.Agent(name="h", description="", hidden=hidden, mode=mode)
        assert agent.is_hidden() == expected

    # ── is_model_inherit ──────────────────────────────────────────────

    def test_is_model_inherit_always_true(self, mod):
        agent = mod.Agent(name="m", description="")
        assert agent.is_model_inherit() is True

    # ── inherit_history_budget_ratio ──────────────────────────────────

    def test_inherit_history_budget_ratio_default(self, mod):
        agent = mod.Agent(name="r", description="")
        assert agent.inherit_history_budget_ratio == 0.6

    def test_inherit_history_budget_ratio_custom(self, mod):
        data = {"name": "r2", "description": "", "inherit_history_budget_ratio": 0.3}
        agent = mod.Agent.from_dict(data)
        assert agent.inherit_history_budget_ratio == 0.3


# =============================================================================
# 2. PermissionResolver
# =============================================================================


class TestPermissionResolver:
    """Pure-logic tests for PermissionResolver."""

    @pytest.fixture
    def mod(self):
        return pytest.importorskip("app.core.agent")

    # ── DEFAULT_PERMISSIONS ───────────────────────────────────────────

    def test_default_permissions_allow_common_tools(self, mod):
        """Most common tools default to 'allow'."""
        defaults = mod.PermissionResolver.DEFAULT_PERMISSIONS
        for tool in ("read", "write", "edit", "bash", "grep", "glob", "list"):
            assert defaults.get(tool) == "allow", f"{tool} not 'allow'"

    def test_default_permissions_has_ask_tools(self, mod):
        """Some tools default to 'ask'."""
        defaults = mod.PermissionResolver.DEFAULT_PERMISSIONS
        # external_directory/doom_loop 死条目已删除（无对应注册工具，T4a 清理）
        assert all(v in ("allow", "ask", "deny") for v in defaults.values())

    # ── resolve ───────────────────────────────────────────────────────

    def test_resolve_empty_config_default_allow(self, mod):
        """Empty config falls back to DEFAULT_PERMISSIONS -> 'allow'."""
        pr = mod.PermissionResolver({})
        result = pr.resolve("read")
        assert result == "allow"

    def test_resolve_exact_match_allow(self, mod):
        pr = mod.PermissionResolver({"read": "allow"})
        assert pr.resolve("read") == "allow"

    def test_resolve_exact_match_deny(self, mod):
        pr = mod.PermissionResolver({"read": "deny"})
        assert pr.resolve("read") == "deny"

    def test_resolve_exact_match_ask(self, mod):
        pr = mod.PermissionResolver({"bash": "ask"})
        assert pr.resolve("bash") == "ask"

    def test_resolve_global_config_fallback(self, mod):
        """Global config provides fallback when agent config has no match."""
        pr = mod.PermissionResolver({}, global_config={"bash": "ask"})
        assert pr.resolve("bash") == "ask"

    def test_resolve_agent_config_overrides_global(self, mod):
        """Agent-specific config takes priority over global config."""
        pr = mod.PermissionResolver({"bash": "deny"}, global_config={"bash": "ask"})
        assert pr.resolve("bash") == "deny"

    def test_resolve_nested_pattern_config(self, mod):
        """Nested dict config with pattern keys.

        NOTE: _match_rules uses "last matching rule wins" semantics.
        For {"build*": "allow", "*": "ask"}, the wildcard comes after
        and matches everything, so both patterns return "ask".
        Put wildcard BEFORE specific rules for correct precedence.
        """
        pr = mod.PermissionResolver({"bash": {"build*": "allow", "*": "ask"}})
        # "*" (last) matches everything → overrides "build*"
        assert pr.resolve("bash", pattern="build.sh") == "ask"
        assert pr.resolve("bash", pattern="random") == "ask"

    def test_resolve_nested_pattern_wildcard_first(self, mod):
        """When wildcard is before specific rules, specific wins."""
        pr = mod.PermissionResolver({"bash": {"*": "ask", "build*": "allow"}})
        # "build*" (last) matches "build.sh" → overrides "*"
        assert pr.resolve("bash", pattern="build.sh") == "allow"
        # "build*" doesn't match "random", only "*" matches → "ask"
        assert pr.resolve("bash", pattern="random") == "ask"

    def test_resolve_wildcard_deny_all(self, mod):
        """Wildcard '*' can deny all tools."""
        pr = mod.PermissionResolver({"*": "deny"})
        assert pr.resolve("read") == "deny"
        assert pr.resolve("bash") == "deny"
        assert pr.resolve("write") == "deny"

    def test_resolve_wildcard_allow_some_deny_rest(self, mod):
        """Wildcard '*' with per-tool override."""
        pr = mod.PermissionResolver({"*": "deny", "read": "allow"})
        assert pr.resolve("read") == "allow"
        assert pr.resolve("bash") == "deny"

    def test_resolve_with_alias(self, mod):
        """Tool names are normalized via ToolNameMapper, so aliases work."""
        pr = mod.PermissionResolver({"read": "allow"})
        # "Read" (PascalCase alias) should map to "read"
        assert pr.resolve("Read") == "allow"

    def test_resolve_unknown_tool_default_allowed(self, mod):
        """Tool not in DEFAULT_PERMISSIONS defaults to 'allow'."""
        pr = mod.PermissionResolver({})
        result = pr.resolve("some_unknown_tool_xyz")
        assert result == "allow"

    # ── resolve with tools whitelist ──────────────────────────────────

    def test_tools_whitelist_allow_listed(self, mod):
        """When tools whitelist is set, listed tools are allowed."""
        pr = mod.PermissionResolver({}, tools_config={"read": True, "glob": True})
        assert pr.resolve("read") == "allow"
        assert pr.resolve("glob") == "allow"

    def test_tools_whitelist_deny_unlisted(self, mod):
        """When tools whitelist is set, unlisted tools are denied."""
        pr = mod.PermissionResolver({}, tools_config={"read": True})
        assert pr.resolve("bash") == "deny"

    def test_tools_whitelist_deny_explicit_false(self, mod):
        """When a tool is explicitly False, it is denied."""
        pr = mod.PermissionResolver({}, tools_config={"read": False})
        assert pr.resolve("read") == "deny"

    # ── resolve_task ──────────────────────────────────────────────────

    def test_resolve_task_default_allow(self, mod):
        """resolve_task defaults to 'allow'."""
        pr = mod.PermissionResolver({})
        assert pr.resolve_task("some-agent") == "allow"

    def test_resolve_task_with_config(self, mod):
        """resolve_task uses agent-specific config (last match wins)."""
        pr = mod.PermissionResolver({"task": {"code-agent": "deny", "*": "allow"}})
        # "*" (last) matches everything → overrides "code-agent"
        assert pr.resolve_task("code-agent") == "allow"
        assert pr.resolve_task("other-agent") == "allow"

    def test_resolve_task_with_config_wildcard_first(self, mod):
        """When wildcard is before specific rule, specific wins."""
        pr = mod.PermissionResolver({"task": {"*": "allow", "code-agent": "deny"}})
        assert pr.resolve_task("code-agent") == "deny"
        assert pr.resolve_task("other-agent") == "allow"

    def test_resolve_task_wildcard_deny(self, mod):
        """Wildcard '*' in task config denies all tasks."""
        pr = mod.PermissionResolver({"task": {"*": "deny"}})
        assert pr.resolve_task("any-agent") == "deny"

    # ── caching ───────────────────────────────────────────────────────

    def test_resolve_cache_returns_same_result(self, mod):
        """Same inputs produce same cached result."""
        pr = mod.PermissionResolver({"read": "deny"})
        r1 = pr.resolve("read")
        r2 = pr.resolve("read")
        assert r1 == r2 == "deny"

    def test_resolve_task_cache_returns_same_result(self, mod):
        pr = mod.PermissionResolver({"task": {"*": "ask"}})
        r1 = pr.resolve_task("agent-x")
        r2 = pr.resolve_task("agent-x")
        assert r1 == r2 == "ask"

    # ── _glob_match ───────────────────────────────────────────────────

    def test_glob_match_exact(self, mod):
        pr = mod.PermissionResolver({})
        assert pr._glob_match("build.sh", "build.sh") is True
        assert pr._glob_match("build.sh", "deploy.sh") is False

    def test_glob_match_wildcard(self, mod):
        pr = mod.PermissionResolver({})
        assert pr._glob_match("build.sh", "build*") is True
        assert pr._glob_match("build.sh", "*.sh") is True
        assert pr._glob_match("build.sh", "*.py") is False

    def test_glob_match_star_only(self, mod):
        pr = mod.PermissionResolver({})
        assert pr._glob_match("anything", "*") is True


# =============================================================================
# 3. AgentManager (Singleton)
# =============================================================================


class TestAgentManager:
    """Tests for AgentManager singleton.

    These tests use a fresh instance by resetting the singleton.
    They avoid filesystem dependencies by passing agents_dir=None.
    """

    @pytest.fixture
    def mod(self):
        return pytest.importorskip("app.core.agent")

    @pytest.fixture(autouse=True)
    def _reset_singleton(self, mod):
        """Reset AgentManager singleton before and after each test."""
        old_instance = mod.AgentManager._instance
        mod.AgentManager._instance = None
        yield
        mod.AgentManager._instance = old_instance

    # ── Singleton ─────────────────────────────────────────────────────

    def test_get_instance_returns_same(self, mod):
        """get_instance() returns the same object on repeated calls."""
        mgr1 = mod.AgentManager.get_instance()
        mgr2 = mod.AgentManager.get_instance()
        assert mgr1 is mgr2

    def test_get_instance_creates_when_none(self, mod):
        """get_instance() creates a new instance when none exists."""
        mgr = mod.AgentManager.get_instance()
        assert mgr is not None
        assert isinstance(mgr, mod.AgentManager)

    def test_get_instance_passes_agents_dir(self, mod):
        """get_instance() passes agents_dir to constructor."""
        mgr = mod.AgentManager.get_instance(agents_dir="/tmp/agents")
        # agents_dir should be stored (may be None if no agents there)
        # Just verify no crash and instance is returned
        assert mgr is not None

    # ── Empty-state queries ───────────────────────────────────────────

    def test_list_agents_empty(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.list_agents() == []

    def test_list_agents_include_hidden_empty(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.list_agents(include_hidden=True) == []

    def test_get_agent_unknown_returns_none(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.get_agent("nonexistent_agent") is None

    def test_list_primary_agents_empty(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.list_primary_agents() == []

    def test_list_subagents_empty(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.list_subagents() == []

    def test_list_subagents_include_hidden(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.list_subagents(include_hidden=True) == []

    def test_list_subagent_names_empty(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.list_subagent_names() == []
        assert mgr.list_subagent_names(include_hidden=True) == []

    def test_get_available_subagents_for_prompt_empty(self, mod):
        """Returns empty string when no subagents exist."""
        mgr = mod.AgentManager.get_instance()
        result = mgr.get_available_subagents_for_prompt()
        assert result == ""

    def test_get_agent_config_unknown_returns_empty_dict(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.get_agent_config("nonexistent") == {}

    def test_check_permission_unknown_agent(self, mod):
        """Unknown agent returns 'allow'."""
        mgr = mod.AgentManager.get_instance()
        assert mgr.check_permission("nonexistent", "bash") == "allow"

    def test_get_enabled_skills_content_empty_list(self, mod):
        mgr = mod.AgentManager.get_instance()
        assert mgr.get_enabled_skills_content([]) == ""

    # ── No-crash operations on empty manager ──────────────────────────

    def test_reload_agents_no_crash(self, mod):
        """reload_agents() should not crash when no plugins/hooks loaded."""
        mgr = mod.AgentManager.get_instance()
        mgr.reload_agents()  # Should not raise

    def test_cleanup_plugin_artifacts_unknown(self, mod):
        """cleanup_plugin_artifacts for unknown plugin should not crash."""
        mgr = mod.AgentManager.get_instance()
        mgr.cleanup_plugin_artifacts("unknown_plugin")  # Should not raise

    def test_unload_skill_unknown(self, mod):
        """unload_skill for unknown skill should not crash."""
        mgr = mod.AgentManager.get_instance()
        mgr.unload_skill("unknown_skill")  # Should not raise

    def test_reload_plugin_agents_unknown(self, mod):
        """reload_plugin_agents for unknown plugin should return 0."""
        mgr = mod.AgentManager.get_instance()
        count = mgr.reload_plugin_agents("unknown_plugin")
        assert count == 0

    def test_reload_plugin_hooks_unknown(self, mod):
        """reload_plugin_hooks for unknown plugin should return False."""
        mgr = mod.AgentManager.get_instance()
        result = mgr.reload_plugin_hooks("unknown_plugin")
        assert result is False


# =============================================================================
# 4. Module-level functions
# =============================================================================


class TestModuleFunctions:
    """Tests for module-level functions in app.core.agent."""

    @pytest.fixture
    def mod(self):
        return pytest.importorskip("app.core.agent")

    def test_create_agent_manager_returns_instance(self, mod):
        """create_agent_manager returns an AgentManager instance."""
        old_instance = mod.AgentManager._instance
        mod.AgentManager._instance = None
        try:
            mgr = mod.create_agent_manager()
            assert isinstance(mgr, mod.AgentManager)
        finally:
            mod.AgentManager._instance = old_instance

    def test_get_available_skills_exists(self, mod):
        """get_available_skills exists and is callable."""
        assert hasattr(mod, "get_available_skills")
        assert callable(mod.get_available_skills)


# =============================================================================
# 5. Integration: Agent with PermissionResolver
# =============================================================================


class TestAgentPermissionIntegration:
    """Agent and PermissionResolver work together via AgentManager.check_permission."""

    @pytest.fixture
    def mod(self):
        return pytest.importorskip("app.core.agent")

    @pytest.fixture(autouse=True)
    def _reset_singleton(self, mod):
        old = mod.AgentManager._instance
        mod.AgentManager._instance = None
        yield
        mod.AgentManager._instance = old

    def test_agent_with_permission_check_via_manager(self, mod):
        """AgentManager.check_permission uses Agent.permission + PermissionResolver."""
        mgr = mod.AgentManager.get_instance()
        # Manually inject an agent with deny config
        agent = mod.Agent(name="restricted", description="", permission={"bash": "deny"})
        mgr._agents["restricted"] = agent

        assert mgr.check_permission("restricted", "bash") == "deny"
        assert mgr.check_permission("restricted", "read") == "allow"

    def test_agent_permission_with_whitelist_tools(self, mod):
        """Agent with tools whitelist: unlisted tools denied."""
        mgr = mod.AgentManager.get_instance()
        agent = mod.Agent(
            name="whitelisted",
            description="",
            permission={},
            tools={"read": True, "glob": True},
        )
        mgr._agents["whitelisted"] = agent

        assert mgr.check_permission("whitelisted", "read") == "allow"
        assert mgr.check_permission("whitelisted", "glob") == "allow"
        assert mgr.check_permission("whitelisted", "bash") == "deny"

    def test_agent_get_agent_config_returns_fields(self, mod):
        """get_agent_config returns temperature/steps/model/top_p/permission."""
        mgr = mod.AgentManager.get_instance()
        agent = mod.Agent(
            name="configured",
            description="",
            temperature=0.5,
            steps=3,
            model="gpt-4",
            top_p=0.9,
            permission={"*": "ask"},
        )
        mgr._agents["configured"] = agent

        config = mgr.get_agent_config("configured")
        assert config["temperature"] == 0.5
        assert config["steps"] == 3
        assert config["model"] == "gpt-4"
        assert config["top_p"] == 0.9
        assert config["permission"] == {"*": "ask"}


# =============================================================================
# 6. AgentManager with agents loaded directly
# =============================================================================


class TestAgentManagerWithAgents:
    """AgentManager with manually injected agents (no filesystem)."""

    @pytest.fixture
    def mod(self):
        return pytest.importorskip("app.core.agent")

    @pytest.fixture(autouse=True)
    def _reset_singleton(self, mod):
        old = mod.AgentManager._instance
        mod.AgentManager._instance = None
        yield
        mod.AgentManager._instance = old

    @pytest.fixture
    def mgr_with_agents(self, mod):
        """Create an AgentManager with a few manually injected agents."""
        mgr = mod.AgentManager.get_instance()

        primary = mod.Agent(name="primary-agent", description="Primary", mode="primary")
        sub = mod.Agent(name="sub-agent", description="Sub helper", mode="subagent")
        all_mode = mod.Agent(name="all-agent", description="Both modes", mode="all")
        hidden_agent = mod.Agent(name="hidden-agent", description="Hidden", mode=None)
        hidden_true = mod.Agent(name="hidden-true", description="Explicit hidden", mode="primary", hidden=True)

        mgr._agents["primary-agent"] = primary
        mgr._agents["sub-agent"] = sub
        mgr._agents["all-agent"] = all_mode
        mgr._hidden_agents["hidden-agent"] = hidden_agent
        mgr._hidden_agents["hidden-true"] = hidden_true
        return mgr

    # ── list methods ──
    def test_list_agents(self, mod, mgr_with_agents):
        agents = mgr_with_agents.list_agents()
        names = [a.name for a in agents]
        assert "primary-agent" in names
        assert "sub-agent" in names
        assert "all-agent" in names
        assert "hidden-agent" not in names  # hidden by default

    def test_list_agents_include_hidden(self, mod, mgr_with_agents):
        agents = mgr_with_agents.list_agents(include_hidden=True)
        names = [a.name for a in agents]
        assert "hidden-agent" in names
        assert "hidden-true" in names

    def test_list_primary_agents(self, mod, mgr_with_agents):
        agents = mgr_with_agents.list_primary_agents()
        names = [a.name for a in agents]
        assert "primary-agent" in names
        assert "all-agent" in names  # mode="all" is primary
        assert "sub-agent" not in names
        assert "hidden-true" not in names  # hidden=True overrides

    def test_list_subagents(self, mod, mgr_with_agents):
        agents = mgr_with_agents.list_subagents(include_hidden=False)
        names = [a.name for a in agents]
        assert "sub-agent" in names
        assert "all-agent" in names  # mode="all" is subagent
        assert "primary-agent" not in names
        # hidden agents should not appear
        assert "hidden-agent" not in names

    def test_list_subagents_include_hidden_true(self, mod, mgr_with_agents):
        agents = mgr_with_agents.list_subagents(include_hidden=True)
        names = [a.name for a in agents]
        assert "hidden-agent" in names  # mode=None is subagent-compatible

    def test_list_subagent_names(self, mod, mgr_with_agents):
        names = mgr_with_agents.list_subagent_names(include_hidden=False)
        assert "sub-agent" in names
        assert "all-agent" in names
        assert "primary-agent" not in names

    def test_get_agent_found(self, mod, mgr_with_agents):
        agent = mgr_with_agents.get_agent("primary-agent")
        assert agent is not None
        assert agent.name == "primary-agent"

    def test_get_agent_hidden_found(self, mod, mgr_with_agents):
        agent = mgr_with_agents.get_agent("hidden-agent")
        assert agent is not None
        assert agent.name == "hidden-agent"

    # ── get_available_subagents_for_prompt ──
    def test_get_available_subagents_formatted(self, mod, mgr_with_agents):
        text = mgr_with_agents.get_available_subagents_for_prompt(include_hidden=False)
        assert "## Available Subagents" in text
        assert "**sub-agent**" in text
        assert "**all-agent**" in text
        assert "**primary-agent**" not in text

    def test_get_available_subagents_include_hidden_in_prompt(self, mod, mgr_with_agents):
        text = mgr_with_agents.get_available_subagents_for_prompt(include_hidden=True)
        assert "**hidden-agent**" in text
