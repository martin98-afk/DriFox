# -*- coding: utf-8 -*-
"""回归：模型配置卡隐藏「能力声明」六键（2026-10-13 用户决策）。

背景：能力声明六键（声明_支持思考/图像/上下文长度/最大输出/思考参数/思考强度）
曾常驻渲染在模型参数卡（P1-9 段三）。用户决策：参数卡不再显示，声明能力
继续走后端 resolve 链（app/core/modelmeta/model_capabilities.py，零改动）。

两道红线：
1. 六键一个都不许渲染成控件（含 set_config 的常驻补齐路径）
2. 已有声明值必须经 set_config -> get_config 原样透传（hide_in_card 只挡
   渲染，不得丢数据——否则用户已保存的声明被静默抹掉，能力判定漂移）
"""

import pytest
from qfluentwidgets import BodyLabel

DECL_KEYS = (
    "声明_支持思考",
    "声明_支持图像",
    "声明_上下文长度",
    "声明_最大输出",
    "声明_思考参数",
    "声明_思考强度",
)


@pytest.fixture()
def card(qapp):
    """qapp 用 tests/conftest.py 的 session 级共享单例（勿自建 QApplication）。"""
    from app.widgets.cards.settings.model_config_card import ModelConfigCard

    return ModelConfigCard()


def _config(**overrides):
    base = {"温度": 0.7, "最大Token": 32000}
    base.update(overrides)
    return base


def test_declaration_keys_not_rendered(card):
    """红线 1：六键不渲染（config 带值与不带值两种形态都查）"""
    for cfg in (_config(), _config(声明_支持思考=True, 声明_上下文长度=262144)):
        card.set_config("测试", cfg, "m1")
        for key in DECL_KEYS:
            assert key not in card._widgets, f"{key} 不得渲染到模型参数卡"


def test_no_declaration_group_header(card):
    """红线 1 附带：组头与风险提示小字一并消失"""
    card.set_config("测试", _config(声明_支持思考=True), "m1")
    texts = [lb.text() for lb in card.findChildren(BodyLabel)]
    assert "能力声明" not in texts, "组头不得出现"
    assert not any(t.startswith("留空/自动") or t.startswith("已声明") for t in texts), "组头下的来源标注小字不得出现"


def test_existing_declarations_passthrough_on_save(card):
    """红线 2：已有声明值经 set_config -> get_config 原样透传，不丢不改"""
    cfg = _config(
        声明_支持思考=True,
        声明_支持图像=False,
        声明_上下文长度=262144,
        声明_最大输出="8192",
        声明_思考参数="thinking_budget",
        声明_思考强度="high, low",
    )
    card.set_config("测试", cfg, "m1")
    got = card.get_config()
    for key in DECL_KEYS:
        assert got[key] == cfg[key], f"已有声明值 {key} 被静默抹掉或改写"
    assert got["温度"] == 0.7, "常规键不受影响"
