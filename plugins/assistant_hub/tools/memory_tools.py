# -*- coding: utf-8 -*-
"""memory_tools.py — memory_read / memory_write（记忆体系专用工具）

照抄 WorkBuddy 的记忆读写角色（它用通用 Edit 工具，DriFox 因沙箱收口为专用工具）：
- memory_read：读全局记忆 / 项目记忆 / 项目日志（按日期）。无参返回概览。
- memory_write：写全局记忆 / 项目记忆（整体更新）/ 当日日志（追加）。

路径来源：tool_ctx["workdir"]（会话工作目录）与 manager 助手目录；
不经沙箱（工具自身就是记忆边界的实现者）。

manager 取用：优先 sys.modules 的共享实例；缺失期（tools 热重载按
module_prefixes 摘除后、下一轮 hook 尚未重建）经 hooks 文件桥接重建——
工具文件受 AST 安全网限制不能直写 sys.modules（dataclass 模块必须注册
才能加载），而 hooks/inject_assistant.py 有注册权。

开关：Assistant.memory_enabled（默认开）。关闭时写返回暂停提示，读仍可用
（避免残存记忆无法查看/清理）。
"""

import importlib.util
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.tools.result import ToolResult

_MANAGER_MODULE_NAME = "assistant_hub_manager"
_HOOKS_PATH = Path(__file__).resolve().parent.parent / "hooks" / "inject_assistant.py"

_GROUP = "助手记忆"

# 写并发保护：同 (进程内) 文件锁在 store 层；这里加一层短退避重试兜 Windows 句柄
_WRITE_RETRY = 3

# 窗口期桥接缓存（不写 sys.modules：AST 安全网限制；注册在 hooks 文件内完成）
_bridge_mod: Optional[Any] = None
_bridge_lock = threading.Lock()


def _load_manager_via_bridge():
    """热重载窗口期兜底：经 hooks 文件重建共享 manager（mtime 自检缓存桥接）。

    窗口成因：tools 热重载按 module_prefixes 摘除 assistant_hub_manager，
    而工具文件禁止直写 sys.modules（AST 安全网）。hooks 的 _ensure_manager_module
    有注册权（且带 mtime 自检），exec 其调用即可恢复共享实例。
    """
    global _bridge_mod
    try:
        mtime = _HOOKS_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    bridge = _bridge_mod
    if bridge is not None and getattr(bridge, "_bridge_mtime", -1.0) >= mtime:
        return bridge
    with _bridge_lock:
        bridge = _bridge_mod
        if bridge is not None and getattr(bridge, "_bridge_mtime", -1.0) >= mtime:
            return bridge
        try:
            spec = importlib.util.spec_from_file_location("_ah_memory_tools_bridge", str(_HOOKS_PATH))
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            return None
        module._bridge_mtime = mtime
        _bridge_mod = module
        return module


def _get_manager():
    """取共享 manager；sys.modules 缺失（热重载窗口期）时经 hooks 桥接重建。"""
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is None:
        bridge = _load_manager_via_bridge()
        if bridge is None:
            return None
        try:
            return bridge._get_manager()
        except Exception:
            return None
    try:
        return mod.AssistantManager.get_instance()
    except Exception:
        return None


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


