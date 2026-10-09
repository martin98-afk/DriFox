# -*- coding: utf-8 -*-
"""P2-13 回归：能力徽章组件 + tooltip 纯函数

背景
----
「能力徽章」原先在两处各写一份：`model_selector_card.ModelItem` 的 UI 段、
`main_widget` 模型按钮 tooltip 的文本拼装。两处对「什么算能力」的判定迟早漂移，
故抽出 `app/widgets/capability_badges.py`（组件 + 纯函数 + 配色辅助）。

本组覆盖：
1. `build_capability_tooltip` 纯函数三态（思考 / 强度 / 多模态及组合、空 caps）
2. `ModelCapabilityBadges` 组件：三徽章渲染、show 掩码过滤、空 caps 不出徽章
3. `has_any_badge` 与掩码一致（供调用方对齐判定复用）
4. `_format_cost_number` 仍在 model_selector_card.py（红线：main_widget 跨文件 import）
"""

import sys

import pytest
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module")
def _qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    return app


# ══════════════════════════════════════════════════════════════════
# 纯函数：build_capability_tooltip
# ══════════════════════════════════════════════════════════════════


class TestBuildCapabilityTooltip:
    def test_thinking_only(self):
        from app.widgets.capability_badges import build_capability_tooltip

        assert build_capability_tooltip({"supports_thinking": True}) == "开关思考"

    def test_vision_only(self):
        from app.widgets.capability_badges import build_capability_tooltip

        assert build_capability_tooltip({"supports_vision": True}) == "多模态"

    def test_effort_values(self):
        from app.widgets.capability_badges import build_capability_tooltip

        out = build_capability_tooltip({"reasoning_effort_values": ["high", "max"]})
        assert out == "思考强度: high/max"

    def test_all_three_combined(self):
        """三类齐全 → 空格分隔的顺序：思考 → 强度 → 多模态"""
        from app.widgets.capability_badges import build_capability_tooltip

        caps = {
            "supports_thinking": True,
            "reasoning_effort_values": ["low", "medium", "high"],
            "supports_vision": True,
        }
        assert build_capability_tooltip(caps) == "开关思考  思考强度: low/medium/high  多模态"

    def test_empty_caps_returns_empty(self):
        from app.widgets.capability_badges import build_capability_tooltip

        assert build_capability_tooltip({}) == ""
        assert build_capability_tooltip(None) == ""

    def test_false_flags_produce_nothing(self):
        """显式 False 不得出现（防「不支持也显示」的回归）"""
        from app.widgets.capability_badges import build_capability_tooltip

        assert build_capability_tooltip({"supports_thinking": False, "supports_vision": False}) == ""

    def test_cost_fields_ignored(self):
        """纯函数只管能力，成本由调用方拼（职责边界）"""
        from app.widgets.capability_badges import build_capability_tooltip

        assert build_capability_tooltip({"cost": {"input": 1, "output": 2}}) == ""


# ══════════════════════════════════════════════════════════════════
# 组件：ModelCapabilityBadges
# ══════════════════════════════════════════════════════════════════


