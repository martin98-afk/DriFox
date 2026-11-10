# -*- coding: utf-8 -*-
"""自杀守卫：只拦会终止 DriFox 自身进程的命令

背景：bash 工具链三层都不区分目标是 DriFox 自己还是别人的进程
（工具内只判 block / 沙箱默认关闭 / 即使开启也只弹一次审批）。
本模块的判定永久开启，与 sandbox_enabled 无关。
"""

import os
import sys
import threading
from pathlib import Path

import pytest

from app.tools.self_guard import check_self_kill, check_self_kill_argv, guard_tool_call


def _self_image() -> str:
    return Path(sys.executable).name


def _self_name() -> str:
    return _self_image()[:-4] if _self_image().lower().endswith(".exe") else _self_image()


def _absent_pid() -> int:
    pid = 990001
    while pid == os.getpid() or pid > 99999999:
        pid += 1
    return pid


# ══════════════ 命中：精确 PID ══════════════

@pytest.mark.parametrize(
    "command",
    [
        "taskkill /F /PID {pid}",
        "taskkill /F /PID {pid} /T",
        "taskkill /PID {pid}",
        "tskill {pid}",
        'taskkill /F /FI "PID eq {pid}"',
        'cmd /c "taskkill /F /PID {pid}"',
        'powershell -Command "Stop-Process -Id {pid}"',
        'pwsh -c "Stop-Process -Id {pid} -Force"',
        'wmic process where "processid={pid}" call terminate',
        "kill -9 {pid}",
        "kill {pid}",
        'bash -c "kill -9 {pid}"',
    ],
)
def test_rejects_self_pid(command):
    pid = os.getpid()
    reason = check_self_kill(command.format(pid=pid))
    assert reason, command
    assert str(pid) in reason


# ══════════════ 命中：自身镜像名全杀 ══════════════

@pytest.mark.parametrize(
    "command",
    [
        "taskkill /F /IM {image}",
        "taskkill /F /IM {image} /T",
        'taskkill /F /FI "IMAGENAME eq {image}"',
        "killall {name}",
        "pkill {name}",
        "pkill -x {name}",
        'powershell -Command "Stop-Process -Name {name}"',
        'wmic process where "name=\'{image}\'" call terminate',
        "Get-Process -Name {name} | Stop-Process",
        "Get-Process {name} | Stop-Process -Force",
    ],
)
def test_rejects_self_image_name(command):
    cmd = command.format(image=_self_image(), name=_self_name())
    assert check_self_kill(cmd), cmd


def test_rejects_posix_parent_pid():
    reason = check_self_kill("kill -9 $PPID")
    assert reason
    assert str(os.getpid()) in reason


def test_rejects_pipeline_kill_all():
    # Get-Process 不带目标 → 杀所有进程，必然包含自己
    assert check_self_kill("Get-Process | Stop-Process")


# ══════════════ 放行：其它进程 / 非杀进程命令 ══════════════

@pytest.mark.parametrize(
    "command",
    [
        "taskkill /F /PID {pid}",
        "taskkill /F /IM notepad.exe",
        "taskkill /F /FI \"PID eq {pid}\"",
        "kill -9 {pid}",
        "killall notepad",
        "pkill -f notepad",
        "pkill -f \"pytest tests\"",
        'powershell -Command "Stop-Process -Name notepad"',
        'cmd /c "taskkill /F /IM notepad.exe"',
        'wmic process where "name=\'notepad.exe\'" call terminate',
        "echo hello",
        "git commit -m \"fix kill bug\"",
        "git log --grep=kill",
        "psutil_kill_helper.py",
    ],
)
def test_allows_other_targets(command):
    pid = _absent_pid()
    assert check_self_kill(command.format(pid=pid)) is None, command


def test_empty_command_passes():
    assert check_self_kill("") is None
    assert check_self_kill("   ") is None


# ══════════════ 宽泛层：静态解析不到的动态形态 ══════════════

