# -*- coding: utf-8 -*-
"""store.py — 记忆存储层（照抄 WorkBuddy 双层结构 · 每助手独立）。

布局（每助手一套）：
  全局记忆  <app_data>/assistant_hub/<aid>/MEMORY.md        跨项目；模型可读写
  记忆分区  <app_data>/assistant_hub/<aid>/memory_sections.json  全局记忆的分区定义
  项目记忆  {workdir}/.drifox/memory/<aid>/
            ├── YYYY-MM-DD.md   每日工作日志（append-only）
            └── MEMORY.md       项目长期笔记

并发：单实例架构（single_instance 保证），进程级 per-path 锁足够；
读容错返回空串，写用原子替换（tmp + os.replace）防半写。
本模块不 import 主程序，可被 hook 环境独立加载。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 项目记忆目录名（工作目录内）
PROJECT_DIR_NAME = ".drifox"
MEMORY_SUBDIR = "memory"
GLOBAL_MEMORY_FILE = "MEMORY.md"
PROJECT_MEMORY_FILE = "MEMORY.md"
# 已蒸馏日志后缀：YYYY-MM-DD.md → YYYY-MM-DD.distilled.md（内容保留可查，蒸馏/门禁时忽略）
DISTILLED_SUFFIX = ".distilled"

# ── 快照（覆盖写前的历史版本，集中存助手目录，随 Gitee 同步走）──────
SNAPSHOT_DIR_NAME = "memory_snapshots"
SNAPSHOT_KEEP = 10
WORKDIR_STATE_FILE = "workdir_state.json"
# 全局记忆分区定义（用户可编辑；空 schema = 自由格式，行为与改造前一致）
MEMORY_SECTIONS_FILE = "memory_sections.json"
# 默认分区：按「内容成因」划分，不看主题词。分三类是为了让归属判定只剩三选一：
# 我总结出的方法 / 工具自身的固有行为 / 用户亲口说的要求。
# 上限守住 4 个：分区一多分类就不确定，与本功能「严格归属」的前提冲突。
# hint 会被 UI 单行显示，主句控制在 12 字内；完整规则用 detail（悬停提示 + 注入 prompt）。
DEFAULT_SECTIONS = (
    {
        "name": "工程方法论",
        "hint": "做事顺序与验证方法",
        "detail": "做事的顺序、验证与判定方法、质量纪律。写「怎么判断改对了」，不写具体工具行为",
    },
    {
        "name": "工具与环境",
        "hint": "工具平台的客观坑",
        "detail": "工具/平台/框架/命令的客观坑与修法，谁用都会撞上。写工具自身行为，不写本项目业务规则",
    },
    {
        "name": "用户偏好",
        "hint": "用户的长期要求",
        "detail": "用户亲口提的长期要求、表达习惯、明确禁止事项。与具体项目无关的都放这里",
    },
)

_SNAP_NAME_RE = re.compile(r"^\d{8}-\d{6}\.md$")

# 注入相关内容量（manager/prompts 使用）
DAILY_LOOKBACK = 7  # 注入的近期日志条数
STALE_DAILY_DAYS = 30  # 超期未蒸馏阈值（天）
GLOBAL_MEMORY_MAX_CHARS = 4000
PROJECT_MEMORY_MAX_CHARS = 3000
# 单条工作日志正文上限（摘要另计）。日志是索引不是报告，超限直接拒写逼模型压缩
DAILY_ENTRY_MAX_CHARS = 400
# 整体替换收缩检查：现有内容达到 SHRINK_CHECK_MIN_CHARS 且新内容 < 现有*SHRINK_RATIO
# 视为大幅精简，需模型显式 consolidate=true（防「记一条覆盖全部」事故，2026-10-23）
SHRINK_CHECK_MIN_CHARS = 200
SHRINK_RATIO = 0.4

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 工作日志条目前缀的合法等级（对齐 loguru 命名）
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def is_date_name(stem: str) -> bool:
    """文件名是否标准日期（YYYY-MM-DD）；同时是 date 参数的穿越防线。"""
    return bool(_DATE_RE.match(stem))


def parse_daily_stem(stem: str) -> Optional[tuple]:
    """日志文件 stem → (date, distilled)；非日志文件返回 None。

    识别两种形态：`YYYY-MM-DD`（未蒸馏）与 `YYYY-MM-DD.distilled`（已蒸馏）。
    """
    if is_date_name(stem):
        return stem, False
    if stem.endswith(DISTILLED_SUFFIX) and is_date_name(stem[: -len(DISTILLED_SUFFIX)]):
        return stem[: -len(DISTILLED_SUFFIX)], True
    return None


# ── 路径 ─────────────────────────────────────────────────


def global_memory_path(aid_dir: Path) -> Path:
    """全局记忆文件路径（<助手目录>/MEMORY.md）。"""
    return Path(aid_dir) / GLOBAL_MEMORY_FILE


def sections_path(aid_dir: Path) -> Path:
    """全局记忆分区定义路径（<助手目录>/memory_sections.json）。"""
    return Path(aid_dir) / MEMORY_SECTIONS_FILE


def project_dir(workdir: str | Path, aid: str) -> Path:
    """项目记忆目录（{workdir}/.drifox/memory/<aid>）。"""
    return Path(workdir) / PROJECT_DIR_NAME / MEMORY_SUBDIR / aid


def project_memory_path(workdir: str | Path, aid: str) -> Path:
    return project_dir(workdir, aid) / PROJECT_MEMORY_FILE


def daily_path(workdir: str | Path, aid: str, date: str = "") -> Path:
    """当日日志文件路径；date 缺省今天（YYYY-MM-DD）。

    date 必须是标准日期格式：它会被拼进文件名，而 memory_write 是
    danger=safe 工具（不弹审批），不校验就是沙箱穿越向量。
    """
    d = date or datetime.now().strftime("%Y-%m-%d")
    if not is_date_name(d):
        raise ValueError(f"非法日志日期: {d!r}（须为 YYYY-MM-DD）")
    return project_dir(workdir, aid) / f"{d}.md"


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def distilled_daily_path(path: Path) -> Path:
    """未蒸馏日志路径 → 已蒸馏路径（YYYY-MM-DD.md → YYYY-MM-DD.distilled.md）。"""
    return Path(path).parent / (Path(path).stem + DISTILLED_SUFFIX + Path(path).suffix)


# ── 读 ───────────────────────────────────────────────────


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def read_global_memory(aid_dir: Path) -> str:
    return read_text(global_memory_path(aid_dir))


# ── 全局记忆分区（用户可编辑 schema；空 = 自由格式）────────────
#
# 文件结构约定（分区非空时才强制）：
#   [可选总述]            顶部一段，允许文字，禁 "- " 条目行
#   ## 分区名             独占一行，名必须 ∈ schema
#   - 条目                块内只允许 "- " 条目，禁 "#" 开头行
# 分区名的歧义防线：只认行首正好 "## " 的标题行，条目内容里的 ## 因带 "- " 前缀天然不冲突。


def load_sections(aid_dir: Path) -> List[Dict[str, str]]:
    """读分区定义：[{"name","hint"}]；文件缺失/损坏返回 []（= 自由格式）。"""
    raw = read_text(sections_path(aid_dir))
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    items = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: List[Dict[str, str]] = []
    seen: set = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name or name in seen:
            continue
        # 分区名会被拼进 "## " 标题，含换行/井号会破坏解析结构
        if "\n" in name or "\r" in name or name.startswith("#"):
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "hint": str(it.get("hint") or "").strip(),
                "detail": str(it.get("detail") or "").strip(),
            }
        )
    return out


def save_sections(aid_dir: Path, sections: List[Dict[str, str]]) -> Dict:
    """写分区定义（原子替换）。返回 {ok} 或 {ok: False, error}。"""
    clean: List[Dict[str, str]] = []
    seen: set = set()
    for it in sections or []:
        name = str((it or {}).get("name") or "").strip()
        if not name:
            continue
        if "\n" in name or "\r" in name or name.startswith("#"):
            return {"ok": False, "error": f"非法分区名: {name!r}"}
        if name in seen:
            return {"ok": False, "error": f"分区名重复: {name}"}
        seen.add(name)
        clean.append(
            {
                "name": name,
                "hint": str((it or {}).get("hint") or "").strip(),
                "detail": str((it or {}).get("detail") or "").strip(),
            }
        )
    p = sections_path(aid_dir)
    with _lock_for(p):
        _atomic_write(p, json.dumps({"version": 1, "sections": clean}, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "count": len(clean)}


def section_names(sections: List[Dict[str, str]]) -> List[str]:
    return [str(s.get("name") or "") for s in (sections or []) if (s.get("name") or "").strip()]


def default_sections() -> List[Dict[str, str]]:
    """默认分区副本（深拷贝，防调用方改到常量）。"""
    return [dict(s) for s in DEFAULT_SECTIONS]


def ensure_default_sections(aid_dir: Path) -> List[Dict[str, str]]:
    """读分区定义；文件**不存在**时落盘默认分区并返回它。

    与 load_sections 的区别：load 的 [] 同时表示「文件缺失」和「用户清空了所有分区」。
    本函数只在文件缺失时初始化 —— 用户手动删光所有分区（文件仍在、sections 为空数组）
    是要自由格式，不能被重新塞回默认值（否则删不掉）。
    """
    p = sections_path(aid_dir)
    if p.exists():
        return load_sections(aid_dir)
    sections = default_sections()
    save_sections(aid_dir, sections)
    return sections


def sections_prompt_text(sections: List[Dict[str, str]]) -> str:
    """分区清单的 prompt 文本（注入说明书/蒸馏指令共用）；空 schema 返回空串。

    优先用 detail（完整归类规则），无则回落 hint：模型需能判边界，不能只有短标签。
    """
    lines = []
    for s in sections or []:
        name = s.get("name")
        text = str(s.get("detail") or s.get("hint") or "").strip()
        lines.append(f"- {name}：{text}" if text else f"- {name}")
    return "\n".join(lines)


def section_skeleton(sections: List[Dict[str, str]]) -> str:
    """分区骨架（校验失败时回传，模型照填也能通过）。"""
    return "\n\n".join(f"## {s.get('name')}\n- " for s in (sections or []) if (s.get("name") or "").strip())


def parse_section_blocks(text: str) -> Dict:
    """把 MEMORY.md 原文按 "## " 切块。

    Returns:
        {"preamble": str, "blocks": [{"name","lines"}], "block_headers": [{"section","line"}]}
        blocks[].lines 只含非空行（分区内是条目流，空行不承载信息）。
        block_headers 记录块内出现的 "#" 开头行（小节标题，`##`/`###`/`#` 都算违规）。
    """
    preamble_lines: List[str] = []
    blocks: List[Dict] = []
    block_headers: List[Dict] = []
    cur: Optional[Dict] = None
    for idx, line in enumerate((text or "").splitlines(), start=1):
        stripped = line.rstrip()
        if stripped.startswith("## "):
            cur = {"name": stripped[3:].strip(), "lines": [], "start": idx}
            blocks.append(cur)
            continue
        if cur is None:
            preamble_lines.append(stripped)
            continue
        if stripped.startswith("#"):
            block_headers.append({"section": cur["name"], "line": stripped.strip(), "line_no": idx})
            continue
        if stripped.strip():
            cur["lines"].append(stripped)
    return {"preamble": "\n".join(preamble_lines).strip(), "blocks": blocks, "block_headers": block_headers}


def validate_global_sections(text: str, sections: List[Dict[str, str]]) -> Dict:
    """校验全局记忆是否符合分区 schema。

    空 schema 直接通过（自由格式）。校验通过返回 {"ok": True, "parsed": ...}；
    失败返回 {"ok": False, "error": "section_invalid", 诊断字段...}，诊断里带
    skeleton 与逐条 preview，模型无需回读全文即可修。
    """
    parsed = parse_section_blocks(text)
    if not sections:
        return {"ok": True, "parsed": parsed}
    names = section_names(sections)
    valid = set(names)
    diag: Dict = {"ok": False, "error": "section_invalid", "sections": names}

    unknown: List[Dict] = []
    for blk in parsed["blocks"]:
        if blk["name"] in valid:
            continue
        bullets = [ln.strip() for ln in blk["lines"] if ln.strip().startswith("- ")]
        unknown.append(
            {
                "title": blk["name"],
                "count": len(bullets),
                "preview": [b[:80] for b in bullets[:5]],
            }
        )

    preamble_bullets = [ln.strip() for ln in parsed["preamble"].splitlines() if ln.strip().startswith("- ")]

    counts = {n: 0 for n in names}
    for blk in parsed["blocks"]:
        if blk["name"] in valid:
            counts[blk["name"]] += sum(1 for ln in blk["lines"] if ln.strip().startswith("- "))

    total_bullets = sum(counts.values()) + sum(int(u["count"]) for u in unknown)

    if unknown:
        diag["reason"] = "存在清单外的 ## 标题（孤儿分区）"
        diag["unknown_titles"] = unknown
    if parsed["block_headers"]:
        diag.setdefault("reason", "分区块内出现 # 开头行")
        diag["header_in_block"] = parsed["block_headers"]
    if preamble_bullets:
        diag.setdefault("reason", "总述区出现 - 条目行")
        diag["preamble_bullets"] = len(preamble_bullets)
    raw = (text or "").strip()
    has_block = bool(parsed["blocks"])
    if raw and total_bullets == 0 and not has_block:
        # 纯散文团：既无分区标题也无条目，结构无法校验（全删条目但保留 ## 头是合法兜底出口）
        diag.setdefault("reason", "全文没有任何 - 条目")
        diag["empty_entries"] = True

    if diag.get("reason"):
        diag["counts"] = counts
        diag["skeleton"] = section_skeleton(sections)
        diag["hint"] = (
            "按当前分区清单重排：每条内容必须落在某个 ## 分区下并以 - 开头；"
            "孤儿标题下的条目重新分配到最匹配的分区，归属不了的直接删除；"
            "块内 # 小节改为 - 条目或并入上级；总述区不放 - 条目。"
        )
        return diag
    return {"ok": True, "parsed": parsed}


def insert_into_section(text: str, section: str, body: str) -> str:
    """把一条内容插入指定分区块末尾；块不存在则按 schema 外顺序追加到文件末尾。

    只保证落进正确分区，不重排既有内容（最小改动，保持人在看文件时的熟悉顺序）。
    未给 section 或 schema 为空时退化为末尾追加。
    """
    entry = (body or "").strip()
    if not entry:
        return text or ""
    lines = (text or "").rstrip("\n").splitlines()
    if not section:
        lines += ["", entry]
        return "\n".join(lines) + "\n"
    start = None
    for i, ln in enumerate(lines):
        if ln.rstrip().startswith("## ") and ln.rstrip()[3:].strip() == section:
            start = i
            break
    if start is None:
        lines += ["", f"## {section}", entry]
        return "\n".join(lines) + "\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].rstrip().startswith("## "):
            end = j
            break
    insert_at = end
    for j in range(end - 1, start, -1):
        if lines[j].strip():
            insert_at = j + 1
            break
    else:
        insert_at = start + 1
    lines.insert(insert_at, entry)
    return "\n".join(lines) + "\n"


def rename_section_title(text: str, old: str, new: str) -> str:
    """把文件里的 "## 旧名" 改名为 "## 新名"（分区改名时同步，避免制造孤儿）。"""
    if not text or not old or not new or old == new:
        return text
    out = []
    for ln in text.splitlines():
        s = ln.rstrip()
        if s.startswith("## ") and s[3:].strip() == old:
            out.append(f"## {new}")
        else:
            out.append(ln)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def read_project_memory(workdir: str | Path, aid: str) -> str:
    if not workdir:
        return ""
    return read_text(project_memory_path(workdir, aid))


def read_daily(workdir: str | Path, aid: str, date: str = "") -> str:
    """读某天日志全文；未蒸馏版缺失时回落已蒸馏版（蒸馏后仍可查）。"""
    if not workdir:
        return ""
    p = daily_path(workdir, aid, date)
    text = read_text(p)
    if not text.strip():
        text = read_text(distilled_daily_path(p))
    return text


def read_daily_paged(workdir: str | Path, aid: str, date: str = "", page: int = 1, per_page: int = 20) -> Dict:
    """单日日志条目分页读取（防长日志整块砸上下文）。

    按空行切段（条目流天然边界），返回 {ok, content, page, pages, total, distilled}；
    无日志时 {ok: False, error: "not_found"}。
    """
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    p = daily_path(workdir, aid, date)
    raw = read_text(p)
    distilled = False
    if not raw.strip():
        dp = distilled_daily_path(p)
        raw = read_text(dp)
        distilled = dp.exists() and bool(raw.strip())
    if not raw.strip():
        return {"ok": False, "error": "not_found"}
    entries = [e.strip() for e in re.split(r"\n\s*\n", raw) if e.strip()]
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * per_page
    return {
        "ok": True,
        "content": "\n\n".join(entries[start : start + per_page]),
        "page": page,
        "pages": pages,
        "total": total,
        "distilled": distilled,
    }


_ENTRY_HEAD_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (?P<lvl>DEBUG|INFO|WARNING|ERROR) \| ")


def parse_daily_entries(raw: str) -> List[Dict]:
    """日志原文 → 条目列表，解析 loguru 风格前缀（append_daily 写入格式）。

    每条 {"ts","level","summary","body","raw"}；无程序前缀的旧条目/手工条目
    ts/level/summary 为空串，body=原文。summary 与 body 以第一个 " | " 分界，
    body 内部（含换行与 " | "）保持原样。
    """
    out: List[Dict] = []
    for e in (x.strip() for x in re.split(r"\n\s*\n", raw)):
        if not e:
            continue
        m = _ENTRY_HEAD_RE.match(e)
        if not m:
            out.append({"ts": "", "level": "", "summary": "", "body": e, "raw": e})
            continue
        rest = e[m.end() :]
        parts = rest.split(" | ", 1)
        out.append(
            {
                "ts": m.group("ts"),
                "level": m.group("lvl"),
                "summary": parts[0].strip(),
                "body": parts[1].strip() if len(parts) > 1 else "",
                "raw": e,
            }
        )
    return out


def format_daily_entry(ent: Dict, summary_only: bool = False) -> str:
    """条目 → 展示文本：仅摘要时输出「时刻 | 等级 | 摘要」，否则原样。"""
    if not summary_only:
        return str(ent.get("raw") or "")
    if ent.get("ts"):
        return f"{ent['ts']} | {ent['level']} | {ent['summary']}"
    first = str(ent.get("body") or "").splitlines()
    return first[0] if first else ""


def query_daily(
    workdir: str | Path,
    aid: str,
    from_date: str = "",
    to_date: str = "",
    levels: Optional[List[str]] = None,
    search: str = "",
    summary_only: bool = False,
    page: int = 1,
    per_page: int = 20,
) -> Dict:
    """跨天日志结构化查询（运维视图）：日期范围 + 等级 + 搜索 + 仅摘要。

    天间旧→新（时间线正序），含已蒸馏文件；等级过滤时无前缀条目不命中；
    search 先按正则（忽略大小写），编译失败回落子串（结果带 search_mode）。
    条目流统一分页。非法日期抛 ValueError。返回
    {ok, content, page, pages, total, matched, days, search_mode}。
    """
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    for v in (from_date, to_date):
        if v and not is_date_name(v):
            raise ValueError(f"非法日期: {v!r}（须为 YYYY-MM-DD）")
    d = project_dir(workdir, aid)
    if not d.exists():
        return {"ok": False, "error": "not_found"}
    pat = None
    if search:
        try:
            pat = re.compile(search, re.IGNORECASE)
        except re.error:
            pat = None
    q = search.strip().lower()
    lv = {str(x).upper() for x in (levels or [])}
    files: List[tuple] = []
    for f in d.glob("*.md"):
        parsed = parse_daily_stem(f.stem)
        if f.name == PROJECT_MEMORY_FILE or parsed is None:
            continue
        date, _distilled = parsed
        if from_date and date < from_date:
            continue
        if to_date and date > to_date:
            continue
        files.append((date, f))
    total = 0
    matched: List[str] = []
    days: set = set()
    for _date, f in sorted(files):
        raw = read_text(f)
        if not raw.strip():
            continue
        for ent in parse_daily_entries(raw):
            total += 1
            if lv and ent["level"] not in lv:
                continue
            if search:
                hay = ent["raw"]
                if not (pat.search(hay) if pat is not None else q in hay.lower()):
                    continue
            days.add(_date)
            matched.append(format_daily_entry(ent, summary_only))
    sep = "\n" if summary_only else "\n\n"
    pages = max(1, (len(matched) + per_page - 1) // per_page)
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * per_page
    return {
        "ok": True,
        "content": sep.join(matched[start : start + per_page]),
        "page": page,
        "pages": pages,
        "total": total,
        "matched": len(matched),
        "days": sorted(days),
        "search_mode": ("regex" if pat is not None else ("text" if search else "")),
    }


def list_daily(workdir: str | Path, aid: str, limit: int = DAILY_LOOKBACK) -> List[Dict]:
    """近期日志列表（新→旧）：[{"date", "chars", "distilled"}...]；已蒸馏仍列出。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    out: List[Dict] = []
    for f in sorted(d.glob("*.md"), key=lambda p: p.name, reverse=True):
        parsed = parse_daily_stem(f.stem)
        if f.name == PROJECT_MEMORY_FILE or parsed is None:
            continue  # MEMORY.md 与用户杂项不进日志索引
        date, distilled = parsed
        try:
            size = len(f.read_text(encoding="utf-8").strip())
        except Exception:
            continue
        if size <= 0:
            continue
        out.append({"date": date, "chars": size, "distilled": distilled})
        if len(out) >= max(limit, 0):
            break
    return out


