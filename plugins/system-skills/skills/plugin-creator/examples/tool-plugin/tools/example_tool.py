# -*- coding: utf-8 -*-
"""最小工具插件示例 — tools/example_tool.py

参照自真实插件 win-powershell（~/.drifox/plugins/win-powershell/tools/powershell.py），
只保留「可运行的最小结构」，实现逻辑已精简。

结构三要素（缺一不可）：
1. 工具实现函数：返回 ToolResult(True, content=...) 或 ToolResult(False, error=...)
2. _SCHEMA：OpenAI function-calling 格式，给大模型看的参数说明
3. register(registry)：loader 扫描的注册入口

本文件可整体复制改名后使用；需同步修改处见文件内 [改名] 标记。
"""
import subprocess

from app.tools.registry import make_summarize_from_preview
from app.tools.result import ToolResult


# ── 1. 工具实现 ──────────────────────────────────────────
# 约定：参数名/默认值与 _SCHEMA.parameters 对齐；返回 ToolResult。
def _run_command_impl(command: str, timeout: int = 30) -> ToolResult:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"命令超时（>{timeout}s）")

    # 输出解码兜底：严格 UTF-8 失败再回退（Windows 外部 exe 可能输出 GBK）
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        return ToolResult(False, error=f"退出码 {proc.returncode}:\n{stdout or stderr}")
    return ToolResult(True, content=stdout or "(命令执行成功，无输出)")


# ── 2. 参数 Schema（大模型据此填参）─────────────────────
_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_command",  # [改名] 工具名，全仓库唯一
        "description": "执行一条系统命令并返回 stdout。示例工具，仅演示注册结构。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {"type": "integer", "description": "超时秒数"},
            },
            "required": ["command"],
        },
    },
}


# ── 3. 注册入口（PluginToolLoader 调用）────────────────
def register(registry):
    registry.register(
        "run_command",          # [改名] 与 _SCHEMA.function.name 一致
        _SCHEMA,
        impl=_run_command_impl,
        danger="safe",          # [必改] 插件工具必须显式声明 danger（safe/dangerous）
        icon="plugin",          # [改名] tools/icons/ 下的 SVG 文件名（不带扩展名）
        cn_name="示例命令执行",  # 设置界面展示名
        group="示例",           # 设置界面分组
        description="执行一条系统命令",
        aliases=["ExampleCmd"],
        render_mode="",         # 默认折叠卡：长输出可折叠
        preview=_preview,
        summarize=make_summarize_from_preview(_preview),
        metadata={"permission_arg": "command"},  # 权限确认时展示的参数
    )


def _preview(args: dict) -> str:
    """折叠卡预览行。⚠️ 不要在这里做 HTML escape：主程序统一转义，
    这里再转一次会出现 &amp;amp; 双重转义。"""
    args = args or {}
    cmd = str(args.get("command", "")).strip()
    first_line = cmd.splitlines()[0] if cmd else ""
    return f"run: {first_line[:60]}" if first_line else "示例命令执行"