def test_rejects_dynamic_psutil_kill_by_pid():
    """用户实测绕过：python -c 里用 psutil 按 PID 杀宿主"""
    pid = os.getpid()
    cmd = (
        'python -c "import psutil; p=psutil.Process({pid}); '
        "cmd=' '.join(p.cmdline()); assert 'main.py' in cmd; p.kill()\""
    ).format(pid=pid)
    assert check_self_kill(cmd)


def test_rejects_dynamic_os_kill_getppid():
    assert check_self_kill('python -c "import os; os.kill(os.getppid(), 9)"')


def test_rejects_dynamic_os_kill_getpid():
    assert check_self_kill('python -c "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"')


def test_rejects_bare_psutil_process_kill():
    assert check_self_kill('python -c "import psutil; psutil.Process().kill()"')
    assert check_self_kill('python -c "import psutil; psutil.Process().parent().kill()"')


def test_rejects_kill_intent_with_self_image_name():
    assert check_self_kill(f'python -c "import os; os.system(\'taskkill /F /IM {_self_image()}\')"')


def test_rejects_kill_intent_with_pid_literal():
    assert check_self_kill(f'python -c "import os; os.kill({os.getpid()}, 9)"')


def test_broad_layer_allows_plain_scripts():
    assert check_self_kill('python -c "print(\\"hello\\")"') is None
    assert check_self_kill('python -c "import psutil; print(psutil.Process(999999).name())"') is None
    assert check_self_kill('grep -rn "kill" src/') is None


# ══════════════ 宿主统一闸门：guard_tool_call / argv 形态 ══════════════

def test_guard_tool_call_reads_command_arg():
    reason = guard_tool_call("bash", {"command": f"taskkill /F /PID {os.getpid()}"})
    assert reason
    assert guard_tool_call("bash", {"command": "echo hi"}) is None
    assert guard_tool_call("bash", {}) is None
    assert guard_tool_call("bash", {"command": 123}) is None


def test_guard_tool_call_ignores_tool_name():
    # 命令来自哪个插件/工具都不影响判定
    cmd = f"taskkill /F /PID {os.getpid()}"
    assert guard_tool_call("some_third_party_tool", {"command": cmd})
    assert guard_tool_call("bash", {"command": cmd})


def test_check_self_kill_argv():
    assert check_self_kill_argv(["taskkill", "/F", "/PID", str(os.getpid())])
    assert check_self_kill_argv(["taskkill", "/F", "/IM", _self_image()])
    assert check_self_kill_argv(["taskkill", "/F", "/PID", str(_absent_pid())]) is None
    assert check_self_kill_argv([]) is None


# ══════════════ ToolExecutor：真实入口拦截 ══════════════

def _bare_executor(monkeypatch):
    from app.core.tools import tool_executor as te_mod

    te = te_mod.ToolExecutor.__new__(te_mod.ToolExecutor)
    te._builtin_tools = object()
    te._backend = None
    te._lock = threading.Lock()
    te._session_id = None
    te._call_id = None
    te._workdir = os.getcwd()
    te._file_recorder = None
    monkeypatch.setattr(te_mod.ToolExecutor, "is_valid", lambda self: True)
    return te


def test_tool_executor_blocks_self_kill(monkeypatch):
    te = _bare_executor(monkeypatch)
    result = te.execute("bash", {"command": f"taskkill /F /PID {os.getpid()}"})
    assert result.success is False
    assert "已拒绝" in (result.error or "")


def test_tool_executor_blocks_by_image_name(monkeypatch):
    te = _bare_executor(monkeypatch)
    result = te.execute("bash", {"command": f"taskkill /F /IM {_self_image()}"})
    assert result.success is False
    assert "已拒绝" in (result.error or "")


# ══════════════ 架构守卫：防守必须在宿主层 ══════════════

def test_guard_lives_in_host_not_plugin():
    root = Path(__file__).resolve().parents[2]
    plugin_src = (root / "plugins/system-core/tools/terminal_tools.py").read_text(encoding="utf-8")
    host_src = (root / "app/core/tools/tool_executor.py").read_text(encoding="utf-8")
    # 插件层不得自己接守卫（否则等于让插件守自己，新插件可绕过）
    assert "self_guard" not in plugin_src
    # 宿主统一闸门必须调用
    assert "guard_tool_call" in host_src