def stale_daily(workdir: str | Path, aid: str, days: int = STALE_DAILY_DAYS) -> List[str]:
    """超过 days 天且**未蒸馏**的日志日期（蒸馏候选，供注入侧 ACTION 提示）。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: List[str] = []
    for f in sorted(d.glob("*.md")):
        parsed = parse_daily_stem(f.stem)
        if f.name == PROJECT_MEMORY_FILE or parsed is None:
            continue  # 只认标准日期名，其余当用户杂项跳过
        stem, distilled = parsed
        if distilled:
            continue  # 已蒸馏不再是候选
        if stem < cutoff:
            out.append(stem)
    return out


def daily_exists(workdir: str | Path, aid: str, date: str = "") -> bool:
    """某天日志是否已有内容（门禁判据：今天是否已写过）。"""
    if not workdir:
        return False
    try:
        p = daily_path(workdir, aid, date)
        return p.exists() and p.stat().st_size > 0
    except ValueError, OSError:
        return False


def undistilled_before(workdir: str | Path, aid: str, before: str = "") -> List[str]:
    """before（YYYY-MM-DD，缺省今天）之前全部未蒸馏日志日期，旧→新（门禁蒸馏清单）。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    b = before or today()
    out: List[str] = []
    for f in sorted(d.glob("*.md")):
        parsed = parse_daily_stem(f.stem)
        if f.name == PROJECT_MEMORY_FILE or parsed is None:
            continue
        stem, distilled = parsed
        if not distilled and stem < b:
            out.append(stem)
    return out


