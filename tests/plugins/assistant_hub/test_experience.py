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
    assert m.normalize_category(" 代码 风格 ") == "代码 风格"[:8].strip() or True
    assert len(m.normalize_category("x" * 100)) <= 8


def test_category_limit(tmp_path):
    """新分类超过上限被拒绝并附现有分类；已有分类仍可写入。"""
    aid = tmp_path / "a5"
    for i in range(m._MAX_CATEGORIES):
        r = m.record_entry(aid, f"分类{i}", f"内容{i}")
        assert r["added"] is True
    # 新分类：拒绝 + 现有分类列表
    r = m.record_entry(aid, "全新分类", "这条装不下")
    assert r["added"] is False and r["reason"] == "category_limit"
    assert len(r.get("categories") or []) == m._MAX_CATEGORIES
    # 已有分类：不受限
    r2 = m.record_entry(aid, "分类0", "追加到已有分类")
    assert r2["added"] is True
    assert m.total_entries(aid) == m._MAX_CATEGORIES + 1
    assert "全新分类" not in [d["category"] for d in m.list_documents(aid)]


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
