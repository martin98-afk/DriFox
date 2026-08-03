# -*- coding: utf-8 -*-
"""message_content custom 块测试"""

from app.core.message_content import ensure_content_blocks, content_to_markdown, messages_to_api


def test_tool_call_reasoning_content_is_preserved_when_explicitly_empty():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        }
    ]

    api_message = messages_to_api(messages)[0]

    assert "reasoning_content" in api_message
    assert api_message["reasoning_content"] == ""


def test_tool_call_reasoning_content_can_be_required_for_deepseek_compatibility():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        }
    ]

    api_message = messages_to_api(messages, requires_reasoning_content=True)[0]

    assert api_message["reasoning_content"] == ""


def test_ensure_content_blocks_recognizes_custom():
    """识别 custom 块"""
    blocks = ensure_content_blocks(
        [
            {"type": "custom", "custom_type": "plugin_marketplace", "data": {"x": 1}},
            {"type": "text", "text": "hello"},
        ]
    )
    assert len(blocks) == 2
    assert blocks[0]["type"] == "custom"
    assert blocks[0]["custom_type"] == "plugin_marketplace"
    assert blocks[0]["data"] == {"x": 1}


def test_content_to_markdown_renders_custom_via_registry():
    """content_to_markdown 调用注册渲染器

    注意：``UIPluginRegistry.reset()`` 会把单例本身置为 ``None``，下一次
    ``get_instance()`` 会得到全新实例。因此 reset 后必须重新
    ``get_instance()``，否则后续注册会落到旧实例上，而 ``content_to_markdown``
    内部再次 ``get_instance()`` 时拿到的是全新空实例，导致无法命中注册器。
    """
    from app.core.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry.get_instance()
    reg.reset()
    # reset() 会把单例置 None，需重新拿一次
    reg = UIPluginRegistry.get_instance()
    reg.register_content_renderer(
        plugin_name="test", type_name="my_chart", render_func=lambda d, c: f"<chart>{d['title']}</chart>", priority=1
    )

    try:
        blocks = [{"type": "custom", "custom_type": "my_chart", "data": {"title": "T"}}]
        md = content_to_markdown(blocks)
        assert "<chart>T</chart>" in md
    finally:
        # 用与 reset 等价的清理：这里直接重置（reset() 会再次清单例，
        # 但下一个测试会在第一次 get_instance() 时获得新实例）
        reg = UIPluginRegistry.get_instance()
        reg.reset()


def test_content_to_markdown_fallback_for_unregistered_type():
    """未注册的 custom_type 降级为占位文本"""
    blocks = [{"type": "custom", "custom_type": "unknown", "data": {}}]
    md = content_to_markdown(blocks)
    assert "unknown" in md
