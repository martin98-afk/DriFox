# -*- coding: utf-8 -*-
"""memory_tools.py — memory_read / memory_write（记忆体系专用工具）

照抄 WorkBuddy 的记忆读写角色（它用通用 Edit 工具，DriFox 因沙箱收口为专用工具）：
- memory_read：读全局记忆 / 项目记忆 / 项目日志（按日期）。无参返回概览。
- memory_write：写全局记忆 / 项目记忆（mode=append 末尾追加；整体替换带
  乐观锁 base + 收缩确认 consolidate + 锁内快照）/ 当日日志（追加）。

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
from typing import Any, Dict, List, Optional

from app.plugins.sdk import ToolResult

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


_LOG_LEVEL_SET = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _parse_levels(v) -> "List[str] | str":
    """level 参数（逗号分隔，大小写/中文逗号容错）→ 等级列表；非法时返回错误文案。"""
    raw = str(v or "").strip()
    if not raw:
        return []
    out = [s.strip().upper() for s in raw.replace("，", ",").split(",") if s.strip()]
    bad = [s for s in out if s not in _LOG_LEVEL_SET]
    if bad:
        return f"非法等级: {','.join(bad)}（可选 DEBUG/INFO/WARNING/ERROR，逗号分隔）"
    return out


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
            text = mgr.read_global_memory(aid).strip()
            lim = mgr.memory_max_chars(aid)["global"]
            if len(text) > lim:
                text = (
                    text[:lim]
                    + f"\n...（截断：全文 {len(text)} 字符超上限 {lim}。处理原则：合并去重、删过时条目、压缩表述，"
                    "再用 memory_write(scope=global) 整体写回精简版。）"
                )
            fp = mgr.memory_fingerprint(aid, "global")
            return ToolResult(True, content=((text or "（全局记忆为空）") + f"\n(base: {fp})"))
        if scope == "project":
            if not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法读取项目记忆。")
            text = mgr.read_project_memory(aid, wd).strip()
            lim = mgr.memory_max_chars(aid)["project"]
            if len(text) > lim:
                text = (
                    text[:lim]
                    + f"\n...（截断：全文 {len(text)} 字符超上限 {lim}。处理原则：合并去重、删过时条目、压缩表述，"
                    "再用 memory_write(scope=project) 整体写回精简版。）"
                )
            fp = mgr.memory_fingerprint(aid, "project", wd)
            return ToolResult(True, content=((text or "（项目记忆为空）") + f"\n(base: {fp})"))
        if scope == "daily":
            if not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法读取项目日志。")
            levels = _parse_levels(kw.get("level"))
            if isinstance(levels, str):
                return ToolResult(False, error=levels)
            from_d = str(kw.get("from_date") or "").strip()
            to_d = str(kw.get("to_date") or "").strip()
            search = str(kw.get("search") or "").strip()
            summary_only = bool(kw.get("summary_only"))
            has_filter = bool(levels or search or summary_only or from_d or to_d)
            if (date and (from_d or to_d)) or (from_d and to_d and from_d > to_d):
                return ToolResult(False, error="date 与 from/to 互斥；from 不得晚于 to")
            if date and not has_filter:
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
            if date or has_filter:
                # 运维查询模式：日期范围 + 等级 + 搜索 + 仅摘要，跨天条目流分页（时间线正序，含已蒸馏）
                try:
                    r = mgr.query_daily(
                        aid,
                        wd,
                        from_date=from_d or date,
                        to_date=to_d or date,
                        levels=levels,
                        search=search,
                        summary_only=summary_only,
                        page=_to_int(kw.get("page"), 1),
                    )
                except ValueError as e:
                    return ToolResult(False, error=str(e))
                if not r.get("ok"):
                    return ToolResult(False, error=f"查询日志失败: {r.get('error')}")
                cond = [f"{from_d or date or '最早'}~{to_d or date or '今天'}"]
                if levels:
                    cond.append("等级 " + ",".join(levels))
                if search:
                    mode = "正则" if r.get("search_mode") == "regex" else "子串"
                    cond.append(f"搜索「{search}」（{mode}）")
                if summary_only:
                    cond.append("仅摘要")
                stat = f"命中 {r['matched']}/{r['total']} 条 · 第 {r['page']}/{r['pages']} 页"
                if r.get("days"):
                    stat += f" · 涉及 {len(r['days'])} 天"
                if r["pages"] > 1 and r["page"] < r["pages"]:
                    stat += f"（翻页加 page={r['page'] + 1}）"
                head = f"日志查询（{' · '.join(cond)}）：{stat}"
                body = str(r.get("content") or "")
                if not body:
                    return ToolResult(True, content=f"{head}（无命中）")
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


# ── 全局记忆分区校验诊断（写失败时回传给模型的结构化说明）──


def _section_diag_text(diag: dict, mode: str = "replace") -> str:
    """把 validate_global_sections 的诊断结构渲染成模型可照着改的文本。"""
    names = diag.get("sections") or []
    lines = ["写入被拒绝：全局记忆结构与分区不符。"]
    if diag.get("reason"):
        lines.append(f"原因：{diag['reason']}")
    lines.append(f"可用分区（逐字照抄，禁自建）：{'、'.join(names)}")
    unknown = diag.get("unknown_titles") or []
    if unknown:
        lines.append("清单外标题（需重分配或删除其条目）：")
        for u in unknown:
            lines.append(f"  ## {u.get('title')}（{u.get('count')} 条）")
            for p in u.get("preview") or []:
                lines.append(f"    {p}")
    hb = diag.get("header_in_block") or []
    if hb:
        lines.append("分区块内的 # 小节（改为 - 条目或并入上级）：")
        for h in hb:
            lines.append(f"  第 {h.get('line_no')} 行「{h.get('line')}」（位于 ## {h.get('section')}）")
    if diag.get("preamble_bullets"):
        lines.append(f"分区外总述区有 {diag['preamble_bullets']} 条 - 条目：必须搬进某个分区或删除。")
    if diag.get("empty_entries"):
        lines.append("全文没有任何 - 条目：内容必须写成条目，不能是散文。")
    counts = diag.get("counts") or {}
    if counts:
        lines.append("当前分区计数：" + "、".join(f"{k} {v}" for k, v in counts.items()))
    if diag.get("skeleton"):
        lines.append("兜底骨架（照填也能通过）：")
        lines.append(diag["skeleton"].rstrip())
    lines.append(
        "改完用 mode=replace 重交；仍不确定可先 dry_run=true 只校验。"
        "任何条目都归不进分区时，删掉它即可通过校验（不要自建分区）。"
    )
    return "\n".join(lines)


def _counts_text(diag: dict) -> str:
    parsed = diag.get("parsed") or {}
    counts: dict = {}
    for blk in parsed.get("blocks") or []:
        n = sum(1 for ln in blk.get("lines") or [] if ln.strip().startswith("- "))
        counts[blk.get("name")] = n
    return "、".join(f"{k} {v}" for k, v in counts.items()) or "（空）"


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
        if scope in ("global", "project"):
            if scope == "project" and not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法写项目记忆。")
            lim = mgr.memory_max_chars(aid)[scope]
            label = "全局" if scope == "global" else "项目"
            mode = str(kw.get("mode") or "replace").strip().lower()
            if mode not in ("replace", "append"):
                return ToolResult(False, error="mode 必须是 replace（整体替换）/ append（末尾追加）")
            # 全局记忆分区：schema 非空时强制归属（空 = 自由格式，全部跳过）
            sections = mgr.memory_sections(aid) if scope == "global" else []
            section = str(kw.get("section") or "").strip()
            dry_run = bool(kw.get("dry_run")) and scope == "global" and mode == "replace"
            if sections:
                names = [str(s.get("name") or "") for s in sections]
                if mode == "append":
                    if not section:
                        return ToolResult(
                            False,
                            error=(
                                "追加被拒绝：全局记忆已定义分区，append 必须带 section。"
                                f"可用分区：{'、'.join(names)}。"
                                "按内容性质选最匹配的一个；归属不了的条目不要写。"
                            ),
                        )
                    if section not in names:
                        return ToolResult(
                            False,
                            error=(
                                f"追加被拒绝：分区「{section}」不在清单内。"
                                f"可用分区：{'、'.join(names)}。"
                                "禁自建分区，选一个最匹配的；都没有合适的就别写这条。"
                            ),
                        )
                else:
                    v = mgr.validate_global_memory(aid, content)
                    if not v.get("ok"):
                        return ToolResult(
                            False,
                            error=_section_diag_text(v, mode="replace"),
                        )
                    if dry_run:
                        counts = _counts_text(v)
                        return ToolResult(
                            True,
                            content=f"分区校验通过（dry_run，未落盘）。各分区条目数：{counts}。去掉 dry_run 真写即可。",
                        )
            if mode == "append":
                # 末尾追加：不覆盖任何已有行；超限预检（追加后总量）
                cur_text = str(
                    (mgr.read_global_memory(aid) or "")
                    if scope == "global"
                    else (mgr.read_project_memory(aid, wd) or "")
                ).strip()
                new_total = len(cur_text) + len(content) + (1 if cur_text else 0)
                if new_total > lim:
                    return ToolResult(
                        False,
                        error=(
                            f"追加被拒绝：追加后 {new_total}/{lim} 字符超上限。"
                            f"先 memory_read(scope={scope}) 读全文，合并去重后用整体替换（mode 缺省）写回精简版。"
                        ),
                    )
                r = _retry(lambda: mgr.append_memory(aid, scope, content, workdir=wd, section=section))
                if not r.get("ok"):
                    return ToolResult(False, error=f"追加失败: {r.get('error')}")
                where = f"「{section}」分区" if section else ""
                return ToolResult(True, content=f"已追加到{label}记忆{where}（现 {r.get('chars')} 字）。已有内容不受影响。")
            # ── 整体替换：超限预检 → 乐观锁 + 收缩确认 → 锁内快照落盘 ──
            if len(content) > lim:
                return ToolResult(
                    False,
                    error=(
                        f"写入被拒绝：{label}记忆超上限（{len(content)}/{lim} 字符）。"
                        f"处理原则：先 memory_read(scope={scope}) 读全文，合并去重、删过时条目、压缩表述后再整体写回。"
                    ),
                )
            base = str(kw.get("base") or "").strip()
            consolidate = bool(kw.get("consolidate"))
            if scope == "global":
                r = _retry(
                    lambda: mgr.write_memory_guarded(
                        aid,
                        "global",
                        content,
                        base=base,
                        consolidate=consolidate,
                        snapshot=lambda: mgr.snapshot_memory(aid, "global"),
                    )
                )
            else:
                r = _retry(
                    lambda: mgr.write_memory_guarded(
                        aid,
                        "project",
                        content,
                        base=base,
                        consolidate=consolidate,
                        snapshot=lambda: mgr.snapshot_memory(aid, "project", wd),
                        workdir=wd,
                    )
                )
            if not r.get("ok", False):
                err = str(r.get("error"))
                if err == "conflict":
                    return ToolResult(
                        False,
                        error=(
                            f"版本冲突：{label}记忆已被其他会话修改（当前 base: {r.get('cur_base')}）。"
                            f"先 memory_read(scope={scope}) 读最新全文（返回末尾带新 base），"
                            "基于新版本重做修改后带新 base 再写。"
                        ),
                    )
                if err == "shrink":
                    return ToolResult(
                        False,
                        error=(
                            f"写入被拒绝：新内容 {r.get('new_chars')} 字符比现有 {r.get('old_chars')} 字符缩水超 60%。"
                            "若是有意整理（合并/删过时条目）带 consolidate=true 重试；"
                            "若只是想记一条，改用 mode=append。"
                        ),
                    )
                if err == "unreadable":
                    return ToolResult(
                        False,
                        error=(
                            f"{label}记忆文件此刻不可读（可能被网盘同步、杀毒扫描或其他程序占用）。"
                            "为防误覆盖已拒绝写入，请稍后重试；反复出现请检查文件占用状态。"
                        ),
                    )
                return ToolResult(False, error=f"写记忆失败: {err}")
            return ToolResult(True, content=f"已更新{label}记忆 MEMORY.md（{r.get('chars')} 字）。")

        if scope == "daily":
            if not wd:
                return ToolResult(True, content="当前没有设置工作目录，无法写工作日志。")
            # 打蒸馏标：把全部未蒸馏日志归档为 .distilled（保留可查，蒸馏/门禁均忽略）。
            # 全量语义：门禁流程本就是「全部蒸馏完才放行」，逐日打标是机械循环；
            # 一条命令收敛，date 参数因此从工具面彻底移除（写日期一律时钟决定）。
            if kw.get("mark_distilled"):
                try:
                    dates = mgr.undistilled_daily_before(aid, wd)
                except Exception as e:
                    return ToolResult(False, error=f"查询未蒸馏日志失败: {e}")
                if not dates:
                    return ToolResult(True, content="当前没有未打标日志。")
                done, merged_total, failures = [], 0, []
                for d in dates:
                    r = _retry(lambda d=d: mgr.mark_daily_distilled(aid, wd, d))
                    if not r.get("ok"):
                        failures.append(f"{d}: {r.get('error')}")
                        continue
                    done.append(d)
                    merged_total += int(r.get("merged") or 0)
                if failures and not done:
                    return ToolResult(False, error="打标失败: " + "; ".join(failures))
                msg = f"已给 {len(done)} 天日志打蒸馏标（{', '.join(done)}，保留可查）。"
                if merged_total:
                    msg += f" 归档后又有追加的残留条目 {merged_total} 条已并入归档。"
                if failures:
                    msg += " 失败: " + "; ".join(failures)
                return ToolResult(True, content=msg)
            # 新日门禁：今天还没写过、且存在未蒸馏旧日志 → 拒绝并返回蒸馏指令
            today = datetime.now().strftime("%Y-%m-%d")
            if not mgr.daily_exists(aid, wd, today):
                try:
                    gate = mgr.distill_gate_block(aid, wd)
                except Exception:
                    gate = ""  # 门禁查询异常不阻断正常写入
                if gate:
                    return ToolResult(False, error=gate)
            # 追加恒写系统当日日志：日期由时钟决定（date 参数已从工具面移除）
            r = _retry(
                lambda: mgr.append_daily(
                    aid, wd, content, "", str(kw.get("level") or "INFO"), str(kw.get("summary") or "")
                )
            )
            if not r.get("ok"):
                if r.get("error") == "too_long":
                    lim = r.get("limit") or 400
                    return ToolResult(
                        False,
                        error=(
                            f"写入被拒绝：日志正文 {r.get('chars')}/{lim} 字符超上限。"
                            "日志是索引不是报告：只写结论、关键数字、坑与修法；过程叙述、复述需求、"
                            "推理过程全砍。超长内容改写项目 MEMORY.md 或留在会话里。压缩后重试。"
                        ),
                    )
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
            "读长期记忆。scope=global/project：返回对应 MEMORY.md 全文（超上限时截断并附整理原则，整理后用 memory_write 写回精简版）；"
            "scope=daily 带 date 读某天日志全文（长日志自动分页，翻页加 page）；不带 date 列近期索引（limit 控制条数）；"
            "scope=daily 带 level/from_date/to_date/search/summary_only 任一参数进入查询模式：按等级/日期范围/正则过滤，"
            "summary_only=true 只看「时刻|等级|摘要」，跨天条目流按时间线正序分页（date 收窄为单日过滤，与 from_date/to_date 互斥）；"
            "不传 scope 返回概览。已蒸馏日志（.distilled）仍可读。"
            "用户提到过去工作、旧决策、项目约定而当前上下文没有时，先来这里查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "global / project / daily；缺省返回概览"},
                "date": {
                    "type": "string",
                    "description": "scope=daily 时的日期 YYYY-MM-DD；缺省列出近期索引；与 from_date/to_date 互斥",
                },
                "page": {
                    "type": "integer",
                    "description": "scope=daily 且带 date 时：条目分页页码（每页 20 条），缺省第 1 页",
                },
                "level": {
                    "type": "string",
                    "description": "scope=daily 查询模式：按等级过滤，逗号分隔多值，如 WARNING 或 info,error",
                },
                "from_date": {
                    "type": "string",
                    "description": "scope=daily 查询模式：起始日期 YYYY-MM-DD（含），缺省不限；与 date 互斥",
                },
                "to_date": {
                    "type": "string",
                    "description": "scope=daily 查询模式：结束日期 YYYY-MM-DD（含），缺省今天；与 date 互斥",
                },
                "search": {
                    "type": "string",
                    "description": "scope=daily 查询模式：按正则（忽略大小写）过滤条目，编译失败自动回落子串",
                },
                "summary_only": {
                    "type": "boolean",
                    "description": "scope=daily 查询模式：true 只输出「时刻 | 等级 | 摘要」一行，不含正文",
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
            "写长期记忆。最简写法：一条一行，只写结论/关键数字/坑与修法，不写过程。\n"
            "scope=daily：追加当日工作日志。**门槛高**：只有产出跨会话仍有用的结论时才记（定案、踩坑与修法、量化基线、"
            "用户明确要求）；同一任务多轮打磨只记最终结论一次；探路、试错、中途状态、待办一律不记。"
            "正文硬上限 400 字符，超限拒写。禁止：过程叙述、复述需求、推理链、代码/日志粘贴、"
            "分节小标题（## 现象/## 根因）。summary=一句话结论（≤30 字），level=INFO/WARNING/ERROR。"
            "新一天首条 daily 若存在未蒸馏旧日志会被拒，按返回指令蒸馏并打标后重试。\n"
            "scope=project：项目 MEMORY.md。记一条增量内容用 mode=append（末尾追加，不碰已有行）；"
            "整体替换（mode 缺省）必须先读后写，带 memory_read 返回末尾的 base 做版本校验，"
            "新内容比现有缩水超 60% 需 consolidate=true 确认是有意整理。"
            "只记 AGENTS.md 没有的动态沉淀（坑、决策理由、易错点、用户偏好），"
            "一条一碎片、禁散文；AGENTS.md 静态规范（构建/测试/目录/风格/提交/EOL）禁止抄入；超上限拒写（默认 3000 字符）。\n"
            "scope=global：全局 MEMORY.md。记一条用 mode=append；整体替换规则同 project（先读拿 base，大幅精简带 consolidate）。"
            "准入极严：仅用户显式要求的跨项目规则、蒸馏筛出的最珍贵且与项目无关经验；"
            "超上限拒写（默认 4000 字符）。除用户明确要求，不存密钥。"
            "若已定义记忆分区（系统提示列了分区清单）：append 必须带 section，整体替换必须按 ## 分区名 组织，"
            "出现清单外标题/分区块内 # 小节/分区外 - 条目会被拒写并返回诊断；"
            "不确定能否通过时先 dry_run=true 只校验不落盘。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "daily（日志追加）/ project（项目 MEMORY.md）/ global（全局 MEMORY.md）",
                },
                "content": {
                    "type": "string",
                    "description": "daily=细节正文（≤400 字符，超出拒写）；project/global=完整新文件内容（replace）或一条新内容（append）",
                },
                "mode": {
                    "type": "string",
                    "description": "scope=global/project 时：replace（缺省，整体替换）/ append（末尾追加一条，高频记一条用这个，不会覆盖已有内容）",
                },
                "section": {
                    "type": "string",
                    "description": "scope=global 且 mode=append 时必填（已定义分区时）：目标分区名，逐字照抄系统提示的分区清单；内容插到该分区块末尾",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "只跑校验不落盘（不写快照、不校验乐观锁）：scope=global 且 mode=replace 时用于预检分区合规，通过后再真写",
                },
                "base": {
                    "type": "string",
                    "description": "整体替换的乐观锁：memory_read 返回末尾 (base: ...) 的值；磁盘版本与它不一致时拒写（防多会话覆盖）",
                },
                "consolidate": {
                    "type": "boolean",
                    "description": "整体替换时新内容比现有缩水超 60% 必须为 true（确认是有意整理而非误写）",
                },
                "level": {
                    "type": "string",
                    "description": "daily 日志等级 INFO/WARNING/ERROR，缺省 INFO",
                },
                "summary": {
                    "type": "string",
                    "description": "daily 一句话摘要",
                },
                "mark_distilled": {
                    "type": "boolean",
                    "description": "daily 蒸馏完成打标：true 时给 date 指定日志打 .distilled 标（保留可查，蒸馏/门禁均忽略），content 可空",
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
