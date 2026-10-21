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


# ── 增量 DOM 替换（2026-09-28）：软刷新只换 #welcome-sessions-root ──────


def test_render_sessions_body_wraps_root_anchor():
    """sessions body 必须包 #welcome-sessions-root 根锚点（空态同样包）。"""
    html = _render_sessions_body([_S1], [_S2])
    assert 'id="welcome-sessions-root"' in html, "有内容态应包根锚点供增量替换定位"
    empty = _render_sessions_body([], [], suppress_anim=True)
    assert 'id="welcome-sessions-root"' in empty, "空态也应包根锚点"


class _FakePage:
    def __init__(self):
        self.calls = []

    def runJavaScript(self, js):
        self.calls.append(js)


class _FakeViewer:
    def __init__(self, visible=True, js_ready=True, with_page=True):
        self._visible = visible
        self._is_js_ready = js_ready
        self.page_obj = _FakePage() if with_page else None

    def isVisible(self):
        return self._visible

    def page(self):
        return self.page_obj


def _make_card(viewer, lazy_rendered=True):
    from app.widgets import message_card as mc

    card = mc.MessageCard.__new__(mc.MessageCard)
    card._welcome_mode = "sessions"
    card._welcome_recent = []
    card._welcome_top = []
    card._get_welcome_window_context = lambda: {}
    if viewer is not None:
        card.viewer = viewer
    card._lazy_rendered = lazy_rendered
    return card


def test_refresh_welcome_data_prefers_incremental(monkeypatch):
    """可见且 JS 就绪的窗口软刷新走增量替换，不落整页渲染。"""
    from app.widgets import message_card as mc

    viewer = _FakeViewer(visible=True, js_ready=True)
    card = _make_card(viewer)

    def _boom(body_html):
        raise AssertionError("增量路径生效时不应回退整页渲染")

    card._render_welcome_with_body = _boom
    monkeypatch.setattr(
        mc,
        "_render_welcome_body",
        lambda mode, recent, top, window_context=None, suppress_anim=False: "<div>x</div>",
    )
    mc.MessageCard.refresh_welcome_data(card, [_S1], [_S2])
    assert len(viewer.page_obj.calls) == 1, "应恰好发出一次增量替换 JS"
    js = viewer.page_obj.calls[0]
    assert "welcome-sessions-root" in js and "innerHTML" in js
    assert "<div>x</div>" in js


def test_incremental_refresh_falls_back_when_viewer_not_ready():
    """viewer 缺失 / 懒渲染未完成 / JS 未就绪 / 不可见 → 回退整页（返回 False）。"""
    from app.widgets import message_card as mc

    # 无 viewer 属性（__new__ 绕过 __init__ 的 stub 场景）
    assert mc.MessageCard._refresh_welcome_body_incremental(_make_card(None), "<div>x</div>") is False
    # 懒渲染未完成
    assert (
        mc.MessageCard._refresh_welcome_body_incremental(
            _make_card(_FakeViewer(visible=True, js_ready=True), lazy_rendered=False), "<div>x</div>"
        )
        is False
    )
    # JS 未就绪
    assert (
        mc.MessageCard._refresh_welcome_body_incremental(_FakeViewerCase.js_not_ready_card(), "<div>x</div>") is False
    )
    # 窗口不可见（后台 tab，切回时整页补渲拿新数据）
    assert mc.MessageCard._refresh_welcome_body_incremental(_FakeViewerCase.hidden_card(), "<div>x</div>") is False


class _FakeViewerCase:
    @staticmethod
    def js_not_ready_card():
        return _make_card(_FakeViewer(visible=True, js_ready=False))

    @staticmethod
    def hidden_card():
        return _make_card(_FakeViewer(visible=False, js_ready=True))
