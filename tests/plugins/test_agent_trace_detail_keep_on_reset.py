# -*- coding: utf-8 -*-
"""agent_trace 回归：列表全量重置后，正在查看的详情不能被关掉。

背景（真实 bug）：``TraceCollector._sync`` 只要发现已落盘记录「头部内容变化 /
缩短」就发 ``recordsReset``（上下文压缩、hook 注入改写、工具消息合并都会
触发），而 ``TraceCardWidget._pull_records`` 把它和「切换会话」当成一回事：
无条件 ``_hide_detail()`` + ``clear_selection()``。表现为「点开详情，流式跑着
跑着面板自己消失」。

修法：reset 时先记下选中记录的稳定身份 ``(kind, msg_index, 内容指纹)``，重建
后按身份找回并恢复选中；只有记录真的没了（或被筛选挡住）才收起，会话切换
则显式传 ``keep_selection=False``。
"""

from __future__ import annotations

import time

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

pytest.importorskip("qfluentwidgets")

PLUGIN_UI = "plugins/agent_trace"

def _load_ui_modules():
    """按主程序的方式加载插件 ui 包（sys.path 注入 + 相对导入）。"""
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
    return mod_name, importlib.import_module(f"{mod_name}.trace_card")

_MOD_NAME, _trace_card = _load_ui_modules()

TraceCardWidget = _trace_card.TraceCardWidget  # noqa: E402

class _FakeSession:
    def __init__(self, sid: str, messages: list, system_prompt: str = "") -> None:
        self.session_id = sid
        self.messages = messages
        self.system_prompt = system_prompt

class _FakeBackend(QObject):
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

def _idx_of_msg(card, msg_index: int) -> int:
    for i, rec in enumerate(card._visible()):
        if rec.meta.get("msg_index") == msg_index:
            return i
    raise AssertionError(f"没有 msg_index={msg_index} 的记录")

# ──────────────────── 用例 ────────────────────

def test_detail_survives_reset_when_head_rewritten(qapp):
    """头部被改写 + 尾部追加 → recordsReset，但选中的记录没变 → 详情必须留着。"""
    session = _FakeSession("sid-keep", _messages("K", 3))
    backend = _FakeBackend(session)
    mw = _FakeMainWidget("win-keep", backend)

    card = _make_card(qapp, mw)
    try:
        idx = _idx_of_msg(card, 3)
        card._turn_list.select_record(idx)
        qapp.processEvents()
        ident = card._turn_list.selected_identity()
        assert ident is not None
        assert card._detail.isVisible(), "点选后详情应展开"

        # 头部消息改写（hook 注入 / 工具消息补全的典型形态）+ 尾部追加
        session.messages[0]["content"] = "改写后的第一条"
        session.messages.extend(_messages("K2", 1))
        card._collector.refresh()
        qapp.processEvents()

        assert card._detail.isVisible(), "reset 后详情被无故关掉了"
        assert card._turn_list.selected_identity() == ident, "选中的记录变了"
    finally:
        card.close()

def test_detail_survives_reset_when_indices_shift(qapp):
    """上下文压缩 → 序号整体前移，按内容指纹仍应找回同一条记录。"""
    session = _FakeSession("sid-shift", _messages("S", 4))
    backend = _FakeBackend(session)
    mw = _FakeMainWidget("win-shift", backend)

    card = _make_card(qapp, mw)
    try:
        idx = _idx_of_msg(card, 5)
        card._turn_list.select_record(idx)
        qapp.processEvents()
        target = card._visible()[idx]
        ident = card._turn_list.selected_identity()
        assert card._detail.isVisible()

        # 压缩：前 4 条换成一条 summary（目标消息序号 5 → 1）
        session.messages = [
            {"role": "user", "content": "（压缩摘要）", "timestamp": "2026-09-03 09:00:00", "ts_ms": 1}
        ] + session.messages[4:]
        card._collector.refresh()
        qapp.processEvents()

        assert card._detail.isVisible(), "压缩后详情被关掉了"
        # 序号会随压缩前移（5 → 2），身份判定的实质是 kind + 内容指纹
        assert card._turn_list.selected_identity()[:1] + card._turn_list.selected_identity()[2:] == (
            ident[0],
            ident[2],
        )
        cur = card._visible()[card._turn_list.selected_record_idx]
        assert cur.raw == target.raw, "恢复到了另一条记录"
    finally:
        card.close()

def test_detail_closes_when_record_disappears(qapp):
    """选中的记录真被删掉（消息清空）→ 详情收起，不能停在空白面板上。"""
    session = _FakeSession("sid-gone", _messages("G", 3))
    backend = _FakeBackend(session)
    mw = _FakeMainWidget("win-gone", backend)

    card = _make_card(qapp, mw)
    try:
        card._turn_list.select_record(_idx_of_msg(card, 3))
        qapp.processEvents()
        assert card._detail.isVisible()

        session.messages = []
        card._collector.refresh()
        qapp.processEvents()

        assert not card._detail.isVisible(), "记录没了，详情应收起"
        assert card._turn_list.selected_record_idx is None
    finally:
        card.close()

def test_session_switch_drops_detail(qapp):
    """切换会话 → 保留旧选中没有意义，详情必须收起。"""
    session = _FakeSession("sid-A", _messages("A", 3))
    backend = _FakeBackend(session)
    mw = _FakeMainWidget("win-sw", backend)

    card = _make_card(qapp, mw)
    try:
        card._turn_list.select_record(_idx_of_msg(card, 3))
        qapp.processEvents()
        assert card._detail.isVisible()

        backend._session = _FakeSession("sid-B", _messages("B", 3))
        card._switch_collector(mw)
        qapp.processEvents()

        assert card._active_sid == "sid-B"
        assert not card._detail.isVisible(), "切会话后详情应收起"
        assert card._turn_list.selected_record_idx is None
    finally:
        card.close()
