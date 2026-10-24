# -*- coding: utf-8 -*-
"""process.start 能力：catalog 声明、双面可取、启动真实进程、参数校验与审计。"""
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(declared, surface="ui"):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered(surface)
    host = SimpleNamespace()  # process.start 与 host 无关，任意对象即可
    return CapabilityRegistry.get_instance().build_pool(
        surface=surface, host=host, plugin="demo", declared=declared
    )


def _sleep_cmd(seconds: float):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


# ──────────────── catalog 声明 ────────────────


def test_catalog_declares_process_start_both_surfaces():
    from app.plugins.capabilities import catalog
    from app.plugins.contracts.capabilities import CapabilityLevel

    spec = catalog.get("process.start")
    assert spec is not None
    assert spec.level is CapabilityLevel.HIGH
    assert spec.surfaces == frozenset({"ui", "tool"})


# ──────────────── 双面可取 ────────────────


@pytest.mark.parametrize("surface", ["ui", "tool"])
def test_reachable_on_both_surfaces(surface):
    pool = _pool({"process.start"}, surface=surface)
    assert "process.start" in pool
    assert callable(pool["process.start"])


def test_denied_when_not_declared():
    from app.plugins.capabilities.pool import CapabilityDenied

    pool = _pool(set())
    with pytest.raises(CapabilityDenied):
        pool["process.start"]


# ──────────────── 参数校验 ────────────────


def test_rejects_string_cmd():
    """字符串命令一律拒绝：杜绝 shell 注入面，调用方必须自己拆 argv。"""
    start = _pool({"process.start"})["process.start"]
    with pytest.raises(TypeError):
        start("magpie web")
    with pytest.raises(TypeError):
        start([])
    with pytest.raises(TypeError):
        start(None)


def test_missing_executable_raises():
    start = _pool({"process.start"})["process.start"]
    with pytest.raises(OSError):
        start(["__definitely_not_an_executable_xyz__", "--flag"], name="nope")


# ──────────────── 真实进程 ────────────────


def test_starts_real_process_with_env_and_cwd(tmp_path):
    marker = tmp_path / "env.txt"
    start = _pool({"process.start"})["process.start"]
    script = (
        "import os;"
        f"open(r'{marker}', 'w').write(os.environ.get('PX_START_TEST', 'missing'));"
        "import time; time.sleep(5)"
    )
    info = start(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env={"PX_START_TEST": "injected"},
        name="probe",
    )
    assert isinstance(info, dict)
    assert info["pid"] > 0
    assert info["name"] == "probe"

    try:
        proc = None
        for _ in range(50):  # 等子进程写标记文件（最多 5s）
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "子进程未在超时内写出标记文件"
        assert marker.read_text(encoding="utf-8") == "injected"
        assert _pid_alive(info["pid"]), "启动的进程应存活"
    finally:
        _kill(info["pid"])


def test_start_without_optional_kwargs():
    start = _pool({"process.start"})["process.start"]
    info = start(_sleep_cmd(3), name="bare")
    try:
        assert info["pid"] > 0
        assert _pid_alive(info["pid"])
    finally:
        _kill(info["pid"])


# ──────────────── 工具函数 ────────────────


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            import signal

            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
