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


# ── assistant_hub 人格标签卡（mood / plan）───────────────


class TestPersonaTagCards:
    def test_mood_card_four_sections(self):
        from plugins.assistant_hub.ui import _make_tag_renderer

        render = _make_tag_renderer("mood")
        content = "感受：开心\n联想：昨天的对话\n反思：没有偏差\n意志：温柔而坚定"
        html = render(content, {"tag": "mood", "completed": True, "compact": False})
        assert "MOOD" in html
        for kw in ("感受", "联想", "反思", "意志"):
            assert kw in html
        # 布局 table 必须带 layout-table class（主程序全局表格样式按此排除）
        assert 'class="layout-table"' in html
        # 标题与副题同行（之间无 <br>）
        header_part = html.split("<br>")[0]
        assert "MOOD" in header_part and "内心独白" in header_part

    def test_font_size_scales_with_system(self):
        """字号必须经 scale_font_size 派生（跟随系统 UI 字号），禁止写死 px。"""
        import re

        from app.utils.design_tokens import scale_font_size
        from plugins.assistant_hub.ui import _make_tag_renderer

        html = _make_tag_renderer("mood")("感受：x", {"tag": "mood", "completed": True})
        sizes = {int(m) for m in re.findall(r"font-size:(\d+)px", html)}
        # 渲染器使用的基准集合（值 13 / 键名与标题 12 / 副题 11），每个输出字号
        # 都必须等于某基准经系统缩放后的结果
        expected = {scale_font_size(b) for b in (11, 12, 13)}
        assert sizes and sizes.issubset(expected), f"{sizes} ⊄ {expected}"

    def test_plan_card_skin(self):
        from plugins.assistant_hub.ui import _make_tag_renderer

        render = _make_tag_renderer("plan")
        content = "目标：修 bug\n路径：先复现\n风险：改错文件\n取舍：小步提交"
        html = render(content, {"tag": "plan", "completed": True, "compact": False})
        assert "PLAN" in html
        for kw in ("目标", "路径", "风险", "取舍"):
            assert kw in html
        assert "#6c8ebf" in html  # plan 靛蓝皮肤

    def test_unknown_tag_neutral_skin(self):
        from plugins.assistant_hub.ui import _make_tag_renderer

        render = _make_tag_renderer("custom_x")
        html = render("状态：正常", {"tag": "custom_x", "completed": True})
        assert "CUSTOM_X" in html
        assert 'class="layout-table"' in html

    def test_xss_escaped(self):
        from plugins.assistant_hub.ui import _make_tag_renderer

        render = _make_tag_renderer("mood")
        html = render("感受：<script>alert(1)</script>", {"tag": "mood", "completed": True})
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_streaming_placeholder(self):
        from plugins.assistant_hub.ui import _make_tag_renderer

        mood_html = _make_tag_renderer("mood")("感受：开心", {"tag": "mood", "completed": False})
        assert "解析中" in mood_html
        plan_html = _make_tag_renderer("plan")("目标：x", {"tag": "plan", "completed": False})
        assert "推演中" in plan_html

    def test_persona_block_tags_fallback(self):
        """PersonaRegistry 不可用时回退内置 mood/plan。"""
        from plugins.assistant_hub.ui import _persona_block_tags

        tags = _persona_block_tags()
        assert isinstance(tags, list) and tags
        for t in tags:
            assert t == t.lower()
