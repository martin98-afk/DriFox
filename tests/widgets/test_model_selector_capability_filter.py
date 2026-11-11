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
    rows, display_to_config = build_selector_provider_data(valid, "chat-1")
    assert [r[0] for r in rows] == ["示例"]
    assert list(display_to_config) == ["示例"]
