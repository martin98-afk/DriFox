# -*- coding: utf-8 -*-
"""
内置命令管理器

管理三类内置命令：
1. 函数型命令（FUNCTION）- 选中/发送时执行指定函数
2. 提示词替换命令（PROMPT）- 选中/发送时将命令替换为指定提示词文本，然后继续发送
3. 智能体命令（AGENT/SUBAGENT）- 选中/发送时替换为智能体提示词，或通过 --subagent 启动子智能体

使用方式：
    from app.core.command_manager import CommandManager, CommandType
    
    manager = CommandManager.get_instance()
    manager.register("new", CommandType.FUNCTION, description="新建会话")
    manager.register("init", CommandType.PROMPT, description="项目笔记初始化",
                     prompt_text="请分析此代码库...")

# 获取所有命令（供 CommandCard 显示）
    commands = manager.get_all_commands()

# 在发送前拦截
    result = manager.execute(text)
    if result is not None:
        match result.type:
            case CommandType.FUNCTION:
                handler_map[result.command_name]()
                return  # 已处理，不发送给 AI
            case CommandType.PROMPT | CommandType.AGENT:
                # 用替换文本继续发送
                text = result.replacement
"""
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set


@dataclass
class CommandParameter:
    """命令参数定义（用于 detail 模式交互式参数列表）"""
    name: str                # 显示名称，如 "--with-context", "--model="
    description: str = ""    # 说明文字
    param_type: str = "flag" # "flag" | "value" | "positional"
    required: bool = False   # 是否必填（选填参数在 UI 上显示为灰色/标记）
    value_options: list = field(default_factory=list)  # value 类型的可选值列表（硬编码）


class CommandType(Enum):
    """命令类型枚举"""
    FUNCTION = auto()   # 函数型命令：执行指定函数
    PROMPT = auto()     # 提示词替换命令：替换为提示词后继续发送
    AGENT = auto()      # 智能体命令：替换为智能体提示词后继续发送
    SUBAGENT = auto()   # 子智能体命令：智能体命令 + --subagent 参数，启动子智能体任务


@dataclass
class CommandResult:
    """命令执行结果"""
    type: CommandType            # 命令类型
    command_name: str = ""       # 匹配到的命令名
    replacement: str = ""        # 提示词替换文本（仅 PROMPT/AGENT 命令）
    subagent_task: str = ""      # 子智能体任务描述（仅 SUBAGENT 命令）
    subagent_with_context: bool = False  # --with-context 是否传递主智能体上下文
    subagent_model_value: str = ""      # --model=xxx 原始值（如 "OpenAI:gpt-4o"）
    remainder: str = ""          # 命令后的用户输入（保留部分）


@dataclass
class CommandDefinition:
    """单个命令的定义"""
    name: str
    type: CommandType            # 命令类型枚举
    description: str = ""
    argument_hint: str = ""      # 参数提示（显示在命令卡片 detail 模式）
    prompt_text: str = ""        # PROMPT/AGENT 命令使用
    parameters: List[CommandParameter] = field(default_factory=list)  # 可交互参数列表
    shortcut: str = ""           # 快捷键，如 "Ctrl+Shift+B"

    def to_display_dict(self) -> Dict[str, str]:
        """返回供 CommandCard 显示用的字典"""
        type_map = {
            CommandType.FUNCTION: "command",
            CommandType.PROMPT: "prompt",
            CommandType.AGENT: "agent",
            CommandType.SUBAGENT: "agent",
        }
        display_type = type_map.get(self.type, "command")
        result = {
            "name": self.name,
            "description": self.description,
            "type": display_type,
        }
        if self.shortcut:
            result["shortcut"] = self.shortcut
        return result


def _pick_first_entry(entries: Dict[CommandType, "CommandDefinition"]) -> "CommandDefinition":
    """按优先级 AGENT > PROMPT > FUNCTION 从 entries 中选一个，兜底取第一个"""
    for t in (CommandType.AGENT, CommandType.PROMPT, CommandType.FUNCTION):
        if t in entries:
            return entries[t]
    # 兜底：取第一个注册的类型
    return next(iter(entries.values()))


