# -*- coding: utf-8 -*-
"""DiffViewWidget.update_diff 签名短路（#28）行为测试。

契约：同内容重复刷新跳过全量重建（setHtml 不触发）；内容变化必重建；
签名属性随内容更新。主题切换走整卡重建（refresh_theme → _reconcile），
不受短路影响。
"""

from __future__ import annotations

import sys
import time

import pytest
from PyQt5.QtWidgets import QApplication

from app.widgets.markdown_block_viewer import DiffViewWidget


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _count_setHtml(widget, monkeypatch):
    """替换实例 setHtml 为计数桩，返回计数列表。"""
    calls = []
    monkeypatch.setattr(widget, "setHtml", lambda html: calls.append(html))
    return calls


def test_repeat_same_content_skips_rebuild(qapp, monkeypatch):
    w = DiffViewWidget("line-a\nline-b")
    calls = _count_setHtml(w, monkeypatch)
    for _ in range(50):
        w.update_diff("line-a\nline-b")
    assert calls == []  # 同内容 50 次，零重建


def test_content_change_rebuilds(qapp, monkeypatch):
    w = DiffViewWidget("line-a\nline-b")
    calls = _count_setHtml(w, monkeypatch)
    w.update_diff("line-a\nline-c")
    assert len(calls) == 1  # 内容变化必重建
    w.update_diff("line-a\nline-c")
    assert len(calls) == 1  # 再刷同内容仍跳过


def test_signature_updates_with_content(qapp):
    w = DiffViewWidget("v1")
    sig1 = w._diff_sig
    w.update_diff("v2")
    assert w._diff_sig != sig1
    w.update_diff("v2")
    assert w._diff_sig != sig1  # 稳定


def test_equal_length_swap_not_stale(qapp):
    """等长替换（避免任何采样假阴性）必须重建。"""
    w = DiffViewWidget("aaaa")
    with pytest.MonkeyPatch.context() as mp:
        calls = _count_setHtml(w, mp)
        w.update_diff("bbbb")
        assert len(calls) == 1


def test_bench_3000_lines_50_repeats(qapp):
    """量化：3000 行 diff 连续 50 次刷新，短路启用应远快于每次重建。"""
    diff = "\n".join(f"{'+' if i % 2 else '-'} line-{i} with content padding" for i in range(3000))
    w = DiffViewWidget(diff)

    t0 = time.perf_counter()
    for _ in range(50):
        w.update_diff(diff)
    dt_shortcircuit = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(50):
        w._diff_sig = None  # 模拟旧行为：无短路，每次全量重建
        w.update_diff(diff)
    dt_rebuild = time.perf_counter() - t0

    assert dt_shortcircuit < dt_rebuild, (dt_shortcircuit, dt_rebuild)
    print(f"\n[bench] shortcircuit={dt_shortcircuit:.3f}s rebuild={dt_rebuild:.3f}s")
