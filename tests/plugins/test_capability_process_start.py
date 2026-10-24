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
    from app.plugins.capabilities.process_registry import ProcessRegistry
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    ProcessRegistry.reset_instance()
    yield
    # 兑底：回收测试期间启动的真实进程（demo 及其他插件名）
    try:
        ProcessRegistry.get_instance().kill_all()
    except Exception:
        pass
    ProcessRegistry.reset_instance()
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(declared, surface="ui"):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered(surface)
    host = SimpleNamespace()  # process.start 与 host 无关，任意对象即可
    return CapabilityRegistry.get_instance().build_pool(surface=surface, host=host, plugin="demo", declared=declared)


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
        f"import os;open(r'{marker}', 'w').write(os.environ.get('PX_START_TEST', 'missing'));import time; time.sleep(5)"
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


# ──────────────── 台账与限额 ────────────────


def test_registry_limit_per_plugin():
    """per-plugin 上限 8：第 9 个启动被拒，杀掉后限额恢复。"""
    from app.plugins.capabilities.process_registry import ProcessRegistry

    registry = ProcessRegistry.get_instance()
    start = _pool({"process.start"})["process.start"]
    pids = []
    try:
        for i in range(8):
            info = start(_sleep_cmd(30), name=f"p{i}")
            pids.append(info["pid"])
        with pytest.raises(RuntimeError, match="上限"):
            start(_sleep_cmd(30), name="overflow")
        assert registry.count_for("demo") == 8
    finally:
        registry.kill_plugin("demo")
    assert registry.count_for("demo") == 0
    for pid in pids:
        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        assert not _pid_alive(pid), f"pid={pid} 应被 kill_plugin 树杀"


def test_registry_limit_global():
    """全局上限 32：混多插件名填满后，任何插件再启动均被拒。"""
    from unittest.mock import MagicMock

    from app.plugins.capabilities.process_registry import ProcessRecord, ProcessRegistry

    registry = ProcessRegistry.get_instance()
    plugins = set()
    try:
        for i in range(32):
            name = f"plug{i % 4}"
            plugins.add(name)
            proc = MagicMock()
            proc.poll.return_value = None
            registry.register(
                ProcessRecord(
                    plugin=name,
                    name=f"n{i}",
                    pid=100000 + i,
                    argv0="x",
                    started_at=0.0,
                    proc=proc,
                )
            )
        with pytest.raises(RuntimeError, match="全局上限"):
            registry.check_limit("fresh-plugin")
    finally:
        for name in plugins:
            registry.kill_plugin(name)


def test_kill_plugin_terminates_tree():
    """kill_plugin 树杀名下进程：pid 死亡 + 台账清空 + 幂等。"""
    from app.plugins.capabilities.process_registry import ProcessRegistry

    registry = ProcessRegistry.get_instance()
    start = _pool({"process.start"})["process.start"]
    info = start(_sleep_cmd(30), name="victim")
    assert _pid_alive(info["pid"])
    assert registry.kill_plugin("demo") == 1
    deadline = time.time() + 5
    while time.time() < deadline and _pid_alive(info["pid"]):
        time.sleep(0.1)
    assert not _pid_alive(info["pid"])
    assert registry.count_for("demo") == 0
    assert registry.kill_plugin("demo") == 0  # 幂等


def test_kill_all_clears_everything():
    from unittest.mock import MagicMock

    from app.plugins.capabilities.process_registry import ProcessRecord, ProcessRegistry

    registry = ProcessRegistry.get_instance()
    start = _pool({"process.start"})["process.start"]
    start(_sleep_cmd(30), name="a")
    proc = MagicMock()
    proc.poll.return_value = None
    registry.register(ProcessRecord(plugin="other", name="b", pid=100001, argv0="x", started_at=0.0, proc=proc))
    assert registry.kill_all() == 2
    assert registry.snapshot() == []


# ──────────────── env 白名单 ────────────────


