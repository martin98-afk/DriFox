# -*- coding: utf-8 -*-
"""交接（handoff）三件套：注入 hook、指引 hook、update_handoff 工具"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / "plugins" / "system-hooks" / "hooks"
TOOLS_DIR = ROOT / "plugins" / "system-tools" / "tools"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _inject_handoff():
    return _load(HOOKS_DIR / "inject_handoff.py", "inject_handoff_under_test")


def _handoff_guide():
    return _load(HOOKS_DIR / "inject_handoff_guide.py", "inject_handoff_guide_under_test")


def _handoff_tools():
    return _load(TOOLS_DIR / "handoff_tools.py", "handoff_tools_under_test")


class TestInjectHandoffHook:
    def test_outputs_prefix_and_body(self):
        mod = _inject_handoff()
        out = mod.hook(
            "SessionStart",
            {"handoff": {"content": "## 任务目标\nX", "source_session_id": "abc123"}},
        )
        assert "另一个语言模型" in out
        assert "## 任务目标" in out
        assert "abc123" in out

    def test_empty_without_payload(self):
        mod = _inject_handoff()
        assert mod.hook("SessionStart", {}) == ""
        assert mod.hook("SessionStart", {"handoff": {"content": "   "}}) == ""


class TestHandoffGuideHook:
    def test_primary_gets_guide(self):
        mod = _handoff_guide()
        out = mod.hook("BuildSystemPrompt", {"current_role": "primary"})
        assert "update_handoff" in out
        assert "剩余任务与下一步" in out

    def test_subagent_gets_nothing(self):
        mod = _handoff_guide()
        assert mod.hook("BuildSystemPrompt", {"current_role": "subagent"}) == ""
        assert mod.hook("BuildSystemPrompt", {}) == ""


class TestUpdateHandoffTool:
    def _ctx(self, tmp_path, session_id="sid-1"):
        return {"workdir": str(tmp_path), "session_id": session_id, "env": {}}

    def test_writes_file_with_frontmatter(self, tmp_path):
        mod = _handoff_tools()
        result = mod._update_handoff_impl("## 任务目标\n测试", tool_ctx=self._ctx(tmp_path))
        assert result.success is True
        path = tmp_path / ".drifox" / "handoff" / "sid-1.md"
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert 'session_id: "sid-1"' in text
        assert "## 任务目标" in text

    def test_updates_keep_created_at_and_injected(self, tmp_path):
        mod = _handoff_tools()
        ctx = self._ctx(tmp_path)
        mod._update_handoff_impl("## 任务目标\nA", tool_ctx=ctx)
        path = tmp_path / ".drifox" / "handoff" / "sid-1.md"
        first = path.read_text(encoding="utf-8")
        created = [ln for ln in first.splitlines() if ln.startswith("created_at")][0]
        path.write_text(first.replace('injected_into: []', 'injected_into: ["s1"]'), encoding="utf-8")

        mod._update_handoff_impl("## 任务目标\nB", tool_ctx=ctx)
        second = path.read_text(encoding="utf-8")
        assert created in second
        assert 'injected_into: ["s1"]' in second
        assert "## 任务目标\nB" in second

    def test_empty_content_rejected(self, tmp_path):
        mod = _handoff_tools()
        assert mod._update_handoff_impl("  ", tool_ctx=self._ctx(tmp_path)).success is False

    def test_missing_session_id_rejected(self, tmp_path):
        mod = _handoff_tools()
        result = mod._update_handoff_impl("## 任务目标\nX", tool_ctx={"workdir": str(tmp_path)})
        assert result.success is False

    def test_switch_false_has_no_data(self, tmp_path):
        mod = _handoff_tools()
        result = mod._update_handoff_impl("## 任务目标\nX", tool_ctx=self._ctx(tmp_path))
        assert result.data is None

    def test_switch_true_reports_request(self, tmp_path):
        mod = _handoff_tools()
        result = mod._update_handoff_impl("## 任务目标\nX", switch=True, tool_ctx=self._ctx(tmp_path))
        assert result.success is True
        assert result.data["handoff_switch"] is True
        assert result.data["handoff_path"].endswith("sid-1.md")
        assert "新会话" in result.content

    def test_falls_back_to_app_data_without_workdir(self, tmp_path):
        mod = _handoff_tools()
        ctx = {"workdir": "", "session_id": "sid-2", "env": {"app_data_dir": str(tmp_path)}}
        assert mod._update_handoff_impl("## 任务目标\nX", tool_ctx=ctx).success is True
        assert (tmp_path / "handoff" / "sid-2.md").is_file()
