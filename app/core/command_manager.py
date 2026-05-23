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
    command_name: str = ""         # 匹配到的命令名
    replacement: str = ""          # 提示词替换文本（仅 prompt 命令）


@dataclass
class CommandDefinition:
    """单个命令的定义"""
    name: str
    type: str                      # "function" or "prompt"
    description: str = ""
    prompt_text: str = ""          # 仅 prompt 命令使用

    def to_display_dict(self) -> Dict[str, str]:
        """返回供 CommandCard 显示用的字典"""
        return {
            "name": self.name,
            "description": self.description,
            "type": "command",
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
        prompt_text: str = "",
    ):
        """注册一个内置命令

        Args:
            name: 命令名（不含 /）
            command_type: "function" 或 "prompt"
            description: 描述文本（显示在命令卡片中）
            prompt_text: 仅 prompt 命令使用，替换后的提示词文本
        """
        self._commands[name] = CommandDefinition(
            name=name,
            type=command_type,
            description=description,
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
        """
        cmd_name = self.parse_command_name(text)
        if not cmd_name or cmd_name not in self._commands:
            return CommandResult(handled=False)

        cmd = self._commands[cmd_name]

        if cmd.type == "function":
            return CommandResult(
                handled=True,
                is_function=True,
                command_name=cmd_name,
            )
        elif cmd.type == "prompt":
            return CommandResult(
                handled=True,
                is_prompt=True,
                command_name=cmd_name,
                replacement=cmd.prompt_text,
            )

        return CommandResult(handled=False)
