# -*- coding: utf-8 -*-
"""agent_trace 回归：加载历史会话后轨迹必须重新投影。

背景（真实 bug）：``TraceCardWidget._switch_collector`` 曾对「同窗口」直接
``return`` 以省掉一次全量投影（切标签性能）。但**加载历史会话是静默切换** ——
主程序走 ``main_widget.session_manager.set_current_session(session)``，既不发
``backend.session_changed``（只有 ``backend.set_current_session`` 才发），也不发
``_hook_messages_updated``。于是没有任何东西驱动重新投影，卡片一直停在旧会话上
（表现为「加载历史会话后轨迹不显示」）。

本文件锁定三条兜底路径：
1. ``_switch_collector`` 同窗口分支按 session_id 比对重投影；
2. ``_on_tick`` 心跳探测（卡片已打开、没有 showEvent / tab 事件时）；
3. ``backend.session_changed`` 信号订阅。
"""

from __future__ import annotations

import time

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

pytest.importorskip("qfluentwidgets")

PLUGIN_UI = "plugins/agent_trace"


def _load_ui_modules():
    """按主程序的方式加载插件 ui 包（sys.path 注入 + 相对导入）。

    ⚠️ 必须显式导入子模块：``ui/__init__.py`` 的 ``register_ui`` 是延迟导入，
    只 exec 包本身拿不到 ``trace_card`` / ``trace_models``。
    """
    import importlib
    import importlib.util
    import os
    import sys

    root = os.path.abspath(".")
    ui_path = os.path.join(root, PLUGIN_UI, "ui")
    if ui_path not in sys.path:
        sys.path.insert(0, ui_path)
    if root not in sys.path:
        sys.path.insert(0, root)
    mod_name = "ui_plugin_agent_trace"
    if sys.modules.get(mod_name) is None:
        spec = importlib.util.spec_from_file_location(mod_name, os.path.join(ui_path, "__init__.py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    return mod_name, importlib.import_module(f"{mod_name}.trace_card"), importlib.import_module(
        f"{mod_name}.trace_models"
    )


_MOD_NAME, _trace_card, _trace_models = _load_ui_modules()

TraceCardWidget = _trace_card.TraceCardWidget  # noqa: E402
kind_color = _trace_models.kind_color  # noqa: E402
EntryKind = _trace_models.EntryKind  # noqa: E402


# ──────────────────── 假对象 ────────────────────


class _FakeSession:
    def __init__(self, sid: str, messages: list, system_prompt: str = "") -> None:
        self.session_id = sid
        self.messages = messages
        self.system_prompt = system_prompt


class _FakeBackend(QObject):
    """仅实现 collector / 卡片实际用到的信号与 API。"""

    _hook_messages_updated = pyqtSignal()
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


def _messages(tag: str, n: int) -> list:
    base = int(time.time() * 1000) - 60_000
    msgs = []
    for i in range(n):
        msgs.append(
            {
                "role": "user",
                "content": f"{tag}-user-{i}",
                "timestamp": "2026-09-03 09:00:00",
                "ts_ms": base + i * 1000,
            }
        )
        msgs.append(
            {
                "role": "assistant",
                "content": f"{tag}-assistant-{i}",
                "timestamp": "2026-09-03 09:00:01",
                "ts_ms": base + i * 1000 + 500,
            }
        )
    return msgs


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


def test_switch_collector_reprojects_when_session_changed(qapp):
    """同窗口「静默」换会话（加载历史会话）→ 必须重新投影。"""
    session_a = _FakeSession("sid-A", _messages("A", 2), system_prompt="sys-A")
    backend = _FakeBackend(session_a)
    mw = _FakeMainWidget("win-1", backend)

    card = _make_card(qapp, mw)
    try:
        assert card._active_sid == "sid-A"
        total_a = card._turn_list.total_count
        assert total_a > 0, "首个会话应有记录"
        assert any("A-user-0" in (r.raw or "") for r in card._visible())

        # ⚠️ 模拟「加载历史会话」：只换 session 对象，不发任何信号
        session_b = _FakeSession("sid-B", _messages("B", 5), system_prompt="sys-B")
        backend._session = session_b

        card._switch_collector(mw)
        qapp.processEvents()

        assert card._active_sid == "sid-B"
        assert card._turn_list.total_count != total_a, "会话换了但列表没重投影"
        assert any("B-user-4" in (r.raw or "") for r in card._visible()), "应显示新会话内容"
        assert not any("A-user-0" in (r.raw or "") for r in card._visible()), "仍残留旧会话内容"
    finally:
        card.close()


def test_tick_detects_silent_session_switch(qapp):
    """卡片已打开、没有任何事件时，心跳也要能发现会话换了。"""
    session_a = _FakeSession("sid-A", _messages("A", 1))
    backend = _FakeBackend(session_a)
    mw = _FakeMainWidget("win-2", backend)

    card = _make_card(qapp, mw)
    try:
        assert card._turn_list.total_count > 0
        backend._session = _FakeSession("sid-C", _messages("C", 3))
        card._on_tick()
        qapp.processEvents()
        assert card._active_sid == "sid-C"
        assert any("C-user-2" in (r.raw or "") for r in card._visible())
    finally:
        card.close()


def test_session_changed_signal_triggers_refresh(qapp):
    """backend.session_changed（新建/切换会话路径）→ 重新投影。"""
    session_a = _FakeSession("sid-A", _messages("A", 1))
    backend = _FakeBackend(session_a)
    mw = _FakeMainWidget("win-3", backend)

    card = _make_card(qapp, mw)
    try:
        assert card._turn_list.total_count > 0
        backend._session = _FakeSession("sid-D", _messages("D", 4))
        # 只有 backend.set_current_session 才会发的信号
        backend.session_changed.emit("sid-D")
        qapp.processEvents()
        assert any("D-user-3" in (r.raw or "") for r in card._visible())
    finally:
        card.close()


def test_kind_color_covers_hook_alias():
    """CONTEXT 的 value 是 "HOOK" —— 按 value 查表会退化成兜底灰。

    Enum 的 ``__hash__`` 走 ``_name_``，所以键必须是成员本身或用成员反查，
    否则所有 HOOK 条目的徽章/条带/详情标题全变灰。
    """
    assert kind_color(EntryKind.CONTEXT).name() == "#9ece6a", kind_color(EntryKind.CONTEXT).name()
    assert kind_color(EntryKind.TOOL).name() == "#7dcfff"
    assert kind_color(EntryKind.SYSTEM).name() == "#7aa2f7"
    # 字符串兼容（历史调用方）
    assert kind_color("CONTEXT").name() == "#9ece6a"
    assert kind_color("HOOK").name() == "#9ece6a"
    # 未知类型不炸，兜底灰
    assert kind_color("NOPE").name() == "#888888"