def mark_distilled(workdir: str | Path, aid: str, date: str) -> Dict:
    """把 YYYY-MM-DD.md 归档为 YYYY-MM-DD.distilled.md（保留可查，蒸馏时忽略）。

    归档档已存在但 .md 还有残留条目（蒸馏打标之后又有追加，典型是跨天会话
    的尾部条目落回了旧日期文件）时：把残留条目并入归档尾部再删 .md，幂等
    收敛。此前直接返回 already，而蒸馏门禁只认「未蒸馏文件是否存在」——
    打标说已完成、门禁说没完成，两条状态互相锁死（实测发生过）。
    Returns:
        merged: 并入的残留条目数（首次归档为 0）。
    """
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    if not is_date_name(date or ""):
        return {"ok": False, "error": "bad_date"}
    p = daily_path(workdir, aid, date)
    with _lock_for(p):
        target = distilled_daily_path(p)
        if target.exists():
            residue = read_text(p)
            if not residue.strip():
                # 空残留（如抖动重试留下的空壳）：直接删掉收敛
                try:
                    p.unlink()
                except OSError:
                    pass
                return {"ok": True, "date": date, "file": str(target), "merged": 0}
            base = read_text(target).rstrip("\n")
            merged = len(parse_daily_entries(residue))
            _atomic_write(target, base + "\n\n" + residue.strip("\n") + "\n")
            try:
                p.unlink()
            except OSError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "date": date, "file": str(target), "merged": merged}
        if not p.exists():
            return {"ok": False, "error": "not_found"}
        try:
            os.replace(p, target)
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "date": date, "file": str(target), "merged": 0}


