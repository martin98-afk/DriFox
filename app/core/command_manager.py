# -*- coding: utf-8 -*-
"""
内置命令管理器

管理两类内置命令：
1. 函数型命令（function）- 选中/发送时执行指定函数
2. 提示词替换命令（prompt）- 选中/发送时将命令替换为指定提示词文本，然后继续发送

使用方式：
    manager = CommandManager.get_instance()
    manager.register("new", "function", description="新建会话")
    manager.register("init", "prompt", description="项目笔记初始化",
                     prompt_text="请分析此代码库...")

# 获取所有命令（供 CommandCard 显示）
    commands = manager.get_all_commands()

# 在发送前拦截
    result = manager.execute(text)
    if result.handled:
        if result.is_function:
            # 调用对应的处理函数
            handler_map[result.command_name]()
            return  # 已处理，不发送给 AI
        elif result.is_prompt:
            # 用替换文本继续发送
            text = result.replacement
"""
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CommandResult:
    """命令执行结果"""
    handled: bool = False          # 是否匹配到了命令
    is_function: bool = False      # 是否为函数型命令
    is_prompt: bool = False        # 是否为提示词替换命令
    is_agent: bool = False         # 是否为智能体命令
    is_subagent: bool = False      # 是否触发子智能体任务（智能体命令 + --subagent）
    command_name: str = ""         # 匹配到的命令名
    agent_name: str = ""           # 智能体名称（agent 命令使用）
    subagent_task: str = ""        # 子智能体任务描述（is_subagent=True 时使用）
    replacement: str = ""          # 提示词替换文本（仅 prompt/agent 命令）
    remainder: str = ""            # 命令后的用户输入（保留部分）


