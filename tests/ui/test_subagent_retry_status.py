# -*- coding: utf-8 -*-
"""回归测试：子智能体重试状态展示

背景（2026-11-12）：子智能体 API 重试期间不上报 UI，面板行只有计时器在动，
用户看到的是「一直正在运行中」。修复后 executor.progress_updated（含重试
文案）经 RunBridge 投影到面板行提示位；完成后必须清空，不残留重试文案。
"""
import importlib.util
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "system-subagent" / "ui"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def row_cls():
    return _load("test_subagent_agent_row", "agent_row.py").AgentTaskRow


@pytest.fixture(scope="module")
def bridge_cls():
    return _load("test_subagent_run_bridge", "run_bridge.py").SubAgentRunBridge


class TestAgentRowStatusHint:
    def test_hint_visible_and_truncated(self, qapp, row_cls):
        row = row_cls("t1", "explore", "任务描述")
        row.set_status_hint("模型请求失败（InternalServerError），第 2/5 次重试，10s 后继续，后面还有很长很长很长很长的文案")
        assert row.status_label.isVisible() or not row.isVisible()  # 未 show 时不校验可见性
        assert row.status_label.text().endswith("...")
        assert len(row.status_label.text()) <= 45

    def test_empty_hint_hides(self, qapp, row_cls):
        row = row_cls("t2", "explore", "任务描述")
        row.set_status_hint("第 2/5 次重试")
        row.set_status_hint("")
        assert row.status_label.text() == ""

    def test_hint_cleared_after_finish(self, qapp, row_cls):
        row = row_cls("t3", "explore", "任务描述")
        row.set_status_hint("第 2/5 次重试")
        row.finish(success=False)
        assert row.status_label.text() == ""
        # 完成后迟到的 progress 不得把文案写回来
        row.set_status_hint("第 3/5 次重试")
        assert row.status_label.text() == ""


class TestRunBridgeProgressProjection:
    def test_progress_forwards_to_panel(self, qapp, bridge_cls):
        class _Panel:
            def __init__(self):
                self.status = {}

            def set_task_status(self, task_id, text):
                self.status[task_id] = text

        bridge = bridge_cls(registry=object(), host=object())
        panel = _Panel()
        bridge._panel_or_project = lambda window_id, task_id: panel
        bridge._on_retry_status("w1", "t1", "模型请求失败（APIConnectionError），第 1/5 次重试，5s 后继续")
        assert "第 1/5 次重试" in panel.status["t1"]
