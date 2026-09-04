# -*- coding: utf-8 -*-
"""schema_filter.py — 助手工具权限档位（对话前 schema 过滤）

按当前会话归属助手的 tool_access 档位，裁剪发给 LLM 的工具 schema：
- full    不过滤（默认）
- readonly 仅保留安全类工具（registry danger 分类驱动，动态，MCP 工具走启发式兜底）
- minimal 仅保留 bash

注册：register(registry)（PluginToolLoader 调用）→ ToolRegistry 单例
register_schema_filter("assistant_hub", fn)；插件禁用/卸载/热重载时
unload_plugin_tools 按 owner 自动清理。

manager 获取：只读 sys.modules["assistant_hub_manager"]（与 experience_tools 同模式）。
"""

import sys
from typing import Any, Dict, List

from loguru import logger

_MANAGER_MODULE_NAME = "assistant_hub_manager"


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
        return [s for s in schemas if s.get("function", {}).get("name", "") == "bash"]
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
        from app.tools.registry import ToolRegistry

        ToolRegistry.get_instance().register_schema_filter("assistant_hub", filter_tools_schema)
        logger.debug("[assistant_hub] schema 过滤器已注册")
    except Exception as e:
        logger.warning(f"[assistant_hub] schema 过滤器注册失败: {e}")
