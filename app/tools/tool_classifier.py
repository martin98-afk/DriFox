# -*- coding: utf-8 -*-
"""
工具安全分类器 — 按功能语义将工具分为危险/安全两类
"""

# 危险工具：修改文件、执行命令、控制桌面、上传文件
DANGEROUS_TOOLS = frozenset({
    "write", "edit", "multi_edit",           # 文件写入
    "bash", "bg_start", "bg_stop",           # 终端命令
    "mouse", "keyboard",                      # 桌面控制
    "upload_file",                            # 文件上传
    "edit_project_note", "todowrite",         # 状态修改
    "stage_files",                            # 文件标记
    "subagent_para", "subagent_dag",          # 子智能体（可执行任意代码/修改文件）
})

# 安全工具：只读、查询、无副作用
SAFE_TOOLS = frozenset({
    "read", "grep", "list", "glob", "scan_repo",     # 文件只读
    "webfetch", "websearch",                          # 网络查询
    "bg_logs", "bg_list",                             # 后台查看
    "screenshot",                                     # 截图
    "get_diagnostics",                                # 诊断
    "read_project_note", "todoread",                  # 笔记/待办只读
    "question", "skill", "list_skills",               # 交互/技能
    "subagent_status",                                # 子智能体（只读查询）
    "mcp_list_servers",                               # MCP列表
    "lsp",                                            # LSP 工具（只读）
})


def classify_tool_danger(tool_name: str) -> str:
    """判断工具危险级别

    Args:
        tool_name: 工具名（内置名或 mcp__xxx 格式名）

    Returns:
        "dangerous" | "safe"
    """
    # MCP 工具：取 mcp__{server}__{toolname} 中的 toolname 部分
    base_name = tool_name
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        base_name = parts[2] if len(parts) > 2 else tool_name

    if base_name in DANGEROUS_TOOLS:
        return "dangerous"
    return "safe"


def get_tool_counts(toggles: dict) -> tuple:
    """统计当前危险/安全工具启用数量

    Args:
        toggles: {tool_name: bool, ...}

    Returns:
        (dangerous_count, safe_count)
    """
    dangerous_count = 0
    safe_count = 0
    for name, enabled in toggles.items():
        if enabled:
            if classify_tool_danger(name) == "dangerous":
                dangerous_count += 1
            else:
                safe_count += 1
    return dangerous_count, safe_count


def get_default_toggles(tool_names: list) -> dict:
    """生成默认全开的 toggles 字典

    Args:
        tool_names: 工具名列表

    Returns:
        {tool_name: True, ...}
    """
    return {name: True for name in tool_names}
