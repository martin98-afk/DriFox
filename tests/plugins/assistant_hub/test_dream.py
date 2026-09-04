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
    "- 单元A\n- 单元B",  # atomize
    "- 单元A\n- 单元B",  # dedupe
    "- 单元A（优化）\n- 单元B（优化）",  # optimize
    "## 主题\n\n- 单元A（优化）\n- 单元B（优化）",  # compose
    '{"semantic_ok": true, "provenance_ok": true, "sufficient_compression": true, "feedback": ""}',  # verify
    # forget：facts 原样保留合成稿、daily 全保留、longterm 清空（≤输入即可过守门）
    '{"facts": "## 主题\\n\\n- 单元A（优化）\\n- 单元B（优化）", "keep_daily": ["2026-08-30"], "longterm": ""}',
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


def test_dream_runs_with_only_today(tmp_path):
    """facts/longterm 为空但 today.md 有内容时应放行。

    compile_facts 依赖 sessions.db 的对话样本，全新助手往往还没产出 facts.md；
    此时 Dream 的输入（today + daily）已经足够，不应报 dream_no_memory。
    """
    aid = tmp_path / "a2_today_only"
    mem = aid / "memory"
    mem.mkdir(parents=True)
    (mem / "today.md").write_text("- 今天聊了部署\n- 用户偏好简洁", encoding="utf-8")

    runner = m.DreamRunner(aid, llm=FakeLLM(list(_DREAM_REPLIES)))
    r = runner.start("manual")
    assert r["ok"] is True and r["changed"] is True
    # 产出单写 facts.md；longterm 保持日记沉淀语义，不被 Dream 覆盖
    assert (mem / "facts.md").read_text(encoding="utf-8").strip()
    assert not (mem / "longterm.md").exists() or not (mem / "longterm.md").read_text(encoding="utf-8").strip()


def test_dream_legacy_duplication_migrated(tmp_path):
    """旧版双写污染（facts==longterm）：Dream 后 longterm 清空，内容已随 composed 落入 facts。"""
    aid = tmp_path / "a2_dup"
    mem = aid / "memory"
    mem.mkdir(parents=True)
    dup = "- 事实甲\n- 事实乙"
    (mem / "facts.md").write_text(dup, encoding="utf-8")
    (mem / "longterm.md").write_text(dup, encoding="utf-8")
    (mem / "today.md").write_text("- 今天聊了部署", encoding="utf-8")

    runner = m.DreamRunner(aid, llm=FakeLLM(list(_DREAM_REPLIES)))
    r = runner.start("manual")
    assert r["ok"] is True
    assert (mem / "longterm.md").read_text(encoding="utf-8").strip() == ""
    assert (mem / "facts.md").read_text(encoding="utf-8").strip()


def test_sections_text_dedupes_legacy_duplication():
    """facts==longterm 时 _sections_text 只拼一份，避免双倍重复输入。"""
    S = m.DreamSections
    dup = "- 事实甲"
    s = S(facts=dup, today="", daily=[], longterm=dup)
    assert s.facts in m._sections_text(s)
    assert m._sections_text(s).count("事实甲") == 1
    # 不同内容不误伤
    s2 = S(facts="- 事实甲", today="", daily=[], longterm="- 长期沉淀")
    assert "长期沉淀" in m._sections_text(s2)


def test_dream_whitespace_only_counts_as_empty(tmp_path):
    """只有空白字符仍视为无记忆（避免放行后被空 prompt 打到引擎）。"""
    aid = tmp_path / "a2_blank"
    mem = aid / "memory"
    mem.mkdir(parents=True)
    (mem / "today.md").write_text("   \n\t\n", encoding="utf-8")
    runner = m.DreamRunner(aid, llm=FakeLLM([]))
    r = runner.start("manual")
    assert r["ok"] is False and "no_memory" in (r.get("error") or "")


def test_has_any_memory_considers_all_sections():
    """准入判定覆盖 facts / today / daily / longterm 四段。"""
    S = m.DreamSections
    assert m._has_any_memory(S(facts="", today="", daily=[], longterm="")) is False
    assert m._has_any_memory(S(facts="- 事实", today="", daily=[], longterm="")) is True
    assert m._has_any_memory(S(facts="", today="- 今日", daily=[], longterm="")) is True
    assert m._has_any_memory(S(facts="", today="", daily=[{"date": "d", "body": "近期"}], longterm="")) is True
    assert m._has_any_memory(S(facts="", today="", daily=[], longterm="- 长期")) is True


def test_dream_reentry_locked(tmp_path):
    aid = tmp_path / "a3"
    _seed_memory(aid)
    lock = m._locks.setdefault(str(aid), __import__("threading").Lock())
    with lock:
        runner = m.DreamRunner(aid, llm=FakeLLM([]))
        r = runner.start("manual")
        assert r["ok"] is False and "running" in (r.get("error") or "")