# ── 写 ───────────────────────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_global_memory(aid_dir: Path, content: str) -> bool:
    """整体替换全局记忆 MEMORY.md。"""
    path = global_memory_path(aid_dir)
    with _lock_for(path):
        _atomic_write(path, (content or "").rstrip() + "\n")
    return True


def write_project_memory(workdir: str | Path, aid: str, content: str) -> bool:
    """整体替换项目 MEMORY.md。"""
    if not workdir:
        return False
    path = project_memory_path(workdir, aid)
    with _lock_for(path):
        _atomic_write(path, (content or "").rstrip() + "\n")
    return True


def memory_fingerprint(path: Path) -> str:
    """记忆文件版本指纹（乐观锁 base）：mtime_ns + 字符数；文件缺失返回 empty。

    memory_read 返回给模型，整体替换时带回校验：不一致 = 磁盘已被别处改过。
    """
    try:
        st = Path(path).stat()
        return f"m{st.st_mtime_ns}c{st.st_size}"
    except OSError:
        return "empty"


def append_memory_file(path: Path, text: str, section: str = "") -> Dict:
    """向 MEMORY.md 追加一块内容（不碰已有行；高频「记一条」的低风险通道）。

    section 非空时插入到对应 "## 分区" 块末尾（块不存在则在文件末尾新建该块），
    空则退化为文件末尾追加。已有内容原样保留。
    组装新增换行一律用 \n，交由 _atomic_write 的 write_text 按 os.linesep
    翻译，与既有文件的 CRLF 现状保持一致。
    返回 {ok, chars}（写后总字符）或 {ok: False, error}。
    """
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "empty"}
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        old = read_text(path)
        if section:
            new = insert_into_section(old, section, body)
        else:
            new = (old.rstrip("\r\n") + "\n" + body + "\n") if old.strip() else (body + "\n")
        _atomic_write(path, new)
        try:
            chars = len(path.read_text(encoding="utf-8").strip())
        except Exception:
            chars = 0
    return {"ok": True, "chars": chars}


