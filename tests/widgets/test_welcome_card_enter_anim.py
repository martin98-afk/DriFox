# -*- coding: utf-8 -*-
"""回归测试：欢迎卡片软刷新不重播 session-item 进入动画

修复背景（2026-08-24）：
- 其他标签页对话完成 → `_notify_history_data_changed(broadcast=True)` 跨窗口广播
  → 本窗口走「软更新」`refresh_welcome_data()` → `set_content` 整体重设 innerHTML
  → 所有 `.session-item` 重新插入 DOM → CSS `session-item-in` 进入动画重播。
- 修复：软刷新（数据更新）时只静默更新列表，session-item 内联 `animation:none`
  覆盖 CSS 进入动画；首次进入（create_welcome_card）仍播放 stagger fade-in。
- 配套：软刷新复用首次渲染的固定问候语（`_welcome_greeting`），避免无谓跳变。
"""

import pytest

from app.widgets.message_card import _render_sessions_body, _render_welcome_body


_S1 = {"title": "会话一", "session_id": "id-1", "last_time": "刚刚", "message_count": 3}
_S2 = {"title": "会话二", "session_id": "id-2", "last_time": "昨天", "message_count": 10}


def _count_session_items(html: str) -> int:
    return html.count('data-type="session"')


def test_render_sessions_body_default_plays_enter_anim():
    """默认渲染（首次进入）应保留 stagger 进入动画延迟。"""
    html = _render_sessions_body([_S1], [_S2])
    assert _count_session_items(html) == 2
    assert "animation-delay" in html, "首次渲染应保留 session-item 进入动画延迟"
    assert 'style="animation: none;"' not in html


def test_render_sessions_body_suppress_anim_quiet_update():
    """软刷新（数据更新）应内联 animation:none 覆盖进入动画，且列表项完整。"""
    html = _render_sessions_body([_S1], [_S2], suppress_anim=True)
    assert _count_session_items(html) == 2, "抑制动画不应丢失任何会话项"
    assert 'style="animation: none;"' in html, "软刷新应用内联 animation:none 抑制进入动画"
    assert "animation-delay" not in html, "软刷新不应再播 stagger 延迟"


def test_render_sessions_body_empty_no_crash():
    html = _render_sessions_body([], [], suppress_anim=True)
    assert "welcome-empty" in html


def test_render_welcome_body_passes_suppress_anim_through():
    """_render_welcome_body 的 suppress_anim 应透传到 session-item 内联样式。"""
    off = _render_welcome_body("sessions", [_S1], [_S2], suppress_anim=False)
    on = _render_welcome_body("sessions", [_S1], [_S2], suppress_anim=True)
    assert "animation-delay" in off
    assert 'style="animation: none;"' in on
    # 点击链（data-type/session-id）在两种模式下都必须保留
    assert 'data-type="session"' in on
    assert 'data-session-id="id-1"' in on


def test_refresh_welcome_data_calls_render_with_suppress_true(monkeypatch):
    """refresh_welcome_data 软刷新须以 suppress_anim=True 重渲染 sessions body。

    直接验证渲染入口的参数，确保回归（有人改回默认即会失败）。
    """
    from app.widgets import message_card as mc

    captured = {}

    def _fake_render_welcome_body(mode, recent, top, window_context=None, suppress_anim=False):
        captured["suppress_anim"] = suppress_anim
        return "<div class='session-item'>x</div>"

    monkeypatch.setattr(mc, "_render_welcome_body", _fake_render_welcome_body)

    # 构造最小 MessageCard 替身：绕过 __init__，仅挂 refresh_welcome_data 所需属性
    card = mc.MessageCard.__new__(mc.MessageCard)

    def _fake_get_welcome_window_context():
        return {}

    card._welcome_mode = "sessions"
    card._welcome_recent = [_S1]
    card._welcome_top = [_S2]
    card._get_welcome_window_context = _fake_get_welcome_window_context
    card._render_welcome_with_body = lambda body_html: None  # 跳过真实渲染

    # 数据相对上一次「已渲染」发生变化 → 进入重渲染分支
    card._welcome_recent = []  # 清空以触发 new != old 分支
    card._welcome_top = []
    mc.MessageCard.refresh_welcome_data(card, [_S1], [_S2])
    assert captured.get("suppress_anim") is True
