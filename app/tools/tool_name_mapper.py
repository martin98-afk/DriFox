# -*- coding: utf-8 -*-
"""
工具名别名映射器 — 纯内部机制

将其他大模型平台（Claude Code, Cursor 等）使用的工具名
翻译为 DriFoxx 原生工具名，使跨平台插件能正确工作。

LLM 永远看到 DriFoxx 原生名，此映射器只在系统内部使用。
"""

from typing import Dict, List, Optional


class ToolNameMapper:
    """
    工具名别名注册表（纯内部使用）

    用法：
        ToolNameMapper.to_native("Read")  → "read"
        ToolNameMapper.to_native("read")  → "read"  (passthrough)
    """

    # DriFoxx 原生名 → 其他平台常见名称列表
    ALIAS_MAP: Dict[str, List[str]] = {
        "read": ["Read", "ReadFile", "ReadFiles", "cat"],
        "write": ["Write", "WriteFile", "CreateFile", "create_file"],
        "edit": ["Edit", "TextEdit", "ReplaceInFile", "replace"],
        "multi_edit": ["MultiEdit", "MultiEditTool"],
        "grep": ["Grep", "Search", "SearchFiles", "Find"],
        "glob": ["Glob", "LS", "ListFiles", "list_files"],
        "list": ["List", "LS", "Ls", "ListDir", "list_directory"],
        "bash": ["Bash", "Terminal", "RunCommand", "execute_command", "shell", "Command"],
        "question": ["Question", "AskUser", "ask_user", "AskUserQuestion"],
        "websearch": ["WebSearch", "web_search", "Search", "SearchWeb"],
        "webfetch": ["WebFetch", "Fetch", "FetchPage", "FetchUrl"],
        "subagent_para": ["subagents-para", "subagent-para", "TaskBatch", "Batch", "task", "Task"],
        "subagent_status": ["subagent-status", "TaskStatus", "Status"],
        "subagent_dag": ["subagent-dag", "subagent-teams", "SubagentDag", "Dag"],
        "todowrite": ["TodoWrite", "todo_write"],
        "todoread": ["TodoRead", "todo_read"],
        "skill": ["Skill"],
        "list_skills": ["ListSkills", "listSkills"],
        "scan_repo": ["ScanRepo", "scan_repo"],
        "mcp_list_servers": ["McpListServers", "mcp_list_servers"],
        "bg_start": ["BgStart", "bg_start"],
        "bg_stop": ["BgStop", "bg_stop"],
        "bg_logs": ["BgLogs", "bg_logs"],
        "bg_list": ["BgList", "bg_list"],
        "stage_files": ["StageFiles", "stage_files"],
        "read_project_note": ["ReadProjectNote", "read_project_note"],
        "edit_project_note": ["EditProjectNote", "edit_project_note"],
        "mcp": ["Mcp"],  # MCP 工具前缀
    }

    # 反向映射：外来名 → 原生名（延迟构建）
    _reverse_map: Optional[Dict[str, str]] = None

    @classmethod
    def to_native(cls, name: str) -> str:
        """将任意已知工具名转换为 DriFoxx 原生名

        如果已经是原生名或未知名，原样返回。
        """
        if not name:
            return name

        name_lower = name.lower()

        # 快速路径：已经是原生名
        if name_lower in cls.ALIAS_MAP:
            return name_lower

        # 延迟构建反向映射
        if cls._reverse_map is None:
            cls._reverse_map = {}
            for native, aliases in cls.ALIAS_MAP.items():
                for alias in aliases:
                    cls._reverse_map[alias] = native

        # 精确匹配（保留大小写，如 "Read" → "read"）
        if name in cls._reverse_map:
            return cls._reverse_map[name]

        # 不区分大小写匹配（如 "ReAd" → "read"）
        if name_lower in cls._reverse_map:
            return cls._reverse_map[name_lower]

        return name  # 未知名，原样返回

    @classmethod
    def is_known(cls, name: str) -> bool:
        """检查工具名是否可映射到已知的 DriFoxx 工具

        已知工具包括：
        - 内置工具（ALIAS_MAP 中的原生名）
        - MCP 工具（以 mcp__ 开头的动态工具名）

        Args:
            name: 工具名（如 "Read", "SomeCustomTool"）

        Returns:
            True 表示已知工具，False 表示无法映射的未知名
        """
        if not name:
            return False
        native = cls.to_native(name)
        if native in cls.ALIAS_MAP:
            return True
        # MCP 工具（动态发现，运行时注入）
        if native.startswith("mcp__"):
            return True
        return False

    @classmethod
    def register_alias(cls, native_name: str, alias: str):
        """动态注册别名（供插件或测试使用）"""
        native_lower = native_name.lower()
        if native_lower not in cls.ALIAS_MAP:
            cls.ALIAS_MAP[native_lower] = []
        if alias not in cls.ALIAS_MAP[native_lower]:
            cls.ALIAS_MAP[native_lower].append(alias)
        # 清除反向映射缓存以便重建
        cls._reverse_map = None
