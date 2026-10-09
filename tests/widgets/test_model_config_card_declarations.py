# -*- coding: utf-8 -*-
"""P1-9 段三回归：模型配置卡「能力声明」六键。

关键红线：「自动」→ get_config **删键**（防 copy 残留——残留的声明值会
假冒用户声明压制 models.dev 自动链）。
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PyQt5.QtCore import QCoreApplication

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture()
def card(_qapp):
    from app.widgets.cards.settings.model_config_card import ModelConfigCard

    return ModelConfigCard()


def _config(**overrides):
    base = {"温度": 0.7, "最大Token": 32000}
    base.update(overrides)
    return base


def test_tri_state_auto_deletes_key(card):
    """红线：三态选「自动」→ get_config 删键（防 copy 残留假声明）"""
    card.set_config("测试", _config(声明_支持思考=True), "m1")
    widget = card._widgets["声明_支持思考"][1]
    widget.setCurrentText("自动")

    got = card.get_config()
    assert "声明_支持思考" not in got, "「自动」必须删键（残留会假冒用户声明）"
    assert got["温度"] == 0.7, "常规键不受影响"


def test_tri_state_on_off_roundtrip(card):
    """三态 开启/关闭 → bool 写入；bool 值装载回显正确"""
    card.set_config("测试", _config(声明_支持思考=True, 声明_支持图像=False), "m1")
    assert card._widgets["声明_支持思考"][1].currentText() == "开启"
    assert card._widgets["声明_支持图像"][1].currentText() == "关闭"

    card._widgets["声明_支持图像"][1].setCurrentText("开启")
    got = card.get_config()
    assert got["声明_支持思考"] is True
    assert got["声明_支持图像"] is True
    # 键缺失回显「自动」
    card.set_config("测试", _config(), "m1")
    assert card._widgets["声明_支持思考"][1].currentText() == "自动"


def test_lineedit_int_empty_deletes_and_int_stored(card):
    """lineedit_int：空串 → 删键；数值 → 原样写入"""
    card.set_config("测试", _config(声明_上下文长度=262144, 声明_最大输出=""), "m1")
    assert card._widgets["声明_上下文长度"][1].text() == "262144"
    assert card._widgets["声明_最大输出"][1].text() == ""

    card._widgets["声明_上下文长度"][1].setText("")
    card._widgets["声明_最大输出"][1].setText("8192")
    got = card.get_config()
    assert "声明_上下文长度" not in got, "留空 = 未声明，删键"
    assert got["声明_最大输出"] == "8192"


def test_lineedit_text_passthrough(card):
    """思考参数/强度 lineedit：文本透传（resolve 链容忍空串）"""
    card.set_config("测试", _config(声明_思考参数="thinking_budget", 声明_思考强度="high, low"), "m1")
    got = card.get_config()
    assert got["声明_思考参数"] == "thinking_budget"
    assert got["声明_思考强度"] == "high, low"


def test_declaration_group_rendered_with_sources_hint(card, monkeypatch):
    """能力声明组渲染 + 来源标注小字（自动链生效层）"""
    import app.core.modelmeta.model_capabilities as mc

    monkeypatch.setattr(
        mc,
        "get_model_capabilities",
        lambda model, provider="": {"context_limit": 128000, "supports_thinking": True},
    )
    card.set_config("测试", _config(声明_支持思考=True, 声明_上下文长度=""), "m1")

    labels = [lb.text() for lb in card.findChildren(type(card._widgets["声明_支持思考"][0]))]
    assert any("声明" in t for t in labels), "声明键应渲染"
    # 六键全部在卡上
    for key in (
        "声明_支持思考",
        "声明_支持图像",
        "声明_上下文长度",
        "声明_最大输出",
        "声明_思考参数",
        "声明_思考强度",
    ):
        assert key in card._widgets, f"{key} 应在配置卡渲染"
