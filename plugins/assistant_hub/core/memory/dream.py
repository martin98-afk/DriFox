# -*- coding: utf-8 -*-
"""dream.py — 记忆 Dream 整理管线（LLM 五步 + 版本快照，对齐 openhanako dream/runner.ts）。

管线：快照 sections → sha256 → atomize → dedupe → optimize → compose → verify
      （压缩不足回炉重 compose 一次）→ 快照对比 → create_revision(before) → apply → 清 pending。

版本：memory/dream/revisions/<id>.json 存完整 before 快照（≤10 份按 mtime 修剪）；
恢复前对当前内容存 kind="pre_restore" revision。

每日自动：lastAutomaticAttemptDate / lastSuccessfulManualDate 双水位，每逻辑日各最多一次。
并发：per-aid threading.Lock，重入直接返回 running 错误（不阻塞）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_MAX_REVISIONS = 10
# ── 遗忘步名额（火灾取物 · 物竞天择）─────────────────────
FORGET_FACTS_LIMIT = 15
FORGET_DAILY_LIMIT = 3
FORGET_LONGTERM_LIMIT = 20
FORGET_BUDGET_CHARS = 3000
# 确定性硬截预算：LLM 无视名额时按行截断（输出已要求重要度降序）
FORGET_FACTS_BUDGET_CHARS = 1500
FORGET_LONGTERM_BUDGET_CHARS = 1400
_locks: Dict[str, threading.Lock] = {}


class DreamAlreadyRunningError(RuntimeError):
    pass


def _load(name: str, rel: str):
    """加载同包模块（importlib 手动注册 sys.modules）。

    mtime 自检：sys.modules 里的旧对象在插件热重载时无人清理（app 侧 purge
    只覆盖 gateway/ui 等前缀），命中即返回会卡住旧代码（如 prompts 新增函数
    不可见）→ 命中后比对源文件 mtime，更新则重新加载替换。
    """
    key = f"assistant_hub_core.{name}"
    import sys

    path = _THIS.parent.parent / rel
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    mod = sys.modules.get(key)
    if mod is not None and getattr(mod, "_source_mtime", -1.0) >= mtime:
        return mod
    spec = importlib.util.spec_from_file_location(key, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    module._source_mtime = mtime
    return module


def _prompts():
    return _load("memory.prompts", "memory/prompts.py")


def _compile_mod():
    return _load("memory.compile", "memory/compile.py")


# ── sections 快照 ───────────────────────────────────────


@dataclass
class DreamSections:
    facts: str
    today: str
    daily: List[Dict[str, str]]  # [{"date","body"}]
    longterm: str

    def to_json(self) -> dict:
        return {"facts": self.facts, "today": self.today, "daily": self.daily, "longterm": self.longterm}

    @classmethod
    def from_json(cls, d: dict) -> "DreamSections":
        return cls(
            facts=str(d.get("facts") or ""),
            today=str(d.get("today") or ""),
            daily=list(d.get("daily") or []),
            longterm=str(d.get("longterm") or ""),
        )


def snapshot_sections(aid_dir: Path) -> DreamSections:
    _compile_mod()
    mem = aid_dir / "memory"
    daily: List[Dict[str, str]] = []
    ddir = mem / "daily"
    if ddir.exists():
        for f in sorted(ddir.glob("*.md")):
            body = f.read_text(encoding="utf-8") if f.exists() else ""
            if body.strip():
                daily.append({"date": f.stem, "body": body})
    return DreamSections(
        facts=_read(mem / "facts.md"),
        today=_read(mem / "today.md"),
        daily=daily,
        longterm=_read(mem / "longterm.md"),
    )


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _trim_to_budget(text: str, limit: int) -> str:
    """按行累积截断到字符预算内（保序保头部；遗忘输出已要求重要度降序，截尾保重要）。

    首行即超限时行内硬截（预算硬保证优先于行完整性）。
    """
    if len(text) <= limit:
        return text
    out: List[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    if not out:
        return text[:limit]
    return "\n".join(out)


def _sections_text(s: DreamSections) -> str:
    parts = [s.facts, s.today]
    parts.extend(d["body"] for d in s.daily)
    # 历史污染防御：旧版 Dream 将合成结果双写进 facts/longterm，导致两段逐字相同；
    # 输入拼接时去重，避免同一内容占双倍 token、压缩后仍保留双份重复。
    lt = "" if s.longterm.strip() and s.longterm.strip() == s.facts.strip() else s.longterm
    parts.append(lt)
    return "\n\n".join(p for p in parts if p.strip())


def _has_any_memory(s: DreamSections) -> bool:
    """是否有任何可整理的记忆内容。

    ⚠️ 准入判定用「任意段落有内容」，不是「facts/longterm 有内容」：
    Dream 的实际输入是 ``_sections_text()``（facts + today + daily + longterm），
    产出写回 facts.md 与 longterm.md。若按 facts/longterm 卡准入，全新助手
    明明已有 today.md（记忆传送带跑通了）、却因 compile_facts 还没产出
    facts.md 而永远报 dream_no_memory —— 手动 Dream 直接不可用。
    """
    return bool(_sections_text(s).strip())


def _hash_sections(s: DreamSections) -> str:
    return hashlib.sha256(json.dumps(s.to_json(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def apply_sections(aid_dir: Path, s: DreamSections) -> None:
    mem = aid_dir / "memory"
    _write(mem / "facts.md", s.facts)
    _write(mem / "longterm.md", s.longterm)
    # today/daily 属"过程段"：Dream 只重写 facts/longterm 可编辑段（对齐原版 editable sections）
    # 遗忘步扩展：daily 与 s.daily 对齐（不在集合内的文件删除）；restore 传全量即全量恢复
    ddir = mem / "daily"
    if ddir.exists():
        keep = {str(d.get("date") or "") for d in s.daily}
        for f in ddir.glob("*.md"):
            if f.stem not in keep:
                try:
                    f.unlink()
                except Exception:
                    pass


# ── state（每日水位 + lastRun 报告）─────────────────────


def _state_path(aid_dir: Path) -> Path:
    return aid_dir / "memory" / "dream" / "state.json"


def _read_state(aid_dir: Path) -> Dict:
    try:
        d = json.loads(_read(_state_path(aid_dir)))
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def _write_state(aid_dir: Path, state: Dict) -> None:
    _write(_state_path(aid_dir), json.dumps(state, ensure_ascii=False, indent=2))


# ── revisions ───────────────────────────────────────────


def _rev_dir(aid_dir: Path) -> Path:
    return aid_dir / "memory" / "dream" / "revisions"


def create_revision(
    aid_dir: Path,
    *,
    run_id: str,
    trigger: str,
    before: DreamSections,
    kind: str = "dream",
    restores_revision_id: str = "",
) -> str:
    revision_id = f"{datetime.now().isoformat(timespec='seconds').replace(':', '-')}-{hashlib.sha1(run_id.encode()).hexdigest()[:6]}"
    doc = {
        "revisionId": revision_id,
        "runId": run_id,
        "trigger": trigger,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "restoresRevisionId": restores_revision_id,
        "before": before.to_json(),
    }
    _write(_rev_dir(aid_dir) / f"{revision_id}.json", json.dumps(doc, ensure_ascii=False, indent=2))
    _prune_revisions(aid_dir, keep={revision_id})
    return revision_id


def _prune_revisions(aid_dir: Path, keep: set) -> None:
    d = _rev_dir(aid_dir)
    if not d.exists():
        return
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    remaining = len(files)
    for f in reversed(files):
        if remaining <= _MAX_REVISIONS:
            break
        if f.stem in keep:
            continue
        try:
            f.unlink()
            remaining -= 1
        except Exception:
            pass


def _read_revision(aid_dir: Path, revision_id: str) -> Optional[Dict]:
    p = _rev_dir(aid_dir) / f"{revision_id}.json"
    if "/" in revision_id or ".." in revision_id or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_revisions(aid_dir: Path) -> List[Dict]:
    d = _rev_dir(aid_dir)
    if not d.exists():
        return []
    out: List[Dict] = []
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            before = doc.get("before") or {}
            out.append(
                {
                    "revisionId": doc.get("revisionId") or f.stem,
                    "trigger": doc.get("trigger") or "",
                    "kind": doc.get("kind") or "dream",
                    "createdAt": doc.get("createdAt") or "",
                    "restoresRevisionId": doc.get("restoresRevisionId") or "",
                    "factsChars": len(str(before.get("facts") or "")),
                    "longtermChars": len(str(before.get("longterm") or "")),
                    "dailyCount": len(before.get("daily") or []),
                }
            )
        except Exception:
            continue
    return out


# ── DreamRunner ─────────────────────────────────────────


class DreamRunner:
    def __init__(self, aid_dir: Path, *, llm: Callable, progress: Optional[Callable] = None):
        self._aid = Path(aid_dir)
        self._llm = llm
        self._progress = progress  # progress(step: int, total: int, name: str)

    def _report(self, step: int, total: int, name: str) -> None:
        if self._progress:
            try:
                self._progress(step, total, name)
            except Exception:
                pass

    # ── 主流程 ──
    def start(self, trigger: str, logical_date: str = "") -> Dict:
        self._logical_date = logical_date or _today_str()
        lock = _locks.setdefault(str(self._aid), threading.Lock())
        if not lock.acquire(blocking=False):
            return {"ok": False, "error": "dream_already_running"}
        try:
            return self._run(trigger)
        finally:
            lock.release()

    def _run(self, trigger: str) -> Dict:
        run_id = f"dream-{int(time.time() * 1000)}"
        state = _read_state(self._aid)

        before = snapshot_sections(self._aid)
        if not _has_any_memory(before):
            err = "dream_no_memory"
            state["lastRun"] = {"runId": run_id, "trigger": trigger, "status": "failed", "error": err, "at": _now()}
            _write_state(self._aid, state)
            return {"ok": False, "error": err}
        before_hash = _hash_sections(before)

        prompts = _prompts()
        total = 6
        try:
            llm = self._llm
            self._report(1, total, "原子化")
            units = (llm(prompts.build_dream_atomize(_sections_text(before))) or "").strip()
            self._report(2, total, "去重")
            deduped = (llm(prompts.build_dream_dedupe(units)) or "").strip()
            self._report(3, total, "优化")
            optimized = (llm(prompts.build_dream_optimize(deduped)) or "").strip()
            self._report(4, total, "合成")
            composed = (llm(prompts.build_dream_compose(optimized)) or "").strip()
            self._report(5, total, "校验")
            verdict = self._verify(prompts, before, composed)
            if not verdict.get("sufficient_compression", True):
                # 压缩不足：回炉重 compose 一次（软目标），再校验一次
                composed = (
                    llm(prompts.build_dream_compose(f"【上一稿压缩不足，请更激进地聚合：】\n{optimized}")) or ""
                ).strip()
                verdict = self._verify(prompts, before, composed)
            # verify 降级为警告：语义/溯源不通过不再阻断（LLM 质检对长条目压缩误杀率高），
            # 结果记入 lastRun.warning 留痕，整理照常落盘。
            verify_warning = ""
            if not (verdict.get("semantic_ok", True) and verdict.get("provenance_ok", True)):
                verify_warning = str(verdict.get("feedback") or "语义/溯源校验未通过（仅警告）")
        except Exception as e:
            state["lastRun"] = {"runId": run_id, "trigger": trigger, "status": "failed", "error": str(e), "at": _now()}
            _write_state(self._aid, state)
            return {"ok": False, "error": str(e)}

        # 快照对比：Dream 期间记忆被改 → 放弃
        current = snapshot_sections(self._aid)
        if _hash_sections(current) != before_hash:
            state["lastRun"] = {
                "runId": run_id,
                "trigger": trigger,
                "status": "failed",
                "error": "memory_changed",
                "at": _now(),
            }
            _write_state(self._aid, state)
            return {"ok": False, "error": "memory_changed"}

        # 单写 facts：Dream 输出只落入 facts.md；longterm 保持「过期日记沉淀」语义
        # （roll_daily_window 专属），不再被合成结果覆盖 → 消除 facts/longterm 永久双份重复。
        # 迁移：检测到 longterm 与 facts 逐字相同（旧版双写污染），清空 longterm，
        # 其内容已随 composed 落入 facts。
        longterm_after = before.longterm
        if before.facts.strip() and before.longterm.strip() == before.facts.strip():
            longterm_after = ""

        # 第 6 步 遗忘：火灾取物 · 物竞天择，名额内抢救记忆；失败降级五步结果
        forget_warning = ""
        forgotten = None
        try:
            forgotten = self._try_forget(prompts, before, composed, longterm_after)
        except Exception as e:
            forget_warning = f"遗忘步异常已跳过: {e}"
        if forgotten is None and not forget_warning:
            forget_warning = "遗忘步失败已跳过（解析/守门未过），保留五步结果"

        if forgotten is not None:
            after = DreamSections(
                facts=forgotten.facts, today=before.today, daily=forgotten.daily, longterm=forgotten.longterm
            )
        else:
            after = DreamSections(facts=composed, today=before.today, daily=before.daily, longterm=longterm_after)
        warning_text = "；".join(w for w in (verify_warning, forget_warning) if w)
        if _hash_sections(after) == before_hash:
            state.update(self._finalize(state, run_id, trigger, "succeeded", "", before_hash, before_hash))
            if warning_text:
                state["lastRun"]["warning"] = warning_text
            if trigger == "manual":
                state["lastSuccessfulManualDate"] = self._logical_date
            _write_state(self._aid, state)
            return {"ok": True, "run_id": run_id, "changed": False, "revision_id": "", "warning": warning_text}

        revision_id = create_revision(self._aid, run_id=run_id, trigger=trigger, before=before)
        # pending-apply：崩溃恢复标记
        _write(
            self._aid / "memory" / "dream" / "pending-apply.json",
            json.dumps({"revisionId": revision_id, "after": after.to_json()}, ensure_ascii=False),
        )
        apply_sections(self._aid, after)
        try:
            (self._aid / "memory" / "dream" / "pending-apply.json").unlink()
        except Exception:
            pass

        state = self._finalize(state, run_id, trigger, "succeeded", revision_id, before_hash, _hash_sections(after))
        if warning_text:
            state["lastRun"]["warning"] = warning_text
        if trigger == "manual":
            state["lastSuccessfulManualDate"] = self._logical_date
        else:
            state["lastAutomaticAttemptDate"] = self._logical_date
        _write_state(self._aid, state)
        return {"ok": True, "run_id": run_id, "changed": True, "revision_id": revision_id, "warning": warning_text}

    def _verify(self, prompts, before: DreamSections, composed: str) -> Dict:
        raw = (self._llm(prompts.build_dream_verify(_sections_text(before), composed)) or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _try_forget(self, prompts, before: DreamSections, composed: str, longterm: str) -> Optional[DreamSections]:
        """第 6 步遗忘：LLM 按火灾/物竞天择在名额内抢救记忆。

        返回 None 表示遗忘失败（调用方降级五步结果）。守门：
        - JSON 解析失败 / facts 为空 / keep_daily 非列表 → None
        - 幻觉守门：输出长度超过对应输入（只删不增被违反）→ None
        - daily 超名额确定性删最旧；总量超预算继续删最旧 daily
        """
        raw = (self._llm(prompts.build_dream_forget(composed, longterm, before.today, before.daily)) or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        d = json.loads(raw)
        if not isinstance(d, dict):
            return None
        facts = str(d.get("facts") or "").strip()
        lt = str(d.get("longterm") or "").strip()
        keep = d.get("keep_daily")
        if not facts or not isinstance(keep, list):
            return None
        # 幻觉守门：遗忘只能删减（合并等价碎片不会增字符）；
        # longterm 输入为空串（迁移清空）时输出非空同样视为增写
        if len(facts) > len(composed) or len(lt) > len(longterm.strip()):
            return None
        keep_set = {str(x) for x in keep}
        # 确定性名额执行：LLM 无视名额时按行截断（重要度降序，截尾保重要）
        facts = _trim_to_budget(facts, FORGET_FACTS_BUDGET_CHARS)
        lt = _trim_to_budget(lt, FORGET_LONGTERM_BUDGET_CHARS)
        daily_keep = sorted((x for x in before.daily if x.get("date") in keep_set), key=lambda x: x["date"])
        # 名额兜底：超名额删最旧
        daily_keep = daily_keep[-FORGET_DAILY_LIMIT:]
        # 字符预算兜底：继续删最旧 daily（longterm/facts 超限不硬截，语义优先）
        while (
            daily_keep
            and len(facts) + len(lt) + sum(len(x.get("body") or "") for x in daily_keep) > FORGET_BUDGET_CHARS
        ):
            daily_keep.pop(0)
        return DreamSections(facts=facts, today=before.today, daily=daily_keep, longterm=lt)

    @staticmethod
    def _finalize(
        state: Dict, run_id: str, trigger: str, status: str, revision_id: str, before_hash: str, after_hash: str
    ) -> Dict:
        state["lastRun"] = {
            "runId": run_id,
            "trigger": trigger,
            "status": status,
            "revisionId": revision_id,
            "changed": before_hash != after_hash,
            "at": _now(),
        }
        return state

    # ── 每日自动 ──
    def start_automatic_if_eligible(self, logical_date: str) -> Optional[Dict]:
        state = _read_state(self._aid)
        if state.get("lastAutomaticAttemptDate") == logical_date:
            return None
        if state.get("lastSuccessfulManualDate") == logical_date:
            return None
        return self.start("automatic", logical_date=logical_date)

    # ── 状态 / 版本 ──
    def status(self) -> Dict:
        return _read_state(self._aid)

    def list_revisions(self) -> List[Dict]:
        return list_revisions(self._aid)

    def restore_revision(self, revision_id: str) -> Dict:
        doc = _read_revision(self._aid, revision_id)
        if doc is None:
            return {"ok": False, "error": "dream_revision_not_found"}
        lock = _locks.setdefault(str(self._aid), threading.Lock())
        if not lock.acquire(blocking=False):
            return {"ok": False, "error": "dream_already_running"}
        try:
            before = DreamSections.from_json(doc.get("before") or {})
            current = snapshot_sections(self._aid)
            # 恢复前存 pre_restore 快照
            create_revision(
                self._aid,
                run_id=f"restore-{int(time.time() * 1000)}",
                trigger="restore",
                before=current,
                kind="pre_restore",
                restores_revision_id=revision_id,
            )
            apply_sections(self._aid, before)
            state = _read_state(self._aid)
            state["lastRun"] = {
                "runId": f"restore-{revision_id[:16]}",
                "trigger": "restore",
                "status": "succeeded",
                "revisionId": revision_id,
                "at": _now(),
            }
            _write_state(self._aid, state)
            return {"ok": True, "restored": revision_id}
        finally:
            lock.release()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")
