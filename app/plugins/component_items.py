# -*- coding: utf-8 -*-
"""插件组件细项枚举（D10：per-tool / per-hook / per-template 粒度）

把「某插件的某组件下到底有哪些可单独开关的条目」这件事收敛到一处，
供设置卡 UI 展开子项时调用，也供过滤链路反查条目合法性。

设计要点：
- **纯函数、无状态**：每次调用都从磁盘 / registry 现读，不缓存。
  调用方是「用户展开某个组件」这种低频交互，缓存收益低却容易与
  热重载失同步。
- **尽力而为**：任何一路枚举失败都只记 warning 并返回已收集到的部分，
  绝不让设置界面因为一个插件的怪异目录结构而整体崩掉。
- **条目 id 必须稳定**：它会被写进 `Settings.disabled_plugin_components`
  （形如 `plugin:component:item_id`）。因此只使用天然稳定的标识——
  注册名 / hook id / 文件名 stem——不使用序号或路径。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger


@dataclass(frozen=True)
class ComponentItem:
    """组件下的一个可开关条目"""

    id: str  # 稳定标识（写入配置用）
    label: str = ""  # 展示名（空则回退 id）
    description: str = ""  # 副标题（辅助辨认，可空）

    @property
    def display_label(self) -> str:
        return self.label or self.id


# 细项来源分类：同一类目录结构共用一套枚举策略
_MD_SUBDIRS = frozenset({"commands", "agents", "skills", "themes"})
_YAML_SUBDIRS = frozenset({"team_templates"})
_PY_SUBDIRS = frozenset(
    {
        "providers",
        "model_adapters",
        "loop_policies",
        "storages",
        "serializers",
        "gateways",
        "engines",
    }
)
# 整体开关、不支持细分的组件（ui 是插件的一个 __init__ 入口，内部槽位
# 由插件自己 register_* 决定，切分没有稳定 id）
_ATOMIC_COMPONENTS = frozenset({"ui"})

_DESC_MAX = 72


def _shorten(text: str, limit: int = _DESC_MAX) -> str:
    """压缩描述为单行短文本（hook 的 prompt 常是多行长文）"""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _resolve_plugin_path(plugin_name: str) -> Optional[Path]:
    """从 PluginManager 取插件根目录"""
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        plugin = PluginManager.get_instance().get_plugin(plugin_name)
        return plugin.path if plugin is not None else None
    except Exception:
        return None


# ── 各组件枚举实现 ────────────────────────────────


def _items_tools(plugin_name: str) -> List[ComponentItem]:
    """工具：从 ToolRegistry 按 source 反查该插件注册的工具

    插件工具是惰性加载的，这里必须先触发一次加载，否则展开时列表为空。
    """
    from app.tools import _ensure_plugin_tools_loaded
    from app.tools.registry import ToolRegistry

    _ensure_plugin_tools_loaded()
    source = f"plugin:{plugin_name}"
    items: List[ComponentItem] = []
    for reg in ToolRegistry.get_instance().list():
        if reg.source != source:
            continue
        items.append(
            ComponentItem(
                id=reg.name,
                label=getattr(reg, "display_cn_name", "") or reg.name,
                description=_shorten(getattr(reg, "description", "") or ""),
            )
        )
    return sorted(items, key=lambda it: it.id)


def _items_hooks(plugin_dir: Optional[Path]) -> List[ComponentItem]:
    """Hook：读 hooks/hooks.json，每条带 id 的 hook 为一个条目

    结构：{"hooks": {EventName: [{"matcher": ..., "hooks": [{id, type, ...}]}]}}
    """
    if plugin_dir is None:
        return []
    hooks_file = plugin_dir / "hooks" / "hooks.json"
    if not hooks_file.exists():
        return []

    try:
        with open(hooks_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[ComponentItems] 解析 {hooks_file} 失败: {e}")
        return []

    items: List[ComponentItem] = []
    seen: set = set()
    for event_name, rules in (data.get("hooks") or {}).items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            for hook in rule.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                hook_id = hook.get("id")
                # 无 id 的 hook 无法稳定寻址，跳过（不写进配置）
                if not hook_id or hook_id in seen:
                    continue
                seen.add(hook_id)
                hook_type = hook.get("type", "")
                desc = hook.get("prompt") or hook.get("command") or hook.get("function") or ""
                items.append(
                    ComponentItem(
                        id=hook_id,
                        label=hook_id,
                        description=_shorten(f"{event_name} · {hook_type} · {desc}"),
                    )
                )
    return sorted(items, key=lambda it: it.id)


def _items_mcp(plugin_dir: Optional[Path]) -> List[ComponentItem]:
    """MCP：.mcp.json 的 mcpServers 下每个 server 为一个条目"""
    return _items_json_keys(plugin_dir / ".mcp.json" if plugin_dir else None, wrapper="mcpServers")


def _items_lsp(plugin_dir: Optional[Path]) -> List[ComponentItem]:
    """LSP：.lsp.json 顶层每个 server 为一个条目（无 mcpServers 包装）"""
    return _items_json_keys(plugin_dir / ".lsp.json" if plugin_dir else None, wrapper="")


def _items_json_keys(json_path: Optional[Path], wrapper: str = "") -> List[ComponentItem]:
    if json_path is None or not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[ComponentItems] 解析 {json_path} 失败: {e}")
        return []
    node = data.get(wrapper) if wrapper else data
    if not isinstance(node, dict):
        return []
    return [ComponentItem(id=name) for name in sorted(node.keys())]


def _items_stem(directory: Optional[Path], pattern: str) -> List[ComponentItem]:
    """按文件名 stem 枚举（md / yaml / py 目录通用）"""
    if directory is None or not directory.exists():
        return []
    items = [ComponentItem(id=p.stem) for p in sorted(directory.glob(pattern)) if not p.name.startswith("_")]
    return sorted(items, key=lambda it: it.id)


# ── 对外 API ──────────────────────────────────────


def list_component_items(
    plugin_name: str,
    component: str,
    plugin_path: Optional[Path] = None,
) -> List[ComponentItem]:
    """列出插件某组件下的全部可开关条目

    Args:
        plugin_name: 插件目录名
        component: 组件名（tools / hooks / team_templates / ...）
        plugin_path: 插件根目录；缺省时向 PluginManager 查询

    Returns:
        条目列表（按 id 排序）。不支持细分的组件（如 ui）返回空列表。
    """
    if component in _ATOMIC_COMPONENTS:
        return []

    path = plugin_path or _resolve_plugin_path(plugin_name)
    try:
        if component == "tools":
            return _items_tools(plugin_name)
        if component == "hooks":
            return _items_hooks(path)
        if component == "mcp":
            return _items_mcp(path)
        if component == "lsp":
            return _items_lsp(path)
        if component in _MD_SUBDIRS:
            return _items_stem(path / component if path else None, "*.md")
        if component in _YAML_SUBDIRS:
            return _items_stem(path / component if path else None, "*.yaml")
        if component in _PY_SUBDIRS:
            return _items_stem(path / component if path else None, "*.py")
    except Exception as e:
        logger.warning(f"[ComponentItems] 枚举 {plugin_name}:{component} 失败: {e}")
    return []


def supports_items(component: str) -> bool:
    """该组件是否支持细项级开关"""
    return component not in _ATOMIC_COMPONENTS


def count_component_items(plugin_name: str, component: str, plugin_path: Optional[Path] = None) -> int:
    """条目数量（UI 未展开时只需数字，避免构造完整对象）"""
    return len(list_component_items(plugin_name, component, plugin_path))


def describe_items(plugin_name: str, component: str, plugin_path: Optional[Path] = None) -> Dict[str, str]:
    """返回 {item_id: label}，供过滤链路快速取展示名"""
    return {it.id: it.display_label for it in list_component_items(plugin_name, component, plugin_path)}


# ── 全量索引（搜索用） ─────────────────────────────
#
# 搜索关键词可能命中的是细项 id（用户想关掉某个具体工具时会直接搜工具名），
# 这意味着要遍历全部插件 × 全部组件。逐个现读会放大成上百次目录扫描，
# 因此缓存一份索引。TTL 到期或 invalidate_item_index() 后重建（插件热重载、
# 工具重新注册等场景由调用方触发）。

_item_index_cache: Optional[Tuple[float, Dict[str, Dict[str, List[ComponentItem]]]]] = None
_ITEM_INDEX_TTL = 60.0


def invalidate_item_index() -> None:
    """丢弃细项索引缓存（插件/工具发生增删后调用）"""
    global _item_index_cache
    _item_index_cache = None


def _enabled_plugin_entries() -> List[Tuple[str, Path, List[str]]]:
    """[(plugin_name, plugin_path, [components])]，已启用且至少含一个组件"""
    from app.plugins.managers.plugin_manager import PluginManager

    entries: List[Tuple[str, Path, List[str]]] = []
    for plugin in PluginManager.get_instance().get_enabled_plugins():
        comps = [c for c, v in plugin.components.items() if v]
        if comps:
            entries.append((plugin.name, plugin.path, comps))
    return entries


def build_item_index(
    entries: Optional[List[Tuple[str, Path, List[str]]]] = None,
    force: bool = False,
) -> Dict[str, Dict[str, List[ComponentItem]]]:
    """构建 {plugin_name: {component: [ComponentItem]}} 索引（带 TTL 缓存）

    Args:
        entries: 预取的插件条目；缺省时向 PluginManager 查询全部已启用插件
        force: 忽略 TTL 强制重建

    Returns:
        只包含「确有细项」的插件与组件——空组件不进索引，
        这样 UI 可以据此决定是否显示展开按钮。
    """
    global _item_index_cache
    if not force and _item_index_cache is not None:
        ts, index = _item_index_cache
        if time.time() - ts < _ITEM_INDEX_TTL:
            return index

    if entries is None:
        try:
            entries = _enabled_plugin_entries()
        except Exception as e:
            logger.warning(f"[ComponentItems] 枚举已启用插件失败: {e}")
            entries = []

    index: Dict[str, Dict[str, List[ComponentItem]]] = {}
    for name, path, comps in entries:
        per: Dict[str, List[ComponentItem]] = {}
        for comp in comps:
            items = list_component_items(name, comp, path)
            if items:
                per[comp] = items
        if per:
            index[name] = per

    _item_index_cache = (time.time(), index)
    return index
