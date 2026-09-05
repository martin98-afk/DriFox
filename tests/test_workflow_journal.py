# -*- coding: utf-8 -*-
"""RunJournal 单测：journal.jsonl 落盘 / 指纹 / completed_map / status.json 原子写。"""
import json
import threading

from plugins.workflow.tools.workflow_tool import RunJournal


class TestRunJournal:
    def test_append_and_fingerprint(self, tmp_path):
        j = RunJournal(tmp_path)
        j.record_phase("plan", "读文档")
        j.record_phase("build", None)
        fp = j.fingerprint("p", "build", "m1", None)
        j.record_agent_start("a1", fp, "build", "m1")
        j.record_agent_end("a1", "done", 1.5, "结果文本")

        lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4
        recs = [json.loads(x) for x in lines]
        assert recs[0]["type"] == "phase" and recs[0]["detail"] == "读文档"
        assert recs[1]["detail"] is None
        assert recs[2]["type"] == "agent_start" and recs[2]["fingerprint"] == fp
        assert recs[3]["type"] == "agent_end" and recs[3]["result"] == "结果文本"
        # seq 单调
        assert [r["seq"] for r in recs] == [1, 2, 3, 4]

    def test_fingerprint_stable_and_sensitive(self, tmp_path):
        j = RunJournal(tmp_path)
        fp1 = j.fingerprint("p", "build", "m1", None)
        assert j.fingerprint("p", "build", "m1", None) == fp1
        assert j.fingerprint("p2", "build", "m1", None) != fp1  # prompt 变 → 变
        assert j.fingerprint("p", "review", "m1", None) != fp1  # 角色变 → 变
        assert j.fingerprint("p", "build", "m2", None) != fp1  # model 变 → 变
        assert j.fingerprint("p", "build", "m1", {"type": "object"}) != fp1  # schema 变 → 变

    def test_completed_map(self, tmp_path):
        j = RunJournal(tmp_path)
        j.record_agent_start("a1", "fp1", "build", None)
        j.record_agent_end("a1", "done", 1.0, "旧结果")
        j.record_agent_start("a2", "fp2", "build", None)
        # a2 未 end → 不在 completed_map
        cm = RunJournal(tmp_path).completed_map()
        assert cm["a1"]["result"] == "旧结果" and cm["a1"]["status"] == "done"
        assert "a2" not in cm

    def test_completed_map_missing_file(self, tmp_path):
        assert RunJournal(tmp_path).completed_map() == {}

    def test_write_status_atomic(self, tmp_path):
        j = RunJournal(tmp_path)
        j.write_status({"phases": [], "agents": [], "state": "running"})
        assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["state"] == "running"
        assert not list(tmp_path.glob("status.json.tmp*"))  # 无残留临时文件

    def test_thread_safety(self, tmp_path):
        j = RunJournal(tmp_path)

        def worker(n):
            for k in range(20):
                j.append("log", msg=f"{n}-{k}")

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 80
        seqs = [json.loads(x)["seq"] for x in lines]
        assert len(set(seqs)) == 80  # seq 无重复
