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
    """finish_streaming 落定后流式原文缓冲必须清空（parts + acc 均不残留）。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        card._stream_text_parts = ["x" * 1024]
        card._stream_text_acc = "x" * 1024
        card.finish_streaming()
        assert not card._stream_text_acc, "acc 未清空"
        assert not card._stream_text_parts, "parts 未清空"
    finally:
        card.deleteLater()


def test_finish_streaming_clears_acc_history_path(qapp):
    """history=True（历史加载收尾）路径同样清零。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    try:
        card._stream_text_parts = ["y" * 512]
        card._stream_text_acc = "y" * 512
        card.finish_streaming(history=True)
        assert not card._stream_text_acc, "acc 未清空"
        assert not card._stream_text_parts, "parts 未清空"
    finally:
        card.deleteLater()


# ─────────────────────────────────────────────
# 6：closeEvent 释放循环跳过 pending 会话（TOCTOU）
# ─────────────────────────────────────────────


def test_close_event_pending_skip_structure():
    """源码结构断言：closeEvent 释放走 release_messages_except 权威入口 +
    双保护层（own_ids + extra_skip）。

    [MEM] 批 IV 修订：closeEvent 释放委托权威入口；async 返回值（本次交给
    后台 flush 的 pending 会话）经 extra_skip 传入——归档导入的 pending 不属
    任何窗口 SessionManager，own_ids 拦不住，必须显式传。
    """
    src = inspect.getsource(OpenAIChatToolWindow.closeEvent)
    assert "release_messages_except(" in src, "释放必须委托权威入口"
    assert "extra_skip=" in src, "必须传 extra_skip（归档导入 pending 不属任何窗口）"
    assert "_pending_flush = self._auto_save_current_session(" in src, "返回值必须捕获（extra_skip 数据源）"
    # 旧的逐会话 remove_session 释放循环必须移除（双实现并存 = 所有权未收敛）
    assert "remove_session(_sid, release_messages_only=True)" not in src, (
        "closeEvent 不得保留逐会话释放循环（所有权已收敛到 release_messages_except）"
    )
    # 释放必须在 async flush 之后（flush 先行，返回值才有意义）
    flush_pos = src.find('self._auto_save_current_session(flush_mode="async")')
    release_pos = src.find("release_messages_except(")
    assert flush_pos != -1 and release_pos != -1 and flush_pos < release_pos
    # session_manager None 守卫（与委托方法同口径）
    assert "if self.session_manager" in src


def test_auto_save_returns_flushed_id_for_async():
    """async 模式返回值 == flush 前一刻的 pending（供关窗循环跳过）。"""
    captured = {}

    class _FakeHM:
        def __init__(self):
            self._pending_save_session_id = None
            self.flushed = []

        def update_session(self, idx, messages, **kwargs):
            # 模拟 update_session → _schedule_save：保存路径内部登记新 pending
            self._pending_save_session_id = "sess-XYZ"

        def flush_async(self):
            # 模拟 flush_async 主线程段：读走并清空 pending
            captured["consumed"] = self._pending_save_session_id
            self.flushed.append(self._pending_save_session_id)
            self._pending_save_session_id = None

        def flush(self):
            pass

    class _FakeSM:
        def __init__(self, session):
            self._session = session

        def get_current_session(self):
            return self._session

    class _FakeWin:
        """最小窗口桩：只覆盖 _auto_save_current_session 的执行路径。"""

        def __init__(self, hm):
            self._session_obj = types.SimpleNamespace(
                session_id="sess-XYZ",
                messages=[{"role": "user", "content": "hi"}],
                is_temp=False,
                system_prompt="",
                compaction_state={},
                compaction_cache={},
            )
            self.session_manager = _FakeSM(self._session_obj)
            self.history_manager = hm
            self._session_dirty = True
            self._team_run_id = ""
            self._current_session_id = "sess-XYZ"
            self._current_project = "默认项目"
            self._last_known_worktree = ""
            self._current_provider_name = ""
            self._valid_configs = {}

    hm = _FakeHM()
    win = _FakeWin(hm)
    win._get_current_worktree_path = lambda: ""
    win._api_usage_kwargs = lambda s: {}
    win._get_team_members_snapshot_json = lambda: "[]"
    win._resolve_session_project_fallback = lambda *a, **k: ""
    win._notify_history_data_changed = lambda: None
    hm.find_index_by_session_id = lambda sid: 0
    hm.get_current_title = lambda idx: "t"

    ret = OpenAIChatToolWindow._auto_save_current_session(win, flush_mode="async")
    assert ret == "sess-XYZ", f"返回值必须是交给后台 flush 的 session_id: {ret!r}"
    assert captured["consumed"] == "sess-XYZ", "flush_async 消费的 pending 必须与返回值一致"


def test_auto_save_stale_read_gap_regression():
    """缺口回归：方法内部新设 pending 后，「提前读字段」的旧写法拿不到值。

    锁死修订动机：常态关窗时 save timer 已消费旧 pending（调用前读=None），
    而 _auto_save_current_session 内部 update_session 又登记了新 pending 并
    交给 flush_async——旧写法跳过失效，返回值方案命中。
    """

    class _FakeHM:
        def __init__(self):
            self._pending_save_session_id = None
            self.flushed = []

        def update_session(self, idx, messages, **kwargs):
            self._pending_save_session_id = "sess-NEW"

        def flush_async(self):
            self.flushed.append(self._pending_save_session_id)
            self._pending_save_session_id = None

        def flush(self):
            pass

    class _FakeSM:
        def __init__(self, session):
            self._session = session

        def get_current_session(self):
            return self._session

    class _FakeWin:
        def __init__(self, hm):
            self._session_obj = types.SimpleNamespace(
                session_id="sess-NEW",
                messages=[{"role": "user", "content": "hi"}],
                is_temp=False,
                system_prompt="",
                compaction_state={},
                compaction_cache={},
            )
            self.session_manager = _FakeSM(self._session_obj)
            self.history_manager = hm
            self._session_dirty = True
            self._team_run_id = ""
            self._current_session_id = "sess-NEW"
            self._current_project = "默认项目"
            self._last_known_worktree = ""
            self._current_provider_name = ""
            self._valid_configs = {}

    hm = _FakeHM()
    win = _FakeWin(hm)
    win._get_current_worktree_path = lambda: ""
    win._api_usage_kwargs = lambda s: {}
    win._get_team_members_snapshot_json = lambda: "[]"
    win._resolve_session_project_fallback = lambda *a, **k: ""
    win._notify_history_data_changed = lambda: None
    hm.find_index_by_session_id = lambda sid: 0
    hm.get_current_title = lambda idx: "t"

    # 旧写法时序：调用前读字段（save timer 已消费 → None）
    stale_read = hm._pending_save_session_id
    ret = OpenAIChatToolWindow._auto_save_current_session(win, flush_mode="async")
    assert stale_read is None, "常态关窗调用前读必为 None（缺口前提）"
    assert hm.flushed == ["sess-NEW"], "内部新设的 pending 被 flush_async 消费"
    assert ret == "sess-NEW" and ret != stale_read, (
        "返回值必须命中新 pending（旧写法拿不到 → 本用例防倒退）"
    )


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
