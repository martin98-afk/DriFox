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
_locks: Dict[str, threading.Lock] = {}


class DreamAlreadyRunningError(RuntimeError):
    pass


def _load(name: str, rel: str):
    key = f"assistant_hub_core.{name}"
    import sys

    mod = sys.modules.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(key, str(_THIS.parent.parent / rel))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
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
    cm = _compile_mod()
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


def _sections_text(s: DreamSections) -> str:
    parts = [s.facts, s.today]
    parts.extend(d["body"] for d in s.daily)
    parts.append(s.longterm)
    return "\n\n".join(p for p in parts if p.strip())


def _editable_chars(s: DreamSections) -> int:
    return len(s.facts.strip()) + len(s.longterm.strip())


def _hash_sections(s: DreamSections) -> str:
    return hashlib.sha256(json.dumps(s.to_json(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def apply_sections(aid_dir: Path, s: DreamSections) -> None:
    mem = aid_dir / "memory"
    _write(mem / "facts.md", s.facts)
    _write(mem / "longterm.md", s.longterm)
    # today/daily 属"过程段"：Dream 只重写 facts/longterm 可编辑段（对齐原版 editable sections）


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
        if _editable_chars(before) == 0:
            err = "dream_no_memory"
            state["lastRun"] = {"runId": run_id, "trigger": trigger, "status": "failed", "error": err, "at": _now()}
            _write_state(self._aid, state)
            return {"ok": False, "error": err}
        before_hash = _hash_sections(before)

        prompts = _prompts()
        total = 5
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
            if not (verdict.get("semantic_ok") and verdict.get("provenance_ok")):
                raise RuntimeError(f"dream_verify_failed: {verdict.get('feedback') or '语义/溯源校验未通过'}")
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

        after = DreamSections(facts=composed, today=before.today, daily=before.daily, longterm=composed)
        if _hash_sections(after) == before_hash:
            state.update(self._finalize(state, run_id, trigger, "succeeded", "", before_hash, before_hash))
            if trigger == "manual":
                state["lastSuccessfulManualDate"] = self._logical_date
            _write_state(self._aid, state)
            return {"ok": True, "run_id": run_id, "changed": False, "revision_id": ""}

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
        if trigger == "manual":
            state["lastSuccessfulManualDate"] = self._logical_date
        else:
            state["lastAutomaticAttemptDate"] = self._logical_date
        _write_state(self._aid, state)
        return {"ok": True, "run_id": run_id, "changed": True, "revision_id": revision_id}

    def _verify(self, prompts, before: DreamSections, composed: str) -> Dict:
        raw = (self._llm(prompts.build_dream_verify(_sections_text(before), composed)) or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

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
