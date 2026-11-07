# -*- coding: utf-8 -*-
"""模型能力编辑弹窗的「接口入口」下拉回归。

背景（2026-11-07）：参数卡渲染是配置驱动的（`config.items()`），三条注入链都
不产出「协议入口」键 → 参数卡永远不显示该行。用户决策把编辑入口收口到本弹窗，
与六项能力声明同屏；落盘仍写 `LLM.ModelOverrides` 同条目。
"""

import pytest


@pytest.fixture()
def dialog_parent(qapp):
    """MaskDialogBase 构造期要读 parent.width()，必须给真实父控件"""
    from PyQt5.QtWidgets import QWidget

    return QWidget()


@pytest.fixture()
def overrides(monkeypatch):
    """替换 Settings.llm_model_overrides 为可控假条目，测试后自动还原"""
    from app.utils.config import Settings

    class _FakeItem:
        def __init__(self, value):
            self.value = value

    item = _FakeItem({})
    monkeypatch.setattr(Settings, "llm_model_overrides", item, raising=False)
    return item


def _make_dialog(parent):
    from app.widgets.model_capability_edit_dialog import ModelCapabilityEditDialog

    return ModelCapabilityEditDialog("gpt-6-luna", "OpenCode Go", parent=parent)


def _items(combo):
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_protocol_row_options(qapp, dialog_parent, overrides):
    """下拉至少含 自动 / Responses API 两项，userData 为空串与 responses"""
    dialog = _make_dialog(dialog_parent)
    items = _items(dialog.protocol_combo)
    assert ("自动", "") in items
    assert ("Responses API", "responses") in items


def test_protocol_row_echoes_saved_value(qapp, dialog_parent, overrides):
    """已保存 responses → 打开弹窗即选中对应项"""
    overrides.value = {"OpenCode Go||gpt-6-luna": {"协议入口": "responses"}}
    dialog = _make_dialog(dialog_parent)
    assert dialog.protocol_combo.currentData() == "responses"


def test_protocol_row_keeps_unregistered_transport(qapp, dialog_parent, overrides):
    """手选插件已禁用/卸载 → 原值单独成项保留，不静默回退自动"""
    overrides.value = {"OpenCode Go||gpt-6-luna": {"协议入口": "transport:gone_plugin"}}
    dialog = _make_dialog(dialog_parent)
    assert dialog.protocol_combo.currentData() == "transport:gone_plugin"


def test_protocol_row_defaults_to_auto(qapp, dialog_parent, overrides):
    """无保存值 → 首项「自动」"""
    dialog = _make_dialog(dialog_parent)
    assert dialog.protocol_combo.currentData() == ""
    assert dialog.protocol_entry == ""


def test_save_returns_protocol_entry(qapp, dialog_parent, overrides):
    """保存时把下拉 userData 交给调用方（空值 = 自动），声明集不受影响"""
    dialog = _make_dialog(dialog_parent)
    idx = dialog.protocol_combo.findData("responses")
    assert idx >= 0
    dialog.protocol_combo.setCurrentIndex(idx)
    dialog._on_save()
    assert dialog.saved is True
    assert dialog.protocol_entry == "responses"
    assert dialog.declared == {}


def test_reset_clears_protocol_entry(qapp, dialog_parent, overrides):
    """「全部重置为自动」把接口入口一起清空"""
    overrides.value = {"OpenCode Go||gpt-6-luna": {"协议入口": "responses"}}
    dialog = _make_dialog(dialog_parent)
    dialog._on_reset()
    assert dialog.protocol_entry == ""