def write_memory_guarded(
    path: Path,
    content: str,
    base: str = "",
    consolidate: bool = False,
    snapshot=None,
) -> Dict:
    """带护栏的整体替换：乐观锁 + 收缩确认 + 快照，全部在锁内完成。

    base 非空且与磁盘指纹不一致 → conflict（模型基于陈旧上下文，禁止覆盖）。
    现有内容达 SHRINK_CHECK_MIN_CHARS 且新内容 < 现有*SHRINK_RATIO 且未显式
    consolidate → shrink（防拼接不全的「记一条覆盖全部」）。
    校验全过才调 snapshot 回调（异常吞掉，不阻断写入）再原子落盘。
    返回 {ok: True, chars} 或 {ok: False, error, ...细节}。
    """
    body = (content or "").rstrip()
    with _lock_for(path):
        # 读不可信防护：文件存在但连 stat 都失败（网盘同步/杀毒占用/权限抖动）时
        # fingerprint 会回落 "empty" 哨兵——若放行，base="empty" 的「首写」语义
        # 会直接覆盖一个临时读不到的真实文件。fail-closed：拒写。
        exists = path.exists()
        cur_fp = memory_fingerprint(path)
        if exists and cur_fp == "empty":
            return {"ok": False, "error": "unreadable"}
        if base and base != cur_fp:
            return {"ok": False, "error": "conflict", "cur_base": cur_fp}
        if exists:
            try:
                old_text = path.read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                # 内容读不到：shrink 检查会拿空串失效，同样拒写
                return {"ok": False, "error": "unreadable"}
        else:
            old_text = ""
        new_chars = len(body.strip())
        if (
            not consolidate
            and len(old_text) >= SHRINK_CHECK_MIN_CHARS
            and new_chars < len(old_text) * SHRINK_RATIO
        ):
            return {
                "ok": False,
                "error": "shrink",
                "old_chars": len(old_text),
                "new_chars": new_chars,
            }
        if snapshot is not None:
            try:
                snapshot()
            except Exception:
                pass  # 快照失败不阻断写入（与既有语义一致）
        _atomic_write(path, body + "\n")
    return {"ok": True, "chars": new_chars}


