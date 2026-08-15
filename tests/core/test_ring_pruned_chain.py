# -*- coding: utf-8 -*-
"""S2 修复复验：ring 层 pruned_tokens 链路（set_usage → tooltip 数据）

review 指出 main_widget 未把 snapshot 的 pruned_tokens 传给 ring.set_usage，
导致 tooltip「工具结果截断节省」恒不显示。本测试钉住 ring 层契约：
set_usage(pruned_tokens=...) 必须存储并在 tooltip 数据中透传。
（main_widget 两处调用点已补齐接线，代码走读确认）
"""
import pytest
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_ring(qapp):
    from app.widgets.context_usage_ring import ContextUsageRing

    return ContextUsageRing()


class TestRingPrunedTokensChain:
    def test_set_usage_stores_pruned(self, qapp):
        ring = _make_ring(qapp)
        ring.set_usage(50, 1000, 2000, pruned_tokens=1234)
        assert ring._pruned_tokens == 1234

    def test_default_pruned_is_zero(self, qapp):
        """未传 pruned_tokens 时不报错且为 0（向后兼容）"""
        ring = _make_ring(qapp)
        ring.set_usage(10, 100, 200)
        assert ring._pruned_tokens == 0

    def test_tooltip_data_contains_pruned(self, qapp):
        """_rebuild_tooltip 传给 tooltip 的数据含 pruned_tokens"""
        ring = _make_ring(qapp)
        ring.set_usage(50, 1000, 2000, pruned_tokens=500)
        ring._rebuild_tooltip()
        data = ring._tooltip._data
        assert data.get("pruned_tokens") == 500

    def test_tooltip_renders_pruned_line(self, qapp):
        """_rebuild_extras 渲染「工具结果截断节省」行"""
        ring = _make_ring(qapp)
        ring.set_usage(50, 1000, 2000, pruned_tokens=8888)
        ring._tooltip.set_data({
            "used_tokens": 1000,
            "budget_tokens": 2000,
            "percent": 50,
            "ring_color": "#5aa9ff",
            "compaction": {},
            "normal_tokens": 0,
            "compacted_tokens": 0,
            "breakdown": [],
            "cache": {},
            "pruned_tokens": 8888,
        })
        ring._tooltip._rebuild_extras(ring._tooltip._data)
        text = ring._tooltip._extras.text()
        assert "工具结果截断节省: 8,888 tokens" in text

    def test_tooltip_hides_zero_pruned(self, qapp):
        """pruned_tokens=0 时不显示节省行"""
        ring = _make_ring(qapp)
        ring.set_usage(50, 1000, 2000, pruned_tokens=0)
        ring._tooltip.set_data({
            "used_tokens": 1000,
            "budget_tokens": 2000,
            "percent": 50,
            "ring_color": "#5aa9ff",
            "compaction": {},
            "normal_tokens": 0,
            "compacted_tokens": 0,
            "breakdown": [],
            "cache": {},
            "pruned_tokens": 0,
        })
        ring._tooltip._rebuild_extras(ring._tooltip._data)
        text = ring._tooltip._extras.text()
        assert "工具结果截断节省" not in text
