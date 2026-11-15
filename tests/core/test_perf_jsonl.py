# -*- coding: utf-8 -*-
"""perf_jsonl 公共件单测（观测线批次 B0）。

锁的是工具契约，不是性能数字：
- 并发追加：多线程各写 N 行，总行数 = 2N 且每行都是合法 JSON（不撕裂）
- 坏行鲁棒：文件已有损坏行时继续追加不受影响，坏行原样保留
- 零侵入：新进程仅 import 不调用，不产生任何文件
- env 门控：布尔读取口径一致
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.infra import perf_jsonl  # noqa: E402


@pytest.fixture()
def isolated_cwd(tmp_path, monkeypatch):
    """CWD 切到 tmp_path：JSONL 落点随之隔离，不污染真实 .perf_out。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_concurrent_append_keeps_every_line_intact(isolated_cwd):
    threads_n, per_thread = 8, 25

    def _writer(tid: int) -> None:
        for i in range(per_thread):
            perf_jsonl.append_event("b0.concurrent", i, {"tid": tid})

    workers = [threading.Thread(target=_writer, args=(t,)) for t in range(threads_n)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    lines = perf_jsonl.trace_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == threads_n * per_thread
    for line in lines:
        rec = json.loads(line)  # 任一行被写坏即抛 JSONDecodeError
        assert rec["proc"] == "main"
        assert rec["metric"] == "b0.concurrent"
        assert rec["tags"]["tid"] in range(threads_n)
        assert datetime.fromisoformat(rec["ts"]).tzinfo is not None


def test_append_survives_existing_corrupt_line(isolated_cwd):
    path = perf_jsonl.trace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"broken": not json\n', encoding="utf-8", newline="\n")

    perf_jsonl.append_event("b0.after_corrupt", 1)
    perf_jsonl.append_event("b0.after_corrupt", 2)

    raw = path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 3
    assert raw[0].startswith('{"broken"')  # 坏行原样保留，未被吞/覆写
    good = [json.loads(line) for line in raw[1:]]
    assert [rec["value"] for rec in good] == [1, 2]


def test_import_only_creates_nothing(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    code = "import app.core.infra.perf_jsonl as m; print(m.trace_path())"
    env = dict(os.environ, PYTHONPATH=str(_ROOT), QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (cwd / ".perf_out").exists()
    assert list(cwd.iterdir()) == []


def test_env_flag_truth_table(monkeypatch):
    for truthy in ("1", "true", "TRUE", "on", "yes", " on "):
        monkeypatch.setenv("DRIFOX_B0_FLAG", truthy)
        assert perf_jsonl.env_flag("DRIFOX_B0_FLAG") is True, truthy
    for falsy in ("", "0", "false", "no", "off", "2"):
        monkeypatch.setenv("DRIFOX_B0_FLAG", falsy)
        assert perf_jsonl.env_flag("DRIFOX_B0_FLAG") is False, falsy
    monkeypatch.delenv("DRIFOX_B0_FLAG", raising=False)
    assert perf_jsonl.env_flag("DRIFOX_B0_FLAG") is False
