# -*- coding: utf-8 -*-
"""message_content custom 块测试"""
from app.core.message_content import ensure_content_blocks, content_to_markdown


def test_ensure_content_blocks_recognizes_custom():
    """识别 custom 块"""
    blocks = ensure_content_blocks([
        {"type": "custom", "custom_type": "plugin_marketplace", "data": {"x": 1}},
        {"type": "text", "text": "hello"},
    ])
    assert len(blocks) == 2
    assert blocks[0]["type"] == "custom"
    assert blocks[0]["custom_type"] == "plugin_marketplace"
    assert blocks[0]["data"] == {"x": 1}


def test_content_to_markdown_renders_custom_via_registry():
    """content_to_markdown 调用注册渲染器"""
    from app.core.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry.get_instance()
    reg.reset()
    reg.register_content_renderer(
        plugin_name="test", type_name="my_chart",
        render_func=lambda d, c: f"<chart>{d['title']}</chart>", priority=1
    )

    blocks = [{"type": "custom", "custom_type": "my_chart", "data": {"title": "T"}}]
    md = content_to_markdown(blocks)
    assert "<chart>T</chart>" in md
    reg.reset()


def test_content_to_markdown_fallback_for_unregistered_type():
    """未注册的 custom_type 降级为占位文本"""
    blocks = [{"type": "custom", "custom_type": "unknown", "data": {}}]
    md = content_to_markdown(blocks)
    assert "unknown" in md
