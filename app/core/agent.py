# -*- coding: utf-8 -*-
"""
智能体模块 - OpenCode 风格的 agent 配置系统。

支持 Markdown 格式定义、Permission 系统、Primary/Subagent/Hidden 模式。
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any, Union

import yaml
from loguru import logger

from app.tools import get_builtin_tools_schema
from app.core.hook_manager import HookManager


@dataclass
class Agent:
    name: str
    description: str
    mode: Optional[str] = None  # None 表示未声明，不显示在 UI；"primary"/"all" 表示可作为主智能体
    permission: Dict[str, Any] = field(default_factory=dict)
    temperature: Optional[float] = None
    steps: Optional[int] = None
    model: Optional[str] = None
    hidden: Optional[bool] = None  # None 表示未声明；True 表示隐藏；False 表示显式显示
    task_permissions: Dict[str, str] = field(default_factory=dict)
    color: Optional[str] = None
    top_p: Optional[float] = None
    prompt: str = ""
    tools: Dict[str, bool] = field(default_factory=dict)
    inherit_history: bool = False  # 是否继承主智能体历史消息
    inherit_history_count: Optional[int] = None  # 继承最近 N 条消息，None 表示全部
    inherit_history_max_chars: Optional[int] = 500  # （已弃用）旧版每条消息最大字符数，新版使用 budget 比例
    inherit_history_budget_ratio: float = 0.6  # 上下文注入最多占 context budget 的比例（0.1~0.8）

    @classmethod
    def from_dict(cls, data: Dict) -> "Agent":
        tools = data.get("tools", {})
        if isinstance(tools, list):
            tools = {t: True for t in tools}
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            mode=data.get("mode"),
            permission=data.get("permission", {}),
            temperature=data.get("temperature"),
            steps=data.get("steps"),
            model=data.get("model"),
            hidden=data.get("hidden"),
            task_permissions=data.get("task_permissions", {}),
            color=data.get("color"),
            top_p=data.get("top_p"),
            prompt=data.get("prompt", ""),
            tools=tools,
            inherit_history=data.get("inherit_history", False),
            inherit_history_count=data.get("inherit_history_count"),
            inherit_history_max_chars=data.get("inherit_history_max_chars", 500),
            inherit_history_budget_ratio=float(data.get("inherit_history_budget_ratio", 0.6)),
        )

    def to_dict(self) -> Dict:
        result = {
            "name": self.name,
            "description": self.description,
            "permission": self.permission,
        }
        if self.mode is not None:
            result["mode"] = self.mode
        if self.temperature is not None:
            result["temperature"] = self.temperature
        if self.steps is not None:
            result["steps"] = self.steps
        if self.model:
            result["model"] = self.model
        if self.hidden is True:
            result["hidden"] = True
        if self.task_permissions:
            result["task_permissions"] = self.task_permissions
        if self.color:
            result["color"] = self.color
        if self.top_p is not None:
            result["top_p"] = self.top_p
        if self.prompt:
            result["prompt"] = self.prompt
        if self.tools:
            result["tools"] = self.tools
        if self.inherit_history:
            result["inherit_history"] = True
        if self.inherit_history_count is not None:
            result["inherit_history_count"] = self.inherit_history_count
        if self.inherit_history_max_chars != 500:
            result["inherit_history_max_chars"] = self.inherit_history_max_chars
        if self.inherit_history_budget_ratio != 0.6:
            result["inherit_history_budget_ratio"] = self.inherit_history_budget_ratio
        return result

    def is_primary(self) -> bool:
        """是否可作为主智能体显示在 UI 中：mode 为 primary/all，且 hidden 不为 True"""
        if self.hidden is True:
            return False
        return self.mode in ("primary", "all")

    def is_subagent(self) -> bool:
        """是否可作为子智能体：mode 为 subagent/all"""
        return self.mode in ("subagent", "all")

    def is_hidden(self) -> bool:
        """是否隐藏（显式 hidden=True 或未声明 mode）"""
        return self.hidden is True or self.mode is None


class PermissionResolver:
    DEFAULT_PERMISSIONS = {
        "*": "allow",
        "read": "allow",
        "edit": "allow",
        "write": "allow",
        "multi_edit": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": "allow",
        "task": "allow",
        "skill": "allow",
        "webfetch": "allow",
        "websearch": "allow",
        "todoread": "allow",
        "todowrite": "allow",
        "mcp_list_servers": "allow",
        "mcp": "allow",  # MCP 工具前缀匹配
        "external_directory": "ask",
        "doom_loop": "ask",
    }

    def __init__(
            self,
            permission_config: Dict[str, Any],
            global_config: Optional[Dict[str, Any]] = None,
            tools_config: Optional[Union[Dict[str, bool], List[str]]] = None,
    ) -> None:
        self._config = permission_config
        self._global = global_config or {}
        if tools_config is None:
            self._tools_config: Dict[str, bool] = {}
        elif isinstance(tools_config, list):
            self._tools_config = {t: True for t in tools_config}
        else:
            self._tools_config = tools_config
        self._cache: Dict[tuple, str] = {}
        self._task_cache: Dict[str, str] = {}

    def resolve(self, tool: str, pattern: str = "*") -> str:
        cache_key = (tool, pattern)
        if cache_key in self._cache:
            return self._cache[cache_key]

        if tool in self._tools_config:
            result = "allow" if self._tools_config[tool] else "deny"
            self._cache[cache_key] = result
            return result

        if "*" in self._tools_config:
            result = "allow" if self._tools_config["*"] else "deny"
            self._cache[cache_key] = result
            return result

        rules = self._collect_rules(tool)

        result = self._match_rules(pattern, rules)
        self._cache[cache_key] = result
        return result

    def resolve_task(self, subagent_name: str) -> str:
        if subagent_name in self._task_cache:
            return self._task_cache[subagent_name]

        rules = self._collect_rules("task")

        if not rules:
            rules = [("*", self.DEFAULT_PERMISSIONS.get("task", "allow"))]

        result = self._match_rules(subagent_name, rules)
        self._task_cache[subagent_name] = result
        return result

    def _collect_rules(self, tool: str) -> List[tuple]:
        rules = []

        global_tool_config = self._global.get(tool, {})
        if isinstance(global_tool_config, str):
            rules.append(("*", global_tool_config))
        elif isinstance(global_tool_config, dict):
            for k, v in global_tool_config.items():
                rules.append((k, v))

        agent_tool_config = self._config.get(tool, {})
        if isinstance(agent_tool_config, str):
            rules.append(("*", agent_tool_config))
        elif isinstance(agent_tool_config, dict):
            for k, v in agent_tool_config.items():
                rules.append((k, v))

        return rules

    def _match_rules(self, pattern: str, rules: List[tuple]) -> str:
        if not rules:
            return self.DEFAULT_PERMISSIONS.get("*", "allow")

        last_match = None
        for key, value in rules:
            if key == "*" or self._glob_match(pattern, key):
                last_match = value

        if last_match:
            return last_match

        return self.DEFAULT_PERMISSIONS.get("*", "allow")

    def _glob_match(self, text: str, pattern: str) -> bool:
        return fnmatch.fnmatch(text, pattern)

class AgentManager:
    """Agent/Skill 管理器（全局单例，跨窗口共享）"""

    _instance = None

    @classmethod
    def get_instance(cls, agents_dir: Optional[str] = None, hook_manager: Optional[HookManager] = None) -> "AgentManager":
        """获取全局唯一的 AgentManager 实例（首次创建时加载 agents，后续复用）"""
        if cls._instance is None:
            cls._instance = cls(agents_dir, hook_manager)
        return cls._instance

    def __init__(self, agents_dir: Optional[str] = None, hook_manager: Optional[HookManager] = None):
        self.agents_dir = Path(agents_dir) if agents_dir else None
        self._agents: Dict[str, Agent] = {}
        self._hidden_agents: Dict[str, Agent] = {}
        self._hook_manager = hook_manager
        self._global_permission: Dict[str, Any] = {}
        self._builtin_tools = None  # BuiltinTools 实例，用于获取 MCP schema
        self._load_agents()

    def _load_agents(self):
        """加载智能体：插件路径优先，后备路径兜底"""
        # 1. 从所有已启用插件加载（PluginManager 已初始化时）
        self._load_agents_from_plugins()

        # 2. 从后备目录加载（兼容旧配置）
        if self.agents_dir and self.agents_dir.exists():
            self._load_agents_from_dir(self.agents_dir)

        # 3. 加载 hooks
        if self._hook_manager is not None:
            # 3a. 插件 hooks 目录（顶层 hooks/）
            self._load_plugin_hooks()

            # 3b. 技能中的 hooks（skills/{name}/hooks/hooks.json）
            self._load_skills_hooks()

    def _load_agents_from_plugins(self):
        """从所有已启用插件加载智能体"""
        try:
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for plugin in pm.get_enabled_plugins():
                    agent_dir = plugin.path / "agents"
                    if agent_dir.exists():
                        self._load_agents_from_dir(agent_dir)
        except (ImportError, Exception):
            pass

    def reload_agents(self):
        """"重新从已启用插件加载智能体（动态注入/注出后调用）

        注意：仅重载 agents，不重载 hooks。hooks 的重新加载由调用方单独处理。
        """
        self._agents.clear()
        self._hidden_agents.clear()
        self._load_agents()
        logger.info(f"[AgentManager] Reloaded agents: {len(self._agents)} visible, {len(self._hidden_agents)} hidden")

    def _load_skills_hooks(self, force: bool = False):
        """加载 skills 目录中的 hooks

        Args:
            force: 为 True 时强制重新加载（reload_agents 时调用）
        """
        try:
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for skill_path in pm.get_skill_paths():
                    self._hook_manager.load_hooks_from_skills(skill_path, force=force)
        except (ImportError, Exception):
            pass

    def _load_plugin_hooks(self):
        """加载插件顶层 hooks/ 目录中的 hooks"""
        try:
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for hooks_dir in pm.get_hooks_dirs():
                    if not hooks_dir.exists() or not hooks_dir.is_dir():
                        continue
                    # plugins/system/hooks/hooks.json — 直接加载
                    self._hook_manager.load_hooks_from_directory_flat(hooks_dir)
        except (ImportError, Exception):
            pass

    def _load_agents_from_dir(self, agents_dir: Path):
        """从指定目录加载所有智能体"""
        if not agents_dir.exists():
            return

        for md_file in agents_dir.glob("*.md"):
            try:
                agent = self._parse_markdown_agent(md_file)
                if agent:
                    if agent.is_hidden():
                        self._hidden_agents[agent.name] = agent
                    else:
                        self._agents[agent.name] = agent
                    logger.info(
                        f"[AgentManager] Loaded agent: {agent.name} (mode={agent.mode}, hidden={agent.hidden})"
                    )
            except Exception as e:
                logger.error(f"[AgentManager] Failed to load {md_file}: {e}")

        for yaml_file in agents_dir.glob("*.yaml"):
            try:
                agent = self._parse_yaml_agent(yaml_file)
                if agent:
                    if agent.is_hidden():
                        self._hidden_agents[agent.name] = agent
                    else:
                        self._agents[agent.name] = agent
                    logger.info(f"[AgentManager] Loaded agent (yaml): {agent.name}")
            except Exception as e:
                logger.error(f"[AgentManager] Failed to load {yaml_file}: {e}")

    def _parse_markdown_agent(self, file_path: Path) -> Optional[Agent]:
        content = file_path.read_text(encoding="utf-8")

        if not content.startswith("---"):
            return None

        parts = content.split("---", 3)
        if len(parts) < 3:
            return None

        # parts[0] = "", parts[1] = frontmatter, parts[2] = body
        # 多行 description 等复杂字段内容在 body 中，不会污染 frontmatter
        frontmatter = parts[1]
        body = parts[2].strip()

        try:
            meta = yaml.safe_load(frontmatter)
        except Exception as e:
            logger.error(f"[AgentManager] YAML parse failed for {file_path}: {e}")
            return None
        if not meta:
            return None

        agent = Agent.from_dict(meta)
        agent.name = file_path.stem
        agent.prompt = body

        return agent

    def _parse_yaml_agent(self, file_path: Path) -> Optional[Agent]:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        if not data:
            return None
        return Agent.from_dict(data)

    def get_agent(self, name: str) -> Optional[Agent]:
        return self._agents.get(name) or self._hidden_agents.get(name)

    def list_agents(self, include_hidden: bool = False) -> List[Agent]:
        if include_hidden:
            return list(self._agents.values()) + list(self._hidden_agents.values())
        return list(self._agents.values())

    def list_primary_agents(self) -> List[Agent]:
        return [a for a in self._agents.values() if a.is_primary()]

    def list_subagents(self, include_hidden: bool = False) -> List[Agent]:
        agents = self._agents.values()
        if include_hidden:
            agents = list(agents) + list(self._hidden_agents.values())
        return [a for a in agents if a.is_subagent()]

    def list_subagent_names(self, include_hidden: bool = False) -> List[str]:
        """获取所有子智能体的名称列表（用于工具 schema enum）"""
        agents = self.list_subagents(include_hidden=include_hidden)
        return [a.name for a in agents]

    def get_available_subagents_for_prompt(self, include_hidden: bool = False) -> str:
        """
        获取可用于主智能体提示词中的子智能体列表（格式化文本）。

        用于主智能体提示词动态注入可用子智能体信息，避免硬编码。

        Returns:
            格式化子智能体列表文本，格式：
            ## Available Subagents
            - name: description
            - name: description
            ...
        """
        agents = self.list_subagents(include_hidden=include_hidden)
        if not agents:
            return ""

        lines = ["## Available Subagents\n可直接使用的子智能体列表："]
        for a in agents:
            lines.append(f"- **{a.name}**: {a.description}")

        return "\n".join(lines)

    def unload_skill(self, skill_name: str):
        """卸载一个技能，包括其 Hooks"""
        if skill_name in self._agents:
            del self._agents[skill_name]
        if skill_name in self._hidden_agents:
            del self._hidden_agents[skill_name]
        if self._hook_manager:
            self._hook_manager.unregister_skill_hooks(skill_name)
        logger.info(f"[AgentManager] Unloaded skill: {skill_name}")

    def get_agent_tools_schema(
            self, agent_name: str, global_permission: Optional[Dict[str, Any]] = None, is_subagent_call: bool = False
    ) -> List[Dict]:
        agent = self.get_agent(agent_name)
        if not agent:
            return []

        all_tools = get_builtin_tools_schema(self, builtin_tools=self._builtin_tools)

        # 【新增】子智能体禁止使用交互和嵌套子智能体工具（需要用户交互或发布子智能体，不支持）
        forbidden_tools = {"question", "task_batch", "task_status"}
        if is_subagent_call:
            all_tools = [t for t in all_tools if t["function"]["name"].lower() not in forbidden_tools]

        perm_resolver = PermissionResolver(
            agent.permission, global_permission or {}, agent.tools
        )

        filtered_tools = []
        for tool in all_tools:
            tool_name = tool["function"]["name"].lower()
            permission = perm_resolver.resolve(tool_name)
            if permission in ("allow", "ask"):
                filtered_tools.append(tool)

        return filtered_tools

    def get_agent_system_prompt(
            self,
            agent_name: str,
            base_prompt: str = "",
            is_subagent_call: bool = False,
    ) -> str:
        """
        获取智能体的系统提示词。

        Args:
            agent_name: 智能体名称
            base_prompt: 基础提示词（通常为 skill 内容）
            is_subagent_call: 是否为子智能体调用上下文。
                - True: 主智能体通过 task_batch 调用子智能体（子智能体看到的是任务描述，不是完整上下文）
                - False: 主智能体自身运行，或子智能体独立运行
        """
        agent = self.get_agent(agent_name)
        if not agent:
            return base_prompt

        # 通用编码契约（所有智能体）
        global_contract = """
