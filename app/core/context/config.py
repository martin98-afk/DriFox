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


def cfg_ratio(key: str, default: float) -> float:
    """比例配置：整数百分比（84）→ 0.84；也接受 0.xx 写法（<=1 时视为已归一化）。

    plugin.json 的 number 类型无小数支持，故比例字段以整数百分比录入。
    非法值 clamp 到 (0.01, 0.99) —— 比例越界会让 cascade 永不达标或立刻达标。
    """
    raw = cfg_float(key, default)
    if raw > 1.0:
        raw = raw / 100.0
    return min(max(raw, 0.01), 0.99)
