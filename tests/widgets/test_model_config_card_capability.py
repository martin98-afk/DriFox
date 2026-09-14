# -*- coding: utf-8 -*-
"""模型配置卡「模型能力」分组渲染测试。

QApplication 由 tests/conftest.py 的全局 qapp fixture 提供（含
AA_ShareOpenGLContexts 前置设置）。
"""

import pytest


@pytest.fixture
def card(qapp):
    from app.widgets.cards.settings.model_config_card import ModelConfigCard

    return ModelConfigCard()


def test_capability_group_exists(card):
    """「模型能力」分组已登记"""
    from app.widgets.cards.settings.model_config_card import _FIELD_GROUPS

    names = [name for name, _keys in _FIELD_GROUPS]
    assert "模型能力" in names


def test_capability_group_first(card):
    """能力分组排在最前（用户填自定义模型时第一眼看到）"""
    from app.widgets.cards.settings.model_config_card import _FIELD_GROUPS

    assert _FIELD_GROUPS[0][0] == "模型能力"


def test_capability_fields_rendered(card):
    """传入能力字段时渲染出对应控件"""
    config = {"支持思考": True, "支持多模态": False, "思考参数": "thinking"}
    card.set_config("测试服务商", config, "my-custom-model")
    for key in ("支持思考", "支持多模态", "思考参数"):
        assert key in card._widgets, f"{key} 未渲染"


def test_collect_roundtrip(card):
    """渲染后能回收同样的值"""
    config = {"支持思考": True, "支持多模态": False}
    card.set_config("测试服务商", config, "my-custom-model")
    collected = card.get_config()
    assert collected.get("支持思考") is True
    assert collected.get("支持多模态") is False


def test_provider_name_stored(card):
    """set_config 保存 provider_name（combobox 分支的能力查询需要）"""
    card.set_config("测试服务商", {"支持思考": True}, "m", "P")
    assert card.current_provider_name == "P"


def test_provider_name_defaults_empty(card):
    card.set_config("测试服务商", {"支持思考": True}, "m")
    assert card.current_provider_name == ""


def test_effort_values_from_override_used(card, monkeypatch):
    """用户填的「思考等级可选值」应进入思考等级下拉（覆盖 PARAM_SCHEMA 固定默认）"""
    from app.core import model_capabilities as mc

    # 构造 reasoning_effort 型的自定义模型能力（等价于用户覆盖生效后的结果）
    monkeypatch.setattr(
        mc,
        "get_model_capabilities",
        lambda model_name, provider_name="": {
            "supports_thinking": True,
            "thinking_param": "reasoning_effort",
            "reasoning_effort_values": ["low", "ultra"],
        },
    )
    config = {"思考等级": "low", "思考模式": True}
    card.set_config("测试服务商", config, "custom-model", "P")

    widget = card._widgets.get("思考等级", (None, None))[1]
    assert widget is not None, "思考等级未渲染（reasoning_effort 型应渲染）"
    options = [widget.itemText(i) for i in range(widget.count())]
    assert options == ["low", "ultra"], f"用户覆盖的可选值未生效，实际选项: {options}"
