# -*- coding: utf-8 -*-
"""memory_tools.py — memory_read / memory_write（记忆体系专用工具）

照抄 WorkBuddy 的记忆读写角色（它用通用 Edit 工具，DriFox 因沙箱收口为专用工具）：
- memory_read：读全局记忆 / 项目记忆 / 项目日志（按日期）。无参返回概览。
- memory_write：写全局记忆 / 项目记忆（整体更新）/ 当日日志（追加）。

路径来源：tool_ctx["workdir"]（会话工作目录）与 manager 助手目录；
不经沙箱（工具自身就是记忆边界的实现者），与 recall/record_experience 同款
"只读 sys.modules 共享 manager"模式。

开关：Assistant.memory_enabled（默认开）。关闭时写返回暂停提示，读仍可用
（避免残存记忆无法查看/清理）。
"""

import sys
import time
from typing import Any, Dict, Optional

from app.tools.result import ToolResult

_MANAGER_MODULE_NAME = "assistant_hub_manager"

_GROUP = "助手记忆"

# 写并发保护：同 (进程内) 文件锁在 store 层；这里加一层短退避重试兜 Windows 句柄
_WRITE_RETRY = 3


def _get_manager():
    """只读共享 manager（绝不写 sys.modules：loader AST 安全网拒绝变异型入口）。"""
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is None:
        return None
    return mod.AssistantManager.get_instance()


def _paused() -> ToolResult:
    return ToolResult(True, content="记忆功能已暂停。已有内容会保留，但现在不能写入。请在助手中心开启记忆开关。")


def _resolve_aid(mgr, tool_ctx) -> str:
    """记忆归属助手：按工具执行会话解析（override 优先，否则主助手）。"""
    sid = ""
    try:
        sid = str((tool_ctx or {}).get("session_id") or "")
    except Exception:
        sid = ""
    try:
        return mgr.resolve_session_aid(sid)
    except Exception:
        return mgr.active_id()


def _workdir(tool_ctx) -> str:
    try:
        return str((tool_ctx or {}).get("workdir") or "")
    except Exception:
        return ""


def _enabled_or_reload(mgr, aid: str, a) -> bool:
    """记忆开关判定：内存 False 时重读盘一次（热重载窗口期实例内存/盘分歧防御）。"""
    if a is not None and getattr(a, "memory_enabled", False):
        return True
    try:
        mgr.reload_from_disk()
    except Exception:
        return False
    a2 = mgr.get(aid)
    return a2 is not None and getattr(a2, "memory_enabled", False)


# ── memory_read ──────────────────────────────────────────


def _read_impl(tool_ctx, **kw) -> ToolResult:
    mgr = _get_manager()
    if mgr is None:
        return ToolResult(False, error="assistant_manager 不可用")
    aid = _resolve_aid(mgr, tool_ctx)
    if not aid or not mgr.has(aid):
        return ToolResult(True, content="当前没有激活的助手。")
    scope = str(kw.get("scope") or "").strip().lower()
    date = str(kw.get("date") or "").strip()
    wd = _workdir(tool_ctx)

    try:
        if scope == "global":
            text = mgr.read_global_memory(aid)
            return ToolResult(True, content=text.strip() or "（全局记忆为空）")
        if scope == "project":
            if not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法读取项目记忆。")
            text = mgr.read_project_memory(aid, wd)
            return ToolResult(True, content=text.strip() or "（项目记忆为空）")
        if scope == "daily":
            if not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法读取项目日志。")
            if date:
                text = mgr.read_daily(aid, wd, date)
                return ToolResult(True, content=text.strip() or f"（{date} 无日志）")
            items = mgr.list_daily(aid, wd, limit=7)
            if not items:
                return ToolResult(True, content="（暂无工作日志）")
            lines = ["近期工作日志（读某天加 date=YYYY-MM-DD）："]
            for it in items:
                lines.append(f"- {it['date']}（{it['chars']} 字）")
            return ToolResult(True, content="\n".join(lines))
        # 无 scope：概览
        paths = mgr.memory_paths(aid, wd)
        lines = [f"全局记忆：{paths.get('global')}（{len(mgr.read_global_memory(aid).strip())} 字）"]
        if wd:
            lines.append(f"项目记忆：{paths.get('project')}（{len(mgr.read_project_memory(aid, wd).strip())} 字）")
            items = mgr.list_daily(aid, wd, limit=7)
            lines.append("近期日志：" + ("、".join(f"{it['date']}（{it['chars']} 字）" for it in items) or "（无）"))
        else:
            lines.append("项目记忆：（当前无工作目录）")
        return ToolResult(True, content="\n".join(lines))
    except Exception as e:
        return ToolResult(False, error=f"读取记忆失败: {e}")


# ── memory_write ─────────────────────────────────────────


