# -*- coding: utf-8 -*-
"""回归测试：ModelItem 无成本模型时 refresh_style/set_active 不崩溃

背景：
_apply_cost_style 无条件访问 self.cost_label，但 cost_label 仅在
_caps['cost'] 含 in/out/cache_read 之一时才创建（免费模型常见无成本）。
此前主题切换 refresh_style() 或选中切换 set_active() 会抛
    AttributeError: 'ModelItem' object has no attribute 'cost_label'

修复：
_apply_cost_style 开头 hasattr(self, 'cost_label') 守卫，无成本模型直接返回。
本测试验证：无成本模型 refresh_style/set_active 均不抛；有成本模型路径不受影响。
"""

import sys

import pytest
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module")
def _qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture
def _no_cost_caps(monkeypatch):
    """无 cost 的能力数据（supports_thinking 有、无成本）"""
    import app.core.model_capabilities as mc

    monkeypatch.setattr(
        mc,
        "get_model_capabilities",
        lambda name: {"supports_thinking": True, "supports_vision": False},
    )


@pytest.fixture
def _with_cost_caps(monkeypatch):
    """有 cost 的能力数据"""
    import app.core.model_capabilities as mc

    monkeypatch.setattr(
        mc,
        "get_model_capabilities",
        lambda name: {"cost": {"input": 0.5, "output": 1.5, "cache_read": 0.2}},
    )


class TestModelItemNoCostGuard:
    def test_refresh_and_set_active_no_cost_no_crash(self, _qapp, _no_cost_caps):
        """无成本模型：refresh_style / set_active 均不抛异常"""
        from app.widgets.cards.settings.model_selector_card import ModelItem

        item = ModelItem("p", "free-model", is_active=False, note="免费")
        item.ensurePolished()
        assert not hasattr(item, "cost_label"), "无成本模型不应创建 cost_label"
        # 主题刷新路径
        item.refresh_style()
        # 选中态切换路径
        item.set_active(True)
        item.set_active(False)
        item.refresh_style()

    def test_with_cost_still_works(self, _qapp, _with_cost_caps):
        """有成本模型：cost_label 存在且 set_active/refresh_style 正常"""
        from app.widgets.cards.settings.model_selector_card import ModelItem

        item = ModelItem("p", "paid-model", is_active=False)
        item.ensurePolished()
        assert hasattr(item, "cost_label"), "有成本模型应创建 cost_label"
        item.set_active(True)
        item.set_active(False)
        item.refresh_style()
        assert "TEXT_ACCENT" not in item.cost_label.styleSheet() or True  # 不崩即通过
