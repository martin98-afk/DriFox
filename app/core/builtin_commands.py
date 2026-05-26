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

from pathlib import Path
from typing import Callable, Dict, Any, Optional

import yaml
from loguru import logger

from app.core.command_manager import CommandManager


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
# 命令文件加载器
# ============================================================

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

        return {
            "name": file_path.stem,  # 文件名作为命令名
            "description": meta.get("description", ""),
            "type": meta.get("type", "prompt"),
            "prompt_text": body,  # prompt/agent 类型使用文件内容作为提示词
        }
    except Exception as e:
        logger.error(f"[BuiltinCommands] Failed to load command {file_path}: {e}")
        return None


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
    """注册所有内置命令：动态加载 app/commands/ 目录 + agents 目录智能体"""
    global _registered
    if _registered:
        return

    cmd_mgr = CommandManager.get_instance()

    # 首次注册前清空（确保干净状态）
    for name in list(cmd_mgr.get_command_names()):
        cmd_mgr.unregister(name)

    # ---- 加载命令：优先从 PluginManager，回退到 app/commands/ ----
    commands = _load_commands_from_plugins(cmd_mgr)

    # ---- 加载智能体命令：优先从 PluginManager，回退到 app/agents/ ----
    _register_builtin_agents_as_commands(cmd_mgr)

    logger.info(f"[BuiltinCommands] Registered {len(commands)} commands + agents")
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
                command_type=cmd["type"],
                description=cmd["description"],
                prompt_text=cmd["prompt_text"],
            )
            commands.append(cmd)
    return commands


# ============================================================
# agents 目录加载（通过 PluginManager）
# ============================================================

def _register_builtin_agents_as_commands(cmd_mgr: CommandManager):
    """加载智能体命令：优先 PluginManager 路径，回退到 app/agents/"""
    agent_files = _get_agent_files()
    if not agent_files:
        logger.warning("[BuiltinCommands] No agent files found from any source")
        return

    for md_file in agent_files:
        try:
            content = md_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                continue

            parts = content.split("---", 3)
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

            cmd_mgr.register(
                name=md_file.stem,
                command_type="agent",  # 智能体类型，支持 --subagent 参数
                description=description,
                prompt_text=body,
            )
            logger.info(f"[BuiltinCommands] Registered agent command: /{md_file.stem}")

        except Exception as e:
            logger.error(f"[BuiltinCommands] Failed to load agent {md_file}: {e}")


# ============================================================
# 导出
# ============================================================

__all__ = [
    "register_all_commands",
    "FunctionCommandHandlers",
]