class CommandManager:
    """
    内置命令管理器（单例）

    负责命令的注册、查询和解析执行。
    不持有 UI 方法引用，执行时由调用方提供 handler 映射。
    """

    _instance: Optional["CommandManager"] = None

    @classmethod
    def get_instance(cls) -> "CommandManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（主要用于测试）"""
        cls._instance = None

    def __init__(self):
        # 按 (名称 → {类型 → 定义}) 组织，同名不同类型可共存
        self._commands: Dict[str, Dict[CommandType, CommandDefinition]] = {}

    # ---- 注册 / 注销 ----

    def register(
        self,
        name: str,
        command_type: CommandType,
        description: str = "",
        argument_hint: str = "",
        prompt_text: str = "",
        parameters: Optional[List[CommandParameter]] = None,
        shortcut: str = "",
    ):
        """注册一个内置命令

        Args:
            name: 命令名（不含 /）
            command_type: CommandType 枚举
            description: 描述文本（显示在命令卡片中）
            argument_hint: 参数提示（如 "<system-dir> | --portfolio <parent-dir>"）
            prompt_text: PROMPT/AGENT 命令使用，替换后的提示词文本
            parameters: 可交互参数列表（用于 detail 模式参数补全）
            shortcut: 快捷键，如 "Ctrl+Shift+B"
        """
        if name not in self._commands:
            self._commands[name] = {}
        self._commands[name][command_type] = CommandDefinition(
            name=name,
            type=command_type,
            description=description,
            argument_hint=argument_hint,
            prompt_text=prompt_text,
            parameters=parameters or [],
            shortcut=shortcut,
        )

    def unregister(self, name: str):
        """取消注册一个命令（移除该名称下的所有类型）"""
        self._commands.pop(name, None)

    def has_command(self, name: str) -> bool:
        """检查命令是否已注册（任一类型）"""
        entries = self._commands.get(name, {})
        return len(entries) > 0

    def get_command(self, name: str) -> Optional[CommandDefinition]:
        """获取命令定义（同名多类型时返回第一个）"""
        entries = self._commands.get(name, {})
        if not entries:
            return None
        # 返回第一个注册的类型（稳定顺序：按 CommandType 枚举值排序）
        for cmd in entries.values():
            return cmd

    # ---- 查询 ----

    def get_all_commands(self) -> List[Dict[str, str]]:
        """获取所有命令的显示列表（供 CommandCard 使用）

        同名不同类型的命令都会返回（如 /review 命令和 /review 智能体同时存在）。
        """
        result = []
        for entries in self._commands.values():
            for cmd in entries.values():
                result.append(cmd.to_display_dict())
        return result

    def get_command_names(self) -> List[str]:
        """获取所有命令名"""
        return list(self._commands.keys())

    # ---- 解析 ----

    # 显示后缀 → 显示类型映射
    _SUFFIX_TO_TYPE = {
        "-skill": "skill",
        "-prompt": "prompt",
        "-cmd": "command",
        "-agent": "agent",
    }

    @staticmethod
    def parse_suffixed_name(name: str):
        """解析带后缀的命令名，返回 (原始名, 显示类型)

        如 "tdd-skill" → ("tdd", "skill")
        如 "tdd" → ("tdd", None)
        """
        for suffix, dtype in CommandManager._SUFFIX_TO_TYPE.items():
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[:-len(suffix)], dtype
        return name, None

    @staticmethod
    def parse_command_name(text: str) -> Optional[str]:
        """从输入文本中提取命令名（去掉 / 前缀）

        "/new" -> "new"
        "/init some text" -> "init"
        "hello" -> None
        """
        text = text.strip()
        if text.startswith("/"):
            parts = text[1:].split(maxsplit=1)
            return parts[0] if parts else None
        return None

    @staticmethod
    def parse_active_params(text: str) -> Set[str]:
        """从输入文本中提取已存在的参数名

        匹配规则：
        - --key=value → "--key="
        - --flag      → "--flag"

        Args:
            text: 输入框文本

        Returns:
            参数名集合，如 {"--with-context", "--model="}
        """
        if not text or not text.strip():
            return set()

        result: Set[str] = set()

        # 1. --key=value 形式的完整参数（如 --model=gpt-4o → "--model="）
        for m in re.finditer(r'--[\w-]+=', text):
            result.add(m.group())

        # 2. 独立 --flag 形式的参数（前后是空白/字符串边界，且不跟 =）
        for m in re.finditer(r'(?:^|\s)(--[\w-]+)(?=\s|$)', text):
            flag = m.group(1)
            if flag + "=" not in result:  # 排除已被 --key= 覆盖的
                result.add(flag)

        return result

    def is_builtin_command(self, text: str) -> bool:
        """判断输入文本是否匹配某个已注册的内置命令"""
        name = self.parse_command_name(text)
        if name is None:
            return False
        # 去除后缀检查
        base_name, suffix_type = self.parse_suffixed_name(name)
        if suffix_type == "skill":
            return False  # 技能由外部处理
        return self.has_command(base_name or name)

    def is_known_command_name(self, name: str) -> bool:
        """根据命令名判断是否为内置命令（不含 /，任一类型存在即可）"""
        base_name, suffix_type = self.parse_suffixed_name(name)
        if suffix_type == "skill":
            return False
        return self.has_command(base_name or name)

    # ---- 执行 ----

    # 显示类型 → CommandType 映射（来自 CommandCard 的 display_type）
    _DISPLAY_TYPE_MAP = {
        "command": CommandType.FUNCTION,
        "prompt": CommandType.PROMPT,
        "agent": CommandType.AGENT,
    }

    def execute(self, text: str,
                preferred_display_type: Optional[str] = None) -> Optional[CommandResult]:
        """解析并执行命令

        Args:
            text: 用户输入的完整文本
            preferred_display_type: 可选，来自 CommandCard 的 display_type
                （"command"/"prompt"/"agent"/"skill"），按此类型优先匹配执行

        Returns:
            Optional[CommandResult]:
            - None: 不是内置命令（或指定为 skill 类型）
            - CommandResult(type=FUNCTION): 函数命令，调用方需执行对应 handler
            - CommandResult(type=PROMPT): 提示词替换命令，使用 replacement 作为发送文本
            - CommandResult(type=AGENT): 智能体命令，使用 replacement 作为发送文本
            - CommandResult(type=SUBAGENT): 智能体命令 + --subagent，触发子智能体任务

        同名不同类型同时存在时执行策略：
        1. 如果命令名带后缀（如 "tdd-skill"），提取原始名+目标类型，优先匹配
        2. 如果提供了 preferred_display_type（来自卡片选中），优先匹配该类型
        3. 否则按优先级：AGENT > PROMPT > FUNCTION
           （保持与原有"智能体覆盖命令"行为一致）
        """
        cmd_name = self.parse_command_name(text)
        if not cmd_name:
            return None

        # 解析后缀：如 "tdd-skill" → base="tdd", type="skill"
        base_name, suffix_type = self.parse_suffixed_name(cmd_name)

        # skill 类型不由 CommandManager 处理，返回 None 由主流程走技能替换
        if suffix_type == "skill":
            return None

        # 有后缀时优先使用后缀推断的类型
        if suffix_type:
            preferred_display_type = preferred_display_type or suffix_type
            cmd_name = base_name

        if cmd_name not in self._commands:
            return None

        entries = self._commands[cmd_name]
        if not entries:
            return None

        # 选取匹配类型：优先卡片选中/后缀推断 → 回落默认优先级
        if preferred_display_type:
            preferred = self._DISPLAY_TYPE_MAP.get(preferred_display_type)
            if preferred and preferred in entries:
                cmd = entries[preferred]
            else:
                cmd = _pick_first_entry(entries)
        else:
            cmd = _pick_first_entry(entries)

        # 提取命令后的用户输入（保留部分）
        remainder = ""
        text_stripped = text.strip()
        if text_stripped.startswith("/"):
            first_space = text_stripped.find(" ")
            if first_space > 0:
                remainder = text_stripped[first_space + 1:]

        if cmd.type == CommandType.FUNCTION:
            return CommandResult(
                type=CommandType.FUNCTION,
                command_name=cmd_name,
                remainder=remainder,
            )

        # PROMPT / AGENT：检查 --subagent 参数
        if cmd.type in (CommandType.AGENT, CommandType.PROMPT) and remainder:
            subagent_match = remainder.find("--subagent")
            if subagent_match >= 0:
                # 解析 --subagent 参数
                before_subagent = remainder[:subagent_match].rstrip()
                after_subagent = remainder[subagent_match + len("--subagent"):].lstrip()

                # 解析子智能体 flag：--with-context, --model=xxx
                text_for_task, subagent_with_context, subagent_model_value = (
                    self._parse_subagent_flags(after_subagent)
                )

                if cmd.type == CommandType.AGENT:
                    # AGENT 命令：用自己作为子智能体执行
                    # 找到下一个 -- 参数的位置（从任务描述中分离后续 -- 参数）
                    next_flag_match = re.search(r"(?:^|\s)(--)", text_for_task)
                    if next_flag_match:
                        split_pos = next_flag_match.start(1)
                        subagent_task = text_for_task[:split_pos].strip()
                        remainder_after_subagent = text_for_task[split_pos:].strip()
                    else:
                        subagent_task = text_for_task.strip()
                        remainder_after_subagent = ""

                    # 重新组装 remainder
                    parts = [p for p in (before_subagent, remainder_after_subagent) if p]
                    remainder = " ".join(parts)

                    return CommandResult(
                        type=CommandType.SUBAGENT,
                        command_name=cmd_name,
                        subagent_task=subagent_task,
                        subagent_with_context=subagent_with_context,
                        subagent_model_value=subagent_model_value,
                        remainder=remainder,
                    )
                else:
                    # PROMPT 命令：使用统一任务执行智能体（task-executor）
                    # 合并所有用户参数（--subagent 之前 + 移除 --with-context/--model= 之后）
                    user_params_parts = [p for p in (before_subagent, text_for_task) if p]
                    user_params = " ".join(user_params_parts) if user_params_parts else ""

                    # 构造子智能体任务：命令提示词 + 用户参数
                    task = f"请执行 /{cmd_name} 命令。"
                    task += f"\n\n命令提示词：\n---\n{cmd.prompt_text}\n---"
                    if user_params:
                        task += f"\n\n用户参数：\n{user_params}"

                    return CommandResult(
                        type=CommandType.SUBAGENT,
                        command_name="task-executor",
                        subagent_task=task,
                        subagent_with_context=subagent_with_context,
                        subagent_model_value=subagent_model_value,
                        remainder="",
                    )

        # 正常的 PROMPT / AGENT 提示词替换
        return CommandResult(
            type=cmd.type,  # PROMPT or AGENT
            command_name=cmd_name,
            replacement=cmd.prompt_text,
            remainder=remainder,
        )

    @staticmethod
    def _parse_subagent_flags(text: str):
        """从文本开头解析 --with-context 和 --model=xxx 标志

        支持 --model="Azure OpenAI:gpt-4o" 带引号值的格式。
        返回 (剩余文本, with_context, model_value)。
        """
        with_context = False
        model_value = ""

        while text:
            text = text.lstrip()
            if text.startswith("--with-context"):
                with_context = True
                text = text[len("--with-context"):].lstrip()
            elif text.startswith("--model="):
                eq_pos = text.find("=")
                after_eq = text[eq_pos + 1:]

                # 支持带引号的值：--model="Azure OpenAI gpt-4o"
                if after_eq.startswith('"'):
                    close_quote = after_eq.find('"', 1)
                    if close_quote >= 0:
                        model_value = after_eq[1:close_quote]
                        text = after_eq[close_quote + 1:].lstrip()
                    else:
                        # 没有闭合引号，取到末尾
                        model_value = after_eq[1:]
                        text = ""
                else:
                    space_pos = after_eq.find(" ")
                    if space_pos < 0:
                        model_value = after_eq
                        text = ""
                    else:
                        model_value = after_eq[:space_pos]
                        text = after_eq[space_pos:].lstrip()
            else:
                break

        return text, with_context, model_value
