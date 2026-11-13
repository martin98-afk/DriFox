"""工具块折叠头：有 description 时用它当标题，工具名让位，右侧继续显文件路径。
背景：编辑类工具必填 description 后，折叠头原本是「编辑 描述 (路径)」三段挤一行，
说明被截断、路径也看不清。约定改为：有说明就用说明当标题，右侧只留路径；
无说明维持「工具名 + 参数预览」。
"""
import re

from app.widgets.render_helpers import render_tool_block, _get_tool_cn_name
from app.utils.design_tokens import Colors

_TITLE_RE = re.compile(r"white-space: nowrap; flex: 0 0 auto;[^>]*>([^<]*)</span>")


def _header(html: str) -> str:
    """截到 body 之前的折叠头（参数表格在 body 里，避免误判路径出现在头部）"""
    return html.split('class="cm-collapsible__body"')[0]


def _title(html: str) -> str:
    """折叠头标题 span 的文本（与右侧参数预览区分开）"""
    m = _TITLE_RE.search(_header(html))
    return m.group(1) if m else ""


def _render(**args):
    return render_tool_block("edit", args, "已编辑", True, tool_call_id="tc1")


class TestDescAsTitle:
    def test_desc_replaces_cn_name_and_keeps_path(self):
        """有 description：标题是描述，右侧仍显文件路径。"""
        html = render_tool_block(
            "edit",
            {"path": "a.py", "description": "同步共享模块的必填语义说明"},
            "已编辑",
            True,
            tool_call_id="tc1",
        )
        assert _title(html) == "同步共享模块的必填语义说明"
        assert "a.py" in _header(html)

    def test_no_desc_keeps_cn_name_and_preview(self):
        """无 description：维持原名与参数预览。"""
        html = _render(path="a.py")
        assert _title(html) == _get_tool_cn_name("edit")
        assert "a.py" in _header(html)

    def test_blank_desc_falls_back(self):
        """空白 description 视同没填，不产生空标题。"""
        html = _render(path="a.py", description="   ")
        assert _title(html) == _get_tool_cn_name("edit")
        assert "a.py" in _header(html)

    def test_long_desc_shown_in_full(self):
        """描述不截断（宁可右侧路径让位），长描述整段出现在头部。"""
        long_desc = "很长" * 40
        head = _header(_render(path="a.py", description=long_desc))
        assert long_desc in head
        assert "a.py" in head

    def test_diff_stats_survive_desc_title(self):
        """有 description 时右侧 +N/-N 仍要显示（只有参数预览让位）。"""
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n ctx\n-old\n+new\n+new2"
        html = render_tool_block(
            "edit",
            {"path": "a.py", "description": "抽离缓存"},
            "已编辑",
            True,
            tool_call_id="tc1",
            diff=diff,
        )
        head = _header(html)
        assert "tool-diff-stats" in head
        assert _title(html) == "抽离缓存"


class TestTitleColorToken:
    """标题色必须来自主题 token（写死会让换主题失效）。"""

    def test_plain_tool_follows_token(self, monkeypatch):
        monkeypatch.setattr(Colors, "TOOL_TITLE", "#123456")
        assert "#123456" in _header(_render(path="a.py"))

    def test_mcp_tool_follows_token(self, monkeypatch):
        monkeypatch.setattr(Colors, "TOOL_TITLE_MCP", "#654321")
        html = render_tool_block("mcp__srv__tool", {}, "ok", True, tool_call_id="tc2")
        assert "#654321" in _header(html)


class TestLongPathLayout:
    def test_long_path_shortened_single_line(self):
        """绝对长路径首尾缩短保留文件名，且右侧是单行（换行会撑破行高与下一块重叠）。"""
        long_path = (
            "/Users/dingma/.drifox/workspaces/DriFoxPlugins/plugins/"
            "gateway-telegram/gateways/telegram.py"
        )
        head = _header(_render(path=long_path, description="加编辑消息流式"))
        assert "telegram.py" in head
        assert "…" in head
        assert "-webkit-line-clamp" not in head
