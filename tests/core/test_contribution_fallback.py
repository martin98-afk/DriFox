# -*- coding: utf-8 -*-
"""命令/智能体贡献回落：注册表有定义时 CommandManager.execute 与 get_agent 命中"""

from app.plugins.registries.contribution_registry import ContributionRegistry

def test_execute_falls_back_to_contrib_command():
    from app.core.commands.command_manager import CommandManager, CommandType

    reg = ContributionRegistry.get_instance()
    reg.register_command("t_plugin", {
        "name": "hello-contrib",
        "description": "测试贡献命令",
        "display_type": "prompt",
        "prompt_text": "你好，贡献命令",
    })
    try:
        mgr = CommandManager.get_instance()
        result = mgr.execute("/hello-contrib 补充内容")
        assert result is not None
        assert result.type == CommandType.PROMPT
        assert "你好，贡献命令" in (result.replacement or "")
        assert result.remainder == "补充内容"
    finally:
        reg.unregister_source("t_plugin")

def test_execute_still_none_for_unknown():
    mgr = CommandManagerHolder.mgr()
    assert mgr.execute("/definitely-not-a-command-xyz") is None

class CommandManagerHolder:
    @staticmethod
    def mgr():
        from app.core.commands.command_manager import CommandManager

        return CommandManager.get_instance()

def test_get_agent_falls_back_to_contrib():
    from app.core.conversation.agent import AgentManager

    reg = ContributionRegistry.get_instance()
    reg.register_agent("t_plugin", {
        "name": "contrib-agent",
        "description": "贡献智能体",
        "mode": "subagent",
        "prompt": "# 贡献\n正文",
    })
    try:
        mgr = AgentManager(agents_dir=None)
        agent = mgr.get_agent("contrib-agent")
        assert agent is not None
        assert agent.name == "contrib-agent"
        assert agent.prompt.startswith("# 贡献")
    finally:
        reg.unregister_source("t_plugin")
