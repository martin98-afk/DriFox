# -*- coding: utf-8 -*-
"""
内置命令注册

从 app/commands/ 目录动态加载命令文件，替代原来的硬编码方式。

命令文件格式（Markdown + Frontmatter）:
```markdown
---
description: 命令描述
type: function|prompt|agent
---

prompt 类型的命令内容作为提示词文本
function 类型仅使用 frontmatter，内容可为空
```

用法：
    from app.core.builtin_commands import register_all_commands
    register_all_commands()

对于 function 类型命令的执行，由 main_widget.py 调用 FunctionCommandHandlers.get(name) 获取处理器。
"""

import json
import time
from pathlib import Path
from typing import Callable, Dict, Any, Optional, List, Tuple
from hashlib import md5

import yaml
from loguru import logger

from app.core.command_manager import CommandManager, CommandType, CommandParameter
from app.tools.tool_name_mapper import ToolNameMapper


# ============================================================
# function 命令的处理器注册表
# ============================================================

class FunctionCommandHandlers:
    """
    function 类型命令的处理器映射表

    用于解耦命令定义和命令执行：
    - builtin_commands.py: 定义命令（从 .md 文件加载）
    - main_widget.py: 注册处理器并执行
    """
    _handlers: Dict[str, Callable] = {}

    @classmethod
    def register(cls, name: str, handler: Callable[[str], None]):
        """注册处理器

        Args:
            name: 命令名
            handler: 处理函数，接受 args 参数 (str)
        """
        cls._handlers[name] = handler
        logger.debug(f"[FunctionHandlers] Registered handler: {name}")

    @classmethod
    def get(cls, name: str) -> Optional[Callable]:
        """获取处理器"""
        return cls._handlers.get(name)

    @classmethod
    def has(cls, name: str) -> bool:
        """检查是否有该命令的处理器"""
        return name in cls._handlers


# ============================================================
# 解析结果缓存（避免每次启动重解析 60+ .md 文件）
# ============================================================

_CACHE_VERSION = 1
_cache_disabled = False  # 调试用，设为 True 跳过缓存


def _get_cache_dir() -> Path:
    """获取缓存目录"""
    from app.utils.utils import get_app_data_dir
    cache_dir = get_app_data_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_cache_file() -> Path:
    return _get_cache_dir() / "commands_cache.json"


def _collect_all_source_files() -> List[Tuple[str, float]]:
    """收集所有命令和智能体文件的 (绝对路径, mtime) 列表
    
    用于生成 cache_key：任一文件变更即触发重新解析。
    """
    files: List[Tuple[str, float]] = []
    seen: set = set()

    for src in _get_command_sources():
        abspath = str(src.resolve())
        if abspath not in seen:
            seen.add(abspath)
            try:
                mtime = src.stat().st_mtime
            except OSError:
                mtime = 0
            files.append((abspath, mtime))

    for src in _get_agent_files():
        abspath = str(src.resolve())
        if abspath not in seen:
            seen.add(abspath)
            try:
                mtime = src.stat().st_mtime
            except OSError:
                mtime = 0
            files.append((abspath, mtime))

    files.sort(key=lambda x: x[0])  # 稳定排序保证一致性
    return files


def _compute_cache_key(files: List[Tuple[str, float]]) -> str:
    """根据文件路径和修改时间计算缓存 key"""
    h = md5()
    h.update(str(_CACHE_VERSION).encode())
    for path, mtime in files:
        h.update(f"{path}:{mtime}\n".encode())
    return h.hexdigest()


