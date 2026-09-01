# -*- coding: utf-8 -*-
"""test_dream.py — Dream 管线测试（fake LLM + tmp 目录）。"""
import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "dream.py"

spec = importlib.util.spec_from_file_location("test_dream_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_dream_mod", m)
spec.loader.exec_module(m)


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, **kw):
        content = messages[-1]["content"]
        self.calls.append(content[:30])
        return self.replies.pop(0) if self.replies else "（无）"


def _seed_memory(aid: Path, facts: str = "- 用户偏好简洁", longterm: str = "- 长期事实A"):
    mem = aid / "memory"
    (mem / "daily").mkdir(parents=True, exist_ok=True)
    (mem / "facts.md").write_text(facts, encoding="utf-8")
    (mem / "today.md").write_text("- 今天聊了部署", encoding="utf-8")
    (mem / "daily" / "2026-08-30.md").write_text("# 2026-08-30\n\n- 30日的事", encoding="utf-8")
    (mem / "longterm.md").write_text(longterm, encoding="utf-8")


_DREAM_REPLIES = [
    "- 单元A\n- 单元B",              # atomize
    "- 单元A\n- 单元B",              # dedupe
    "- 单元A（优化）\n- 单元B（优化）",  # optimize
    "## 主题\n\n- 单元A（优化）\n- 单元B（优化）",  # compose
    '{"semantic_ok": true, "provenance_ok": true, "sufficient_compression": true, "feedback": ""}',  # verify
]


def test_dream_normal_flow(tmp_path):
    aid = tmp_path / "a1"
    _seed_memory(aid)
    llm = FakeLLM(list(_DREAM_REPLIES))
    runner = m.DreamRunner(aid, llm=llm)
    r = runner.start("manual")
    assert r["ok"] is True and r["changed"] is True
    assert r["revision_id"]
    # revision 落盘（含 before 快照）
    revs = runner.list_revisions()
    assert len(revs) == 1 and revs[0]["kind"] == "dream"
    # 修剪到 10 份以内（造 12 次重复流）
    for _ in range(11):
        llm2 = FakeLLM(list(_DREAM_REPLIES))
        m.DreamRunner(aid, llm=llm2).start("manual")
    assert len(runner.list_revisions()) <= 10


def test_dream_empty_memory(tmp_path):
    aid = tmp_path / "a2"
    (aid / "memory").mkdir(parents=True)
    runner = m.DreamRunner(aid, llm=FakeLLM([]))
    r = runner.start("manual")
    assert r["ok"] is False and "no_memory" in (r.get("error") or "")


def test_dream_reentry_locked(tmp_path):
    aid = tmp_path / "a3"
    _seed_memory(aid)
    lock = m._locks.setdefault(str(aid), __import__("threading").Lock())
    with lock:
        runner = m.DreamRunner(aid, llm=FakeLLM([]))
        r = runner.start("manual")
        assert r["ok"] is False and "running" in (r.get("error") or "")


def test_dream_verify_semantic_fail_aborts(tmp_path):
    aid = tmp_path / "a4"
    _seed_memory(aid)
    bad_verify = (
        '{"semantic_ok": false, "provenance_ok": true, "sufficient_compression": true,'
        ' "feedback": "编造了原文没有的事实"}'
    )
    llm = FakeLLM(["- 单元A", "- 单元A", "- 单元A", "## 主题\n\n- 单元A", bad_verify])
    before_longterm = (aid / "memory" / "longterm.md").read_text(encoding="utf-8")
    runner = m.DreamRunner(aid, llm=llm)
    r = runner.start("manual")
    assert r["ok"] is False and "verify" in (r.get("error") or "")
    # 未应用：longterm 保持原样，无 revision
    assert (aid / "memory" / "longterm.md").read_text(encoding="utf-8") == before_longterm
    assert runner.list_revisions() == []


def test_dream_restore_revision(tmp_path):
    aid = tmp_path / "a5"
    _seed_memory(aid, longterm="- 原始长期内容")
    runner = m.DreamRunner(aid, llm=FakeLLM(list(_DREAM_REPLIES)))
    r1 = runner.start("manual")
    assert r1["ok"] is True
    # Dream 应用后 longterm 已变；恢复到 before
    r2 = runner.restore_revision(r1["revision_id"])
    assert r2["ok"] is True
    lt = (aid / "memory" / "longterm.md").read_text(encoding="utf-8")
    assert "原始长期内容" in lt
    # 恢复动作本身存 pre_restore revision
    kinds = [x["kind"] for x in runner.list_revisions()]
    assert "pre_restore" in kinds


def test_dream_automatic_double_watermark(tmp_path):
    aid = tmp_path / "a6"
    _seed_memory(aid)
    runner = m.DreamRunner(aid, llm=FakeLLM(list(_DREAM_REPLIES)))
    # 第一次自动：执行
    r1 = runner.start_automatic_if_eligible("2026-09-01")
    assert r1 is not None and r1["ok"] is True
    # 同日第二次自动：拒绝
    assert runner.start_automatic_if_eligible("2026-09-01") is None
    # 手动成功当天：自动也拒绝
    runner2 = m.DreamRunner(aid, llm=FakeLLM(list(_DREAM_REPLIES)))
    runner2.start("manual")
    assert runner2.start_automatic_if_eligible("2026-09-01") is None
    # 次日：放行
    assert runner2.start_automatic_if_eligible("2026-09-02") is not None
