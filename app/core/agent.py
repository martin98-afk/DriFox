# -*- coding: utf-8 -*-
"""
智能体模块 - OpenCode 风格的 agent 配置系统。

支持 Markdown 格式定义、Permission 系统、Primary/Subagent/Hidden 模式。
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

import yaml
from loguru import logger

from app.core.builtin_commands import _generate_tool_restriction_text
from app.core.hook_manager import HookManager
from app.tools import get_builtin_tools_schema
from app.tools.tool_name_mapper import ToolNameMapper

# 文件级 mtime 缓存：{插件名/目录: {文件路径: (mtime, Agent)}}
# 增量重载时未变更文件不重新 parse，直接复用缓存中的 Agent 对象重新登记，
# 避免「新增 1 个 agent → 插件全量重解析所有 agent 文件」的热重载开销。
_agent_file_cache: Dict[str, Dict[str, tuple]] = {}


def _subagent_forbidden_tools() -> set:
    """子智能体调用时禁止使用的工具集合（registry 派生，不硬编码工具名）。

    规则（与旧硬编码 {question, subagent_para, subagent_status, subagent_dag} 等价）：
    - metadata["interactive"]：交互式提问（question）
    - group=="子智能体"：嵌套子智能体工具（subagent_para/subagent_status/subagent_dag）
    """
    try:
        from app.tools.registry import ToolRegistry

        reg = ToolRegistry.get_instance()
        return {
            r.name for r in reg.list()
            if (r.metadata or {}).get("interactive") or r.group == "子智能体"
        }
    except Exception:
        # registry 不可用：回退旧集合（保持过滤语义不失效）
        return {"question", "subagent_para", "subagent_status", "subagent_dag"}


@dataclass
class Agent:
    name: str
    description: str
    mode: Optional[str] = None  # None 表示未声明，不显示在 UI；"primary"/"all" 表示可作为主智能体
    permission: Dict[str, Any] = field(default_factory=dict)
    temperature: Optional[float] = None
    steps: Optional[int] = None
    model: Optional[str] = None  # None/"inherit" 表示继承主智能体模型；具体值表示使用指定模型
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

    def is_model_inherit(self) -> bool:
        """是否继承主智能体模型配置（已弃用：子智能体统一继承主智能体模型）"""
        return True

    @classmethod
    def from_dict(cls, data: Dict) -> "Agent":
        tools = data.get("tools", {})
        if isinstance(tools, str):
            # "Read, Glob, Grep, Bash" → ["Read", "Glob", "Grep", "Bash"]
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        if isinstance(tools, list):
            tools = {ToolNameMapper.to_native(t): True for t in tools if ToolNameMapper.is_known(t)}
        elif isinstance(tools, dict):
            tools = {ToolNameMapper.to_native(k): v for k, v in tools.items() if ToolNameMapper.is_known(k)}
        elif tools is None:
            tools = {}
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            mode=data.get("mode", "subagent"),
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
        """是否可作为子智能体：mode 为 subagent/all，或未声明 mode（外部导入智能体默认可作为子智能体）"""
        # primary 模式下明确不可作为子智能体
        if self.mode == "primary":
            return False
        # subagent/all 模式可作为子智能体
        if self.mode in ("subagent", "all"):
            return True
        # mode=None 或其他情况（外部导入智能体默认可作为子智能体）
        return True

    def is_hidden(self) -> bool:
        """是否隐藏（显式 hidden=True 或未声明 mode）"""
        return self.hidden is True or self.mode is None


class PermissionResolver:
    """
    工具权限解析器

    根据配置解析工具调用的权限判定（allow/ask/deny）。

    解析优先级（从高到低）：
    1. agent-specific permission_config 中的工具名
    2. agent-specific permission_config 中的通配符 `*`
    3. DEFAULT_PERMISSIONS 中定义的默认规则

    设计：
    - 用于智能体级别的权限解析（仅限 agent.yaml 中配置了 permission_config 的智能体）
    - 全局级别的权限解析直接由 AuthService 处理（不在此类）
    - 内部使用缓存避免重复解析同名工具
    """

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
            self._tools_config = {ToolNameMapper.to_native(t): True for t in tools_config if ToolNameMapper.is_known(t)}
        else:
            self._tools_config = {
                ToolNameMapper.to_native(k): v for k, v in tools_config.items() if ToolNameMapper.is_known(k)
            }
        self._cache: Dict[tuple, str] = {}
        self._task_cache: Dict[str, str] = {}

    def resolve(self, tool: str, pattern: str = "*") -> str:
        tool = ToolNameMapper.to_native(tool)
        cache_key = (tool, pattern)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 白名单语义：如果 tools 非空，未列出的工具全部拒绝
        if self._tools_config:
            if tool in self._tools_config:
                result = "allow" if self._tools_config[tool] else "deny"
                self._cache[cache_key] = result
                return result
            else:
                # 工具不在白名单中 → 拒绝
                self._cache[cache_key] = "deny"
                return "deny"

        # tools 为空 → 退回到权限配置和默认行为
        if "*" in self._tools_config:
            # 这条路径现在不可达（上面非空已返回），保留用于日志兼容
            result = "allow" if self._tools_config.get("*", True) else "deny"
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

        # 遍历所有 key 的别名变体，保证匹配
        for config in (self._global, self._config):
            tool_config = self._get_any_key(config, tool)
            if tool_config == {}:
                # 【修复】_get_any_key 返回 {} 表示该 tool 在 config 中没有具体规则。
                # 此时收集通配符 `*` 配置作为兜底（_get_any_key 不处理通配符 key），
                # 解决 `permission: {"*": "deny"}` 这类配置被静默吞掉的问题。
                # 关键：必须在"非空 dict"分支之前判断，否则空 dict 会被当作嵌套规则处理。
                if "*" in config and not isinstance(config["*"], dict):
                    rules.append(("*", config["*"]))
            elif isinstance(tool_config, str):
                # 字符串值：该 tool 的精确规则
                rules.append(("*", tool_config))
            elif isinstance(tool_config, dict):
                # 非空 dict 值：嵌套 pattern 规则
                for k, v in tool_config.items():
                    rules.append((k, v))

        return rules

    @staticmethod
    def _get_any_key(d: dict, native_key: str) -> object:
        """从字典中获取指定键的值，支持原生名查找（兼容大小写别名）

        优先精确匹配，然后尝试别名匹配。
        """
        # 先尝试精确匹配
        if native_key in d:
            return d[native_key]
        # 再尝试原生名匹配（如果 native_key 本身是别名，先转原生）
        native = ToolNameMapper.to_native(native_key)
        if native in d:
            return d[native]
        # 遍历所有 key 做别名匹配
        for k in d:
            if ToolNameMapper.to_native(k) == native:
                return d[k]
        return {}

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
    def get_instance(
        cls, agents_dir: Optional[str] = None, hook_manager: Optional[HookManager] = None
    ) -> "AgentManager":
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
        # 插件来源跟踪：plugin_name -> set of agent_names，用于增量重载
        self._plugin_agents: Dict[str, Set[str]] = {}
        # 系统提示词缓存：避免每次 tool iteration 都重新触发 BuildSystemPrompt hooks
        # key: (agent_name, is_subagent_call), value: system_prompt string
        self._system_prompt_cache: Dict[tuple, str] = {}
        self._load_agents()

    def _load_agents(self, force: bool = False):
        """加载智能体：插件路径优先，后备路径兜底

        Args:
            force: 为 True 时强制重新加载 hooks（reload_agents 时调用）
        """
        # 1. 从所有已启用插件加载（PluginManager 已初始化时）
        self._load_agents_from_plugins()

        # 2. 从后备目录加载（兼容旧配置）
        if self.agents_dir and self.agents_dir.exists():
            self._load_agents_from_dir(self.agents_dir)

        # 3. 加载 hooks
        if self._hook_manager is not None:
            # 3a. 插件 hooks 目录（顶层 hooks/）
            self._load_plugin_hooks()

            # 3b. [已移除] 旧技能 hooks 加载路径，hooks 全部来自插件 hooks/

    def _load_agents_from_plugins(self):
        """从所有已启用插件加载智能体（跟踪每个智能体的来源插件）"""
        try:
            from app.core.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for plugin in pm.get_enabled_plugins():
                    agent_dir = plugin.path / "agents"
                    if agent_dir.exists():
                        self._load_agents_from_dir(agent_dir, source_plugin=plugin.name)
        except ImportError, Exception:
            pass

    def reload_agents(self):
        """重新从已启用插件加载智能体和 hooks（运行时重载用）"""
        self._agents.clear()
        self._hidden_agents.clear()

        # 先注销所有插件级 hooks，再重新加载（force=True 确保全量刷新）
        if self._hook_manager is not None:
            self._unload_plugin_hooks()

        self._load_agents(force=True)
        logger.info(f"[AgentManager] Reloaded agents: {len(self._agents)} visible, {len(self._hidden_agents)} hidden")

    def reload_plugin_agents(self, plugin_name: str) -> int:
        """只重载指定插件的智能体和 hooks（增量重载，不清除其他插件）

        Args:
            plugin_name: 插件名称

        Returns:
            加载的智能体数量（0 表示插件无 agents 目录或不存在）
        """
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance()

        # 跳过未启用的插件
        if not pm.is_enabled(plugin_name):
            logger.debug(f"[AgentManager] Plugin '{plugin_name}' is disabled, skip reload")
            return 0

        plugin = pm.get_plugin(plugin_name)
        if not plugin or not plugin.path.exists():
            return 0

        # 1. 移除该插件之前加载的智能体
        for agent_name in self._plugin_agents.get(plugin_name, set()):
            self._agents.pop(agent_name, None)
            self._hidden_agents.pop(agent_name, None)
        self._plugin_agents[plugin_name] = set()

        # 2. 重载该插件的 hooks
        if self._hook_manager is not None:
            self._unload_plugin_hooks_for_plugin(plugin)

        # 3. 重新加载该插件的智能体
        agent_dir = plugin.path / "agents"
        if agent_dir.exists():
            self._load_agents_from_dir(agent_dir, source_plugin=plugin_name)

        count = len(self._plugin_agents.get(plugin_name, set()))
        logger.info(
            f"[AgentManager] Reloaded plugin agents: plugin={plugin_name}, "
            f"count={count}, total_visible={len(self._agents)}"
        )
        return count

    def reload_plugin_hooks(self, plugin_name: str) -> bool:
        """只重载指定插件的 hooks，不涉及智能体（hooks-only 增量重载）

        Args:
            plugin_name: 插件名称

        Returns:
            True 表示成功处理（可能有 hooks 文件），False 表示插件不存在/无 hooks 目录
        """
        if self._hook_manager is None:
            return False
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if not pm.is_enabled(plugin_name):
            return False
        plugin = pm.get_plugin(plugin_name)
        if not plugin:
            return False
        hooks_dir = plugin.path / "hooks"
        hooks_file = hooks_dir / "hooks.json"
        # 先清理老的 hooks 注册（无论目录是否存在都要清理）
        self._hook_manager.unregister_skill_hooks(plugin.name)
        if hooks_file.exists():
            self._hook_manager._clear_config_watcher(str(hooks_file))
        if hooks_dir.exists() and hooks_dir.is_dir():
            # is_system_plugin 用于标记系统内置插件的 hook，在 UI 上禁止删除
            self._hook_manager.load_hooks_from_directory_flat(
                hooks_dir, skill_name=plugin.name, is_system_plugin=plugin.is_system
            )
            return True
        return False

    def _unload_plugin_hooks_for_plugin(self, plugin):
        """注销并重新加载指定插件的 hooks（使用插件名作为 skill key）"""
        if self._hook_manager is None:
            return
        hooks_dir = plugin.path / "hooks"
        hooks_file = hooks_dir / "hooks.json"
        if hooks_dir.exists() and hooks_dir.is_dir():
            # 清除配置去重缓存，允许用新 key 重新注册
            self._hook_manager._clear_config_watcher(str(hooks_file))
            self._hook_manager.unregister_skill_hooks(plugin.name)
            # is_system_plugin 用于标记系统内置插件的 hook，在 UI 上禁止删除
            self._hook_manager.load_hooks_from_directory_flat(
                hooks_dir, skill_name=plugin.name, is_system_plugin=plugin.is_system
            )

    def _unload_plugin_hooks(self):
        """注销所有插件级 hooks（reload 时调用）"""
        try:
            from app.core.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for plugin in pm.get_enabled_plugins():
                    hooks_dir = plugin.path / "hooks"
                    hooks_file = hooks_dir / "hooks.json"
                    if hooks_dir.exists() and hooks_dir.is_dir():
                        self._hook_manager._clear_config_watcher(str(hooks_file))
                        self._hook_manager.unregister_skill_hooks(plugin.name)
        except ImportError, Exception:
            pass

    def cleanup_plugin_artifacts(self, plugin_name: str):
        """插件被删除时，清理该插件在 AgentManager/HookManager 中的残留数据

        包括：智能体、hidden 智能体、插件来源跟踪、hooks 注册、文件级 mtime 缓存。
        不碰命令和主题（由 backend._reload_single_plugin 统一处理）。
        """
        # 1. 清理智能体
        for agent_name in self._plugin_agents.get(plugin_name, set()):
            self._agents.pop(agent_name, None)
            self._hidden_agents.pop(agent_name, None)
        self._plugin_agents.pop(plugin_name, None)

        # 2. 清理 hooks
        if self._hook_manager is not None:
            self._hook_manager.unregister_skill_hooks(plugin_name)

        # 3. 清理文件级 mtime 缓存（防止插件重装时复用已删除插件的旧 Agent 对象）
        _agent_file_cache.pop(plugin_name, None)

        logger.info(f"[AgentManager] Cleaned up artifacts for removed plugin: {plugin_name}")

    def _load_plugin_hooks(self):
        """加载插件顶层 hooks/ 目录中的 hooks（使用插件名作为 skill key）"""
        try:
            from app.core.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for plugin in pm.get_enabled_plugins():
                    hooks_dir = plugin.path / "hooks"
                    if not hooks_dir.exists() or not hooks_dir.is_dir():
                        continue
                    # 使用插件名作为 skill_name，确保每个插件的 hooks 独立可寻址
                    # is_system_plugin 用于标记系统内置插件的 hook，在 UI 上禁止删除
                    self._hook_manager.load_hooks_from_directory_flat(
                        hooks_dir, skill_name=plugin.name, is_system_plugin=plugin.is_system
                    )
        except ImportError, Exception:
            pass

    def _load_agents_from_dir(self, agents_dir: Path, source_plugin: str = None):
        """从指定目录加载所有智能体

        带文件级 mtime 缓存：解析前比对文件 mtime，未变更的文件不重新 parse，
        直接复用缓存中的 Agent 对象重新登记（保证 _plugin_agents / _agents 不丢注册）。

        Args:
            agents_dir: 智能体目录路径
            source_plugin: 来源插件名称（用于增量重载跟踪）
        """
        if not agents_dir.exists():
            return

        # 缓存 key：插件名（增量重载）或目录路径（后备目录加载）
        cache_key = source_plugin or str(agents_dir)
        file_cache = _agent_file_cache.setdefault(cache_key, {})

        for md_file in agents_dir.glob("*.md"):
            try:
                fpath = str(md_file)
                try:
                    mtime = md_file.stat().st_mtime
                except OSError:
                    mtime = 0.0
                cached = file_cache.get(fpath)
                if cached is not None and cached[0] == mtime:
                    # 文件未变更：复用缓存对象，不重新 parse（不打印 Loaded）
                    agent = cached[1]
                else:
                    agent = self._parse_markdown_agent(md_file)
                    if agent:
                        file_cache[fpath] = (mtime, agent)
                        logger.info(
                            f"[AgentManager] Loaded agent: {agent.name} (mode={agent.mode}, hidden={agent.hidden})"
                        )
                if agent:
                    # 同名智能体去重：先加载的（系统插件优先）保留，后续的跳过
                    target_dict = self._hidden_agents if agent.is_hidden() else self._agents
                    if agent.name in target_dict:
                        logger.debug(
                            f"[AgentManager] Skip duplicate agent: {agent.name} (already loaded, source={md_file})"
                        )
                        continue
                    if source_plugin:
                        self._plugin_agents.setdefault(source_plugin, set()).add(agent.name)
                    target_dict[agent.name] = agent
            except Exception as e:
                logger.error(f"[AgentManager] Failed to load {md_file}: {e}")

        for yaml_file in agents_dir.glob("*.yaml"):
            try:
                fpath = str(yaml_file)
                try:
                    mtime = yaml_file.stat().st_mtime
                except OSError:
                    mtime = 0.0
                cached = file_cache.get(fpath)
                if cached is not None and cached[0] == mtime:
                    # 文件未变更：复用缓存对象，不重新 parse（不打印 Loaded）
                    agent = cached[1]
                else:
                    agent = self._parse_yaml_agent(yaml_file)
                    if agent:
                        file_cache[fpath] = (mtime, agent)
                        logger.info(f"[AgentManager] Loaded agent (yaml): {agent.name}")
                if agent:
                    target_dict = self._hidden_agents if agent.is_hidden() else self._agents
                    if agent.name in target_dict:
                        logger.debug(
                            f"[AgentManager] Skip duplicate agent: {agent.name} (already loaded, source={yaml_file})"
                        )
                        continue
                    if source_plugin:
                        self._plugin_agents.setdefault(source_plugin, set()).add(agent.name)
                    target_dict[agent.name] = agent
            except Exception as e:
                logger.error(f"[AgentManager] Failed to load {yaml_file}: {e}")

    def _parse_markdown_agent(self, file_path: Path) -> Optional[Agent]:
        content = file_path.read_text(encoding="utf-8")

        if not content.startswith("---"):
            return None

        # 兼容 body 中包含 --- 分隔符的情况（如代码块分隔）
        # 使用 split("---", 2) 保留所有 body 内容
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        # parts[0] = "", parts[1] = frontmatter, parts[2] = body (完整)
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

        # 在提示词第一行追加工具限制说明
        restriction_text = _generate_tool_restriction_text(meta)
        if restriction_text:
            agent.prompt = restriction_text + body
        else:
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

    def list_subagents(self, include_hidden: bool = True) -> List[Agent]:
        agents = self._agents.values()
        if include_hidden:
            agents = list(agents) + list(self._hidden_agents.values())
        return [a for a in agents if a.is_subagent()]

    def list_subagent_names(self, include_hidden: bool = True) -> List[str]:
        """获取所有子智能体的名称列表（用于工具 schema enum）"""
        agents = self.list_subagents(include_hidden=include_hidden)
        return [a.name for a in agents]

    def get_available_subagents_for_prompt(self, include_hidden: bool = True) -> str:
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

        lines = ["## Available Subagents\n可直接使用的子智能体列表(可供subagent_para和subagent_dag使用)："]
        for a in agents:
            lines.append(f"- **{a.name}**: {a.description[:300]}")

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
        self,
        agent_name: str,
        global_permission: Optional[Dict[str, Any]] = None,
        is_subagent_call: bool = False,
        builtin_tools=None,
    ) -> List[Dict]:
        """获取智能体的工具 schema。

        Args:
            agent_name: 智能体名称
            global_permission: 全局权限覆盖
            is_subagent_call: 是否为子智能体调用
            builtin_tools: BuiltinTools 实例，用于获取团队上下文。
                          多窗口场景下必须传入当前窗口的实例，否则 is_in_team
                          检查会使用 AgentManager 单例的最后覆盖值（可能指向错误窗口）。
        """
        agent = self.get_agent(agent_name)
        if not agent:
            return []

        # 优先使用调用方传入的 builtin_tools（多窗口隔离），回退到 AgentManager 单例的引用
        _bt = builtin_tools if builtin_tools is not None else self._builtin_tools

        all_tools = get_builtin_tools_schema(self, builtin_tools=_bt)

        # 【新增】子智能体禁止使用交互和嵌套子智能体工具（需要用户交互或发布子智能体，不支持）
        # registry 派生：interactive（question）+ 子智能体组（subagent_para/subagent_status/subagent_dag）
        forbidden_tools = _subagent_forbidden_tools()
        if is_subagent_call:
            # 被主智能体调用时，强制过滤
            all_tools = [t for t in all_tools if t["function"]["name"].lower() not in forbidden_tools]

        # 团队协作工具：仅当当前窗口已加入团队时才暴露
        # 避免浪费 token 和误导 LLM（非团队成员看到也用不了）
        # 【多窗口修复】使用传入的 builtin_tools（_bt）而非 self._builtin_tools，
        # 确保 is_in_team 检查的是当前窗口的团队状态，而非全局单例的最后覆盖值。
        is_in_team = False
        bt_window_id = ""

        if _bt:
            bt_window_id = getattr(_bt, "_team_window_id", "")
            if bt_window_id:
                try:
                    from app.core.team_manager import TeamManager

                    is_in_team = TeamManager.get_instance().is_team_member(bt_window_id)
                except Exception:
                    pass  # 团队管理器未就绪时，不暴露团队工具

        logger.debug(
            f"[TeamToolsSchema] agent={agent_name}, is_subagent_call={is_subagent_call}, "
            f"team_window_id={bt_window_id!r}, is_in_team={is_in_team}"
        )

        # 团队专用工具（registry 标记 team_only=True，可扩展）：
        # 非团队成员从 schema 定义中过滤（LLM 看不到），仅团队成员可用。
        from app.tools.registry import ToolRegistry
        from app.tools.tool_name_mapper import ToolNameMapper

        # D10：统一大小写/别名后比较（registry 原生名与 LLM 传入名可能大小写/别名不同）
        team_only_tools = set(ToolRegistry.get_instance().team_only_tools())
        team_only_norm = {ToolNameMapper.to_native(n).lower() for n in team_only_tools}

        filtered_tools = []
        for tool in all_tools:
            tool_name = ToolNameMapper.to_native(tool["function"]["name"]).lower()
            # 团队专用工具：仅团队成员可见
            if tool_name in team_only_norm:
                if is_in_team:
                    filtered_tools.append(tool)
                continue
            # ★ T24（按产品指示）：schema 保持静态完整——**不过滤 deny 工具**。
            # 工具定义在请求开头一次性给全（prompt 缓存稳定，不因 UI/运行态
            # 变化导致 tools 参数漂移），deny 由执行层（engine/subagent_worker
            # 的 _check_ui_tool_permission）实时拦截。UI 调整立即生效且缓存不丢。
            filtered_tools.append(tool)

        return filtered_tools

    def get_agent_system_prompt(
        self,
        agent_name: str,
        base_prompt: str = "",
        is_subagent_call: bool = False,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        获取智能体的系统提示词。

        Args:
            agent_name: 智能体名称
            base_prompt: 基础提示词（通常为 skill 内容）
            is_subagent_call: 是否为子智能体调用上下文。
                - True: 主智能体通过 subagent_para 调用子智能体（子智能体看到的是任务描述，不是完整上下文）
                - False: 主智能体自身运行，或子智能体独立运行
            extra_context: 额外上下文（如 project_root/project_name），传递给 BuildSystemPrompt hook
        """
        # ── 主智能体身份回退：仅当未指定 agent 时使用全局默认 ──
        # 多窗口场景下，每个窗口的 ChatEngine 已维护自己的 _current_agent，
        # 不再全局覆盖。仅当 agent_name 为 "build"（引擎默认值）且用户通过
        # hook 设置卡片选择了其他智能体时，才回退到全局设置。
        if not is_subagent_call and agent_name in ("build", "", None):
            try:
                from app.utils.config import Settings

                primary_agent = Settings.get_instance().llm_primary_agent.value
                if primary_agent and primary_agent != agent_name:
                    if self.get_agent(primary_agent):
                        agent_name = primary_agent
            except Exception:
                pass

        agent = self.get_agent(agent_name)

        # ── 构建 agent identity 内容，通过 context 传递给 BuildSystemPrompt hook ──
        # 主智能体：hook (inject_agent_identity) 负责注入；子智能体：直接作为 base
        if agent:
            if agent.prompt:
                agent_identity = agent.prompt
            else:
                agent_identity = f"""# {agent.name}
{agent.description}

## Available Tools
Use the tools available to you based on your permissions."""
        else:
            agent_identity = base_prompt or ""

        # ── 构建 base prompt ──
        # 子智能体：agent 身份作为 base + 任务描述（base_prompt）附加
        # 主智能体：agent 身份由 hook 注入，base 仅为 base_prompt（通常为空）
        if is_subagent_call:
            base = agent_identity
            if base_prompt and base:
                base = base + "\n\n" + base_prompt
            elif base_prompt:
                base = base_prompt
        else:
            base = base_prompt or ""

        # Collect BuildSystemPrompt hook contributions
        if self._hook_manager:
            # 预取智能体身份/技能内容/子智能体列表，通过 context 传递给 hooks
            # （hooks 不能直接 import app 内部模块，所有数据必须从 context 读取）
            hook_ctx: Dict[str, Any] = {
                "agent_name": agent_name,
                "is_subagent_call": is_subagent_call,
                "current_role": "subagent" if is_subagent_call else "primary",
                "agent_identity_content": agent_identity,
            }
            # 合并外部上下文（由 context_builder 传入 project_root/project_name）
            if extra_context:
                hook_ctx.update(extra_context)
            # 技能内容
            try:
                from app.utils.config import Settings

                enabled_skills = Settings.get_instance().llm_enabled_skills.value
                if enabled_skills:
                    sk_content = self.get_enabled_skills_content(enabled_skills)
                    if sk_content:
                        hook_ctx["enabled_skills_content"] = sk_content
            except Exception:
                pass
            # 子智能体列表（仅主智能体需要）
            if not is_subagent_call:
                try:
                    sub_info = self.get_available_subagents_for_prompt()
                    if sub_info:
                        hook_ctx["available_subagents_content"] = sub_info
                except Exception:
                    pass
            results = self._hook_manager.trigger_event(
                "BuildSystemPrompt",
                context=hook_ctx,
                current_message="",
                trigger_async=False,
            )
            contributions = [r.output.strip() for r in results if r.success and r.output.strip()]
            if contributions:
                base = base + "\n\n" + "\n\n".join(contributions)

        return base

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
                display_name = skill.get("qualified_name", skill["name"])
                result_parts.append(f"\n### {display_name}\n{skill.get('description', '')}\n")

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

        perm_resolver = PermissionResolver(agent.permission, global_permission or {}, agent.tools)
        return perm_resolver.resolve(tool, pattern)


def get_available_skills() -> List[Dict]:
    """获取内置 skills 列表"""
    from app.utils.utils import get_local_skills

    return get_local_skills()


def create_agent_manager(agents_dir: Optional[str] = None) -> AgentManager:
    return AgentManager.get_instance(agents_dir)
