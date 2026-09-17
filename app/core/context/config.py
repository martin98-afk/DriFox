# -*- coding: utf-8 -*-
"""内置 tier 的阈值读取 — 经 PluginConfigStore 三级链（环境变量 → 存储 → 默认）。

插件名固定 "system-context"：八个内置 tier 的阈值统一由该系统插件的
config_schema 承载，用户可在设置面板调整，也可经 env 覆盖。

读取失败一律回退 default —— tier 不得因配置异常而崩（异常隔离是硬约束）。
"""

from __future__ import annotations

from typing import Any

PLUGIN_NAME = "system-context"


def cfg(key: str, default: Any = None) -> Any:
    """读原始生效值。"""
    try:
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        val = PluginConfigStore().get(PLUGIN_NAME, key)
    except Exception:
        return default
    if val is None or val == "":
        return default
    return val


def cfg_int(key: str, default: int) -> int:
    try:
        return int(cfg(key, default))
    except (TypeError, ValueError):
        return default


def cfg_float(key: str, default: float) -> float:
    try:
        return float(cfg(key, default))
    except (TypeError, ValueError):
        return default


def cfg_bool(key: str, default: bool) -> bool:
    val = cfg(key, default)
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("1", "true", "yes", "on")


# 免裁剪名单的内置默认值（config_schema 未注册 / 未配置时的兜底）
# 正式来源有二：① 系统插件 config_schema（用户可在设置页改）
#             ② 工具注册时声明的 metadata["no_prune"] / ["no_offload"]
# 三者取并集，任一命中即豁免。
DEFAULT_PRUNE_SKIP_TOOLS = "question,skill,todoread"
DEFAULT_OFFLOAD_SKIP_TOOLS = "question,todowrite,todoread,manage_skill,mcp_list_servers,skill"


def _parse_tool_list(raw: Any) -> frozenset:
    if not raw:
        return frozenset()
    return frozenset(n.strip() for n in str(raw).split(",") if n.strip())


def prune_skip_tools() -> frozenset:
    """免截断工具名单：配置值 ∪ 内置默认。"""
    return _parse_tool_list(cfg("prune_skip_tools", DEFAULT_PRUNE_SKIP_TOOLS)) | _parse_tool_list(
        DEFAULT_PRUNE_SKIP_TOOLS
    )


def offload_skip_tools() -> frozenset:
    """免落盘工具名单：配置值 ∪ 内置默认。"""
    return _parse_tool_list(cfg("offload_skip_tools", DEFAULT_OFFLOAD_SKIP_TOOLS)) | _parse_tool_list(
        DEFAULT_OFFLOAD_SKIP_TOOLS
    )


def cfg_ratio(key: str, default: float) -> float:
    """比例配置：整数百分比（84）→ 0.84；也接受 0.xx 写法（<=1 时视为已归一化）。

    plugin.json 的 number 类型无小数支持，故比例字段以整数百分比录入。
    非法值 clamp 到 (0.01, 0.99) —— 比例越界会让 cascade 永不达标或立刻达标。
    """
    raw = cfg_float(key, default)
    if raw > 1.0:
        raw = raw / 100.0
    return min(max(raw, 0.01), 0.99)
