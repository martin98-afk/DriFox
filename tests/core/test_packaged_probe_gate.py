# -*- coding: utf-8 -*-
"""诊断设施打包版门卫回归测试。

锁的契约：**诊断设施不随发布版流出**。打包版（sys.frozen / sys._MEIPASS）
即便设置了对应环境变量，也不得起线程、不得注册 gc 回调、不得落盘、不得
触发强退。源码运行态保持原行为（对照组），避免把自用诊断一起关掉。

门卫是模块加载时求值的（_startup_probe._ENABLED、card_viewers._RENDER_PROBE），
所以必须在 import 之前注入 sys.frozen —— 用子进程跑，不污染主测试进程的
模块缓存与 sys 状态。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, tmp_path: Path, **env_overrides: str) -> subprocess.CompletedProcess:
    """在隔离 CWD 的子进程里跑 code；env 覆盖由调用方给。"""
    env = dict(os.environ, PYTHONPATH=str(_ROOT), QT_QPA_PLATFORM="offscreen")
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.mark.parametrize("frozen_setup", [
    "sys.frozen = True",
    "sys._MEIPASS = '/fake/meipass'",
])
def test_env_flag_forced_off_when_packaged(tmp_path, frozen_setup):
    """打包版 env_flag 恒 False：CPU/GC/HANDLE/GOVERNOR/RENDER 等全被收口。"""
    code = (
        f"import sys\n{frozen_setup}\n"
        "from app.core.infra.telemetry import perf_jsonl as m\n"
        "print(m.env_flag('DRIFOX_TEST_FLAG'))"
    )
    proc = _run(code, tmp_path, DRIFOX_TEST_FLAG="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_env_flag_still_works_in_source(tmp_path):
    """对照组：源码运行态不被误关。"""
    code = (
        "from app.core.infra.telemetry import perf_jsonl as m\n"
        "print(m.env_flag('DRIFOX_TEST_FLAG'))"
    )
    proc = _run(code, tmp_path, DRIFOX_TEST_FLAG="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True"


@pytest.mark.parametrize("frozen_setup", [
    "sys.frozen = True",
    "sys._MEIPASS = '/fake/meipass'",
])
def test_startup_traces_not_mounted_when_packaged(tmp_path, frozen_setup):
    """CPU/GC trace 打包版不起线程、不注册 gc.callbacks。"""
    code = (
        "import sys, gc, threading\n"
        f"{frozen_setup}\n"
        "from app.core.infra.telemetry.startup import start_thread_dumper, start_gc_tracer\n"
        "t0, g0 = threading.active_count(), len(gc.callbacks)\n"
        "t = start_thread_dumper(interval_s=999)\n"
        "cb = start_gc_tracer()\n"
        "print(t is None, cb is None, threading.active_count() == t0, len(gc.callbacks) == g0)"
    )
    proc = _run(
        code, tmp_path,
        DRIFOX_CPU_TRACE="1", DRIFOX_GC_TRACE="1",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True True True True"


@pytest.mark.parametrize("frozen_setup", [
    "sys.frozen = True",
    "sys._MEIPASS = '/fake/meipass'",
])
def test_startup_probe_disabled_when_packaged(tmp_path, frozen_setup):
    """_startup_probe 打包版：_ENABLED/trace_enabled 恒 False，mark/dump 无输出、不强退。"""
    code = (
        "import sys, io, contextlib\n"
        f"{frozen_setup}\n"
        "from app.utils import _startup_probe as s\n"
        "s.mark('x'); s.mark_mem('y')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    s.dump()\n"
        "print(s._ENABLED, s.trace_enabled(), repr(buf.getvalue()))"
    )
    proc = _run(
        code, tmp_path,
        DRIFOX_STARTUP_PROBE="1", DRIFOX_STARTUP_PROBE_EXIT_MS="50",
    )
    # EXIT_MS 若被触发，os._exit(50) 会让 returncode 非 0 / stdout 丢失
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False False ''"


def test_startup_probe_still_enabled_in_source(tmp_path):
    """对照组：源码运行态 STARTUP_PROBE=1 仍能开。"""
    code = (
        "from app.utils import _startup_probe as s\n"
        "print(s._ENABLED, s.trace_enabled())"
    )
    proc = _run(code, tmp_path, DRIFOX_STARTUP_PROBE="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True True"
