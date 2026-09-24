# -*- coding: utf-8 -*-
"""compile.py — 记忆四段传送带编译器（对齐 openhanako lib/memory/compile.ts v4 语义）。

传送带：session 对话 → today.md（当日水位线增量）→ daily/{date}.md（日切蒸馏）
        → week 装配（近 6 日纯文件）→ roll_daily_window（滚出窗口 fold 进 longterm）
        → facts.md（重要事实增量）→ assemble（四段拼 memory.md，注入用）。

目录布局（每助手 memory/ 下）：
  memory.md / facts.md / today.md / today-state.json / daily/*.md / longterm.md

LLM 失败策略：单步失败记日志、返回 {"ok": False, "error": ...}，不中断日批后续步骤。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 模块路径（hook 独立加载场景经 load_core_module 加载）
_THIS = Path(__file__).resolve()

# week 段展示今天之前的 6 个已结束逻辑日
DAILY_WINDOW_RETENTION_DAYS = 6
# assemble 总量硬上限（字符）
ASSEMBLE_MAX_CHARS = 12000
# 段级字符预算：assemble 按段截断保头部，保证四段都在、总量不破总闸
SECTION_BUDGETS = {"facts": 2200, "recent": 1200, "today": 2800, "longterm": 800}
# compile_today / compile_daily 输出源头硬预算：超了先重试压缩，仍超则按行截断
TODAY_MAX_CHARS = 2800
DAILY_MAX_CHARS = 800
# recent 段索引尾注：daily 全文不入注入，LLM 需要时用读文件工具按此路径自查
RECENT_INDEX_HINT = "（以上仅索引；需要详情用读文件工具按路径查阅：{dir}）"
# 注入预算提示（memory.md 目标 ≤2000 token ≈ 4000 中文字符，超出靠 ASSEMBLE_MAX_CHARS 硬截）
MEMORY_TARGET_CHARS = 4000


def _core(name: str):
    """加载同包模块（session_store / prompts），兼容 importlib 独立加载。

    mtime 自检：插件热重载不清理 assistant_hub_core.* 的 sys.modules 缓存，
    命中即返回会卡旧代码 → 比对源文件 mtime，更新则重新加载替换。
    """
    key = f"assistant_hub_core.{name}"
    path = _THIS.parent.parent / f"{name}.py"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    mod = sys.modules.get(key)
    if mod is not None and getattr(mod, "_source_mtime", -1.0) >= mtime:
        return mod
    spec = importlib.util.spec_from_file_location(key, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    # 直接写 sys.modules：assistant_hub_core. 前缀已在 plugin.json module_prefixes 声明
    # （早先用 sys_modules() 包一层绕开静态扫描，属规避手法，已纠正）。
    sys.modules[key] = module
    spec.loader.exec_module(module)
    module._source_mtime = mtime
    return module


# ── 路径辅助 ────────────────────────────────────────────


def memory_dir(aid_dir: Path) -> Path:
    return Path(aid_dir) / "memory"


def daily_dir(aid_dir: Path) -> Path:
    return memory_dir(aid_dir) / "daily"


def _ensure(aid_dir: Path) -> None:
    daily_dir(aid_dir).mkdir(parents=True, exist_ok=True)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _clip_lines(body: str, budget: int) -> str:
    """段内按行截断保头部（预算内原样返回）；行级累积超预算即停，截尾标注。"""
    if len(body) <= budget:
        return body
    out: List[str] = []
    used = 0
    for ln in body.splitlines():
        if used + len(ln) + 1 > budget:
            break
        out.append(ln)
        used += len(ln) + 1
    if not out:
        out = [body[:budget].rstrip()]
    return "\n".join(out).rstrip() + "\n…（超预算已截）"


def _split_topic(text: str) -> Tuple[str, str]:
    """提取蒸馏输出首行 ``TOPIC: 主题``；无则回退空主题。返回 (topic, body)。"""
    lines = text.splitlines()
    if lines and lines[0].strip().lower().startswith("topic:"):
        topic = lines[0].split(":", 1)[1].strip()
        return topic, "\n".join(lines[1:]).strip()
    return "", text.strip()


def _topic_slug(topic: str) -> str:
    """主题转文件名安全片段：替换路径非法字符与空白，限长 24。"""
    bad = '\\/:*?"<>|\r\n\t '
    slug = "".join("-" if ch in bad else ch for ch in topic).strip("-")
    return slug[:24]


def _shrink_to_budget(llm: Callable, text: str, budget: int) -> str:
    """编译输出超预算时重试压缩一次，仍超则按行截断（LLM 无视行数约束的硬兜底）。"""
    if len(text) <= budget:
        return text
    try:
        prompts = _load_prompts()
        shrunk = (llm(prompts.build_shrink_overrun(text, budget)) or "").strip()
    except Exception as e:
        logger.warning(f"[assistant_hub.compile] 超预算压缩失败: {e}")
        shrunk = ""
    return _clip_lines(shrunk or text, budget)


def logical_today() -> str:
    return _core("session_store").logical_day()


def logical_yesterday() -> str:
    """上一逻辑日（对 04:00 边界安全，供日批蒸馏定日期）。

    例：09-06 凌晨 02:00（逻辑日 09-05）→ now-1day=09-05 02:00 → hour<4 → 09-04。
    直接用 now-1day 的日期会在深夜触发日批时错把当天草稿提前蒸馏。
    """
    return _core("session_store").logical_day(datetime.now() - timedelta(days=1))


def _day_start(logical_date: str) -> str:
    """逻辑日 04:00 起点的时间戳字符串（sessions.updated_at 比较）。"""
    d = datetime.strptime(logical_date, "%Y-%m-%d") + timedelta(hours=4)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _day_end(logical_date: str) -> str:
    d = datetime.strptime(logical_date, "%Y-%m-%d") + timedelta(days=1, hours=4)
    return d.strftime("%Y-%m-%d %H:%M:%S")


# ── today.md 编译（水位线增量）─────────────────────────


def _read_today_state(aid_dir: Path) -> Dict:
    raw = _read(memory_dir(aid_dir) / "today-state.json")
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("logical_date", "")
            data.setdefault("last_msg_cursor", {})
            return data
    except Exception:
        pass
    return {"logical_date": "", "last_msg_cursor": {}}


def _write_today_state(aid_dir: Path, state: Dict) -> None:
    state["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    _write(memory_dir(aid_dir) / "today-state.json", json.dumps(state, ensure_ascii=False, indent=2))


def compile_today(
    aid_dir: Path,
    *,
    llm: Callable,
    now: Optional[datetime] = None,
    _session_filter: Optional[Callable] = None,
) -> Dict:
    """当日增量编译：水位线以来新增轮次 → LLM 合并进 today.md。

    now/_session_filter 仅测试用（固定时钟 / 收窄参与编译的会话）。
    """
    today = _core("session_store").logical_day(now)
    state = _read_today_state(aid_dir)
    # 日期切换后重置草稿（草稿应由日批先蒸馏进 daily；此处兜底）
    if state.get("logical_date") != today:
        state = {"logical_date": today, "last_msg_cursor": {}}
        _write_today_state(aid_dir, state)

    conn = _core("session_store").connect_ro()
    if conn is None:
        return {"ok": False, "changed": False, "error": "sessions.db 不可用"}
    try:
        store = _core("session_store")
        sessions = store.sessions_between(conn, _day_start(today), _day_end(today))
    finally:
        conn.close()
    if _session_filter is not None:
        sessions = [s for s in sessions if _session_filter(s)]

    cursor: Dict[str, int] = dict(state["last_msg_cursor"])
    new_msgs: List[dict] = []
    for s in sessions:
        seen = int(cursor.get(s["session_id"], 0))
        msgs = s.get("msgs") or []
        if len(msgs) > seen:
            new_msgs.extend(msgs[seen:])
            cursor[s["session_id"]] = len(msgs)

    if not new_msgs:
        return {"ok": True, "changed": False, "reason": "无新增轮次"}

    turns = _core("session_store").turns_text([{"msgs": new_msgs}], max_chars=16000)
    prev_today = _read(memory_dir(aid_dir) / "today.md")
    try:
        prompts = _load_prompts()
        compiled = (llm(prompts.build_compile_today(turns, prev_today)) or "").strip()
    except Exception as e:
        logger.warning(f"[assistant_hub.compile] today 编译失败: {e}")
        return {"ok": False, "changed": False, "error": str(e)}
    if not compiled:
        return {"ok": False, "changed": False, "error": "LLM 返回空"}
    compiled = _shrink_to_budget(llm, compiled, TODAY_MAX_CHARS)

    _write(memory_dir(aid_dir) / "today.md", compiled)
    state["last_msg_cursor"] = cursor
    _write_today_state(aid_dir, state)
    return {"ok": True, "changed": True, "chars": len(compiled)}


def _load_prompts():
    key = "assistant_hub_core.memory.prompts"
    mod = sys.modules.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(key, str(_THIS.parent / "prompts.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


# ── daily 蒸馏 / roll / facts ───────────────────────────


def compile_daily(aid_dir: Path, prev_today_text: str, day: str, *, llm: Callable) -> Dict:
    """把昨日 today 草稿蒸馏成日记，写 daily/<day>-<主题>.md（主题取 TOPIC 行）。

    文件名带主题供 memory.md 近期段索引化（仅列文件名，LLM 按需读文件）；
    同日判重按日期前缀 glob（兼容旧无主题命名）。
    """
    if list(daily_dir(aid_dir).glob(f"{day}*.md")) or not prev_today_text.strip():
        return {"ok": True, "changed": False, "reason": "已存在或空"}
    try:
        prompts = _load_prompts()
        text = (llm(prompts.build_compile_daily(prev_today_text)) or "").strip()
    except Exception as e:
        logger.warning(f"[assistant_hub.compile] daily 蒸馏失败: {e}")
        return {"ok": False, "error": str(e)}
    if not text:
        return {"ok": False, "error": "LLM 返回空"}
    topic, body = _split_topic(text)
    body = _shrink_to_budget(llm, body, DAILY_MAX_CHARS)
    slug = _topic_slug(topic)
    stem = f"{day}-{slug}" if slug else day
    out_path = daily_dir(aid_dir) / f"{stem}.md"
    _write(out_path, f"# {day}\n\n{body}\n")
    return {"ok": True, "changed": True, "file": str(out_path)}


def roll_daily_window(aid_dir: Path, keep: int = DAILY_WINDOW_RETENTION_DAYS) -> List[str]:
    """窗口外的 daily fold 进 longterm.md 后删除源文件；返回被 fold 的日期。"""
    ddir = daily_dir(aid_dir)
    if not ddir.exists():
        return []
    files = sorted(ddir.glob("*.md"))
    to_fold = files[:-keep] if len(files) > keep else []
    if not to_fold:
        return []
    lt_path = memory_dir(aid_dir) / "longterm.md"
    existing = _read(lt_path)
    parts: List[str] = []
    for f in to_fold:
        body = _read(f).strip()
        if body:
            parts.append(body)
        try:
            f.unlink()
        except Exception:
            pass
    if parts:
        merged = (existing.rstrip() + "\n\n" + "\n\n".join(parts)).strip() + "\n"
        _write(lt_path, merged)
    return [f.stem for f in to_fold]


def compile_facts(aid_dir: Path, *, llm: Callable) -> Dict:
    """重要事实增量编译（输入：今日+近 3 日 daily 的对话尾部样本）。"""
    facts_path = memory_dir(aid_dir) / "facts.md"
    existing = _read(facts_path)
    today = logical_today()
    conn = _core("session_store").connect_ro()
    if conn is None:
        return {"ok": False, "error": "sessions.db 不可用"}
    try:
        store = _core("session_store")
        start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d 04:00:00")
        sessions = store.sessions_between(conn, start, _day_end(today))
    finally:
        conn.close()
    turns = _core("session_store").turns_text(sessions, max_chars=12000)
    if not turns.strip():
        return {"ok": True, "changed": False, "reason": "无对话"}
    try:
        prompts = _load_prompts()
        text = (llm(prompts.build_compile_facts(existing, turns)) or "").strip()
    except Exception as e:
        logger.warning(f"[assistant_hub.compile] facts 编译失败: {e}")
        return {"ok": False, "error": str(e)}
    if not text:
        return {"ok": False, "error": "LLM 返回空"}
    _write(facts_path, text + "\n")
    return {"ok": True, "changed": True}


# ── assemble：四段拼 memory.md ──────────────────────────


def build_compiled_markdown(sections: Dict[str, str]) -> str:
    """sections: {"facts","recent","today","longterm"} → markdown 文本（空段跳过）。"""
    titles = [
        ("facts", "## 重要事实"),
        ("recent", "## 近期"),
        ("today", "## 今日"),
        ("longterm", "## 长期记忆"),
    ]
    parts: List[str] = []
    for key, title in titles:
        body = (sections.get(key) or "").strip()
        if body:
            parts.append(f"{title}\n\n{body}")
    return "\n\n".join(parts)


def assemble(aid_dir: Path) -> str:
    """同步拼四段 → 写 memory.md 并返回；全空返回 ""（不写盘）。

    段级预算（SECTION_BUDGETS）按段截断保头部，总闸 ASSEMBLE_MAX_CHARS 兜底；
    recent 段索引化：仅列 daily 文件名清单（日期+主题+字符数），
    全文不入注入，LLM 需要时用读文件工具按 RECENT_INDEX_HINT 路径自查。
    """
    mem = memory_dir(aid_dir)
    recent_lines: List[str] = []
    ddir = daily_dir(aid_dir)
    if ddir.exists():
        for f in sorted(ddir.glob("*.md"))[-DAILY_WINDOW_RETENTION_DAYS:]:
            body = _read(f).strip()
            if body:
                recent_lines.append(f"- {f.stem}（{len(body)}字）")
    if recent_lines:
        recent_lines.append(RECENT_INDEX_HINT.format(dir=str(ddir)))
    raw = {
        "facts": _clip_lines(_read(mem / "facts.md").strip(), SECTION_BUDGETS["facts"]),
        "recent": _clip_lines("\n".join(recent_lines).strip(), SECTION_BUDGETS["recent"]),
        "today": _clip_lines(_read(mem / "today.md").strip(), SECTION_BUDGETS["today"]),
        "longterm": _clip_lines(_read(mem / "longterm.md").strip(), SECTION_BUDGETS["longterm"]),
    }
    text = build_compiled_markdown(raw)
    if not text:
        return ""
    if len(text) > ASSEMBLE_MAX_CHARS:
        # 段级预算兜底后的总闸（理论到不了，防御性保留）
        text = text[:ASSEMBLE_MAX_CHARS].rstrip() + "\n…（已截断）"
    _write(mem / "memory.md", text)
    return text