## Global Coding Contract
- 这是一个代码工作台，不是普通闲聊窗口。
- 优先围绕"相关文件、实施动作、验证方式、剩余风险"组织输出。
- 回答要像工程师交付，不要像客服聊天。
""".strip()

        # 子智能体额外约束
        subagent_constraints = """
## 子智能体约束
- 【禁止】使用 `question` 工具（需要用户交互，不支持）
- 【禁止】使用 `task_batch` 和 `task_status` 工具（子智能体不能再发布子智能体）
- 【禁止】使用 `todowrite` 工具（避免与主智能体冲突）
- 【必须】任务一次性执行完毕，不支持中途暂停或等待用户确认
- 【必须】独立完成任务，不需要主智能体介入
- 如果遇到不确定的情况，根据已有信息做出合理假设并继续执行
- 如需收集信息，使用 webfetch/websearch 工具代替提问
""".strip()

        # 主智能体额外约束
        primary_constraints = """
## 主智能体约束
### 主动提问规范
- 需要向用户确认的信息，优先使用 `question` 工具。

### 推荐问题规范
- 当你预测到用户接下来可能需要的帮助时，请按以下格式给出追问清单（放在回复末尾）：
  - [问题描述1](ask)
  - [问题描述2](ask)
  
### 文件引用规范
- 当你想要引用某个本地存在的文件时，请按以下格式引用：
  - [文件名](file|文件路径)
  - [文件夹名](file|文件夹路径)
