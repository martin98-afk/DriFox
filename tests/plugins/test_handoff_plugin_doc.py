# -*- coding: utf-8 -*-
"""交接文档读写与新鲜度判定（插件侧独立实现）

覆盖契约：frontmatter 四字段、路径约定、15 分钟文档年龄上限。
"""

import time


def _load():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "plugins" / "system-handoff" / "lib" / "handoff_doc.py"
    spec = importlib.util.spec_from_file_location("handoff_doc_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_old_document_not_fresh():
    mod = _load()
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
    assert mod.is_fresh({"updated_at": old}, []) is False


def test_fresh_document_with_no_new_messages():
    mod = _load()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    assert mod.is_fresh({"updated_at": now}, []) is True


def test_roundtrip(tmp_path):
    mod = _load()
    meta = {"session_id": "s1", "updated_at": "2026-10-27 20:00:00", "injected_into": []}
    text = mod.dump_front(meta) + "\n## 任务目标\nX\n"
    p = tmp_path / "s1.md"
    p.write_text(text, encoding="utf-8")
    doc = mod.load(p)
    assert doc["meta"]["session_id"] == "s1"
    assert "## 任务目标" in doc["body"]


def test_mark_injected_appends(tmp_path):
    mod = _load()
    meta = {"session_id": "s1", "updated_at": "2026-10-27 20:00:00", "injected_into": []}
    p = tmp_path / "s1.md"
    p.write_text(mod.dump_front(meta) + "\n## 任务目标\nX\n", encoding="utf-8")
    mod.mark_injected(p, "new-1")
    doc = mod.load(p)
    assert doc["meta"]["injected_into"] == ["new-1"]
