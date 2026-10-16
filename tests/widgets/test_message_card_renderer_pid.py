# -*- coding: utf-8 -*-
"""B4 强回收链路通电（批 I）回归测试。

覆盖四项修复的 Python 侧不变量：
1. MessageCard._renderer_pid property 转发 viewer（card 层历史上只清零不转发，
   导致 _unloaded_pids 恒空、强回收从未生效）；
2. _kill_renderer 平台感知判定（p.name() 前缀匹配 + --type=renderer 硬判据，
   拒绝 gpu-process等同型子进程）；
3. finish_streaming 落定清零 _stream_text_acc（流式全文副本残留）；
4. closeEvent 释放循环跳过 async flush 的 pending 会话（TOCTOU 写空窗口）。

测试环境无法创建真实 QWebEngineView（offscreen + stub viewer 惯例），
kill 链路用 fake psutil 模块验证判定逻辑本身。
运行::
    python -m pytest tests/widgets/test_message_card_renderer_pid.py -v
"""

import inspect
import sys
import types
from pathlib import Path

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.main_widget import OpenAIChatToolWindow
from app.widgets.message_card import MessageCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubViewer:
    """最小 viewer 桩：只携带 _renderer_pid 属性（property 转发验证用）。"""

    def __init__(self, pid: int = 0):
        self._renderer_pid = pid


# ─────────────────────────────────────────────
# 1/2/3：_renderer_pid property 转发 + 缓存兜底
# ─────────────────────────────────────────────


def test_renderer_pid_forwards_to_alive_viewer(qapp):
    """viewer 存活时 card._renderer_pid == viewer._renderer_pid。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        viewer = _StubViewer(pid=12345)
        card.viewer = viewer
        assert card._renderer_pid == 12345
        # viewer 侧更新后转发读取跟随
        viewer._renderer_pid = 54321
        assert card._renderer_pid == 54321
    finally:
        card.deleteLater()


def test_renderer_pid_zero_after_viewer_detached(qapp):
    """viewer 置 None 后 card._renderer_pid == 0（无缓存兜底时）。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        card.viewer = _StubViewer(pid=999)
        assert card._renderer_pid == 999
        card.viewer = None
        assert card._renderer_pid == 0
    finally:
        card.deleteLater()


def test_renderer_pid_setter_caches_fallback(qapp):
    """setter 写入后 getter 读到缓存值（cleanup 清零语义 = 缓存兜底）。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        # viewer 在场时 setter 也要生效（cache 写入，getter 仍转发 viewer）
        card.viewer = _StubViewer(pid=111)
        card._renderer_pid = 222
        assert card.__dict__["_renderer_pid_cache"] == 222
        # viewer 摘除后：getter 回落缓存值（cleanup 清零后可读 0）
        card.viewer = None
        assert card._renderer_pid == 222
        # cleanup 的清零路径：写入 0 → 缓存 0 → getter 读 0
        card._renderer_pid = 0
        assert card._renderer_pid == 0
        # None/负值防御：int(value or 0)
        card._renderer_pid = None
        assert card._renderer_pid == 0
    finally:
        card.deleteLater()


def test_cleanup_resets_pid_cache_not_break_forwarding(qapp):
    """cleanup 内既有的 self._renderer_pid = 0 走 setter，语义不变。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        card.viewer = _StubViewer(pid=777)
        card.cleanup()
        # cleanup 后 viewer=None + PID 清零（缓存 0）
        assert card.viewer is None
        assert card._renderer_pid == 0
    finally:
        card.deleteLater()


# ─────────────────────────────────────────────
# 4：_kill_renderer 平台感知判定（fake psutil）
# ─────────────────────────────────────────────


class _FakeProcess:
    """psutil.Process 桩：按注册表返回，记录 terminate/kill 调用。"""

    instances = {}  # pid -> spec dict
    created = {}  # pid -> _FakeProcess 实例（断言用）

    def __init__(self, pid):
        self.pid = pid
        spec = _FakeProcess.instances.get(pid)
        if spec is None:
            raise _FakeNoSuchProcess()  # 未注册 pid 视为不存在
        self._name = spec["name"]
        self._cmdline = spec["cmdline"]
        self.terminated = False
        self.killed = False
        _FakeProcess.created[pid] = self

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class _FakeTimeoutExpired(Exception):
    pass


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


@pytest.fixture()
def fake_psutil():
    """注入 fake psutil 模块（测试环境未安装 psutil）。"""
    mod = types.ModuleType("psutil")
    mod.Process = _FakeProcess
    mod.TimeoutExpired = _FakeTimeoutExpired
    mod.NoSuchProcess = _FakeNoSuchProcess
    mod.AccessDenied = _FakeAccessDenied
    saved = sys.modules.get("psutil")
    sys.modules["psutil"] = mod
    yield mod
    if saved is None:
        sys.modules.pop("psutil", None)
    else:
        sys.modules["psutil"] = saved
    _FakeProcess.instances = {}
    _FakeProcess.created = {}


def test_kill_renderer_rejects_gpu_process(fake_psutil):
    """--type=gpu-process 同名进程：拒绝 kill（防全卡黑屏）。"""
    _FakeProcess.instances[101] = {
        "name": "QtWebEngineProcess",
        "cmdline": ["/path/QtWebEngineProcess", "--type=gpu-process", "--x"],
    }
    assert OpenAIChatToolWindow._kill_renderer(101) is False
    assert _FakeProcess.created[101].terminated is False