@dataclass
class CommandDefinition:
    """单个命令的定义"""
    name: str
    type: str                      # "function", "prompt", "agent"
    description: str = ""
    argument_hint: str = ""        # 参数提示（显示在命令卡片 detail 模式）
    prompt_text: str = ""          # prompt/agent 命令使用

    def to_display_dict(self) -> Dict[str, str]:
        """返回供 CommandCard 显示用的字典"""
        # 根据 command_type 映射显示类型
        type_map = {
            "function": "command",   # 普通命令
            "prompt": "prompt",      # 提示词命令（init/review/theme等）
            "agent": "agent",        # 智能体（agents 目录加载）
        }
        display_type = type_map.get(self.type, "command")
        return {
            "name": self.name,
            "description": self.description,
            "type": display_type,
        }


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
        self._commands: Dict[str, CommandDefinition] = {}

    # ---- 注册 / 注销 ----

    def register(
        self,
        name: str,
        command_type: str,
        description: str = "",
        argument_hint: str = "",
        prompt_text: str = "",
    ):
        """注册一个内置命令

        Args:
            name: 命令名（不含 /）
            command_type: "function", "prompt" 或 "agent"
            description: 描述文本（显示在命令卡片中）
            argument_hint: 参数提示（如 "<system-dir> | --portfolio <parent-dir>"）
            prompt_text: prompt/agent 命令使用，替换后的提示词文本
        """
        self._commands[name] = CommandDefinition(
            name=name,
            type=command_type,
            description=description,
            argument_hint=argument_hint,
            prompt_text=prompt_text,
        )

    def unregister(self, name: str):
        """取消注册一个命令"""
        self._commands.pop(name, None)

    def has_command(self, name: str) -> bool:
        """检查命令是否已注册"""
        return name in self._commands

    def get_command(self, name: str) -> Optional[CommandDefinition]:
        """获取命令定义"""
        return self._commands.get(name)

    # ---- 查询 ----

    def get_all_commands(self) -> List[Dict[str, str]]:
        """获取所有命令的显示列表（供 CommandCard 使用）"""
        return [cmd.to_display_dict() for cmd in self._commands.values()]

    def get_command_names(self) -> List[str]:
        """获取所有命令名"""
        return list(self._commands.keys())

    # ---- 解析 ----

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

    def is_builtin_command(self, text: str) -> bool:
        """判断输入文本是否匹配某个已注册的内置命令"""
        name = self.parse_command_name(text)
        return name is not None and name in self._commands

    def is_known_command_name(self, name: str) -> bool:
        """根据命令名判断是否为内置命令（不含 /）"""
        return name in self._commands

    # ---- 执行 ----

    def execute(self, text: str) -> CommandResult:
        """解析并执行命令

        Args:
            text: 用户输入的完整文本

        Returns:
            CommandResult:
            - handled=False: 不是内置命令
            - handled=True, is_function=True: 函数命令，调用方需执行对应 handler
            - handled=True, is_prompt=True: 提示词替换命令，使用 replacement 作为发送文本
            - handled=True, is_subagent=True: 智能体命令 + --subagent，触发子智能体任务
        """
        cmd_name = self.parse_command_name(text)
        if not cmd_name or cmd_name not in self._commands:
            return CommandResult(handled=False)

        cmd = self._commands[cmd_name]

        if cmd.type == "function":
            # 提取命令后的用户输入（保留部分）
            remainder = ""
            text_stripped = text.strip()
            if text_stripped.startswith("/"):
                # 找到 /cmd 后的部分
                first_space = text_stripped.find(" ")
                if first_space > 0:
                    remainder = text_stripped[first_space + 1:]

            return CommandResult(
                handled=True,
                is_function=True,
                command_name=cmd_name,
                remainder=remainder,
            )
        elif cmd.type in ("prompt", "agent"):
            # 提取命令后的用户输入（保留部分）
            remainder = ""
            text_stripped = text.strip()
            if text_stripped.startswith("/"):
                first_space = text_stripped.find(" ")
                if first_space > 0:
                    remainder = text_stripped[first_space + 1:]

            # 检查是否有 --subagent 参数（仅智能体命令支持）
            # 规则：--subagent 之后到下一个 -- 参数之前的所有内容作为子智能体任务描述
            has_subagent = False
            subagent_task = ""
            remainder_after_subagent = ""
            if cmd.type == "agent" and remainder:
                subagent_match = remainder.find("--subagent")
                if subagent_match >= 0:
                    has_subagent = True
                    # 分割内容：subagent 之前 + subagent 及之后
                    before_subagent = remainder[:subagent_match].rstrip()
                    after_subagent = remainder[subagent_match + len("--subagent"):].lstrip()
                    
                    # 从 subagent 内容中提取任务和剩余参数
                    # 找到下一个 -- 参数的位置
                    next_flag_pos = -1
                    for i in range(len(after_subagent)):
                        if after_subagent[i] == '-' and (i == 0 or after_subagent[i-1] == ' '):
                            # 检查是否是 -- 开头的参数（跳过开头的 -）
                            if after_subagent[i:].startswith("--"):
                                next_flag_pos = i
                                break
                    
                    if next_flag_pos >= 0:
                        subagent_task = after_subagent[:next_flag_pos].strip()
                        remainder_after_subagent = after_subagent[next_flag_pos:].strip()
                    else:
                        subagent_task = after_subagent.strip()
                        remainder_after_subagent = ""
                    
                    # 重新组装 remainder：subagent 之前 + subagent 之后的参数
                    if before_subagent or remainder_after_subagent:
                        remainder = before_subagent
                        if remainder_after_subagent:
                            remainder = f"{remainder} {remainder_after_subagent}" if remainder else remainder_after_subagent
                    else:
                        remainder = ""

            if has_subagent:
                # 触发子智能体任务（允许空任务描述，子智能体将按自身指令执行）
                return CommandResult(
                    handled=True,
                    is_subagent=True,
                    is_agent=True,
                    command_name=cmd_name,
                    agent_name=cmd_name,
                    subagent_task=subagent_task,
                    remainder=remainder,
                )
            
            # 正常的提示词替换
            return CommandResult(
                handled=True,
                is_prompt=(cmd.type == "prompt"),
                is_agent=(cmd.type == "agent"),
                command_name=cmd_name,
                replacement=cmd.prompt_text,
                remainder=remainder,
            )

        return CommandResult(handled=False)
