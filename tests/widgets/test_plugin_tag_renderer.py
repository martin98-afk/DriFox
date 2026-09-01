# -*- coding: utf-8 -*-
"""插件内联标签渲染器（tag renderer）测试。

覆盖：
- UIPluginRegistry.register_tag_renderer / get_tag_renderer / 卸载清理
- message_card._inject_tag_cards（HTML 路径）：完成态 / 流式半截 / 孤立 close / 直通
- markdown_block_viewer.parse_blocks（Qt 路径）：tag 块切分 / 与 code fence 混排
- assistant_hub 的 <mood> 渲染器：四段解析 / XSS escape / 流式占位
"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def clean_tag_registry():
    """清空 tag renderer 注册表，测试后恢复清理（不影响其他扩展点单例状态）。"""
    reg = UIPluginRegistry.get_instance()
    reg._tag_renderers.clear()
    yield reg
    reg._tag_renderers.clear()


# ── 注册表 ──────────────────────────────────────────────


class TestTagRendererRegistry:
    def test_register_and_get(self, clean_tag_registry):
        reg = clean_tag_registry
        reg.register_tag_renderer("p1", "Mood", lambda c, ctx: "A", priority=0)
        assert reg.get_tag_renderer("mood") is not None
        assert reg.get_tag_renderer("MOOD") is not None  # 大小写归一
        assert "mood" in reg.get_registered_tag_names()

    def test_priority_override(self, clean_tag_registry):
        reg = clean_tag_registry
        reg.register_tag_renderer("p1", "mood", lambda c, ctx: "A", priority=0)
        reg.register_tag_renderer("p2", "mood", lambda c, ctx: "B", priority=-1)
        assert reg.get_tag_renderer("mood").plugin_name == "p1"  # 低优先级被忽略
        reg.register_tag_renderer("p2", "mood", lambda c, ctx: "B", priority=5)
        assert reg.get_tag_renderer("mood").plugin_name == "p2"  # 高优先级覆盖

    def test_invalid_tag_name_rejected(self, clean_tag_registry):
        with pytest.raises(ValueError):
            clean_tag_registry.register_tag_renderer("p1", "bad tag!", lambda c, ctx: "")

    def test_unregister_cleans_registration(self, clean_tag_registry):
        reg = clean_tag_registry
        reg.register_tag_renderer("p1", "mood", lambda c, ctx: "A")
        assert reg._has_any_registration("p1")
        reg._tag_renderers = {k: v for k, v in reg._tag_renderers.items() if v.plugin_name != "p1"}
        assert not reg._has_any_registration("p1")
        assert reg.get_tag_renderer("mood") is None


# ── HTML 路径（message_card._inject_tag_cards）──────────


class TestInjectTagCards:
    def test_completed_block_replaced(self, clean_tag_registry):
        clean_tag_registry.register_tag_renderer("p1", "mood", lambda c, ctx: f"CARD[{c}]")
        from app.widgets.message_card import _inject_tag_cards

        out = _inject_tag_cards("<mood>\n感受：开心\n</mood>\n\n正文内容", True)
        assert "CARD[\n感受：开心\n]" in out
        assert "<mood>" not in out
        assert "正文内容" in out

    def test_streaming_unclosed(self, clean_tag_registry):
        clean_tag_registry.register_tag_renderer("p1", "mood", lambda c, ctx: f"CARD[{c}|{ctx['completed']}]")
        from app.widgets.message_card import _inject_tag_cards

        assert "CARD[感受：开心|False]" in _inject_tag_cards("<mood>感受：开心", False)
        assert "CARD[感受：开心|True]" in _inject_tag_cards("<mood>感受：开心</mood>", True)

    def test_orphan_close_cleaned(self, clean_tag_registry):
        """无 open 的孤立 close（流式半截产物）应被清理（对齐 _inject_think_cards 行为）。"""
        clean_tag_registry.register_tag_renderer("p1", "mood", lambda c, ctx: "X")
        from app.widgets.message_card import _inject_tag_cards

        out = _inject_tag_cards("abc</mood>def", True)
        assert "</mood>" not in out
        assert "abc" in out and "def" in out

    def test_no_registration_passthrough(self, clean_tag_registry):
        """无注册标签 → 原样直通（零开销，标签保留由默认渲染处理）。"""
        from app.widgets.message_card import _inject_tag_cards

        md = "<mood>感受：x</mood> 正文"
        assert _inject_tag_cards(md, True) == md

    def test_empty_content_dropped(self, clean_tag_registry):
        clean_tag_registry.register_tag_renderer("p1", "mood", lambda c, ctx: "X")
        from app.widgets.message_card import _inject_tag_cards

        assert _inject_tag_cards("<mood>   </mood>", True) == ""


# ── Qt 路径（markdown_block_viewer.parse_blocks）────────


class TestParseBlocksTag:
    def test_tag_block_split(self, clean_tag_registry):
        clean_tag_registry.register_tag_renderer("p1", "mood", lambda c, ctx: f"<b>{c}</b>")
        from app.widgets.markdown_block_viewer import parse_blocks

        blocks = parse_blocks("<mood>感受：a</mood>\n\n```python\nprint(1)\n```\n尾段")
        types = [b["type"] for b in blocks]
        assert "tag" in types, types
        assert "code" in types
        tag_block = next(b for b in blocks if b["type"] == "tag")
        assert tag_block["tag"] == "mood"
        assert tag_block["completed"] is True
        assert "感受：a" in tag_block["html"]

    def test_streaming_unclosed(self, clean_tag_registry):
        clean_tag_registry.register_tag_renderer("p1", "mood", lambda c, ctx: "X")
        from app.widgets.markdown_block_viewer import parse_blocks

        blocks = parse_blocks("<mood>感受：a")
        assert blocks[0]["type"] == "tag"
        assert blocks[0]["completed"] is False


# ── assistant_hub <mood> 渲染器 ─────────────────────────


class TestMoodCardRenderer:
    def test_four_sections(self):
        from plugins.assistant_hub.ui import _render_mood_card

        content = "感受：开心\n联想：昨天的对话\n反思：没有偏差\n意志：温柔而坚定"
        html = _render_mood_card(content, {"tag": "mood", "completed": True, "compact": False})
        assert "MOOD" in html
        for kw in ("感受", "联想", "反思", "意志"):
            assert kw in html

    def test_xss_escaped(self):
        from plugins.assistant_hub.ui import _render_mood_card

        html = _render_mood_card("感受：<script>alert(1)</script>", {"completed": True})
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_streaming_placeholder(self):
        from plugins.assistant_hub.ui import _render_mood_card

        html = _render_mood_card("感受：开心", {"completed": False})
        assert "内心独白中" in html
