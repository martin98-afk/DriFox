# -*- coding: utf-8 -*-
"""模型选择器数据构造只收 chat 实例。"""
from app.core.modelmeta.provider_profile import build_selector_provider_data


def test_selector_data_excludes_non_chat():
    valid = {
        "chat-1": {"display_name": "示例", "provider_name": "示例", "模型列表": ["m-chat"],
                   "模型名称": "m-chat", "capability_type": "chat"},
        "img-1": {"display_name": "示例图像", "provider_name": "示例图像",
                  "模型列表": ["dall-e-3"], "模型名称": "dall-e-3",
                  "capability_type": "image_generation"},
    }
    rows, display_to_config, display_to_provider_name = build_selector_provider_data(valid, "chat-1")
    assert [r[0] for r in rows] == ["示例"]
    assert list(display_to_config) == ["示例"]
    # 两反查映射同口径：键集一致，值为 provider_name
    assert list(display_to_provider_name) == ["示例"]
    assert display_to_provider_name["示例"] == "示例"
    # is_current：当前实例为 True
    assert rows[0][2] is True
    # model_list 只含 chat 模型；cur_model 已在列表时不重复前置
    assert rows[0][1] == ["m-chat"]


def test_selector_data_is_current_and_missing_capability_type():
    valid = {
        "chat-1": {"display_name": "甲", "provider_name": "甲", "模型列表": ["m-a"],
                   "模型名称": "m-a", "capability_type": "chat"},
        "chat-2": {"display_name": "乙", "provider_name": "乙", "模型列表": ["m-b"],
                   "模型名称": "m-b", "capability_type": "chat"},
        # 老配置：无 capability_type 键 → 按 chat 放行
        "old-1": {"provider_name": "老配置", "模型列表": ["m-old"], "模型名称": "m-old"},
    }
    rows, display_to_config, display_to_provider_name = build_selector_provider_data(valid, "chat-1")
    assert [r[0] for r in rows] == ["甲", "乙", "老配置"]
    # 非 current 的 chat 配置 is_current 为 False
    assert rows[0][2] is True
    assert rows[1][2] is False
    assert rows[2][2] is False
    # 缺 display_name 键的老配置同样进映射
    assert display_to_config["老配置"] == "old-1"
    assert display_to_provider_name["老配置"] == "老配置"


def test_selector_data_cur_model_prepend():
    # cur_model 不在模型列表中 → 前置插入，当前模型在最前
    valid = {
        "chat-1": {"display_name": "甲", "provider_name": "甲", "模型列表": ["m-a", "m-b"],
                   "模型名称": "m-c", "capability_type": "chat"},
    }
    rows, _, _ = build_selector_provider_data(valid, "chat-1")
    assert rows[0][1] == ["m-c", "m-a", "m-b"]
    assert rows[0][1][0] == "m-c"
