# -*- coding: utf-8 -*-
"""回归：流式结束「差量收尾」的判定与思考块定稿配对

背景
----
流式结束原本无条件全量重渲染：`container.innerHTML = newHtml` 整页替换，
把流式期间已经差量渲染好的稳定区一起销毁重建 —— 表现为结束瞬间内容整体
重排闪一下，工具区坞态归位也跟着跳。

差量收尾改走「稳定区 DOM 不动 + 只把剩余段差量上去 + 流式思考块就地定稿」，
定位靠 `data-flip-key="think-{ordinal}"`（流式态与完成态共用同一 ordinal，
见 `_render_think_block`）。本文件锁定这三件事：

1. `_iter_think_segments` 的 ordinal 必须与 `_inject_think_cards` 完全一致
   （错一位 = 把 A 思考块的内容替换到 B 块上）；
2. `_should_incremental_finalize` 的判定门槛；
3. 开关默认关闭（先合代码不行为）。
"""

from unittest.mock import MagicMock

import pytest

import app.widgets.message_card as mw
from app.widgets.message_card import (  # noqa: E402
    CodeWebViewer,
    _iter_think_segments,
)


def _make_viewer(**overrides):
    """构造 `_should_incremental_finalize` 可运行的最小 viewer 实例。

    不实例化完整 QWebEngineView（依赖重），`__new__` 后手工补齐属性 ——
    PyQt 对象未经 __init__ 时 getattr 未定义属性会抛 RuntimeError。
    """
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._stable_md_len = 0
    v._injected_pending_tools = None
    v._has_active_tool_dom = MagicMock(return_value=False)
    for key, value in overrides.items():
        setattr(v, key, value)
    return v


# ─── 1. 思考块序号：必须与 _inject_think_cards 对齐 ───────────────


def test_iter_think_segments_ordinal_and_closed():
    md = "前言<think>思考A</think>中段<think>思考B</think>结尾"
    assert _iter_think_segments(md) == [(0, "思考A", True), (1, "思考B", True)]


def test_iter_think_segments_unclosed_tail():
    """未闭合（流式输出中途）→ closed=False，末尾不被当成已完成块定稿"""
    md = "前<think>还在想"
    assert _iter_think_segments(md) == [(0, "还在想", False)]


def test_iter_think_segments_empty_block_does_not_take_ordinal():
    """空思考块跳过且不占序号 —— 与 _inject_think_cards 的 `if content.strip()` 一致"""
    md = "<think>   </think>正文<think>真思考</think>"
    assert _iter_think_segments(md) == [(0, "真思考", True)]


def test_iter_think_segments_matches_inject_flip_key():
    """端到端对齐：同一段 md 里，第 N 个块拿到的 ordinal 就是 DOM 上的 flip-key"""
    md = "前<think>A</think>中<think>B</think>后<think>C</think>"
    html = mw._inject_think_cards(md, completed=False)
    ordinals = [o for o, _c, _cl in _iter_think_segments(md)]
    assert ordinals == [0, 1, 2]
    for o in ordinals:
        assert f'data-flip-key="think-{o}"' in html


# ─── 2. 判定门槛 ─────────────────────────────────────────────────


def test_finalize_disabled_by_default():
    """开关默认关闭：差量收尾不得在未经实机验证的情况下生效"""
    assert mw.INCREMENTAL_FINALIZE_ENABLED is False


def test_finalize_requires_switch_on(monkeypatch):
    monkeypatch.setattr(mw, "INCREMENTAL_FINALIZE_ENABLED", False)
    v = _make_viewer(_stable_md_len=120)
    assert v._should_incremental_finalize() is False


def test_finalize_requires_stable_region(monkeypatch):
    """流式期间一次差量都没走过 → 没有可保留的稳定区，必须走全量"""
    monkeypatch.setattr(mw, "INCREMENTAL_FINALIZE_ENABLED", True)
    v = _make_viewer(_stable_md_len=0)
    assert v._should_incremental_finalize() is False


def test_finalize_rejected_when_tool_dom_active(monkeypatch):
    """有活跃工具运行框 → 回退全量（工具块需要整页重排归位）"""
    monkeypatch.setattr(mw, "INCREMENTAL_FINALIZE_ENABLED", True)
    v = _make_viewer(_stable_md_len=120, _has_active_tool_dom=MagicMock(return_value=True))
    assert v._should_incremental_finalize() is False


def test_finalize_rejected_when_pending_injected_tools(monkeypatch):
    monkeypatch.setattr(mw, "INCREMENTAL_FINALIZE_ENABLED", True)
    v = _make_viewer(_stable_md_len=120, _injected_pending_tools=["t1"])
    assert v._should_incremental_finalize() is False


def test_finalize_allowed_when_all_conditions_met(monkeypatch):
    monkeypatch.setattr(mw, "INCREMENTAL_FINALIZE_ENABLED", True)
    v = _make_viewer(_stable_md_len=120)
    assert v._should_incremental_finalize() is True


if __name__ == "__main__":
    pytest.main([__file__])