def append_daily(
    workdir: str | Path, aid: str, text: str, date: str = "", level: str = "INFO", summary: str = ""
) -> Dict:
    """追加一条到当日日志（append-only；条目间空行分隔）。

    程序给每条记录打 loguru 风格前缀：`YYYY-MM-DD HH:MM:SS | LEVEL | 摘要 | 细节`。
    时刻与等级是精确字段，由程序生成；模型提供 summary（一句话，便于浏览）与
    text（细节）；level 非法回落 INFO，summary 缺省时退化为三段式。
    正文超 DAILY_ENTRY_MAX_CHARS 直接拒写（日志是索引不是报告）。
    date 参数留给库调用方（工具层对模型侧恒传空 = 系统当日）：日志归属日期
    必须由时钟决定，不允许写入方指定历史日期，否则条目时间戳与文件归属错位。
    """
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "empty"}
    if len(body) > DAILY_ENTRY_MAX_CHARS:
        return {
            "ok": False,
            "error": "too_long",
            "chars": len(body),
            "limit": DAILY_ENTRY_MAX_CHARS,
        }
    lvl = str(level or "INFO").strip().upper()
    if lvl not in _LOG_LEVELS:
        lvl = "INFO"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = f"{stamp} | {lvl} | "
    s = (summary or "").strip()
    if s:
        head += f"{s.splitlines()[0].strip()} | "
    path = daily_path(workdir, aid, date)
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8") as f:
            if not is_new:
                f.write("\n")  # 条目间空行分隔（Markdown 段落边界）
            f.write(head + body + "\n")
    try:
        chars = len(path.read_text(encoding="utf-8"))
    except Exception:
        chars = 0
    return {"ok": True, "file": str(path), "date": path.stem, "chars": chars}


