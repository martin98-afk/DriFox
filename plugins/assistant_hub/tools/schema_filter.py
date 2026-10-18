# -*- coding: utf-8 -*-
"""schema_filter.py — 助手预置工具档位（控制初始注入的 schema）

按当前会话归属助手的 tool_access 档位，控制初始注入的工具 schema（不影响可用性，未注入的可经 tool_search 找回）：
- full    不过滤（默认）
- readonly 仅保留安全类工具（registry danger 分类驱动，动态，MCP 工具走启发式兜底）
- minimal 仅保留 bash / write / edit（无元工具，其余不可调用）
- search  仅保留 tool_search / tool_execute（其余搜索后中转调用）
- none    不注入任何工具

注册：register(registry)（PluginToolLoader 调用）→ ToolRegistry 单例
register_schema_filter("assistant_hub", fn)；插件禁用/卸载/热重载时
unload_plugin_tools 按 owner 自动清理。

manager 获取：只读 sys.modules["assistant_hub_manager"]（与 experience_tools 同模式）。
"""

import sys
from typing import Any, Dict, List

from loguru import logger
from app.plugins.sdk import tool_registry_api

_MANAGER_MODULE_NAME = "assistant_hub_manager"


def _tool_api(kind: str = "read"):
    """工具注册表只读面 / 注入面。

    优先用本模块的 ``ToolRegistry`` 符号（若存在：插件可注入替身，测试常用），
    否则经能力池（tool.read / tool.inject）。两者都不可用时返回 None。
    """
    cap = "tool.inject" if kind == "inject" else "tool.read"
    reg = globals().get("ToolRegistry")
    if reg is not None:
        try:
            inst = reg.get_instance()
            return _registry_api(inst, kind)
        except Exception:
            pass
    try:
        from app.plugins.capabilities.bootstrap import ensure_registered
        from app.plugins.capabilities.registry import CapabilityRegistry

        ensure_registered("tool")
        plugin = ""
        try:
            from pathlib import Path

            parts = Path(__file__).resolve().parts
            if "plugins" in parts:
                plugin = parts[parts.index("plugins") + 1]
        except Exception:
            pass
        pool = CapabilityRegistry.get_instance().build_pool(
            surface="tool", host=None, plugin=plugin, declared={cap, "state.kv", "plugin.log"}
        )
        if cap not in pool:
            return None
        return pool[cap]()
    except Exception:
        return None


def _tool_api(kind: str = "read"):
    """工具注册表只读面 / 注入面（实现见 sdk.tool_registry_api）。"""
    return tool_registry_api(kind, module_globals=globals())

def _get_manager():
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is None:
        return None
    return mod.AssistantManager.get_instance()


def filter_tools_schema(schemas: List[Dict[str, Any]], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按会话归属助手的档位裁剪 schema 列表（ToolRegistry schema 过滤器协议）。"""
    mgr = _get_manager()
    if mgr is None or not schemas:
        return schemas
    sid = str((ctx or {}).get("session_id") or "")
    try:
        mode = mgr.tool_access_for(sid)
    except Exception as e:
        logger.debug(f"[assistant_hub.schema_filter] 档位解析失败，不过滤: {e}")
        return schemas
    if mode == "full":
        return schemas
    if mode == "minimal":
        # bash 万能执行 + 写入/编辑；无元工具，未注入的其余工具不可调用
        return [s for s in schemas if s.get("function", {}).get("name", "") in ("bash", "write", "edit")]
    if mode == "search":
        # 仅两个元工具：搜索找回 schema / 中转执行，懒加载闭环
        return [s for s in schemas if s.get("function", {}).get("name", "") in ("tool_search", "tool_execute")]
    if mode == "none":
        return []
    # readonly：仅安全类工具（registry danger 驱动；mcp__ 工具按启发式兜底）
    from app.tools.tool_classifier import DANGER_SAFE, classify_tool_danger

    kept = []
    for s in schemas:
        name = s.get("function", {}).get("name", "")
        if classify_tool_danger(name) == DANGER_SAFE:
            kept.append(s)
    return kept


def register(registry):
    """工具插件化注册入口（PluginToolLoader 调用）：注册 schema 过滤器。"""
    try:
        api = _tool_api("inject")

        if api is None:
            logger.warning("[assistant_hub] 无 tool.inject 能力，schema 过滤器未注册")
            return
        api["register_schema_filter"]("assistant_hub", filter_tools_schema)
        logger.debug("[assistant_hub] schema 过滤器已注册")
    except Exception as e:
        logger.warning(f"[assistant_hub] schema 过滤器注册失败: {e}")
