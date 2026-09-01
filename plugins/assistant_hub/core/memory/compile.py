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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 模块路径（hook 独立加载场景经 load_core_module 加载）
_THIS = Path(__file__).resolve()

# week 段展示今天之前的 6 个已结束逻辑日
DAILY_WINDOW_RETENTION_DAYS = 6
# assemble 总量硬上限（字符）
ASSEMBLE_MAX_CHARS = 12000
# 注入预算提示（memory.md 目标 ≤2000 token ≈ 4000 中文字符，超出靠 ASSEMBLE_MAX_CHARS 硬截）
MEMORY_TARGET_CHARS = 4000


def _core(name: str):
    """加载同包模块（session_store / prompts），兼容 importlib 独立加载。"""
    key = f"assistant_hub_core.{name}"
    mod = sys_modules().get(key)
    if mod is not None:
        return mod
    path = _THIS.parent.parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(key, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys_modules()[key] = module
    spec.loader.exec_module(module)
    return module


def sys_modules():
    import sys

    return sys.modules


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


def logical_today() -> str:
    return _core("session_store").logical_day()


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

    _write(memory_dir(aid_dir) / "today.md", compiled)
    state["last_msg_cursor"] = cursor
    _write_today_state(aid_dir, state)
    return {"ok": True, "changed": True, "chars": len(compiled)}


def _load_prompts():
    key = "assistant_hub_core.memory.prompts"
    mod = sys_modules().get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(key, str(_THIS.parent / "prompts.py"))
    module = importlib.util.module_from_spec(spec)
    sys_modules()[key] = module
    spec.loader.exec_module(module)
    return module


# ── daily 蒸馏 / roll / facts ───────────────────────────


def compile_daily(aid_dir: Path, prev_today_text: str, day: str, *, llm: Callable) -> Dict:
    """把昨日 today 草稿蒸馏成日记，写 daily/<day>.md。"""
    out_path = daily_dir(aid_dir) / f"{day}.md"
    if out_path.exists() or not prev_today_text.strip():
        return {"ok": True, "changed": False, "reason": "已存在或空"}
    try:
        prompts = _load_prompts()
        text = (llm(prompts.build_compile_daily(prev_today_text)) or "").strip()
    except Exception as e:
        logger.warning(f"[assistant_hub.compile] daily 蒸馏失败: {e}")
        return {"ok": False, "error": str(e)}
    if not text:
        return {"ok": False, "error": "LLM 返回空"}
    _write(out_path, f"# {day}\n\n{text}\n")
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
    today = logical_day()
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
    """同步拼四段 → 写 memory.md 并返回；全空返回 ""（不写盘）。"""
    mem = memory_dir(aid_dir)
    recent_lines: List[str] = []
    ddir = daily_dir(aid_dir)
    if ddir.exists():
        for f in sorted(ddir.glob("*.md"))[-DAILY_WINDOW_RETENTION_DAYS:]:
            body = _read(f).strip()
            if body:
                recent_lines.append(body)
    raw = {
        "facts": _read(mem / "facts.md"),
        "recent": "\n\n".join(recent_lines),
        "today": _read(mem / "today.md"),
        "longterm": _read(mem / "longterm.md"),
    }
    text = build_compiled_markdown(raw)
    if not text:
        return ""
    if len(text) > ASSEMBLE_MAX_CHARS:
        # 按段比例截断（保前部：facts > today > recent > longterm 优先级由顺序体现）
        text = text[:ASSEMBLE_MAX_CHARS].rstrip() + "\n…（已截断）"
    _write(mem / "memory.md", text)
    return text