def _to_int(v, default: int) -> int:
    try:
        return int(v)
    except TypeError, ValueError:
        return default


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
                # 单日全文（条目分页：防长日志整块砸上下文）
                page = _to_int(kw.get("page"), 1)
                r = mgr.read_daily_paged(aid, wd, date, page=page)
                if not r.get("ok"):
                    if r.get("error") == "not_found":
                        return ToolResult(True, content=f"（{date} 无日志）")
                    return ToolResult(False, error=f"读日志失败: {r.get('error')}")
                head = f"{date}（第 {r['page']}/{r['pages']} 页，共 {r['total']} 条）"
                if r.get("distilled"):
                    head += " · 已蒸馏"
                body = str(r.get("content") or "")
                if r["pages"] > 1 and r["page"] < r["pages"]:
                    body += f"\n\n（后续还有内容：翻页加 page={r['page'] + 1}，共 {r['pages']} 页）"
                return ToolResult(True, content=f"{head}\n\n{body}")
            # 近期索引：limit 可调（默认 7，上限 90，防日多被截）
            limit = min(max(_to_int(kw.get("limit"), 7), 1), 90)
            items = mgr.list_daily(aid, wd, limit=limit)
            if not items:
                return ToolResult(True, content="（暂无工作日志）")
            lines = [f"近期工作日志（读某天全文加 date=YYYY-MM-DD，长日志翻页加 page=N；本列前 {len(items)} 天）："]
            for it in items:
                tag = " · 已蒸馏" if it.get("distilled") else ""
                lines.append(f"- {it['date']}（{it['chars']} 字{tag}）")
            return ToolResult(True, content="\n".join(lines))
        # 无 scope：概览
        paths = mgr.memory_paths(aid, wd)
        lines = [f"全局记忆：{paths.get('global')}（{len(mgr.read_global_memory(aid).strip())} 字）"]
        if wd:
            lines.append(f"项目记忆：{paths.get('project')}（{len(mgr.read_project_memory(aid, wd).strip())} 字）")
            items = mgr.list_daily(aid, wd, limit=90)
            daily = "、".join(
                (f"{it['date']}（{it['chars']} 字）" if not it.get("distilled") else f"{it['date']}（已蒸馏）")
                for it in items
            )
            lines.append("日志：" + (daily or "（无）"))
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
    is_mark = scope == "daily" and bool(kw.get("mark_distilled"))
    if not content and not is_mark:
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
            # 打蒸馏标：给 date 指定的日志重命名为 .distilled（保留可查，蒸馏/门禁均忽略）
            if kw.get("mark_distilled"):
                if not date:
                    return ToolResult(False, error="mark_distilled=true 时必须给 date=YYYY-MM-DD")
                r = _retry(lambda: mgr.mark_daily_distilled(aid, wd, date))
                if not r.get("ok"):
                    hint = {"not_found": f"{date} 无未打标日志", "already": f"{date} 已打过标"}.get(
                        str(r.get("error")), str(r.get("error"))
                    )
                    return ToolResult(False, error=f"打标失败: {hint}")
                return ToolResult(True, content=f"已给 {date} 日志打蒸馏标（保留可查，后续蒸馏/门禁均忽略）。")
            # 新日门禁：目标是今天、今天还没写过、且存在未蒸馏旧日志 → 拒绝并返回蒸馏指令
            today = datetime.now().strftime("%Y-%m-%d")
            if (date or today) == today:
                try:
                    gate = mgr.distill_gate_block(aid, wd) if not mgr.daily_exists(aid, wd, today) else ""
                except Exception:
                    gate = ""  # 门禁查询异常不阻断正常写入
                if gate:
                    return ToolResult(False, error=gate)
            r = _retry(lambda: mgr.append_daily(aid, wd, content, date, str(kw.get("level") or "INFO")))
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
            "scope=daily 读项目工作日志（带 date 读某天全文，长日志自动按条目分页、翻页加 page；"
            "不带 date 列近期索引，limit 控制条数，已蒸馏日志带标记仍可读）。"
            "不传 scope 返回记忆概览（路径+字数）。"
            "用户提到过去的工作、旧决策、项目约定而当前上下文没有时，先来这里查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "global / project / daily；缺省返回概览"},
                "date": {"type": "string", "description": "scope=daily 时的日期 YYYY-MM-DD；缺省列出近期索引"},
                "page": {
                    "type": "integer",
                    "description": "scope=daily 且带 date 时：条目分页页码（每页 20 条），缺省第 1 页",
                },
                "limit": {
                    "type": "integer",
                    "description": "scope=daily 不带 date 时：索引条数上限（默认 7，最大 90）",
                },
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
            "scope=daily 为追加写当日日志（程序自动加「时刻 | 等级 |」前缀，content 勿自带时间戳）；新的一天第一次写日志前会先要求蒸馏未蒸馏旧日志，"
            "被拒时按提示完成蒸馏再重试；蒸馏完的日志用 scope=daily + date + mark_distilled=true 打标。"
            "scope=project/global 为整体替换对应 MEMORY.md（先读后写，保持精简）。"
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
                    "description": "要写入的内容（daily=一条简短记录，勿写日期标题与时间戳；project/global=完整的新文件内容）",
                },
                "date": {"type": "string", "description": "scope=daily 可选：补写某天的日期 YYYY-MM-DD，缺省今天"},
                "level": {
                    "type": "string",
                    "description": "scope=daily 可选：日志等级 INFO/WARNING/ERROR，缺省 INFO；时刻由程序自动打点，勿在 content 里写时间",
                },
                "mark_distilled": {
                    "type": "boolean",
                    "description": "scope=daily 时可选：true 表示给 date 指定的日志打蒸馏标（.distilled，保留可查），此时 content 可空",
                },
            },
            "required": ["scope"],
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