def clear_memory(aid_dir: Path, workdir: str | Path = "") -> None:
    """清除该助手的全部记忆：全局 MEMORY.md + 项目记忆目录（含 MEMORY.md 与日志）。"""
    g = global_memory_path(aid_dir)
    with _lock_for(g):
        try:
            if g.exists():
                g.unlink()
        except OSError:
            pass
    if not workdir:
        return
    d = project_dir(workdir, Path(aid_dir).name)
    if not d.exists():
        return
    for f in d.glob("*.md"):
        lock = _lock_for(f)
        with lock:
            try:
                f.unlink()
            except OSError:
                pass


# ── 注入视图 ─────────────────────────────────────────────


def daily_index_text(workdir: str | Path, aid: str, limit: int = DAILY_LOOKBACK) -> str:
    """近期日志索引文本（注入用）；无日志返回空串。"""
    items = list_daily(workdir, aid, limit)
    if not items:
        return ""
    lines = ["近期工作日志（读某天全文加 date=YYYY-MM-DD，长日志自动分页、翻页加 page=N）："]
    for it in items:
        tag = " · 已蒸馏" if it.get("distilled") else ""
        lines.append(f"- {it['date']}（{it['chars']} 字{tag}）")
    return "\n".join(lines)


# ── 快照 ─────────────────────────────────────────────────


def _workdir_key(workdir: str) -> str:
    """workdir → 稳定短哈希（项目快照组目录名，Windows 大小写不敏感）。"""
    norm = os.path.normcase(os.path.normpath(str(workdir)))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def snapshot_dir(aid_dir: Path, kind: str, workdir: str = "") -> Path:
    """快照组目录：global 固定组；project 按工作目录哈希分组。非法参数抛 ValueError。"""
    if kind == "global":
        return Path(aid_dir) / SNAPSHOT_DIR_NAME / "global"
    if kind == "project" and workdir:
        return Path(aid_dir) / SNAPSHOT_DIR_NAME / f"project_{_workdir_key(workdir)}"
    raise ValueError(f"非法快照组: kind={kind!r} workdir={workdir!r}")


def _prune_snapshots(sdir: Path) -> None:
    """按文件名（即时间）排序裁剪，只删合法快照名，origin.json 等不动。"""
    snaps = sorted(p for p in sdir.glob("*.md") if _SNAP_NAME_RE.match(p.name))
    for p in snaps[: max(len(snaps) - SNAPSHOT_KEEP, 0)]:
        try:
            p.unlink()
        except OSError:
            pass


