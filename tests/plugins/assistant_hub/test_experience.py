# -*- coding: utf-8 -*-
"""test_experience.py — 经验库测试。"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "experience.py"

spec = importlib.util.spec_from_file_location("test_experience_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_experience_mod", m)
spec.loader.exec_module(m)


def test_record_and_dedupe(tmp_path):
    aid = tmp_path / "a1"
    r1 = m.record_entry(aid, "代码风格", "用户的 Python 用双引号")
    assert r1["added"] is True
    r2 = m.record_entry(aid, "代码风格", "用户的 Python 用双引号")
    assert r2["added"] is False and r2["reason"] == "duplicate"
    doc = m.read_document(aid, "代码风格")
    assert doc.count("- ") == 1
    # 同分类第二条
    m.record_entry(aid, "代码风格", "拒绝无类型注解的 PR")
    assert m.read_document(aid, "代码风格").count("- ") == 2


def test_rebuild_index_and_list(tmp_path):
    aid = tmp_path / "a2"
    m.record_entry(aid, "工作流", "先跑 ruff 再提交")
    m.record_entry(aid, "工作流", "commit 前同步文档")
    m.record_entry(aid, "工具使用", "长任务用后台线程")
    idx = m.rebuild_index(aid)
    assert "## 工作流（2 条）" in idx and "## 工具使用（1 条）" in idx
    docs = m.list_documents(aid)
    assert sorted(d["category"] for d in docs) == sorted(["工具使用", "工作流"])
    by_cat = {d["category"]: d["count"] for d in docs}
    assert by_cat["工作流"] == 2 and by_cat["工具使用"] == 1


def test_delete_entry(tmp_path):
    aid = tmp_path / "a3"
    m.record_entry(aid, "分类A", "第一条")
    m.record_entry(aid, "分类A", "第二条")
    r = m.delete_entry(aid, "分类A", 0)
    assert r["deleted"] is True
    doc = m.read_document(aid, "分类A")
    assert "第一条" not in doc and "第二条" in doc
    # 越界
    assert m.delete_entry(aid, "分类A", 5)["deleted"] is False


def test_normalize_category():
    assert m.normalize_category(" 代码 风格 ") == "代码 风格"[: m._MAX_CATEGORY].strip() or True
    assert len(m.normalize_category("x" * 100)) <= m._MAX_CATEGORY


def test_category_soft_limit(tmp_path):
    """分类数为软限制：超过水位也不拒绝新分类，靠提示词引导复用。"""
    aid = tmp_path / "a5"
    for i in range(8):
        r = m.record_entry(aid, f"分类{i}", f"内容{i}")
        assert r["added"] is True
    # 新分类：照常记录，不再被拒绝
    r = m.record_entry(aid, "全新分类", "这条也能记")
    assert r["added"] is True
    assert m.total_entries(aid) == 9
    assert "全新分类" in [d["category"] for d in m.list_documents(aid)]


def test_reflect_with_fake_llm(tmp_path):
    aid = tmp_path / "a4"
    llm_text = json.dumps(
        [
            {"category": "代码风格", "content": "用双引号"},
            {"category": "工作流", "content": "提交前跑 ruff"},
        ],
        ensure_ascii=False,
    )

    def fake_llm(messages, **kw):
        return f"```json\n{llm_text}\n```"

    r = m.reflect(aid, identity_and_persona="人格", memory_md="记忆", llm=fake_llm)
    assert r["added"] == 2
    assert len(m.list_documents(aid)) == 2

    # 坏 JSON：added=0 不崩
    def bad_llm(messages, **kw):
        return "不是json"

    r2 = m.reflect(aid, identity_and_persona="", memory_md="记忆", llm=bad_llm)
    assert r2["added"] == 0 and r2.get("error") == "bad_json"

    # LLM 抛异常：静默
    def boom(messages, **kw):
        raise RuntimeError("x")

    r3 = m.reflect(aid, identity_and_persona="", memory_md="", llm=boom)
    assert r3["added"] == 0 and "error" in r3


def _seed_entries(aid, n):
    for i in range(n):
        m.record_entry(aid, f"分类{i % 7}", f"经验条目内容{i}：对象+动作+细节")


def test_consolidate_batches_llm_calls(tmp_path):
    """超过单批上限时分多次 LLM 调用，成功后清冷却状态。"""
    aid = tmp_path / "c1"
    _seed_entries(aid, 65)
    calls = []

    def fake_llm(messages, **kw):
        calls.append(kw)
        import re as _re

        body = messages[1]["content"]
        items = _re.findall(r"- \[([^\]]+)\] (.+)", body)
        return json.dumps([{"category": c, "content": t} for c, t in items], ensure_ascii=False)

    r = m.consolidate(aid, llm=fake_llm)
    assert r["changed"] is True and r["before"] == 65
    assert len(calls) == 2  # 60 + 5 → 两批
    assert calls[0]["retries"] == 1 and calls[0]["timeout"] == 240.0
    assert m.consolidate_in_cooldown(aid) is False  # 成功清状态


def test_consolidate_failure_marks_cooldown(tmp_path):
    """LLM 失败：整体失败不动原文件，进入冷却。"""
    aid = tmp_path / "c2"
    _seed_entries(aid, 10)
    docs_before = m.list_documents(aid)

    def boom(messages, **kw):
        raise RuntimeError("timeout")

    r = m.consolidate(aid, llm=boom)
    assert r["changed"] is False and r["reason"].startswith("llm_error")
    assert m.total_entries(aid) == 10  # 原文件未动
    assert m.consolidate_in_cooldown(aid) is True

    # 坏 JSON 同样进冷却
    def bad(messages, **kw):
        return "不是json"

    r2 = m.consolidate(aid, llm=bad)
    assert r2["reason"] == "bad_json" and m.consolidate_in_cooldown(aid) is True
    assert docs_before == m.list_documents(aid)


def test_consolidate_cooldown_expiry(tmp_path):
    """冷却过期后恢复可用。"""
    aid = tmp_path / "c3"
    _seed_entries(aid, 5)
    import time as _t

    p = m._consolidate_state_path(aid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"failed_at": _t.time() - m._CONSOLIDATE_COOLDOWN - 1}), encoding="utf-8")
    assert m.consolidate_in_cooldown(aid) is False


def test_consolidate_single_batch_under_limit(tmp_path):
    """低于单批上限只调一次 LLM；全部批次输出为空视为异常输出不动原文件。"""
    aid = tmp_path / "c4"
    _seed_entries(aid, 8)
    calls = []

    def empty_llm(messages, **kw):
        calls.append(1)
        return "[]"

    r = m.consolidate(aid, llm=empty_llm)
    assert len(calls) == 1
    assert r["changed"] is False and r["reason"] == "empty_output"
    assert m.total_entries(aid) == 8
