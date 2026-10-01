# -*- coding: utf-8 -*-
"""
工具安全分类器 — 危险级别查询（registry 驱动）

数据源：ToolRegistry（工具插件注册时显式声明的 danger 字段）。
保留旧函数签名兼容调用方，内部实现改为查 registry。
"""
from __future__ import annotations

from typing import List

from app.tools.registry import DANGER_DANGEROUS, DANGER_SAFE, ToolRegistry


def _registry() -> ToolRegistry:
    """惰性获取 registry（避免模块导入时序问题）

    [PERF] 首次读取前确保系统插件工具已加载（幂等）：app.tools.__init__ 不再
    在导入时全量扫描插件，改由此处触发，保证权限控制器等消费方读到的
    registry 与旧行为一致（import 即加载）。
    """
    from app.tools import _ensure_plugin_tools_loaded

    _ensure_plugin_tools_loaded()
    return ToolRegistry.get_instance()


def DANGEROUS_TOOLS() -> frozenset:
    """危险工具集合（动态，registry 驱动）

    [PERF T32] registry 已返回缓存 frozenset，此处不再二次包裹。
    """
    return _registry().dangerous_tools()


def SAFE_TOOLS() -> frozenset:
    """安全工具集合（动态，registry 驱动）

    [PERF T32] registry 已返回缓存 frozenset，此处不再二次包裹。
    """
    return _registry().safe_tools()


# 兼容旧代码：DANGEROUS_TOOLS / SAFE_TOOLS 曾被当作 frozenset 常量使用
# （如 `list(DANGEROUS_TOOLS) + list(SAFE_TOOLS)`、`for tool in DANGEROUS_TOOLS`）。
# 改为函数后上述用法需调用 DANGEROUS_TOOLS()。为降低迁移成本，提供 callable 版本
# 并在 module 级导出函数名（调用方改一行即可）。
# 说明：函数名大写是历史命名，保持向后兼容。

# 兼容辅助：get_all_tools / get_tool_names（供权限控制器等使用）


def get_all_tools() -> List[str]:
    """获取全部已注册工具名（危险+安全，registry 驱动）"""
    return _registry().names()


def get_safe_tools() -> frozenset:
    """获取全部安全工具名（T32 起返回不可变 frozenset）"""
    return _registry().safe_tools()


def _mcp_base_name(tool_name: str) -> str:
    """MCP 工具名 → base_name（mcp__server__tool → tool；非常规格式返回原名）

    统一 classify_tool_danger 与 _dangerous_contains 的 MCP 口径，
    避免两处各自切分导致行为分叉。
    非 "mcp__" 前缀时原样返回。
    """
    if not tool_name.startswith("mcp__"):
        return tool_name
    parts = tool_name.split("__", 2)
    return parts[2] if len(parts) > 2 else tool_name


def _dangerous_contains(tool_name: str, dangerous: frozenset) -> bool:
    """工具是否属于危险集（MCP 名按 base_name 比对，与 old 语义一致）"""
    return _mcp_base_name(tool_name) in dangerous


def classify_tool_danger(tool_name: str) -> str:
    """判断工具危险级别（registry 驱动）

    Args:
        tool_name: 工具名（内置名或 mcp__xxx 格式名）

    Returns:
        "dangerous" | "safe"
    """
    # MCP 工具：未注册，按 toolname 部分启发式判断（沿用旧语义：不在危险表即安全）
    if tool_name.startswith("mcp__"):
        dangerous = DANGEROUS_TOOLS()
        return DANGER_DANGEROUS if _dangerous_contains(tool_name, dangerous) else DANGER_SAFE
    return _registry().get_danger(tool_name)


def get_tool_counts(toggles: dict) -> tuple:
    """统计当前危险/安全工具启用数量（registry 驱动）

    Args:
        toggles: {tool_name: bool, ...}

    Returns:
        (dangerous_count, safe_count)
    """
    # [PERF T32] 危险集循环外提一次（原先每项 classify_tool_danger → O(N²)）
    dangerous = DANGEROUS_TOOLS()
    dangerous_count = 0
    safe_count = 0
    for name, enabled in toggles.items():
        if enabled:
            if _dangerous_contains(name, dangerous):
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


