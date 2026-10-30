# -*- coding: utf-8 -*-
"""历史卡「工具与思考」折叠在池化复用路径的回归测试。

背景（2026-10-29 真机现象）：历史会话加载后工具与思考区默认展开。
根因：历史折叠只发生在 ``CodeWebViewer._on_js_ready``；WebViewPool 复用
实例保留骨架（``_is_js_ready=True``），``_on_js_ready`` 不再触发，而
``reset_for_reuse`` 已摘掉 ``data-collapsed`` → 等价展开。

修复：折叠抽成幂等 ``_apply_history_collapsed``，卡片绑定后（
``MessageCard.ensure_rendered``）也调用一次；骨架重建/归还时复位标记。
"""

import re
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def _viewer_stub(page_calls):
    """最小伪 self：_apply_history_collapsed 只读这几个字段。"""

    class _Page:
        def runJavaScript(self, js):
            page_calls.append(js)

    return SimpleNamespace(
        _is_history=True,
        _history_collapse_applied=False,
        _is_js_ready=True,
        page=lambda: _Page(),
    )


def _apply(viewer):
    from app.widgets.card_viewers import CodeWebViewer

    CodeWebViewer._apply_history_collapsed(viewer)


def test_history_collapse_applied_once():
    """历史卡：JS 就绪时折叠一次，重复调用不再折叠（不覆盖用户手动展开）。"""
    calls = []
    v = _viewer_stub(calls)
    _apply(v)
    assert v._history_collapse_applied is True
    assert len(calls) == 1
    assert "data-collapsed','true'" in calls[0].replace('"', "'")
    _apply(v)
    assert len(calls) == 1, "已折叠过不得重复下发（会折回用户手动展开的区域）"


def test_non_history_card_not_collapsed():
    """本轮对话卡（_is_history=False）不折叠。"""
    calls = []
    v = _viewer_stub(calls)
    v._is_history = False
    _apply(v)
    assert calls == []
    assert v._history_collapse_applied is False


def test_not_collapsed_before_js_ready():
    """新建 viewer 的 JS 未就绪：不得置位，交给 _on_js_ready 承担。"""
    calls = []
    v = _viewer_stub(calls)
    v._is_js_ready = False
    _apply(v)
    assert calls == []
    assert v._history_collapse_applied is False


def test_reset_for_reuse_clears_flag():
    """池化归还后标记必须复位：复用方可能是历史卡，也可能是本轮对话。"""
    src = (ROOT / "app/widgets/card_viewers.py").read_text(encoding="utf-8")
    body = src[src.index("def reset_for_reuse"):]
    body = body[: body.index("def ", 10)]
    assert "_history_collapse_applied = False" in body


def test_skeleton_reload_clears_flag():
    """骨架重建（崩溃自愈 / 池化补载）后 data-collapsed 回到初始值，标记必须失效。"""
    src = (ROOT / "app/widgets/card_viewers.py").read_text(encoding="utf-8")
    body = src[src.index("def _load_skeleton"):]
    body = body[: body.index("def ", 10)]
    assert "_history_collapse_applied = False" in body


def test_on_js_ready_uses_shared_method():
    """折叠实现必须单点：_on_js_ready 不得内联自己的 JS 副本（否则两条路径又分叉）。"""
    src = (ROOT / "app/widgets/card_viewers.py").read_text(encoding="utf-8")
    body = src[src.index("def _on_js_ready"):]
    body = body[: body.index("def ", 10)]
    assert "_apply_history_collapsed()" in body
    assert "_ts.setAttribute('data-collapsed','true')" not in body


def test_message_card_applies_after_viewer_bind():
    """卡片绑定 viewer 后必须按历史态补一次折叠（池化路径唯一触发点）。"""
    src = (ROOT / "app/widgets/message_card.py").read_text(encoding="utf-8")
    # 定位 WebEngine 分支（_is_history 赋值处）之后的绑定段
    idx = src.index("self.viewer._restore_finished_ids = self._finished_streaming_ids")
    tail = src[idx : idx + 800]
    assert re.search(r"self\.viewer\._apply_history_collapsed\(\)", tail)
