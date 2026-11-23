# -*- coding: utf-8 -*-
"""渲染通道 reason 打点：`_should_use_qt_renderer` / `_content_needs_webengine`。

纯观测改造：判定逻辑不动，只在实际走 WebEngine 时打一行
``[render-channel] reason=...``（DEBUG 级，写 ui.log）。
本测试锁定每个 True 出口的 reason，以及「走 Qt 时不打日志」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger  # noqa: E402

import app.widgets.card_render_core as crc  # noqa: E402


@pytest.fixture(autouse=True)
def _auto_channel(monkeypatch):
    monkeypatch.delenv("DRIFOX_QT_RENDERER", raising=False)
    monkeypatch.setenv("DRIFOX_RENDER_CHANNEL_AUTO", "1")


@pytest.fixture()
def captured():
    logs: list[str] = []
    handler_id = logger.add(
        lambda m: logs.append(m.record["message"]), level="DEBUG", format="{message}"
    )
    try:
        yield logs
    finally:
        logger.remove(handler_id)


def _reasons(logs: list[str]) -> list[str]:
    return [x for x in logs if "[render-channel]" in x]


@pytest.mark.parametrize(
    "content,streaming,langs,expected",
    [
        (None, False, "orig", "reason=invalid_type"),
        (12345, False, "orig", "reason=invalid_type"),
        ([{"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}], False, "orig", "reason=nontext_block"),
        ([{"type": "tool_result", "content": "x"}], False, "orig", "reason=nontext_block"),
        ([{"type": "text", "text": ""}], False, "orig", "reason=empty_text"),
        ("普通正文", False, "none", "reason=plugin_fence_not_ready"),
        ("```echarts\n{}\n```", False, "orig", "reason=fence:echarts"),
        ("```mermaid\ngraph TD\n```", False, "orig", "reason=fence:mermaid"),
        ("```myplugin\nx\n```", False, "plugin", "reason=fence:myplugin"),
        ("公式 $E=mc^2$", False, "orig", "reason=latex"),
        ("![a](http://x/a.png)", False, "orig", "reason=image"),
        ("普通正文", True, "orig", "reason=streaming"),
    ],
)
def test_reason_logged_for_every_webengine_exit(
    captured, monkeypatch, content, streaming, langs, expected
):
    if langs == "none":
        monkeypatch.setattr(crc, "_registered_fence_langs", lambda: None)
    elif langs == "plugin":
        monkeypatch.setattr(crc, "_registered_fence_langs", lambda: frozenset({"myplugin"}))

    assert crc._should_use_qt_renderer(content, streaming=streaming) is False
    hits = _reasons(captured)
    assert any(expected in h for h in hits), f"未捕获 {expected}，实际 {hits}"


@pytest.mark.parametrize("content", ["普通中文正文", "```python\nprint(1)\n```", "", []])
def test_no_log_when_qt_channel(captured, content):
    """走纯 Qt 时不得打 [render-channel]（防刷屏）。"""
    assert crc._should_use_qt_renderer(content, streaming=False) is True
    assert _reasons(captured) == []


def _capture_records():
    records: list = []
    hid = logger.add(lambda m: records.append(m.record), level="DEBUG", format="{message}")
    return records, hid


def test_reason_level_defaults_to_debug(monkeypatch):
    """开关关闭（默认）：reason 走 DEBUG，不污染常规日志。"""
    monkeypatch.setattr(crc, "_RENDER_REASON_LOG", False)
    records, hid = _capture_records()
    try:
        crc._should_use_qt_renderer("```echarts\n{}\n```", streaming=False)
    finally:
        logger.remove(hid)
    hits = [r for r in records if "[render-channel]" in r["message"]]
    assert hits and hits[0]["level"].name == "DEBUG"


def test_reason_level_info_when_switch_on(monkeypatch):
    """DRIFOX_RENDER_REASON=1 等价：同样输入走 INFO，用户不改日志级别也能看到。"""
    monkeypatch.setattr(crc, "_RENDER_REASON_LOG", True)
    records, hid = _capture_records()
    try:
        crc._should_use_qt_renderer("```echarts\n{}\n```", streaming=False)
    finally:
        logger.remove(hid)
    hits = [r for r in records if "[render-channel]" in r["message"]]
    assert hits and hits[0]["level"].name == "INFO"