def _write_impl(tool_ctx, **kw) -> ToolResult:
    mgr = _get_manager()
    if mgr is None:
        return ToolResult(False, error="assistant_manager 不可用")
    aid = _resolve_aid(mgr, tool_ctx)
    if not aid or not mgr.has(aid):
        return ToolResult(True, content="当前没有激活的助手。")
    a = mgr.get(aid)
    if not _enabled_or_reload(mgr, aid, a):
        return _paused()

    scope = str(kw.get("scope") or "").strip().lower()
    content = str(kw.get("content") or "").strip()
    date = str(kw.get("date") or "").strip()
    if not content:
        return ToolResult(False, error="content 不能为空")
    wd = _workdir(tool_ctx)

    def _retry(fn):
        last: Optional[Exception] = None
        for i in range(_WRITE_RETRY):
            try:
                return fn()
            except OSError as e:
                last = e
                time.sleep(0.05 * (i + 1))
        raise last  # type: ignore[misc]

    try:
        if scope == "global":
            _retry(lambda: mgr.write_global_memory(aid, content))
            return ToolResult(True, content="已更新全局记忆 MEMORY.md。")

        if scope == "project":
            if not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法写项目记忆。")
            _retry(lambda: mgr.write_project_memory(aid, wd, content))
            return ToolResult(True, content="已更新项目记忆 MEMORY.md。")

        if scope == "daily":
            if not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法写工作日志。")
            r = _retry(lambda: mgr.append_daily(aid, wd, content, date))
            if not r.get("ok"):
                return ToolResult(False, error=f"写日志失败: {r.get('error')}")
            return ToolResult(True, content=f"已追加到 {r.get('date')} 工作日志（现 {r.get('chars')} 字）。")

        return ToolResult(False, error="scope 必须是 global / project / daily 之一")
    except Exception as e:
        return ToolResult(False, error=f"写入记忆失败: {e}")


# 注：写入后**不**失效会话 system prompt 缓存（用户决策 2026-10-13）。
# 已有会话的历史消息本就不会因记忆变化而更新，中途重建 system prompt 只会
# 造成前缀抖动（浪费 API prompt cache），且刚写完的记忆模型自己最清楚。
# 新记忆的可见路径：① 本会话随时 memory_read 读取；② 新会话/清空/压缩时
# 自然重建注入；③ UI「查看」直读磁盘。记忆开关/人格等结构性变更仍走失效
# （见 assistant_card 的开关处理，那类变更各会话必须重新构建身份块）。


_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_read",
        "description": (
            "读你的长期记忆。scope=global 读跨项目全局记忆；scope=project 读当前项目长期笔记；"
            "scope=daily 读项目工作日志（带 date=YYYY-MM-DD 读某天，不带 date 列出近期日志索引）。"
            "不传 scope 返回记忆概览（路径+字数）。"
            "用户提到过去的工作、旧决策、项目约定而当前上下文没有时，先来这里查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "global / project / daily；缺省返回概览"},
                "date": {"type": "string", "description": "scope=daily 时的日期 YYYY-MM-DD；缺省列出最近索引"},
            },
        },
    },
}

_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_write",
        "description": (
            "写你的长期记忆（完成实质工作后必须调用一次 scope=daily 记录；"
            "用户分享了项目约定/偏好时更新 scope=project；用户要求跨项目长期记住某事时 scope=global）。"
            "scope=daily 为追加写当日日志；scope=project/global 为整体替换对应 MEMORY.md（先读后写，保持精简）。"
            "日志只沉淀有跨会话价值的内容：完成的进展、决策、坑、约定；不写待办、瞬时信息、工具报错。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "daily（日志追加）/ project（项目 MEMORY.md 整体更新）/ global（全局 MEMORY.md 整体更新）",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容（daily=一条简短记录；project/global=完整的新文件内容）",
                },
                "date": {"type": "string", "description": "scope=daily 可选：补写某天的日期 YYYY-MM-DD，缺省今天"},
            },
            "required": ["scope", "content"],
        },
    },
}


def register(registry):
    """工具插件化注册入口（PluginToolLoader 调用）"""
    registry.register(
        "memory_read",
        _READ_SCHEMA,
        impl=_read_impl,
        danger="safe",
        icon="experience",
        cn_name="读取记忆",
        group=_GROUP,
        description="读取助手长期记忆（全局/项目/日志）",
        aliases=["读记忆", "查看记忆"],
    )
    registry.register(
        "memory_write",
        _WRITE_SCHEMA,
        impl=_write_impl,
        danger="safe",
        icon="experience",
        cn_name="写入记忆",
        group=_GROUP,
        description="写入助手长期记忆（日志追加/项目笔记/全局记忆）",
        aliases=["写记忆", "记笔记"],
    )
