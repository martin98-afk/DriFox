# -*- coding: utf-8 -*-
"""契约1：plugin.json manifest schema 宽容校验。

策略（宽容优先，避免误伤）：
- 类型不符 → warning + 按缺省处理（version→"0.0.0" 等）或丢弃该字段，不拒载
- 未知字段忽略（插件可自由扩展命名空间）
- 缺 name → 回退目录名（调用方传 fallback_name）+ warning
- 校验 warning 清单由调用方存入 PluginInfo.manifest_warnings（UI 角标后续接入）
"""
from __future__ import annotations

from pathlib import Path
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


# 清单目录查找顺序：与 PluginManager._scan_one_plugin_dir 保持一致
_MANIFEST_DIRS = (".drifox-plugin", ".claude-plugin")


def read_manifest_module_prefixes(plugin_dir) -> List[str]:
    """读取插件声明的模块命名空间前缀（plugin.json → ``module_prefixes``）。

    用途：插件按文件路径 importlib 注册自有共享模块时（如 assistant_hub 的
    ``assistant_hub_manager`` / ``assistant_hub_core.*``），需在清单里声明命名空间，
    AST 安全网据此放行，热重载（``PluginHostService._purge_module_prefixes``）
    据此清理。两处共用同一份声明，避免"加载允许注册、重载却不清理"的漂移。

    读取失败 / 缺字段 / 类型不符 → 返回空表（未声明即不可写，最严口径）。
    """
    import json

    base = Path(plugin_dir)
    for sub in _MANIFEST_DIRS:
        path = base / sub / "plugin.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        raw = (data or {}).get("module_prefixes")
        if not isinstance(raw, list):
            return []
        return [str(x) for x in raw if isinstance(x, str) and x.strip()]
    return []
