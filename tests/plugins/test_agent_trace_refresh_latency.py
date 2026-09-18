# -*- coding: utf-8 -*-
"""agent_trace 刷新链路回归：落盘驱动、记录不闪失、投影失败可重试。

背景（2026-09-18 用户报「记录刷新的延迟非常高」+「加载历史会话有时还不刷新」），
三条独立缺陷：

1. **落盘无信号驱动**：collector 只订阅 ``_hook_messages_updated`` / ``tool_*`` /
   ``stream_*``，而工具迭代真正落盘走的是 worker ``finished_with_messages`` →
   ``executor`` 的 ``messages_updated`` 回调 → ``main_widget._on_messages_updated``
   → ``session.set_messages``，这条链**不发任何 backend 信号**。collector 感知
   不到，只能等下一次 ``tool_result_received`` / 整轮 ``stream_finished`` 才刷。
   本文件锁定「``messages_updated`` 信号必须能驱动重投影」。

2. **工具记录闪失**：``_on_tool_result_received`` 先清 in-flight 尾巴再 ``_sync``，
   而此刻落盘消息通常还没写入 → 清完投影里也没有这条 → 该工具条**从列表凭空
   消失**，直到下一次信号才补上（用户感知就是「延迟」）。正确行为：未落盘时
   收尾成「已完成」可见态，落盘后由 ``_sync`` 用正式记录取代。

3. **投影失败不重试**：``_switch_collector`` 同窗口分支先更新 ``_active_sid``
   再 ``refresh()``，投影抛异常后 sid 已被污染 → ``_on_tick`` 心跳判据
   ``sid != self._active_sid`` 永远为假 → 该会话**永不重试**（「有时不刷新」
   取决于该会话消息是否触发投影异常）。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

pytest.importorskip("qfluentwidgets")


# ──────────────────── 模块加载 ────────────────────


def _load_ui_modules():
    root = os.path.abspath(".")
    ui_path = os.path.join(root, "plugins", "agent_trace", "ui")
    for p in (ui_path, root):
        if p not in sys.path:
            sys.path.insert(0, p)
    mod_name = "agent_trace_refresh_pkg"
    if sys.modules.get(mod_name) is None:
        spec = importlib.util.spec_from_file_location(mod_name, os.path.join(ui_path, "__init__.py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    return (
        importlib.import_module(f"{mod_name}.trace_card"),
        importlib.import_module(f"{mod_name}.trace_models"),
        importlib.import_module(f"{mod_name}.trace_collector"),
    )


_trace_card, _trace_models, _trace_collector = _load_ui_modules()

TraceCardWidget = _trace_card.TraceCardWidget
EntryKind = _trace_models.EntryKind


# ──────────────────── 假对象 ────────────────────


class _FakeSession:
    def __init__(self, sid: str, messages: list, system_prompt: str = "") -> None:
        self.session_id = sid
        self.messages = messages
        self.system_prompt = system_prompt


class _FakeBackend(QObject):
    """含 ``messages_updated``（worker finished_with_messages 的转发信号）。"""

    _hook_messages_updated = pyqtSignal()
    messages_updated = pyqtSignal()
    stream_started = pyqtSignal()
    stream_finished = pyqtSignal(str)
    tool_call_started = pyqtSignal(str, str, dict)
    tool_result_received = pyqtSignal(str, str, dict, object)
    context_updated = pyqtSignal(int, int)
    session_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, session=None) -> None:
        super().__init__()
        self._session = session

    def get_current_session(self):
        return self._session


class _FakeMainWidget:
    def __init__(self, wid: str, backend: _FakeBackend) -> None:
        self._window_id = wid
        self.backend = backend


def _msgs(sid_tag: str, n_rounds: int) -> list:
    base = int(time.time() * 1000) - 60_000
    out = []
    for i in range(n_rounds):
        out.append(
            {
                "role": "user",
                "content": f"{sid_tag}-user-{i}",
                "timestamp": "2026-09-18 09:00:00",
                "ts_ms": base + i * 2000,
            }
        )
        out.append(
            {
                "role": "assistant",
                "content": f"{sid_tag}-assistant-{i}",
                "timestamp": "2026-09-18 09:00:01",
                "ts_ms": base + i * 2000 + 900,
            }
        )
    return out


def _make_card(qapp, mw: _FakeMainWidget) -> TraceCardWidget:
    card = TraceCardWidget()
    card.resize(1200, 700)
    card.show()
    qapp.processEvents()
    card._ctx_provider = lambda: {
        "is_dark": True,
        "colors": {"text_primary": "#f3f6fc", "accent": "#66c6ff"},
        "font_family": "Segoe UI",
        "font_size": 13,
        "main_widget": mw,
        "services": {},
    }
    card._refresh_context()
    qapp.processEvents()
    return card


# ──────────────────── 用例 ────────────────────


def test_messages_updated_signal_drives_reprojection(qapp):
    """落盘信号（worker finished_with_messages 转发）必须能驱动 collector 重投影。

    当前实现里 collector 完全没订阅这条信号 → 工具迭代落盘后轨迹要等
    「下一次 tool_result_received / 整轮 stream_finished」才更新。
    """
    session = _FakeSession("sid-P", _msgs("P", 1))
    backend = _FakeBackend(session)
    mw = _FakeMainWidget("win-p", backend)

    card = _make_card(qapp, mw)
    try:
        c = card._collector
        assert c is not None
        before = len(c.records)

        # 模拟 worker 落盘：session.messages 追加一条 tool 消息，然后发信号
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "bash",
                "content": "ok",
                "timestamp": "2026-09-18 09:00:02",
                "ts_ms": int(time.time() * 1000),
            }
        )
        backend.messages_updated.emit()
        qapp.processEvents()

        assert len(c.records) > before, "落盘信号未驱动重投影（collector 没有订阅 messages_updated）"
        assert any(r.kind == EntryKind.TOOL for r in c.records), "落盘的 tool 记录未进入投影"
    finally:
        card.close()


def test_tool_record_survives_until_persist(qapp):
    """工具结果到达但消息尚未落盘 → 该工具条必须仍可见（不能凭空消失）。

    真实时序：worker 先发 ``tool_result_received``，落盘消息稍后才写入。旧实现
    在这个窗口里既清了 in-flight 尾巴、投影里又没有正式记录 → 列表里的这一条
    直接消失，看起来就是「刷新延迟」。
    """
    session = _FakeSession("sid-G", _msgs("G", 1))
    backend = _FakeBackend(session)
    mw = _FakeMainWidget("win-g", backend)

    card = _make_card(qapp, mw)
    try:
        c = card._collector
        backend.tool_call_started.emit("call-9", "bash", {"command": "ls"})
        qapp.processEvents()
        assert any(r.kind == EntryKind.TOOL and r.is_pending for r in c.visible_records), "工具开始后应有 in-flight 记录"

        # 结果到达，消息尚未落盘
        backend.tool_result_received.emit("call-9", "bash", {"command": "ls"}, {"success": True, "content": "ok"})
        qapp.processEvents()
        tools = [r for r in c.visible_records if r.kind == EntryKind.TOOL]
        assert tools, "工具结果到达后该条记录从列表消失（落盘前无任何可见记录）"
        assert not tools[0].is_pending, "结果已到达，记录不应还标成进行中"

        # 落盘 → 正式记录取代临时记录（同样的工具调用只有一条）
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": "call-9",
                "name": "bash",
                "content": "ok",
                "timestamp": "2026-09-18 09:00:02",
                "ts_ms": int(time.time() * 1000),
            }
        )
        backend.messages_updated.emit()
        qapp.processEvents()
        tools = [r for r in c.visible_records if r.kind == EntryKind.TOOL]
        assert len(tools) == 1, f"落盘后应只剩正式记录，实际 {len(tools)} 条"
        assert not tools[0].is_pending
    finally:
        card.close()


def test_switch_collector_rolls_back_sid_on_projection_failure(qapp, monkeypatch):
    """投影抛异常时不得污染 ``_active_sid``，否则心跳永不重试。

    ``_on_tick`` 用 ``sid != self._active_sid`` 判断会话是否变了。旧实现先赋值
    再 refresh，异常一抛 sid 就是新值 → 判定「没变」→ 该会话永远不刷新
    （用户报「加载历史会话有时还不刷新」）。
    """
    session_a = _FakeSession("sid-A", _msgs("A", 1))
    backend = _FakeBackend(session_a)
    mw = _FakeMainWidget("win-r", backend)

    card = _make_card(qapp, mw)
    try:
        assert card._active_sid == "sid-A"
        c = card._collector
        assert c is not None

        # 换会话，并让投影炸一次
        session_b = _FakeSession("sid-B", _msgs("B", 2))
        backend._session = session_b
        boom = {"n": 0}
        real_project = c._project

        def _flaky(messages, system_prompt, session_id=""):
            boom["n"] += 1
            if boom["n"] <= 1:
                raise RuntimeError("投影炸了（模拟历史消息结构异常）")
            return real_project(messages, system_prompt, session_id=session_id)

        monkeypatch.setattr(c, "_project", _flaky)

        card._switch_collector(mw)
        qapp.processEvents()
        assert card._active_sid != "sid-B" or "B-user-1" in "".join(r.raw for r in card._visible()), (
            "投影失败却把 _active_sid 更新成新会话 → 心跳永不重试"
        )

        # 心跳重试：异常恢复后必须能追上目标会话
        card._on_tick()
        qapp.processEvents()
        assert card._active_sid == "sid-B", "心跳未重试投影"
        assert any("B-user-1" in (r.raw or "") for r in card._visible()), "重试后仍未显示新会话内容"
    finally:
        card.close()


def test_hidden_card_keeps_aux_dirty(qapp):
    """卡片不可见时不得消费 dirty 标志（否则 showEvent 的补刷永不生效）。"""
    session = _FakeSession("sid-H", _msgs("H", 1))
    backend = _FakeBackend(session)
    mw = _FakeMainWidget("win-h", backend)

    card = _make_card(qapp, mw)
    try:
        card.hide()
        qapp.processEvents()
        card._schedule_aux_refresh()
        card._flush_aux_refresh()  # 模拟防抖定时器到点
        assert card._aux_dirty, "不可见时消费了 dirty 标志 → showEvent 补刷失效"

        card.show()
        qapp.processEvents()
        assert not card._aux_dirty, "showEvent 应补刷并清 dirty"
    finally:
        card.close()