""".strip()

        # 【核心修复】根据 is_subagent_call 区分调用上下文
        # 场景1: 主智能体自身运行（primary mode，is_subagent_call=False）
        # 场景2: 主智能体通过 task_batch 调用子智能体（子智能体看到任务描述，is_subagent_call=True）
        # 场景3: 子智能体独立运行（subagent mode，is_subagent_call=False）

        if is_subagent_call:
            # 场景2：被主智能体调用，子智能体看到的是任务描述
            role_constraints = subagent_constraints
        else:
            # 场景1：主智能体运行
            role_constraints = primary_constraints
            # 主智能体需要动态注入可用子智能体列表
            subagents_info = self.get_available_subagents_for_prompt()
            if subagents_info:
                global_contract = global_contract + "\n\n" + subagents_info

        if agent.prompt:
            return "\n\n".join(
                part for part in [agent.prompt, global_contract, role_constraints, base_prompt] if part
            )

        # Fallback 提示词
        fallback_prompt = f"""# {agent.name}
{agent.description}

## Available Tools
Use the tools available to you based on your permissions.

{global_contract}
{role_constraints}
"""
        return "\n\n".join(part for part in [fallback_prompt, base_prompt] if part)

    def get_unified_system_prompt(self) -> str:
        return """# LLM Chatter
