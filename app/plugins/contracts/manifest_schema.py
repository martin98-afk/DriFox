# -*- coding: utf-8 -*-
"""契约1：plugin.json manifest schema 宽容校验。

策略（宽容优先，避免误伤）：
- 类型不符 → warning + 按缺省处理（version→"0.0.0" 等）或丢弃该字段，不拒载
- 未知字段忽略（插件可自由扩展命名空间）
- 缺 name → 回退目录名（调用方传 fallback_name）+ warning
- 校验 warning 清单由调用方存入 PluginInfo.manifest_warnings（UI 角标后续接入）
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from loguru import logger

# 字段规范表：字段名 → (期望类型元组, 缺省值或 None)
# 缺省 None 表示：类型不符时直接丢弃该字段（视为未声明，下游按缺省语义处理）
SPEC: Dict[str, tuple] = {
    "name": ((str,), None),  # 缺失回退目录名（fallback_name）
    "version": ((str,), "0.0.0"),
    "type": ((str,), None),  # 枚举值非关键，宽容
    "icon": ((str, dict), None),
    "platforms": ((list,), None),
    "min_host_version": ((str,), None),
    "api_version": ((int,), 1),
    "dependencies": ((dict,), None),
    "config_schema": ((dict,), None),
    "module_prefixes": ((list,), None),
}


def validate_manifest(
    manifest: dict, source: str = "", fallback_name: Optional[str] = None
) -> Tuple[dict, List[str]]:
    """宽容校验并规范化 manifest（不改传入对象，返回副本）。

    Args:
        manifest: 已解析的清单 dict
        source: 来源标识（目录名，仅用于日志）
        fallback_name: name 缺失时的回退值（目录名）

    Returns:
        (规范化后的 manifest 副本, warning 清单)
    """
    warnings: List[str] = []
    out = dict(manifest or {})
    tag = f"[{source}] " if source else ""

    # name：缺失回退目录名 + warning
    name = out.get("name")
    if name is None or (isinstance(name, str) and not name.strip()):
        if fallback_name:
            warnings.append(f"{tag}字段 name 缺失或为空，回退目录名: {fallback_name!r}")
            out["name"] = fallback_name

    for key, (types, default) in SPEC.items():
        if key == "name":
            continue  # 上方单独处理
        if key not in out or out[key] is None:
            if key not in out and default is not None:
                out[key] = default
            continue
        val = out[key]
        # bool 是 int 子类：api_version=True 这类声明按类型不符处理
        if isinstance(val, bool) and bool not in types and int in types:
            warnings.append(
                f"{tag}字段 {key} 类型应为 {types[0].__name__}（实际 bool），按缺省处理"
            )
            _apply_default(out, key, default)
            continue
        if not isinstance(val, types):
            expect = "/".join(t.__name__ for t in types)
            warnings.append(
                f"{tag}字段 {key} 类型不符（期望 {expect}，实际 {type(val).__name__}），按缺省处理"
            )
            _apply_default(out, key, default)
    return out, warnings


def _apply_default(out: dict, key: str, default) -> None:
    if default is not None:
        out[key] = default
    else:
        out.pop(key, None)
