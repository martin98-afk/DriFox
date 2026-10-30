"""工具块 body 懒注入（lazy_body / body_only）回归。

背景：折叠只是 `.cm-collapsible__body{height:0}` 的视觉隐藏，内容照样进 DOM。
工具块 body 是 DOM 内存大头（pygments 高亮实测膨胀 12.7 倍：12KB diff →
154KB HTML + 3200 个 span）。lazy_body 让不可见的块 body 只留空占位，
展开时由 viewer 从渲染源反查该块、以 body_only 重渲染后注入。
"""

import pytest

from app.widgets.card_render_core import (
    _inject_tool_blocks,
    _render_tool_block_content,
    find_tool_segment_by_call_id,
    _render_markdown_to_html_cached,
)
from app.widgets.render_helpers import render_tool_block

_DIFF = "\n".join(
    ["--- a/x.py", "+++ b/x.py"]
    + [f"@@ -{i},5 +{i},6 @@\n ctx_a_{i}\n- old_{i}\n+ new_{i}\n ctx_b_{i}" for i in range(20)]
)
_RESULT = "RESULT_MARKER " + "x" * 400


def _block_html(**kwargs):
    return render_tool_block(
        "edit",
        {"path": "a.py"},
        _RESULT,
        True,
        tool_call_id="tc1",
        diff=_DIFF,
        **kwargs,
    )


class TestRenderToolBlockLazy:
    def test_lazy_body_has_no_payload(self):
        """折叠态懒占位：不含结果原文、不含 diff 高亮节点。"""
        html = _block_html(collapsed=True, lazy_body=True)
        assert 'data-lazy-body="1"' in html
        assert "RESULT_MARKER" not in html
        assert "tool-diff-inline" not in html
        assert 'data-tool-call-id="tc1"' in html

    def test_lazy_body_much_smaller(self):
        """懒占位体积应远小于完整折叠块（diff 高亮是大头）。"""
        full = _block_html(collapsed=True)
        lazy = _block_html(collapsed=True, lazy_body=True)
        assert len(lazy) * 3 < len(full)

    def test_no_collapse_is_also_lazy(self, monkeypatch):
        """无折叠态块（render_mode=expand）同样懒化：工具区折叠时也不可见。"""
        from app.tools.registry import ToolRegistry

        monkeypatch.setattr(ToolRegistry.get_instance(), "get_render_mode", lambda _name: "expand")
        html = render_tool_block(
            "edit",
            {"path": "a.py"},
            _RESULT,
            True,
            collapsed=False,
            tool_call_id="tc1",
            diff=_DIFF,
            lazy_body=True,
        )
        assert 'data-lazy-body="1"' in html
        assert "tool-block--no-collapse" in html
        assert 'class="tool-block__body"' in html
        assert "RESULT_MARKER" not in html
        assert "tool-diff-inline" not in html

    def test_no_tool_call_id_not_lazy(self):
        """无 tool_call_id 无法反查源文本 → 不懒化（否则展开后空白）。"""
        html = render_tool_block("edit", {"path": "a.py"}, _RESULT, True, collapsed=True, lazy_body=True)
        assert 'data-lazy-body="1"' not in html
        assert "RESULT_MARKER" in html or "tool-diff-inline" in html

    def test_body_only_matches_expanded_body(self):
        """body_only 产出 = 展开态的工具块内容（供注入回填）。"""
        body = _block_html(collapsed=False, body_only=True)
        full = _block_html(collapsed=False)
        assert "tool-diff-inline" in body
        assert "<button" not in body  # 只有内容，不含 header/折叠壳
        assert len(body) < len(full)

    def test_lazy_key_matches_full_key(self):
        """懒占位与完整块的 data-block-key 一致（展开态跨重渲染保持）。"""
        import re

        full = _block_html(collapsed=True)
        lazy = _block_html(collapsed=True, lazy_body=True)
        pat = r'data-block-key="([^"]+)"'
        assert re.search(pat, full).group(1) == re.search(pat, lazy).group(1)


class TestFindToolSegment:
    _MD = (
        "正文\n\n<tool>\nname: bash\nargs: {}\nresult: hello\nsuccess: true\ntool_call_id: t-1\n</tool>\n\n"
        "尾部\n\n<tool>\nname: read\nargs: {}\nresult: world\nsuccess: true\ntool_call_id: t-2\n</tool>"
    )

    def test_find_by_id(self):
        seg = find_tool_segment_by_call_id(self._MD, "t-2")
        assert seg is not None and "world" in seg
        assert "hello" not in seg

    def test_missing_id(self):
        assert find_tool_segment_by_call_id(self._MD, "nope") is None

    def test_fence_content_not_matched(self):
        """代码块里的 <tool> 示例是假块，不得被反查命中。"""
        md = "```text\n<tool>\nname: bash\nresult: fake\nsuccess: true\ntool_call_id: t-9\n</tool>\n```"
        assert find_tool_segment_by_call_id(md, "t-9") is None


