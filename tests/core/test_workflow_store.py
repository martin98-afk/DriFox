# -*- coding: utf-8 -*-
"""workflow 存档层单测：save/list/load/new_run_dir + impl 的 action 分发。"""
import json

import pytest

from plugins.workflow.tools.workflow_tool import (
    list_workflows,
    load_workflow,
    new_run_dir,
    save_workflow,
)


class TestWorkflowStore:
    def test_save_list_load_roundtrip(self, tmp_path):
        save_workflow(tmp_path, "audit", {"name": "audit", "description": "审计"}, "result = 1", {"k": "v"})
        names = list_workflows(tmp_path)["saved"]
        assert "audit" in names
        wf = load_workflow(tmp_path, "audit")
        assert wf["script"] == "result = 1"
        assert wf["meta"]["description"] == "审计"
        assert wf["args"] == {"k": "v"}

    def test_save_overwrites_existing(self, tmp_path):
        save_workflow(tmp_path, "a", {"name": "a"}, "v1", None)
        save_workflow(tmp_path, "a", {"name": "a"}, "v2", None)
        assert load_workflow(tmp_path, "a")["script"] == "v2"

    def test_load_missing_raises_keyerror(self, tmp_path):
        with pytest.raises(KeyError):
            load_workflow(tmp_path, "nope")

    def test_list_empty_root(self, tmp_path):
        assert list_workflows(tmp_path) == {"saved": [], "runs": []}

    def test_new_run_dir_writes_script_and_meta(self, tmp_path):
        rd = new_run_dir(tmp_path, {"name": "audit"}, "result = 1", {"x": 1})
        assert (rd / "script.py").read_text(encoding="utf-8") == "result = 1"
        meta = json.loads((rd / "meta.json").read_text(encoding="utf-8"))
        assert meta["meta"]["name"] == "audit" and meta["args"] == {"x": 1}
        assert rd.parent.name == "runs"


class _SeqEcho:
    """最小 manager：把 prompt 原样返回，验证 from_saved + args 覆盖链路。"""

    def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
        on_finished(task_id, task_description)
        return True

    def cancel_task(self, task_id):
        return True


class TestImplActions:
    """impl 的 action 分发：list/load/save 无需 manager 即可应答。"""

    def _call(self, monkeypatch, tmp_path, **kwargs):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        ctx = {"sub_agent_manager": None, "session_id": "s1"}
        kwargs.setdefault("meta", {"name": "a", "description": "d"})
        return wt._workflow_impl(ctx, **kwargs)

    def test_list_action(self, monkeypatch, tmp_path):
        save_workflow(tmp_path, "a1", {"name": "a1"}, "result = 1", None)
        r = self._call(monkeypatch, tmp_path, action="list")
        assert r.success and "a1" in r.content

    def test_load_action(self, monkeypatch, tmp_path):
        save_workflow(tmp_path, "a1", {"name": "a1"}, "result = 1", {"k": 1})
        r = self._call(monkeypatch, tmp_path, action="load", name="a1")
        assert r.success and "result = 1" in r.content

    def test_save_action(self, monkeypatch, tmp_path):
        r = self._call(monkeypatch, tmp_path, action="save", save_as="my-wf", script="result = 2")
        assert r.success
        assert load_workflow(tmp_path, "my-wf")["script"] == "result = 2"

    def test_save_requires_save_as(self, monkeypatch, tmp_path):
        r = self._call(monkeypatch, tmp_path, action="save", script="result = 2")
        assert not r.success and "save_as" in r.error

    def test_load_missing_name_fails(self, monkeypatch, tmp_path):
        r = self._call(monkeypatch, tmp_path, action="load", name="ghost")
        assert not r.success

    def test_from_saved_overrides_args(self, monkeypatch, tmp_path):
        from plugins.workflow.tools import workflow_tool as wt

        save_workflow(
            tmp_path,
            "echo",
            {"name": "echo", "description": "回声"},
            "result = {'msg': args['msg'], 'r': agent(args['msg'])}",
            {"msg": "默认"},
        )
        monkeypatch.setattr(wt, "wf_root", lambda: tmp_path)
        monkeypatch.setattr(wt.PluginConfigStore, "get", lambda self, plugin, key: None)
        ctx = {"sub_agent_manager": _SeqEcho(), "session_id": "s1"}
        r = wt._workflow_impl(ctx, from_saved="echo", args={"msg": "覆盖"}, foreground=True)
        assert r.success
        assert json.loads(r.content)["result"]["msg"] == "覆盖"
