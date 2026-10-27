# -*- coding: utf-8 -*-
"""交接文档（handoff）读写、新鲜度判定与消费标记"""

import time
from pathlib import Path

from app.core.context.handoff import (
    FRESH_MAX_MESSAGES,
    dump_front,
    handoff_path,
    is_fresh,
    load,
    mark_injected,
    parse_front,
    updated_at_ms,
)


def _meta(ts: str = "2026-10-27 17:00:00", **extra):
    base = {"session_id": "sid-1", "updated_at": ts, "injected_into": []}
    base.update(extra)
    return base


def _msg(ts_ms: int, text: str = "x"):
    return {"role": "user", "content": text, "ts_ms": ts_ms}


class TestFreshness:
    def test_missing_updated_at_is_not_fresh(self):
        assert is_fresh({}, [_msg(1)]) is False

    def test_no_new_messages_is_fresh(self):
        cut = updated_at_ms(_meta())
        assert is_fresh(_meta(), [_msg(cut - 1000)]) is True

    def test_message_count_over_limit_is_stale(self):
        cut = updated_at_ms(_meta())
        msgs = [_msg(cut + 1000, "a") for _ in range(FRESH_MAX_MESSAGES + 1)]
        assert is_fresh(_meta(), msgs) is False

    def test_char_budget_over_limit_is_stale(self):
        cut = updated_at_ms(_meta())
        assert is_fresh(_meta(), [_msg(cut + 1000, "a" * 12001)]) is False

    def test_messages_without_ts_count_as_old(self):
        assert is_fresh(_meta(), [{"role": "user", "content": "a" * 99999}]) is True

    def test_list_content_is_measured(self):
        cut = updated_at_ms(_meta())
        msg = {"role": "assistant", "ts_ms": cut + 1, "content": [{"type": "text", "text": "a" * 20000}]}
        assert is_fresh(_meta(), [msg]) is False


class TestFrontmatter:
    def test_parse_dump_roundtrip(self):
        meta = _meta(project_root="D:/work/DriFox", injected_into=["a", "b"])
        parsed, body = parse_front(dump_front(meta) + "\n## 任务目标\nX\n")
        assert parsed["session_id"] == meta["session_id"]
        assert parsed["project_root"] == "D:/work/DriFox"
        assert parsed["injected_into"] == ["a", "b"]
        assert body.startswith("\n## 任务目标")

    def test_parse_without_front_returns_raw(self):
        meta, body = parse_front("## 任务目标\nX")
        assert meta == {}
        assert body == "## 任务目标\nX"


class TestMarkInjected:
    def _write(self, tmp_path: Path) -> Path:
        path = tmp_path / "sid-1.md"
        path.write_text(dump_front(_meta()) + "\n## 任务目标\nX\n", encoding="utf-8")
        return path

    def test_appends_and_dedupes(self, tmp_path):
        path = self._write(tmp_path)
        mark_injected(path, "new-1")
        mark_injected(path, "new-2")
        mark_injected(path, "new-1")
        doc = load(path)
        assert doc["meta"]["injected_into"] == ["new-2", "new-1"]
        assert doc["body"] == "## 任务目标\nX"

    def test_keeps_only_recent_ids(self, tmp_path):
        path = self._write(tmp_path)
        for i in range(8):
            mark_injected(path, f"s{i}")
        doc = load(path)
        assert doc["meta"]["injected_into"] == ["s3", "s4", "s5", "s6", "s7"]

    def test_missing_file_is_noop(self, tmp_path):
        mark_injected(tmp_path / "nope.md", "x")


class TestLoad:
    def test_missing_file_returns_none(self, tmp_path):
        assert load(tmp_path / "nope.md") is None

    def test_body_only_without_front_is_none(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text("no frontmatter here", encoding="utf-8")
        assert load(path) is None

    def test_handoff_path_uses_project_dir(self):
        p = handoff_path("D:/work/DriFox", "abc")
        assert p == Path("D:/work/DriFox") / ".drifox" / "handoff" / "abc.md"

    def test_handoff_path_falls_back_to_app_data(self, tmp_path=None):
        import tempfile

        app_data = Path(tempfile.mkdtemp())
        p = handoff_path("", "abc", app_data)
        assert p == app_data / "handoff" / "abc.md"


def test_updated_at_ms_parses_local_time():
    ts = "2026-10-27 17:00:00"
    expected = int(time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))) * 1000
    assert updated_at_ms({"updated_at": ts}) == expected
    assert updated_at_ms({"updated_at": "bad"}) == 0