class TestRenderChainLazy:
    def test_inject_tool_blocks_lazy(self):
        md = "<tool>\nname: bash\nargs: {}\nresult: hello\nsuccess: true\ntool_call_id: t-3\n</tool>"
        html = _inject_tool_blocks(md, True, compact=True, lazy_body=True)
        assert 'data-lazy-body="1"' in html
        assert "hello" not in html

    def test_cached_render_lazy_vs_full(self):
        """整条 markdown 渲染链：lazy 版明显更小，且两者缓存互不串味。"""
        md = "\n\n".join(
            f"<tool>\nname: edit\nargs: {{\"path\": \"f{i}.py\"}}\nresult: r{i}\n"
            f"diff:\n{_DIFF}\nsuccess: true\ntool_call_id: t-{i}\n</tool>"
            for i in range(6)
        )
        full = _render_markdown_to_html_cached(md, compact=True, lazy_body=False)
        lazy = _render_markdown_to_html_cached(md, compact=True, lazy_body=True)
        assert len(lazy) * 2 < len(full)
        assert "tool-diff-inline" in full
        assert "tool-diff-inline" not in lazy

    def test_body_only_via_segment(self):
        """展开链路：从渲染源反查段 → body_only 重渲染得到完整内容。"""
        seg = find_tool_segment_by_call_id(
            "<tool>\nname: edit\nargs: {}\nresult: x\ndiff:\n" + _DIFF + "\nsuccess: true\ntool_call_id: t-7\n</tool>",
            "t-7",
        )
        body = _render_tool_block_content.__wrapped__(seg, compact=True, body_only=True)
        assert "tool-diff-inline" in body


class TestWiringGuards:
    """信号链路必须跨过 Page/Viewer 类边界。

    console 分发在 ConsoleMonitorPage（Page）上，而渲染源（_markdown_text、
    工具 markdown 缓存）在 CodeWebViewer（Viewer）上。直接在 Page 里调
    Viewer 的方法 = AttributeError 被 except 吞掉 → 点开无任何反应。
    """

    def test_page_has_tool_body_signal(self):
        from app.widgets.card_viewers import ConsoleMonitorPage

        assert hasattr(ConsoleMonitorPage, "toolBodyRequested")

    def test_viewer_has_serve_tool_body(self):
        from app.widgets.card_viewers import CodeWebViewer

        assert callable(getattr(CodeWebViewer, "_serve_tool_body", None))


class TestContentToMarkdownSlim:
    def test_inline_tool_payload_omitted(self):
        """inline 形态（read 类）的 result/diff/echarts 不进渲染源 markdown。"""
        from app.core.conversation.message_content import content_to_markdown
        from app.tools.registry import ToolRegistry

        reg = ToolRegistry.get_instance()
        try:
            if reg.get_render_mode("read") != "inline":
                pytest.skip("registry 未加载 read 工具（插件未注册）")
        except Exception:
            pytest.skip("registry 不可用")

        md = content_to_markdown(
            [
                {
                    "type": "tool_result",
                    "name": "read",
                    "arguments": {"path": "a.py"},
                    "result": "SECRET_RESULT_TEXT",
                    "diff": "SECRET_DIFF_TEXT",
                    "success": True,
                    "tool_call_id": "t-inline",
                }
            ]
        )
        assert "SECRET_RESULT_TEXT" not in md
        assert "SECRET_DIFF_TEXT" not in md
        assert "a.py" in md  # 参数保留（单行卡预览要用）

    def test_expand_tool_payload_kept(self):
        """非 inline 形态（有 body）的结果仍进渲染源。"""
        from app.core.conversation.message_content import content_to_markdown
        from app.tools.registry import ToolRegistry

        try:
            if ToolRegistry.get_instance().get_render_mode("bash") == "inline":
                pytest.skip("bash 被注册成 inline，本例不适用")
        except Exception:
            pytest.skip("registry 不可用")

        md = content_to_markdown(
            [
                {
                    "type": "tool_result",
                    "name": "bash",
                    "arguments": {"command": "echo hi"},
                    "result": "KEPT_RESULT_TEXT",
                    "success": True,
                    "tool_call_id": "t-bash",
                }
            ]
        )
        assert "KEPT_RESULT_TEXT" in md