class TestModelCapabilityBadges:
    def test_all_three_badges_rendered(self, _qapp):
        from app.widgets.capability_badges import ModelCapabilityBadges

        caps = {
            "supports_thinking": True,
            "reasoning_effort_values": ["high", "max"],
            "supports_vision": True,
        }
        w = ModelCapabilityBadges(caps)
        assert w.think_label.text() == "开关思考"
        assert w.effort_label.text() == "思考强度"
        assert w.vision_label.text() == "多模态"
        assert w.has_any_badge() is True

    def test_missing_categories_skipped(self, _qapp):
        """无数据的类别不占位（不创建属性）"""
        from app.widgets.capability_badges import ModelCapabilityBadges

        w = ModelCapabilityBadges({"supports_thinking": True})
        assert hasattr(w, "think_label")
        assert not hasattr(w, "effort_label"), "无 effort 数据不应创建"
        assert not hasattr(w, "vision_label"), "无 vision 数据不应创建"

    def test_empty_caps_no_badges(self, _qapp):
        from app.widgets.capability_badges import ModelCapabilityBadges

        w = ModelCapabilityBadges({})
        assert w.has_any_badge() is False

    def test_show_mask_filters_badges(self, _qapp):
        """show 掩码只显指定类别（调用方按掩码对齐名称列宽度）"""
        from app.widgets.capability_badges import BADGE_THINKING, ModelCapabilityBadges

        caps = {"supports_thinking": True, "supports_vision": True}
        w = ModelCapabilityBadges(caps, show=frozenset({BADGE_THINKING}))
        assert hasattr(w, "think_label")
        assert not hasattr(w, "vision_label"), "掩码未含 vision → 不渲染"
        assert w.has_any_badge() is True

    def test_empty_mask_renders_nothing(self, _qapp):
        from app.widgets.capability_badges import ModelCapabilityBadges

        w = ModelCapabilityBadges({"supports_thinking": True, "supports_vision": True}, show=frozenset())
        assert w.has_any_badge() is False

    def test_badge_has_tooltip(self, _qapp):
        """徽章带 tooltip（悬停解释），与原实现一致"""
        from app.widgets.capability_badges import ModelCapabilityBadges

        w = ModelCapabilityBadges({"supports_vision": True})
        assert w.vision_label.toolTip() == "支持多模态输入"

    def test_effort_badge_tooltip_includes_values(self, _qapp):
        from app.widgets.capability_badges import ModelCapabilityBadges

        w = ModelCapabilityBadges({"reasoning_effort_values": ["high", "max"]})
        assert "high/max" in w.effort_label.toolTip()

    def test_null_caps_ok(self, _qapp):
        from app.widgets.capability_badges import ModelCapabilityBadges

        w = ModelCapabilityBadges(None)
        assert w.has_any_badge() is False


# ══════════════════════════════════════════════════════════════════
# 红线：成本逻辑不得随迁
# ══════════════════════════════════════════════════════════════════


def test_format_cost_number_stays_in_model_selector_card():
    """`_format_cost_number` 必须仍在 model_selector_card.py

    红线原因：`main_widget.py:7566` 跨文件 `from ...model_selector_card import
    _format_cost_number`——迁移会直接 ImportError 打断启动。
    """
    from app.widgets.cards.settings import model_selector_card as msc

    assert hasattr(msc, "_format_cost_number"), "成本格式化函数必须留在原模块"
    assert msc._format_cost_number(3.0) == "3"
    assert msc._format_cost_number(None) == ""


def test_capability_badges_has_no_cost_logic():
    """反向：能力徽章模块不掺成本逻辑（职责边界）

    只检查**代码**（去掉注释与 docstring 后）不含 cost 相关实现——docstring 里
    明确写「成本逻辑不在此模块」是有意保留的边界说明。
    """
    import ast

    from app.widgets import capability_badges as cb

    assert not hasattr(cb, "_format_cost_number")
    src = open(cb.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    # 去掉 docstring / 注释后的实际代码文本
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(node):
            node.body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    code = ast.unparse(tree)
    assert "cost" not in code.lower(), "能力模块的代码不应出现成本相关逻辑"


# ══════════════════════════════════════════════════════════════════
# 集成：ModelItem 用组件后行为不变
# ══════════════════════════════════════════════════════════════════


class TestModelItemIntegration:
    def test_model_item_exposes_badges_via_component(self, _qapp, monkeypatch):
        """ModelItem 徽章改由组件承载；旧属性名（think_label 等）仍可访问"""
        import app.core.modelmeta.model_capabilities as mc

        monkeypatch.setattr(
            mc,
            "get_model_capabilities",
            lambda name: {"supports_thinking": True, "supports_vision": True},
        )
        from app.widgets.cards.settings.model_selector_card import ModelItem

        item = ModelItem("p", "m", is_active=False)
        assert hasattr(item, "cap_badges"), "应挂组件"
        assert item.think_label is item.cap_badges.think_label
        assert item.vision_label is item.cap_badges.vision_label

    def test_model_item_tooltip_uses_shared_function(self, _qapp, monkeypatch):
        """tooltip 能力段与纯函数同源（拼装结果一致）"""
        import app.core.modelmeta.model_capabilities as mc

        caps = {"supports_thinking": True, "supports_vision": True, "cost": {"input": 1}}
        monkeypatch.setattr(mc, "get_model_capabilities", lambda name: caps)
        from app.widgets.cards.settings.model_selector_card import ModelItem
        from app.widgets.capability_badges import build_capability_tooltip

        item = ModelItem("p", "m", is_active=False)
        tip = item._model_tooltip()
        assert build_capability_tooltip(caps) in tip
        assert "in: 1" in tip, "成本段应保留"
