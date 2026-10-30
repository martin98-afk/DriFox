# -*- coding: utf-8 -*-
"""回归测试：滚轮「内层可滚」判据必须包含坞态正文容器 #content-placeholder。

背景（用户实测，2026-10-30）
--------------------------
「原来鼠标点击 HTML 获得焦点后滚轮优先滚 HTML 内部，现在失效了」。

坞态（简洁模式 + 流式，``body.streaming-dock``）下正文 ``#content-placeholder``
有 ``max-height: 600px; overflow-y: auto``，它是**唯一**真正可滚的容器（body
自己反而不溢出）。而两处 inner-scrollable 判据（reportHeight 第 6 字段、
``_BODY_GEOM_READ_JS`` 轮询/校准）都只查 ``#tool-content`` / 代码块 / 图表区，
漏了它 → ``_inner_scrollable`` 恒 False → 武装态（点击过正文）
``_wheel_belongs_to_outer`` 判「卡内无人能消费」→ 滚轮全交外层。

修复：两处选择器都补 ``#content-placeholder``（非坞态它不溢出，差值 0，
不会误判）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets.card_viewers import _BODY_GEOM_READ_JS, CodeWebViewer

_SRC = Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py"

def test_geom_read_js_includes_content_placeholder():
    """轮询/校准直读表达式必须把坞态正文容器算进内层可滚判据。"""
    assert "#content-placeholder" in _BODY_GEOM_READ_JS
    assert "#tool-content" in _BODY_GEOM_READ_JS

def test_report_height_ins_includes_content_placeholder():
    """reportHeight 第 6 字段（_ins）同源修复，否则 console 链持续回传 0。"""
    src = _SRC.read_text(encoding="utf-8")
    i = src.find("var _ins = 0;")
    assert i > 0
    body = src[i : i + 900]
    assert "'#content-placeholder, #tool-content," in body.replace("\n", " ").replace("  ", " ")

def _viewer(monkeypatch, *, geom_valid: bool, doc_h: int, client_h: int, inner):
    _ = QApplication.instance() or QApplication(sys.argv)
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._body_geom_valid = geom_valid
    v._document_height = doc_h
    v._body_client_height = client_h
    v._body_scroll_top = 0
    v._inner_scrollable = inner
    return v

def test_belongs_to_outer_when_body_and_inner_cannot_scroll(monkeypatch):
    """body 不溢 + 内层确认不可滚 → 交外层（既有语义）。"""
    v = _viewer(monkeypatch, geom_valid=True, doc_h=500, client_h=500, inner=False)
    assert v._wheel_belongs_to_outer(-120) is True

def test_inner_scrollable_keeps_wheel_inside(monkeypatch):
    """内层（坞态正文）可滚 → 绝不能在首道抢给外层。"""
    v = _viewer(monkeypatch, geom_valid=True, doc_h=500, client_h=500, inner=True)
    assert v._wheel_belongs_to_outer(-120) is False

def test_unknown_inner_released_to_chromium(monkeypatch):
    """None（旧骨架未回传）→ 保守放行给 Chromium，不抢内层滚轮。"""
    v = _viewer(monkeypatch, geom_valid=True, doc_h=500, client_h=500, inner=None)
    assert v._wheel_belongs_to_outer(-120) is False

def test_body_scrollable_keeps_wheel_inside(monkeypatch):
    """body 自己可滚（非坞态）→ 卡内优先，不交外层。"""
    v = _viewer(monkeypatch, geom_valid=True, doc_h=2000, client_h=500, inner=False)
    assert v._wheel_belongs_to_outer(-120) is False