def test_env_whitelist_blocks_host_secret(tmp_path, monkeypatch):
    """宿主非白名单变量不进子进程环境；插件增量注入生效；白名单 PATH 可见。"""
    import json

    from app.plugins.capabilities.process_registry import ProcessRegistry

    monkeypatch.setenv("PX_HOST_SECRET", "topsecret")
    marker = tmp_path / "env.json"
    start = _pool({"process.start"})["process.start"]
    script = (
        "import os,json;"
        "open(r'" + str(marker) + "', 'w').write(json.dumps({"
        "'secret': os.environ.get('PX_HOST_SECRET', 'missing'), "
        "'injected': os.environ.get('PX_START_TEST', 'missing'), "
        "'path_seen': bool(os.environ.get('PATH'))}));"
        "import time; time.sleep(5)"
    )
    info = start(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env={"PX_START_TEST": "injected"},
        name="envprobe",
    )
    try:
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "子进程未在超时内写出环境探针"
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["secret"] == "missing", "宿主非白名单变量不得泄漏给子进程"
        assert data["injected"] == "injected", "插件增量 env 应生效"
        assert data["path_seen"], "白名单 PATH 应可见"
    finally:
        ProcessRegistry.get_instance().kill_plugin("demo")


# ──────────────── 首启确认 ────────────────


def test_denied_plugin_rejected_for_session():
    """deny 入会话记忆：同会话内后续 start 直接拒绝，不再弹窗（防骚扰）。"""
    from app.plugins.capabilities.process_registry import ProcessRegistry

    registry = ProcessRegistry.get_instance()
    registry.deny("demo")
    start = _pool({"process.start"})["process.start"]
    with pytest.raises(PermissionError, match="拒绝"):
        start(_sleep_cmd(1), name="blocked")
    assert registry.count_for("demo") == 0


def test_headless_auto_approves_once():
    """无 GUI（pytest）跳过确认后 approve 入记忆：approval_state 流转正确。"""
    from app.plugins.capabilities.process_registry import ProcessRegistry

    registry = ProcessRegistry.get_instance()
    assert registry.approval_state("demo") == "pending"
    start = _pool({"process.start"})["process.start"]
    info = start(_sleep_cmd(3), name="headless")
    try:
        assert registry.approval_state("demo") == "approved"
        assert info["pid"] > 0
    finally:
        registry.kill_plugin("demo")


# ──────────────── 生命周期挂接 ────────────────


def test_disable_plugin_kills_registered_processes(monkeypatch):
    """plugin_manager.disable_plugin 挂接 kill_plugin：禁用即回收名下进程。"""
    from app.plugins.capabilities.process_registry import ProcessRegistry
    from app.plugins.managers.plugin_manager import PluginManager

    registry = ProcessRegistry.get_instance()
    start = _pool({"process.start"})["process.start"]
    info = start(_sleep_cmd(30), name="victim")
    assert _pid_alive(info["pid"])

    pm = PluginManager.__new__(PluginManager)
    monkeypatch.setattr(pm, "_plugins", {"demo": SimpleNamespace(name="demo")}, raising=False)
    monkeypatch.setattr(pm, "_NON_DISABLEABLE", set(), raising=False)
    monkeypatch.setattr(pm, "_get_enabled_set", lambda: {"demo"})
    monkeypatch.setattr(pm, "_get_disabled_set", lambda: set())
    monkeypatch.setattr(pm, "_save_enabled_set", lambda s: None)
    monkeypatch.setattr(pm, "_save_disabled_set", lambda s: None)
    monkeypatch.setattr(pm, "invalidate_mcp_cache", lambda: None)
    monkeypatch.setattr(pm, "_unload_plugin_ui", lambda n: None)
    monkeypatch.setattr(pm, "_rescan_plugin_tools", lambda n, enabled: None)
    monkeypatch.setattr(pm, "_trigger_plugin_changed_hook", lambda *a, **k: None)

    pm.disable_plugin("demo")

    deadline = time.time() + 5
    while time.time() < deadline and _pid_alive(info["pid"]):
        time.sleep(0.1)
    assert not _pid_alive(info["pid"]), "禁用插件后其后台进程应被回收"
    assert registry.count_for("demo") == 0


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