你是一个智能编程助手，基于大语言模型。
使用工具来帮助用户完成编程任务。

## 后台任务回调消息
当收到格式为 `[后台任务状态]` 的用户消息时，表示子智能体任务回调通知。
消息中包含本次完成的任务列表（任务名、任务ID）。
你应该：
1. 使用 task_status 工具，传入消息中提到的任务ID，获取详细结果
2. 根据结果评估完成情况
3. 输出总结或后续建议（如有需要）

不要在回复中重复消息内容，直接给出检查结果和建议。""".strip()

    def get_enabled_skills_content(self, enabled_skills: List[str]) -> str:
        """获取已启用的技能内容"""
        from app.utils.utils import get_local_skills
        
        if not enabled_skills:
            return ""
        
        all_skills = get_local_skills()
        result_parts = [
            "\n\n## 偏好技能\n以下是部分用户偏好的智能体技能，如果以下技能不能满足用户需求，可以使用 `list_skills` 技能加载完整技能列表：\n"
        ]
        
        for skill in all_skills:
            if skill["name"] in enabled_skills:
                result_parts.append(f"\n### {skill['name']}\n{skill.get('description', '')}\n")
        
        return "\n".join(result_parts) if len(result_parts) > 1 else ""

    def get_agent_config(self, agent_name: str) -> Dict[str, Any]:
        agent = self.get_agent(agent_name)
        if not agent:
            return {}

        return {
            "temperature": agent.temperature,
            "steps": agent.steps,
            "model": agent.model,
            "top_p": agent.top_p,
            "permission": agent.permission,
        }

    def check_permission(
            self,
            agent_name: str,
            tool: str,
            pattern: str = "*",
            global_permission: Optional[Dict[str, Any]] = None,
    ) -> str:
        agent = self.get_agent(agent_name)
        if not agent:
            return "allow"

        perm_resolver = PermissionResolver(
            agent.permission, global_permission or {}, agent.tools
        )
        return perm_resolver.resolve(tool, pattern)


def get_available_skills() -> List[Dict]:
    """获取内置 skills 列表"""
    from app.utils.utils import get_local_skills
    return get_local_skills()


def create_agent_manager(agents_dir: Optional[str] = None) -> AgentManager:
    return AgentManager.get_instance(agents_dir)