def test_kill_renderer_accepts_renderer_process(fake_psutil):
    """--type=renderer：mac/Linux 无 .exe 后缀也要 kill（平台感知主目标）。"""
    _FakeProcess.instances[102] = {
        "name": "QtWebEngineProcess",
        "cmdline": ["/path/QtWebEngineProcess", "--type=renderer"],
    }
    assert OpenAIChatToolWindow._kill_renderer(102) is True
    assert _FakeProcess.created[102].terminated is True


def test_kill_renderer_accepts_windows_exe(fake_psutil):
    """Windows QtWebEngineProcess.exe：照常 kill。"""
    _FakeProcess.instances[103] = {
        "name": "QtWebEngineProcess.exe",
        "cmdline": ["C:\\x\\QtWebEngineProcess.exe", "--type=renderer"],
    }
    assert OpenAIChatToolWindow._kill_renderer(103) is True


def test_kill_renderer_rejects_non_qt_process(fake_psutil):
    """非 QtWebEngineProcess 进程（PID 复用）：拒绝 kill。"""
    _FakeProcess.instances[104] = {
        "name": "python3.14",
        "cmdline": ["python3.14", "--type=renderer"],
    }
    assert OpenAIChatToolWindow._kill_renderer(104) is False


def test_kill_renderer_cmd_substring_not_matched(fake_psutil):
    """cmd 路径含同名字样但进程名不同：不误判（name() 而非 cmd 子串）。"""
    _FakeProcess.instances[105] = {
        "name": "python3.14",
        "cmdline": ["python3.14", "/tools/QtWebEngineProcess_helper.py", "--type=renderer"],
    }
    assert OpenAIChatToolWindow._kill_renderer(105) is False


def test_kill_renderer_invalid_pid(fake_psutil):
    """pid<=0：直接拒绝，不触 psutil。"""
    assert OpenAIChatToolWindow._kill_renderer(0) is False
    assert OpenAIChatToolWindow._kill_renderer(-5) is False


def test_kill_renderer_no_such_process(fake_psutil):
    """进程已不存在：静默 False（不抛异常）。"""
    assert OpenAIChatToolWindow._kill_renderer(999999) is False


# ─────────────────────────────────────────────
# 5：finish_streaming 清零 _stream_text_acc
# ─────────────────────────────────────────────


def test_finish_streaming_clears_stream_text_acc(qapp):
    """finish_streaming 落定后 _stream_text_acc == ""（流式全文副本不残留）。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        card._stream_text_acc = "x" * 1024
        card.finish_streaming()
        assert card._stream_text_acc == ""
    finally:
        card.deleteLater()


def test_finish_streaming_clears_acc_history_path(qapp):
    """history=True（历史加载收尾）路径同样清零。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        card._stream_text_acc = "y" * 512
        card.finish_streaming(history=True)
        assert card._stream_text_acc == ""
    finally:
        card.deleteLater()


# ─────────────────────────────────────────────
# 6：closeEvent 释放循环跳过 pending 会话（TOCTOU）
# ─────────────────────────────────────────────


def test_close_event_pending_skip_structure():
    """源码结构断言：pending 读取在 flush 调用之前 + 循环含跳过分支。

    顺序不变量是本修复的核心：flush_async 主线程段第一行即清
    _pending_save_session_id，循环处再读恒为 None → 跳过失效。
    """
    src = inspect.getsource(OpenAIChatToolWindow.closeEvent)
    read_pos = src.find("_pending_flush = getattr(")
    flush_pos = src.find('_auto_save_current_session(flush_mode="async")')
    assert read_pos != -1, "closeEvent 缺少 _pending_flush 捕获"
    assert flush_pos != -1, "closeEvent 缺少 async flush 调用"
    assert read_pos < flush_pos, "pending 读取必须先于 flush 调用（flush 会清 pending id）"
    # 跳过分支存在
    assert "if _pending_flush and _sid == _pending_flush:" in src
    # release 仍走 release_messages_only=True
    assert "remove_session(_sid, release_messages_only=True)" in src


def test_pending_skip_semantics(qapp):
    """行为语义：与 closeEvent 内联循环同构的释放序列，pending 命中的会话不被释放。

    注：循环逻辑内联在 closeEvent 中无法单独调用，此处以同构循环 + fake 对象
    验证语义；源码结构（先读后调 + 跳过分支）由 test_close_event_pending_skip_structure 保证。
    """
    released = []

    class _FakeHM:
        _pending_save_session_id = "sess-B"

        def remove_session(self, sid, release_messages_only=True):
            released.append(sid)

    class _S:
        def __init__(self, sid):
            self.session_id = sid

    hm = _FakeHM()
    sessions = [_S("sess-A"), _S("sess-B"), _S("sess-C")]
    # 与 closeEvent 相同的读取时机：flush 前（此处以 flush 前读取模拟）
    pending_flush = getattr(hm, "_pending_save_session_id", None)
    # flush_async 主线程段行为：清空 pending（不改变已捕获值）
    hm._pending_save_session_id = None
    for _s in sessions:
        _sid = getattr(_s, "session_id", None)
        if not _sid or not hm:
            continue
        if pending_flush and _sid == pending_flush:
            continue
        hm.remove_session(_sid, release_messages_only=True)
    assert released == ["sess-A", "sess-C"], "pending 会话必须被跳过"