def test_dream_verify_semantic_fail_degrades_to_warning(tmp_path):
    """verify 语义/溯源不通过：降级为警告（lastRun.warning + result.warning），整理照常落盘。"""
    aid = tmp_path / "a4"
    _seed_memory(aid)
    bad_verify = (
        '{"semantic_ok": false, "provenance_ok": true, "sufficient_compression": true,'
        ' "feedback": "编造了原文没有的事实"}'
    )
    llm = FakeLLM(["- 单元A", "- 单元A", "- 单元A", "## 主题\n\n- 单元A", bad_verify])
    runner = m.DreamRunner(aid, llm=llm)
    r = runner.start("manual")
    assert r["ok"] is True and r["changed"] is True
    assert "编造了原文没有的事实" in (r.get("warning") or "")
    # 应用成功：longterm 已被整理内容覆盖，revision 已建
    assert (aid / "memory" / "longterm.md").read_text(encoding="utf-8").strip()
    assert len(runner.list_revisions()) == 1
    # lastRun.warning 留痕
    state = runner.status()
    assert "编造了原文没有的事实" in (state.get("lastRun", {}).get("warning") or "")
    assert state["lastRun"]["status"] == "succeeded"


def test_dream_verify_pass_no_warning(tmp_path):
    """verify 全部通过：无 warning 字段，行为与旧成功路径一致。"""
    aid = tmp_path / "a4_ok"
    _seed_memory(aid)
    llm = FakeLLM(list(_DREAM_REPLIES))
    runner = m.DreamRunner(aid, llm=llm)
    r = runner.start("manual")
    assert r["ok"] is True and r.get("warning") == ""
    assert "warning" not in runner.status().get("lastRun", {})


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


def test_build_dream_forget_prompt():
    p = m._prompts()
    msgs = p.build_dream_forget(
        "- 事实A",
        "- 长期沉淀",
        "- 今日草稿",
        [{"date": "2026-08-30", "body": "# 2026-08-30\n\n- 30日的事"}],
    )
    text = msgs[-1]["content"]
    # 火灾/物竞天择场景 + 名额数字 + 只删不增约束 + JSON 输出契约 + 四段输入
    assert "火灾" in text and "名额" in text
    assert "15" in text and "20" in text and "3" in text
    assert "keep_daily" in text
    assert "不增" in text
    assert "- 事实A" in text and "- 长期沉淀" in text and "- 今日草稿" in text
    assert "2026-08-30" in text and "30日的事" in text


def test_apply_sections_syncs_daily(tmp_path):
    aid = tmp_path / "a_apply"
    _seed_memory(aid)  # daily/2026-08-30.md
    (aid / "memory" / "daily" / "2026-08-31.md").write_text("# 2026-08-31\n\n- 31日的事", encoding="utf-8")
    ddir = aid / "memory" / "daily"
    assert (ddir / "2026-08-30.md").exists() and (ddir / "2026-08-31.md").exists()
    # 只保留 08-31：08-30 应被删除
    m.apply_sections(
        aid,
        m.DreamSections(
            facts="- F", today="- T", daily=[{"date": "2026-08-31", "body": "# 2026-08-31\n\n- 31日的事"}], longterm=""
        ),
    )
    assert not (ddir / "2026-08-30.md").exists()
    assert (ddir / "2026-08-31.md").exists()
    # daily 为空列表：清空全部 daily
    m.apply_sections(aid, m.DreamSections(facts="- F", today="- T", daily=[], longterm=""))
    assert not any(ddir.glob("*.md"))


def _seed_memory_multi(aid: Path):
    """5 天 daily + 长 facts/longterm，供遗忘/兜底用例。"""
    mem = aid / "memory"
    (mem / "daily").mkdir(parents=True, exist_ok=True)
    (mem / "facts.md").write_text("\n".join(f"- 事实{i}" for i in range(20)), encoding="utf-8")
    (mem / "today.md").write_text("- 今天聊了部署", encoding="utf-8")
    for i in range(5):
        d = f"2026-08-2{5 + i}"
        (mem / "daily" / f"{d}.md").write_text(f"# {d}\n\n- {d}的事", encoding="utf-8")
    (mem / "longterm.md").write_text("\n".join(f"- 长期{i}：{'细节' * 60}" for i in range(25)), encoding="utf-8")


def _forget_reply(facts: str, keep: list, longterm: str) -> str:
    return json.dumps({"facts": facts, "keep_daily": keep, "longterm": longterm}, ensure_ascii=False)