def snapshot_memory(aid_dir: Path, kind: str, source: Path, workdir: str = "") -> Dict:
    """把 source 文件快照入组；源缺失/空白不快照；异常吞掉返回 ok=False（不得阻断主写）。"""
    try:
        src = Path(source)
        if not src.exists() or src.stat().st_size <= 0:
            return {"ok": False, "error": "no_source"}
        text = src.read_text(encoding="utf-8")
        if not text.strip():
            return {"ok": False, "error": "empty_source"}
        sdir = snapshot_dir(aid_dir, kind, workdir)
        sdir.mkdir(parents=True, exist_ok=True)
        if kind == "project":
            origin = sdir / "origin.json"
            if not origin.exists():
                origin.write_text(json.dumps({"workdir": str(workdir)}, ensure_ascii=False), encoding="utf-8")
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".md"
        while (sdir / name).exists():  # 同秒冲突 +1s，保持名格式即时间序
            dt = datetime.strptime(name[:15], "%Y%m%d-%H%M%S") + timedelta(seconds=1)
            name = dt.strftime("%Y%m%d-%H%M%S") + ".md"
        _atomic_write(sdir / name, text)
        _prune_snapshots(sdir)
        return {"ok": True, "file": name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_snapshots(aid_dir: Path, kind: str, workdir: str = "") -> List[Dict]:
    """快照列表（新→旧）：[{"file", "ts", "chars"}]；非法组或无目录返回 []。"""
    try:
        sdir = snapshot_dir(aid_dir, kind, workdir)
    except ValueError:
        return []
    if not sdir.exists():
        return []
    out: List[Dict] = []
    for p in sorted((q for q in sdir.glob("*.md") if _SNAP_NAME_RE.match(q.name)), reverse=True):
        ts = f"{p.name[0:4]}-{p.name[4:6]}-{p.name[6:8]} {p.name[9:11]}:{p.name[11:13]}:{p.name[13:15]}"
        out.append({"file": p.name, "ts": ts, "chars": len(read_text(p).strip())})
    return out


def read_snapshot(aid_dir: Path, kind: str, name: str, workdir: str = "") -> str:
    """读快照全文；name 仅接受 YYYYMMDD-HHMMSS.md（防穿越），非法抛 ValueError。"""
    if not _SNAP_NAME_RE.match(name or ""):
        raise ValueError(f"非法快照名: {name!r}")
    return read_text(snapshot_dir(aid_dir, kind, workdir) / name)


def restore_snapshot(aid_dir: Path, kind: str, name: str, target: Path, workdir: str = "") -> Dict:
    """恢复快照到 target；恢复前 target 现有内容先入快照（可反悔），原子写回。"""
    content = read_snapshot(aid_dir, kind, name, workdir)
    tgt = Path(target)
    backup = ""
    if tgt.exists() and tgt.stat().st_size > 0:
        r = snapshot_memory(aid_dir, kind, tgt, workdir)
        backup = str(r.get("file") or "")
    _atomic_write(tgt, content)
    return {"ok": True, "restored_from": name, "backup": backup}


# ── 工作目录状态与漂移 ───────────────────────────────────


def _workdir_state_path(aid_dir: Path) -> Path:
    return Path(aid_dir) / WORKDIR_STATE_FILE


def _read_workdir_state(aid_dir: Path) -> Dict:
    try:
        data = json.loads(_workdir_state_path(aid_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_workdir_state(aid_dir: Path, data: Dict) -> None:
    _atomic_write(_workdir_state_path(aid_dir), json.dumps(data, ensure_ascii=False))


def touch_workdir(aid_dir: Path, workdir: str) -> None:
    """记录最近使用的工作目录（仅变化时落盘；memory 工具每次带 workdir 调用）。"""
    if not workdir:
        return
    data = _read_workdir_state(aid_dir)
    if data.get("last_workdir") != str(workdir):
        data["last_workdir"] = str(workdir)
        _write_workdir_state(aid_dir, data)


def detect_workdir_drift(aid_dir: Path, workdir: str) -> Dict:
    """漂移检测：当前项目记忆为空而上次工作目录有记忆 → 可能换了目录（提示，不自动动）。

    当前记忆非空视为正常（顺带 touch）；无记录/同目录/旧记忆为空/已忽略 → 不报。
    """
    if not workdir:
        return {"drifted": False}
    if read_project_memory(workdir, Path(aid_dir).name).strip():
        touch_workdir(aid_dir, workdir)
        return {"drifted": False}
    data = _read_workdir_state(aid_dir)
    old = str(data.get("last_workdir") or "")
    if not old or os.path.normcase(old) == os.path.normcase(str(workdir)):
        return {"drifted": False}
    if _workdir_key(old) in (data.get("dismissed") or []):
        return {"drifted": False}
    old_mem = read_project_memory(old, Path(aid_dir).name).strip()
    if not old_mem:
        return {"drifted": False}
    return {"drifted": True, "old_workdir": old, "old_chars": len(old_mem)}


def dismiss_drift(aid_dir: Path, workdir: str) -> None:
    """把某工作目录加入忽略名单（用户明确说不是漂移）。"""
    if not workdir:
        return
    data = _read_workdir_state(aid_dir)
    key = _workdir_key(workdir)
    dismissed = list(data.get("dismissed") or [])
    if key not in dismissed:
        dismissed.append(key)
        data["dismissed"] = dismissed
        _write_workdir_state(aid_dir, data)


def migrate_project_memory(aid_dir: Path, old_workdir: str, new_workdir: str) -> Dict:
    """把旧工作目录的项目记忆整体迁到新目录（MEMORY.md + 全部日志）；仅新位置记忆为空时允许。

    旧目录文件保留不删（安全）；迁移后 touch 新目录，漂移自然消失。
    """
    if not old_workdir or not new_workdir:
        return {"ok": False, "error": "bad_args"}
    if os.path.normcase(old_workdir) == os.path.normcase(new_workdir):
        return {"ok": False, "error": "same_dir"}
    aid = Path(aid_dir).name
    if read_project_memory(new_workdir, aid).strip():
        return {"ok": False, "error": "target_not_empty"}
    src_dir = project_dir(old_workdir, aid)
    if not src_dir.exists():
        return {"ok": False, "error": "no_source"}
    dst_dir = project_dir(new_workdir, aid)
    dst_dir.mkdir(parents=True, exist_ok=True)
    moved: List[str] = []
    for f in sorted(src_dir.glob("*.md")):
        parsed = parse_daily_stem(f.stem)
        if f.name != PROJECT_MEMORY_FILE and parsed is None:
            continue  # 用户杂项文件不迁
        _atomic_write(dst_dir / f.name, read_text(f))
        moved.append(f.name)
    if not moved:
        return {"ok": False, "error": "no_source"}
    touch_workdir(aid_dir, new_workdir)
    return {"ok": True, "moved": moved}
