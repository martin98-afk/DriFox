# -*- coding: utf-8 -*-
"""`_pending_arrange_count` 异常路径兜底契约（#P4 核实）。

结论先行：当前实现 `_join_new_window_for_template` 对异常路径**已有兜底**——
函数整体包在 try/except 里，except 分支统一递减计数并触发排列
（main_widget.py 约 :6313-6334）。本文件把该现状锁成契约，防止后续重构退化：

- 异常 → 计数照样递减、排列照样触发；异常被 except 记录，**不向上传播**
  （现状语义；若未来改成传播，本文件的 raised 断言会提示需要同步更新）。
- 正常 2 窗口 → 计数归零、排列只触发 1 次。
- 部分失败（1 正常 1 异常）→ 计数不残留、排列只触发 1 次。
"""
import sys
from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtWidgets import QApplication

from app.main_widget import OpenAIChatToolWindow


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_host(count):
    """真实 OpenAIChatToolWindow 实例（__new__ 最小构造）+ 记录排列调用。"""
    host = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    host._pending_arrange_count = count
    arrange_calls = []
    host._do_team_window_arrange = lambda: arrange_calls.append(1)
    host._sync_active_windows_to_team_manager = MagicMock()
    tm = MagicMock()
    tm.get_team_run_id.return_value = "run-1"
    tm.get_template_for_run_id.return_value = {"name": "dev"}
    tm.get_project_for_run_id.return_value = ""
    tm.get_team_project.return_value = ""
    tm.get_team_workdir.return_value = ""
    host._get_team_manager = lambda: tm
    return host, arrange_calls


def _make_win(name="w1", raises=False):
    win = MagicMock()
    win._is_destroyed = False
    win._team_join_retries = 0
    win.backend.agent_manager = MagicMock()
    win.backend.chat_engine = MagicMock()
    win._on_agent_changed = MagicMock()
    win._apply_agent_command_permissions = MagicMock()
    win._refresh_team_ui = MagicMock()
    win._start_team_watcher = MagicMock()
    win._team_agent_name = "build"
    win._team_name = "dev"
    win._team_run_id = "run-1"
    win._window_id = name
    win.window = MagicMock(return_value=win)
    if raises:
        win._on_agent_changed.side_effect = RuntimeError("boom")
    return win


class TestArrangeCountExceptionPath:
    def test_exception_decrements_and_arranges(self):
        """a) 异常路径：计数回 0、排列触发；现状不向上传播（被 except 记录）。"""
        _ensure_qapp()
        host, arrange = _make_host(1)
        win = _make_win(raises=True)

        raised = False
        from app.widgets.tab_manager_window import TabManagerWindow

        with patch.object(TabManagerWindow, "get_instance", return_value=MagicMock()):
            try:
                host._join_new_window_for_template(win, "build", "w1", track_arrange=True)
            except RuntimeError:
                raised = True

        assert host._pending_arrange_count == 0, "异常路径必须收口计数（现有 except 分支已兜底）"
        assert arrange == [1], "计数归零后必须触发一次排列"
        assert raised is False, "现状语义：异常在 except 分支被记录，不向上传播"

    def test_two_windows_normal_arranges_once(self):
        """b) 正常路径不回归：2 窗口全部完成 → 计数 0、排列 1 次。"""
        _ensure_qapp()
        host, arrange = _make_host(2)

        for wid in ("w1", "w2"):
            host._join_new_window_for_template(_make_win(wid), "build", wid, track_arrange=True)

        assert host._pending_arrange_count == 0
        assert arrange == [1], f"排列应只触发 1 次，实际 {arrange}"

    def test_partial_failure_no_residue_and_arranges_once(self):
        """c) 部分失败：1 正常 + 1 异常 → 计数不残留、排列 1 次。"""
        _ensure_qapp()
        host, arrange = _make_host(2)
        from app.widgets.tab_manager_window import TabManagerWindow

        host._join_new_window_for_template(_make_win("w1"), "build", "w1", track_arrange=True)
        assert host._pending_arrange_count == 1 and not arrange

        with patch.object(TabManagerWindow, "get_instance", return_value=MagicMock()):
            host._join_new_window_for_template(_make_win("w2", raises=True), "build", "w2", track_arrange=True)

        assert host._pending_arrange_count == 0, "异常窗口同样要收口计数"
        assert arrange == [1], f"两窗口全部收口后应排列 1 次，实际 {arrange}"

    def test_track_arrange_false_keeps_count(self):
        """反向：track_arrange=False（恢复路径）不递减计数。"""
        _ensure_qapp()
        host, arrange = _make_host(3)

        host._join_new_window_for_template(
            _make_win("w1"), "build", "w1", track_arrange=False, keep_team_name=True
        )

        assert host._pending_arrange_count == 3
        assert arrange == []
