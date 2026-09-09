# -*- coding: utf-8 -*-
"""plugin-marketplace installer git 超时回归测试

背景：_sparse_clone 的 git 子进程原本无任何超时，网络半开/代理失效时
git 无限等待，worker 线程 fn 永不返回 → 市场行永远显示「安装中…」。

修复语义：
- git 命令带 http.lowSpeedLimit/lowSpeedTime 传输停滞自断配置
- 每条 git 命令有总超时兜底，超时杀整棵进程树并抛 CalledProcessError
"""

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


@pytest.fixture
def silent_git_server():
    """accept 后永不响应的假 git 服务器（模拟网络半开/代理黑洞）"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    held = []

    def _loop():
        while True:
            try:
                conn, _ = srv.accept()
                held.append(conn)  # 只 accept，不读不写不关
            except OSError:
                return

    th = threading.Thread(target=_loop, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/fake-plugin.git"
    srv.close()
    for c in held:
        try:
            c.close()
        except OSError:
            pass


def _make_installer():
    from ui.installer import PluginInstaller

    return PluginInstaller.__new__(PluginInstaller)  # 只测 _sparse_clone，不触发 __init__


def test_sparse_clone_timeout_kills_hung_git(silent_git_server, tmp_path):
    """git 挂死时必须在总超时内抛 CalledProcessError，而非无限阻塞"""
    from ui.installer import PluginInstaller

    inst = _make_installer()
    cache = tmp_path / "clone_target"

    t0 = time.time()
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        inst._sparse_clone(silent_git_server, ".", "main", cache, timeout=5)
    elapsed = time.time() - t0

    assert elapsed < 30, f"超时后仍阻塞 {elapsed:.1f}s，进程树未被杀干净"
    assert "已强制终止" in (exc_info.value.stderr or ""), "错误信息应注明 git 命令超时被终止"


def test_sparse_clone_stall_args_in_clone_cmd(monkeypatch, tmp_path):
    """clone 命令必须携带 lowSpeedLimit/lowSpeedTime 传输停滞自断配置"""
    import ui.installer as installer_mod

    captured = {}

    class _FakeProc:
        def __init__(self):
            self.pid = 0
            self.returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(installer_mod.subprocess, "Popen", _fake_popen)

    inst = _make_installer()
    inst._sparse_clone("https://example.com/x.git", ".", "main", tmp_path / "t")

    cmd = captured["cmd"]
    keys = [a for a in cmd if isinstance(a, str) and a.startswith("http.lowSpeed")]
    assert "http.lowSpeedLimit=1000" in keys and "http.lowSpeedTime=30" in keys


def test_sparse_clone_stall_args_in_sparse_checkout_cmd(monkeypatch, tmp_path):
    """sparse-checkout 子命令同样必须携带停滞自断配置（blob 按需下载走网络）"""
    import ui.installer as installer_mod

    cmds = []

    class _FakeProc:
        def __init__(self):
            self.pid = 0
            self.returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def _fake_popen(cmd, **kwargs):
        cmds.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(installer_mod.subprocess, "Popen", _fake_popen)

    inst = _make_installer()
    sub = tmp_path / "sparse_target"
    sub.mkdir()
    inst._sparse_clone("https://example.com/x.git", "plugins/foo", "main", sub)

    assert len(cmds) == 2
    assert any(
        a.startswith("http.lowSpeedTime") for a in cmds[1]
    ), "sparse-checkout set 命令缺少停滞自断配置"


def test_sparse_clone_normal_failure_keeps_stderr(monkeypatch, tmp_path):
    """非超时失败路径行为不变：CalledProcessError 携带 git 原始 stderr"""
    import ui.installer as installer_mod

    class _FakeProc:
        def __init__(self):
            self.pid = 0
            self.returncode = 128

        def communicate(self, timeout=None):
            return "", "fatal: repository 'https://example.com/x.git/' not found"

    monkeypatch.setattr(
        installer_mod.subprocess, "Popen", lambda cmd, **kw: _FakeProc()
    )

    inst = _make_installer()
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        inst._sparse_clone("https://example.com/x.git", ".", "main", tmp_path / "t")

    assert "not found" in (exc_info.value.stderr or "")