def test_dream_forget_step_prunes(tmp_path):
    aid = tmp_path / "a_forget"
    _seed_memory_multi(aid)
    composed = "\n".join(f"- 合成事实{i}" for i in range(10))
    llm = FakeLLM(
        list(_DREAM_REPLIES[:4])
        + ['{"semantic_ok": true, "provenance_ok": true, "sufficient_compression": true, "feedback": ""}']
        + [_forget_reply("- 合成事实0\n- 合成事实1", ["2026-08-29", "2026-08-27"], "- 长期0")]
    )
    r = m.DreamRunner(aid, llm=llm).start("manual")
    assert r["ok"] is True and r["changed"] is True
    mem = aid / "memory"
    # facts 落遗忘后内容；daily 只留 keep 的两天；longterm 落遗忘后内容
    assert (mem / "facts.md").read_text(encoding="utf-8").strip() == "- 合成事实0\n- 合成事实1"
    ddir = mem / "daily"
    kept = sorted(f.stem for f in ddir.glob("*.md"))
    assert kept == ["2026-08-27", "2026-08-29"]
    assert (mem / "longterm.md").read_text(encoding="utf-8").strip() == "- 长期0"
    # revision before 含全量 5 天 daily → 可完整恢复
    revs = m.DreamRunner(aid, llm=FakeLLM([])).list_revisions()
    assert revs and revs[0]["dailyCount"] == 5


def test_dream_forget_invalid_json_degrades(tmp_path):
    aid = tmp_path / "a_degrade"
    _seed_memory_multi(aid)
    llm = FakeLLM(list(_DREAM_REPLIES[:5]) + ["（无法解析）"])
    r = m.DreamRunner(aid, llm=llm).start("manual")
    assert r["ok"] is True  # Dream 整体不失败
    mem = aid / "memory"
    # 降级：五步合成稿落 facts，daily 不删
    facts_text = (mem / "facts.md").read_text(encoding="utf-8")
    assert "合成事实" in facts_text or "单元A" in facts_text
    assert len(list((mem / "daily").glob("*.md"))) == 5
    state = m.DreamRunner(aid, llm=FakeLLM([])).status()
    assert "遗忘" in str(state.get("lastRun", {}).get("warning", ""))


def test_dream_forget_hallucination_guard(tmp_path):
    """遗忘输出比输入长（凭空增写）→ 视为幻觉，降级。"""
    aid = tmp_path / "a_halluc"
    _seed_memory_multi(aid)
    composed = "- 合成事实0"
    bloated = "\n".join(f"- 凭空新事实{i}" for i in range(50))
    llm = FakeLLM(list(_DREAM_REPLIES[:5]) + [_forget_reply(bloated, [], bloated)])
    r = m.DreamRunner(aid, llm=llm).start("manual")
    assert r["ok"] is True
    # facts 落的是五步合成稿（composed），不是 bloated
    facts_text = (aid / "memory" / "facts.md").read_text(encoding="utf-8")
    assert "凭空新事实" not in facts_text


def test_dream_forget_daily_quota_and_budget(tmp_path):
    """keep 超名额 → 确定性删最旧；longterm 超预算硬截 → total ≤ 3000。"""
    aid = tmp_path / "a_quota"
    _seed_memory_multi(aid)
    composed = "- 合成事实0"
    big_longterm = "- 长" * 1034  # 3102 字符：≤ seed longterm（守门过），硬截后 ≤1400
    keep_all = [f"2026-08-2{5 + i}" for i in range(5)]  # 超名额 3 天
    llm = FakeLLM(list(_DREAM_REPLIES[:5]) + [_forget_reply(composed, keep_all, big_longterm)])
    r = m.DreamRunner(aid, llm=llm).start("manual")
    assert r["ok"] is True
    ddir = aid / "memory" / "daily"
    kept = sorted(f.stem for f in ddir.glob("*.md"))
    # 留的是最新几天（名额 3 天，硬截后预算内不再删）
    all_days = sorted(f"2026-08-2{5 + i}" for i in range(5))
    assert len(kept) <= 3
    assert kept == all_days[len(all_days) - len(kept) :]
    # 用落盘值算总量：longterm 已被硬截
    lt_final = (aid / "memory" / "longterm.md").read_text(encoding="utf-8").strip()
    assert len(lt_final) <= 1400
    total = len(composed) + len(lt_final) + sum(len(f.read_text(encoding="utf-8")) for f in ddir.glob("*.md"))
    assert total <= 3000 or not kept


def test_dream_forget_enforces_budget_when_llm_disobeys(tmp_path):
    """LLM 无视名额返回超长 facts → 确定性硬截到预算内（保重要度序前部）。"""
    aid = tmp_path / "a_trim"
    _seed_memory_multi(aid)
    composed = "\n".join(f"- 合成事实{i}：{'内容' * 20}" for i in range(60))  # ~2500 字符
    llm = FakeLLM(
        list(_DREAM_REPLIES[:3])
        + [composed]  # compose：合成稿本身长，遗忘 reply 与其等长 → 幻觉守门过
        + ['{"semantic_ok": true, "provenance_ok": true, "sufficient_compression": true, "feedback": ""}']
        + [_forget_reply(composed, ["2026-08-29"], "- 长期0")]
    )
    r = m.DreamRunner(aid, llm=llm).start("manual")
    assert r["ok"] is True
    facts_text = (aid / "memory" / "facts.md").read_text(encoding="utf-8").strip()
    # 硬截生效：不超预算、头部重要条目保留、尾部条目被丢
    assert len(facts_text) <= 1500
    assert "合成事实0" in facts_text
    assert "合成事实59" not in facts_text
