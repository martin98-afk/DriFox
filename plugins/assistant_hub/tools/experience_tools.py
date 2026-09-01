# -*- coding: utf-8 -*-
"""experience_tools.py — recall_experience / record_experience（工具型经验体系）

对齐 openhanako lib/tools/experience.ts：
- recall_experience：无参 → 返回 experience.md 索引（渐进式披露第一层）；
  带 category → 返回该分类文件全文（第二层）。
- record_experience：写入一条经验并重建索引（助手对话中自主记录）。

开关：Assistant.experience_enabled（默认关）。关闭时工具返回暂停提示。
manager 获取：优先 sys.modules["assistant_hub_manager"]（进程内单例），
兜底按路径加载（与 hooks/inject_assistant.py 同一模式）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

from app.tools.result import ToolResult

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MANAGER_MODULE_NAME = "assistant_hub_manager"

_GROUP = "助手记忆"


def _load_manager_module():
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(_MANAGER_MODULE_NAME, str(_PLUGIN_ROOT / "assistant_manager.py"))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MANAGER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _get_manager():
    mod = _load_manager_module()
    if mod is None:
        return None
    return mod.AssistantManager.get_instance()


def _paused() -> ToolResult:
    return ToolResult(True, content="经验功能已暂停。已有内容会保留，但现在不能读取或记录经验。")


def _recall_impl(tool_ctx, **kw):
    mgr = _get_manager()
    if mgr is None:
        return ToolResult(False, error="assistant_manager 不可用")
    aid = mgr.active_id()
    if not aid or not mgr.has(aid):
        return ToolResult(True, content="当前没有激活的助手。")
    a = mgr.get(aid)
    if a is None or not getattr(a, "experience_enabled", False):
        return _paused()
    category = str(kw.get("category") or "").strip()
    if category:
        text = mgr.experience_read(aid, category)
        if not text.strip():
            return ToolResult(True, content=f"分类「{category}」暂无经验记录。")
        return ToolResult(True, content=text)
    return ToolResult(True, content=mgr.experience_read_index(aid))


def _record_impl(tool_ctx, **kw):
    mgr = _get_manager()
    if mgr is None:
        return ToolResult(False, error="assistant_manager 不可用")
    aid = mgr.active_id()
    if not aid or not mgr.has(aid):
        return ToolResult(True, content="当前没有激活的助手。")
    a = mgr.get(aid)
    if a is None or not getattr(a, "experience_enabled", False):
        return _paused()
    category = str(kw.get("category") or "").strip()
    content = str(kw.get("content") or "").strip()
    if not category or not content:
        return ToolResult(False, error="category 和 content 不能为空")
    r: Dict[str, Any] = mgr.experience_record(aid, category, content)
    if r.get("added"):
        return ToolResult(True, content=f"已记录到经验库「{category}」。")
    return ToolResult(True, content=f"未记录：{r.get('reason', 'unknown')}")


_RECALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recall_experience",
        "description": ("回忆你的工作经验。无参调用返回经验索引（分类列表）；带 category 返回该分类下的全部经验条目。"),
        "parameters": {
            "type": "object",
            "properties": {"category": {"type": "string", "description": "经验分类名（不传返回索引）"}},
        },
    },
}

_RECORD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_experience",
        "description": ("记录一条工作经验（用户偏好、有效工作流、踩坑教训等），供未来的自己做类似事情时回忆。"),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "经验分类（≤8 字，如：代码风格/工作流）"},
                "content": {"type": "string", "description": "经验内容（一句话，具体可执行）"},
            },
            "required": ["category", "content"],
        },
    },
}


def register(registry):
    """工具插件化注册入口（PluginToolLoader 调用）"""
    registry.register(
        "recall_experience",
        _RECALL_SCHEMA,
        impl=_recall_impl,
        danger="safe",
        icon="memory",
        cn_name="回忆经验",
        group=_GROUP,
        description="回忆助手的工作经验（索引/分类）",
        aliases=["回忆经验", "经验"],
    )
    registry.register(
        "record_experience",
        _RECORD_SCHEMA,
        impl=_record_impl,
        danger="safe",
        icon="memory",
        cn_name="记录经验",
        group=_GROUP,
        description="记录一条工作经验到经验库",
        aliases=["记录经验", "记经验"],
    )
