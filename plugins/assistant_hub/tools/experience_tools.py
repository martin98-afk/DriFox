# -*- coding: utf-8 -*-
"""experience_tools.py — recall_experience / record_experience（工具型经验体系）

对齐 openhanako lib/tools/experience.ts：
- recall_experience：无参 → 返回 experience.md 索引（渐进式披露第一层）；
  带 category → 返回该分类文件全文（第二层）。
- record_experience：写入一条经验并重建索引（助手对话中自主记录）。

开关：Assistant.experience_enabled（默认关）。关闭时工具返回暂停提示。
manager 获取：只读 sys.modules["assistant_hub_manager"]，
兜底按路径加载（与 hooks/inject_assistant.py 同一模式）。
"""

import sys
from typing import Any, Dict

from app.tools.result import ToolResult

_MANAGER_MODULE_NAME = "assistant_hub_manager"

_GROUP = "助手记忆"


def _get_manager():
    """只读共享 manager（绝不写 sys.modules：loader AST 安全网拒绝变异型入口）。"""
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is None:
        return None
    return mod.AssistantManager.get_instance()


def _paused() -> ToolResult:
    """暂停提示 + 一次性诊断（定位宿主进程内实例与盘上数据不一致问题后可移除）。"""
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    mgr = mod.AssistantManager.get_instance() if mod is not None else None
    aid = mgr.active_id() if mgr is not None else ""
    a = mgr.get(aid) if mgr is not None and aid else None
    root_val = "?"
    try:
        _r = getattr(mgr, "root", None)
        root_val = str(_r() if callable(_r) else _r)
    except Exception:
        pass
    diag = (
        f"[diag] 模块={id(mod)} 实例={id(mgr)} active_id={aid!r} "
        f"experience_enabled={getattr(a, 'experience_enabled', 'N/A')} root={root_val}"
    )
    return ToolResult(True, content=f"经验功能已暂停。已有内容会保留，但现在不能读取或记录经验。 {diag}")


def _enabled_or_reload(mgr, aid: str, a) -> bool:
    """经验开关判定：内存 False 时重读盘一次（热重载窗口期实例内存/盘分歧防御）。"""
    if a is not None and getattr(a, "experience_enabled", False):
        return True
    try:
        mgr.reload_from_disk()
    except Exception:
        return False
    a2 = mgr.get(aid)
    return a2 is not None and getattr(a2, "experience_enabled", False)


def _resolve_aid(mgr, tool_ctx) -> str:
    """经验归属助手：按工具执行会话解析（override 优先，否则主助手）。"""
    sid = ""
    try:
        sid = str((tool_ctx or {}).get("session_id") or "")
    except Exception:
        sid = ""
    try:
        return mgr.resolve_session_aid(sid)
    except Exception:
        return mgr.active_id()


def _recall_impl(tool_ctx, **kw):
    mgr = _get_manager()
    if mgr is None:
        return ToolResult(False, error="assistant_manager 不可用")
    aid = _resolve_aid(mgr, tool_ctx)
    if not aid or not mgr.has(aid):
        return ToolResult(True, content="当前没有激活的助手。")
    a = mgr.get(aid)
    if not _enabled_or_reload(mgr, aid, a):
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
    aid = _resolve_aid(mgr, tool_ctx)
    if not aid or not mgr.has(aid):
        return ToolResult(True, content="当前没有激活的助手。")
    a = mgr.get(aid)
    if not _enabled_or_reload(mgr, aid, a):
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
        "description": (
            "回忆你的工作经验。无参调用返回经验索引（分类列表）；带 category 返回该分类下的全部经验条目。"
            "当用户交办具体任务（写代码、调研、写文档、分析问题等）时，开工前先查这里有没有相关经验；"
            "闲聊、问答、日常对话无需调用。"
        ),
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
        "description": (
            "记录一条工作经验，供未来的自己做类似事情时回忆。"
            "以下时机应记录：用户指出错误并讲解正确做法；用户明显不耐烦或反复强调某事；"
            "多次尝试后找到有效方法；用户明确说「以后要/不要这样做」；自主工作中踩到坑。"
            "每条简洁直接，一句话。"
        ),
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