def _try_load_cache(key: str) -> Optional[dict]:
    """尝试加载缓存，key 不匹配时返回 None"""
    if _cache_disabled:
        return None
    cache_file = _get_cache_file()
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if data.get("cache_key") == key:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(key: str, commands: list, agents: list):
    """保存缓存"""
    try:
        data = {
            "cache_key": key,
            "version": _CACHE_VERSION,
            "ts": time.time(),
            "commands": commands,
            "agents": agents,
        }
        cache_file = _get_cache_file()
        cache_file.write_text(
            json.dumps(data, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"[BuiltinCommands] 缓存写入失败: {e}")


def _serialize_params(params: list) -> list:
    """将 CommandParameter 列表序列化为可 JSON 序列化的 dict 列表"""
    result = []
    for p in params:
        if hasattr(p, 'name'):
            result.append({
                "name": p.name,
                "description": getattr(p, 'description', ''),
                "param_type": getattr(p, 'param_type', 'flag'),
                "required": getattr(p, 'required', False),
            })
        elif isinstance(p, dict):
            result.append({
                "name": p.get("name", ""),
                "description": p.get("description", ""),
                "param_type": p.get("param_type", "flag"),
                "required": p.get("required", False),
            })
        else:
            result.append({"name": str(p), "description": "", "param_type": "flag", "required": False})
    return result


def _deserialize_params(params: list) -> list:
    """从 JSON 反序列化 CommandParameter 列表"""
    result = []
    for p in params:
        result.append(CommandParameter(
            name=p.get("name", ""),
            description=p.get("description", ""),
            param_type=p.get("param_type", "flag"),
            required=p.get("required", False),
        ))
    return result


def _register_from_cached_commands(cmd_mgr: CommandManager, cached_commands: list) -> int:
    """从缓存数据注册命令，返回注册数"""
    count = 0
    for cmd in cached_commands:
        cmd_mgr.register(
            name=cmd["name"],
            command_type=_parse_command_type(cmd["type"]),
            description=cmd.get("description", ""),
            argument_hint=cmd.get("argument_hint", ""),
            prompt_text=cmd.get("prompt_text", ""),
            parameters=_deserialize_params(cmd.get("parameters", [])),
            shortcut=cmd.get("shortcut", ""),
        )
        count += 1
    return count


def _register_from_cached_agents(cmd_mgr: CommandManager, cached_agents: list) -> int:
    """从缓存数据注册智能体命令，返回注册数"""
    agent_params = [
        CommandParameter("--subagent", "启动子智能体任务（触发 detail 模式）", required=False),
        CommandParameter("--with-context", "传递当前会话历史给子智能体", required=False),
        CommandParameter("--model=", "覆盖模型/服务商，支持: 模型名 / 服务商名 / 服务商:模型名", param_type="value", required=False),
        CommandParameter("<task-desc>", "子智能体任务描述", param_type="positional", required=False),
    ]
    count = 0
    for ag in cached_agents:
        cmd_mgr.register(
            name=ag["name"],
            command_type=CommandType.AGENT,
            description=ag.get("description", ""),
            prompt_text=ag.get("prompt_text", ""),
            parameters=agent_params,
        )
        count += 1
    return count


# ============================================================
# 命令文件加载器
# ============================================================

def _parse_param_name(name_raw: str) -> dict:
    """解析参数名，提取 [] 包裹的可选标记

    Args:
        name_raw: 原始参数名，如 "--branch" 或 "[--verbose]"

    Returns:
        {"name": 清理后的参数名, "required": bool}
    """
    name = name_raw.strip()
    if name.startswith("[") and name.endswith("]"):
        return {"name": name[1:-1].strip(), "required": False}
    return {"name": name, "required": True}


def _parse_raw_params_to_command_params(raw: any) -> list:
    """将 YAML frontmatter 中的参数描述统一转为 CommandParameter 列表

    支持格式：
      1. 数组：parameters: [{name: --branch, desc: ...}, ...]
      2. 字典：parameters: {--branch: desc, [--verbose]: desc}
      3. 字典：argument-hint: {--branch: desc, [--verbose]: desc}（复用旧字段）
    """
    if not raw:
        return []

    params = []

    if isinstance(raw, dict):
        # 格式 2/3：字典 {参数名: 描述}
        for key, value in raw.items():
            parsed = _parse_param_name(key)
            name = parsed["name"]
            desc = str(value) if value else ""
            # 自动推断 param_type
            if name.startswith("--") and name.endswith("="):
                ptype = "value"
            elif name.startswith("--"):
                ptype = "flag"
            else:
                ptype = "positional"
            params.append(CommandParameter(
                name=name, description=desc,
                param_type=ptype, required=parsed["required"],
            ))
    elif isinstance(raw, list):
        # 格式 1：数组 [{name, desc, ...}]
        for p in raw:
            if isinstance(p, str):
                # 列表元素是纯字符串 → 视为 positional 参数名
                parsed = _parse_param_name(p)
                params.append(CommandParameter(
                    name=parsed["name"], description="",
                    param_type="positional", required=parsed["required"],
                ))
                continue
            name = p.get("name", "")
            if not name:
                continue
            parsed = _parse_param_name(name)
            name = parsed["name"]
            desc = p.get("desc", p.get("description", ""))
            # 自动推断 param_type
            if name.startswith("--") and name.endswith("="):
                ptype = "value"
            elif name.startswith("--"):
                ptype = "flag"
            else:
                ptype = "positional"
            # 显式 required 优先，否则从 [] 推断
            required = p.get("required", parsed["required"])
            params.append(CommandParameter(
                name=name, description=desc,
                param_type=ptype, required=required,
            ))

    return params


def _load_command_file(file_path: Path) -> Optional[Dict[str, Any]]:
    """加载单个命令文件"""
    try:
        content = file_path.read_text(encoding="utf-8")

        if not content.startswith("---"):
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        frontmatter = parts[1]
        body = parts[2].strip() if len(parts) > 2 else ""

        meta = yaml.safe_load(frontmatter)
        if not meta:
            return None

        # 生成工具限制说明（有 tools/permission 限制时追加到提示词第一行）
        restriction_text = _generate_tool_restriction_text(meta)
        enhanced_body = restriction_text + body if restriction_text else body

        # 统一解析参数：优先级 parameters > argument-hint（dict）
        raw_params = meta.get("parameters")
        if raw_params is None:
            raw_params = meta.get("argument-hint")
        params = _parse_raw_params_to_command_params(raw_params)

        # 参数来源决定 argument_hint 回退：dict 来源表示已被解析为 params，不再做静态 hint
        raw_argument_hint = meta.get("argument-hint")
        if isinstance(raw_argument_hint, dict):
            argument_hint = ""  # 已被 `argument-hint` 字典解析为参数，不回退到静态文本
        elif isinstance(raw_argument_hint, str):
            argument_hint = raw_argument_hint
        else:
            argument_hint = ""

        return {
            "name": file_path.stem,  # 文件名作为命令名
            "description": meta.get("description", ""),
            "argument_hint": argument_hint,
            "type": meta.get("type", "prompt"),
            "prompt_text": enhanced_body,  # 已包含工具限制说明（如有）
            "parameters": params,
            "shortcut": meta.get("shortcut", ""),
        }
    except Exception as e:
        logger.error(f"[BuiltinCommands] Failed to load command {file_path}: {e}")
        return None


def _parse_command_type(type_str: str) -> CommandType:
    """将 YAML/配置中的字符串类型映射为 CommandType 枚举"""
    mapping = {
        "function": CommandType.FUNCTION,
        "prompt": CommandType.PROMPT,
        "agent": CommandType.AGENT,
    }
    return mapping.get(type_str, CommandType.PROMPT)


def _load_commands_from_dir(commands_dir: Path) -> list:
    """从目录加载所有命令文件"""
    if not commands_dir.exists():
        logger.warning(f"[BuiltinCommands] Commands directory not found: {commands_dir}")
        return []

    commands = []
    for md_file in commands_dir.glob("*.md"):
        cmd = _load_command_file(md_file)
        if cmd:
            commands.append(cmd)
            logger.info(f"[BuiltinCommands] Loaded command: /{cmd['name']} (type={cmd['type']})")

    return commands


# ============================================================
# 注册主入口
# ============================================================

_registered = False  # 模块级标志，避免多窗口重复注册


def register_all_commands():
    """注册所有内置命令：动态加载 app/commands/ 目录 + agents 目录智能体

    支持缓存加速：首次启动时解析所有 .md 文件并缓存结果，
    后续启动若文件未变更则直接加载缓存，避免重复 YAML 解析和文件 IO。
    """
    global _registered
    if _registered:
        return

    cmd_mgr = CommandManager.get_instance()

    # 首次注册前清空（确保干净状态）
    for name in list(cmd_mgr.get_command_names()):
        cmd_mgr.unregister(name)

    # ---- 尝试从缓存加载 ----
    source_files = _collect_all_source_files()
    cache_key = _compute_cache_key(source_files)
    cached = _try_load_cache(cache_key)

    if cached is not None:
        # 缓存命中：直接注册，跳过文件解析
        cmd_count = _register_from_cached_commands(cmd_mgr, cached.get("commands", []))
        agent_count = _register_from_cached_agents(cmd_mgr, cached.get("agents", []))
        logger.info(
            f"[BuiltinCommands] 缓存命中，注册 {cmd_count} 命令 + {agent_count} 智能体"
        )
        # 🛡️ 防御：缓存返回 0 命令时忽略缓存（如 PluginManager 未初始化时写入的空缓存）
        # 重新走完整解析路径以重建正确的缓存
        if cmd_count == 0 and agent_count == 0:
            logger.warning("[BuiltinCommands] 缓存包含 0 命令/0 智能体，忽略缓存，重新解析")
            # 先清掉刚刚注册的 0 条命令
            for name in list(cmd_mgr.get_command_names()):
                cmd_mgr.unregister(name)
        else:
            _registered = True
            return

    # ---- 缓存未命中：逐文件解析 ----
    t0 = time.perf_counter()
    commands = _load_commands_from_plugins(cmd_mgr)
    agents = _register_builtin_agents_as_commands(cmd_mgr)
    elapsed = time.perf_counter() - t0

    logger.info(
        f"[BuiltinCommands] 解析注册完成: {len(commands)} 命令 + {len(agents)} 智能体 "
        f"({elapsed*1000:.0f}ms)"
    )

    # ---- 写回缓存 ----
    serialized_commands = []
    for cmd in commands:
        serialized_commands.append({
            "name": cmd["name"],
            "type": cmd["type"],
            "description": cmd.get("description", ""),
            "argument_hint": cmd.get("argument_hint", ""),
            "prompt_text": cmd.get("prompt_text", ""),
            "parameters": _serialize_params(cmd.get("parameters", [])),
            "shortcut": cmd.get("shortcut", ""),
        })
    serialized_agents = []
    for ag in agents:
        serialized_agents.append({
            "name": ag["name"],
            "description": ag.get("description", ""),
            "prompt_text": ag.get("prompt_text", ""),
        })
    _save_cache(cache_key, serialized_commands, serialized_agents)

    _registered = True


def reload_all_commands():
    """强制重新加载所有内置命令（重置 _registered 标志，用于运行时重载）"""
    global _registered
    _registered = False

    cmd_mgr = CommandManager.get_instance()

    # 清空旧命令
    for name in list(cmd_mgr.get_command_names()):
        cmd_mgr.unregister(name)

    # 重新注册（重新解析 + 更新缓存）
    t0 = time.perf_counter()
    commands = _load_commands_from_plugins(cmd_mgr)
    agents = _register_builtin_agents_as_commands(cmd_mgr)
    elapsed = time.perf_counter() - t0

    logger.info(
        f"[BuiltinCommands] Reloaded {len(commands)} commands + {len(agents)} agents "
        f"({elapsed*1000:.0f}ms)"
    )

    # 重新生成缓存（写回最新解析结果）
    source_files = _collect_all_source_files()
    cache_key = _compute_cache_key(source_files)
    serialized_commands = []
    for cmd in commands:
        serialized_commands.append({
            "name": cmd["name"],
            "type": cmd["type"],
            "description": cmd.get("description", ""),
            "argument_hint": cmd.get("argument_hint", ""),
            "prompt_text": cmd.get("prompt_text", ""),
            "parameters": _serialize_params(cmd.get("parameters", [])),
            "shortcut": cmd.get("shortcut", ""),
        })
    serialized_agents = []
    for ag in agents:
        serialized_agents.append({
            "name": ag["name"],
            "description": ag.get("description", ""),
            "prompt_text": ag.get("prompt_text", ""),
        })
    _save_cache(cache_key, serialized_commands, serialized_agents)

    _registered = True


# ============================================================
# PluginManager 集成
# ============================================================

def _get_command_sources() -> list:
    """获取命令文件源列表"""
    from app.core.plugin_manager import PluginManager
    pm = PluginManager.get_instance()
    if pm.is_initialized():
        cmd_files = pm.get_command_files()
        if cmd_files:
            return cmd_files
    logger.warning("[BuiltinCommands] PluginManager not available, no commands loaded")
    return []


def _get_agent_files() -> list:
    """获取智能体文件列表"""
    from app.core.plugin_manager import PluginManager
    pm = PluginManager.get_instance()
    if pm.is_initialized():
        agent_files = pm.get_agent_files()
        if agent_files:
            return agent_files
    logger.warning("[BuiltinCommands] PluginManager not available, no agents loaded")
    return []


def _load_commands_from_plugins(cmd_mgr: CommandManager) -> list:
    """加载所有命令文件到 CommandManager"""
    cmd_files = _get_command_sources()
    commands = []
    for md_file in cmd_files:
        cmd = _load_command_file(md_file)
        if cmd:
            cmd_mgr.register(
                name=cmd["name"],
                command_type=_parse_command_type(cmd["type"]),
                description=cmd["description"],
                argument_hint=cmd["argument_hint"],
                prompt_text=cmd["prompt_text"],
                parameters=cmd.get("parameters", []),
                shortcut=cmd.get("shortcut", ""),
            )
            commands.append(cmd)
    return commands


# ============================================================
# agents 目录加载（通过 PluginManager）
# ============================================================

def _generate_tool_restriction_text(meta: dict) -> str:
    """从 frontmatter 生成工具限制说明文本（放在提示词第一行）

    支持三种限制字段：
    - tools: 工具白名单列表（仅允许列出的工具）
    - permission: 工具权限配置（deny 表示禁用）
    - allowed-tools: 显式允许的工具列表（来自 Claude Code 等插件格式）

    Args:
        meta: YAML frontmatter 解析结果

    Returns:
        限制说明文本（空字符串表示无限制），格式如：
        "[工具限制] 仅允许以下工具: read, write, edit\n"
    """
    tools = meta.get("tools", None)
    permission = meta.get("permission", None)
    allowed_tools = meta.get("allowed-tools", None)

    lines = []

    # 白名单模式（tools / allowed-tools）与 deny 模式互斥
    # 白名单已隐式拒绝所有不在列表中的工具，无需再输出 deny 行
    whitelist_active = False

    # 白名单模式（tools 字段）
    if tools:
        if isinstance(tools, str):
            # "Read, Glob, Grep, Bash" → ["Read", "Glob", "Grep", "Bash"]
            allowed_list = [t.strip() for t in tools.split(",") if t.strip() and ToolNameMapper.is_known(t.strip())]
        elif isinstance(tools, list):
            allowed_list = [str(t) for t in tools if ToolNameMapper.is_known(t)]
        elif isinstance(tools, dict):
            allowed_list = [str(k) for k, v in tools.items() if v and ToolNameMapper.is_known(k)]
        else:
            allowed_list = []
        if allowed_list:
            mapped = [ToolNameMapper.to_native(t) for t in allowed_list]
            lines.append(f"[工具限制] 仅允许以下工具: {', '.join(mapped)}")
            whitelist_active = True

    # 白名单模式（allowed-tools，来自 Claude Code 等插件格式）
    if allowed_tools and isinstance(allowed_tools, list):
        known = [str(t) for t in allowed_tools if ToolNameMapper.is_known(t)]
        if known:
            mapped = [ToolNameMapper.to_native(t) for t in known]
            lines.append(f"[工具限制] 仅允许以下工具: {', '.join(mapped)}")
            whitelist_active = True

    # deny 模式（仅当没有白名单时才有意义）
    if permission and isinstance(permission, dict) and not whitelist_active:
        denied = [k for k, v in permission.items() if v == "deny"]
        if denied:
            mapped_denied = [ToolNameMapper.to_native(d) for d in denied]
            lines.append(f"[工具限制] 已禁用: {', '.join(mapped_denied)}")

    # 返回：空时返回空字符串（与 agent.py 保持一致，空字符串拼接不产生多余换行）
    return ("\n".join(lines) + "\n\n") if lines else ""


def _register_builtin_agents_as_commands(cmd_mgr: CommandManager) -> List[dict]:
    """加载智能体命令：优先 PluginManager 路径，回退到 app/agents/
    
    Returns:
        已注册的智能体元数据列表，供缓存使用
    """
    agent_files = _get_agent_files()
    registered: List[dict] = []
    if not agent_files:
        logger.warning("[BuiltinCommands] No agent files found from any source")
        return registered

    # 所有智能体命令共享的参数定义
    agent_params = [
        CommandParameter("--subagent", "启动子智能体任务（触发 detail 模式）", required=False),
        CommandParameter("--with-context", "传递当前会话历史给子智能体", required=False),
        CommandParameter("--model=", "覆盖模型/服务商，支持: 模型名 / 服务商名 / 服务商:模型名", param_type="value", required=False),
        CommandParameter("<task-desc>", "子智能体任务描述", param_type="positional", required=False),
    ]

    for md_file in agent_files:
        try:
            content = md_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                continue

            parts = content.split("---", 2)
            if len(parts) < 3:
                continue

            frontmatter = parts[1]
            body = parts[2].strip()

            try:
                meta = yaml.safe_load(frontmatter)
            except Exception as e:
                logger.warning(f"[BuiltinCommands] YAML parse failed for {md_file}: {e}")
                continue
            if not meta:
                continue

            description = meta.get("description", "")

            # 有工具限制时，在提示词第一行追加限制说明
            restriction_text = _generate_tool_restriction_text(meta)
            enhanced_body = restriction_text + body if restriction_text else body

            cmd_mgr.register(
                name=md_file.stem,
                command_type=CommandType.AGENT,
                description=description,
                prompt_text=enhanced_body,
                parameters=agent_params,
            )
            logger.info(f"[BuiltinCommands] Registered agent command: /{md_file.stem}"
                        f"{' (with tool restrictions)' if restriction_text else ''}")
            registered.append({
                "name": md_file.stem,
                "description": description,
                "prompt_text": enhanced_body,
            })

        except Exception as e:
            logger.error(f"[BuiltinCommands] Failed to load agent {md_file}: {e}")

    return registered


# ============================================================
# 导出
# ============================================================

__all__ = [
    "register_all_commands",
    "FunctionCommandHandlers",
]