# -*- coding: utf-8 -*-
"""
Diff 渲染端到端测试（插件化工具系统）

覆盖（T2 计划 P8 + T14 补充 6 + D9 锁定测试）：
P8 — diff 端到端渲染：
- write 工具真实 diff → render_tool_block → .tool-diff-inline 结构（header/file/summary/body）
- +N/-N 统计正确（_render_edit_diff_body 插件闭包输出）
- 文件头正确显示
补充 6 — 失败分支：
- render_tool_block success=False → 不渲染 raw_output_html 文本体（<pre 不出现）、走参数表格
D9 — reconstruct_diff（T5 结论 + T10 落点）：
- 构造 edit 工具块 args 含 operations:[{op,anchor,lines}] + 无 diff 字段
  → 渲染走重建伪 diff 分支（HTML 含 .tool-diff-inline + 锚点/+行）
- 无 operations 时不误触发（无 diff 渲染）

运行: python -m pytest tests/core/test_diff_render_end_to_end.py -v
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.tools.registry import ToolRegistry
from app.widgets.render_helpers import render_tool_block


@pytest.fixture(autouse=True)
def fresh_registry_with_plugins():
    """重置 registry 并加载系统插件（edit/write 的 render 闭包来自插件）"""
    ToolRegistry.reset_instance()
    from app.tools.plugin_tool_loader import load_plugin_tools

    load_plugin_tools()
    yield
    ToolRegistry.reset_instance()


_DIFF = (
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def f():\n"
    '-    return "old"\n'
    '+    return "new"\n'
)


class TestDiffRenderEndToEnd:
    """P8：write 工具 diff → render_tool_block → 插件闭包渲染"""

    def test_tool_diff_inline_structure(self):
        """渲染含 .tool-diff-inline 容器（header/file/summary/body 四段结构）"""
        html = render_tool_block("write", {"path": "x.py"}, result="已写入", success=True, diff=_DIFF)
        assert 'class="tool-diff-inline"' in html
        assert 'class="tool-diff-inline__header"' in html
        assert 'class="tool-diff-inline__file"' in html
        assert 'class="tool-diff-inline__summary"' in html
        assert 'class="tool-diff-inline__body"' in html

    def test_diff_stats_plus_minus(self):
        """+N/-N 统计正确（1 增 1 删）"""
        html = render_tool_block("write", {"path": "x.py"}, result="已写入", success=True, diff=_DIFF)
        assert 'class="tool-diff-inline__add" style="color: #56d364;">+1<' in html
        assert 'class="tool-diff-inline__del" style="color: #ff7b72;">-1<' in html

    def test_diff_file_header(self):
        """文件头显示（diff 文件名）"""
        html = render_tool_block("write", {"path": "x.py"}, result="已写入", success=True, diff=_DIFF)
        assert 'class="tool-diff-inline__file"' in html
        assert "x.py" in html

    def test_diff_content_rendered(self):
        """diff 内容经 _render_diff_preview 输出（新增/删除行标记）"""
        html = render_tool_block("edit", {"path": "x.py"}, result="已编辑", success=True, diff=_DIFF)
        assert 'class="diff-line diff-del"' in html
        assert 'class="diff-line diff-add"' in html

    def test_no_diff_returns_no_inline(self):
        """无 diff → 不渲染 .tool-diff-inline（闭包返回 None 回退默认渲染）"""
        html = render_tool_block("write", {"path": "x.py"}, result="已写入", success=True)
        assert "tool-diff-inline" not in html


class TestFailureBranch:
    """补充 6：success=False 不渲染文本体、走参数表格"""

    def test_failure_no_pre_text_body(self):
        """失败 → <pre 不出现（raw_output_html 仅 success 时渲染）"""
        html = render_tool_block("write", {"path": "x.py"}, result="模拟的错误信息", success=False)
        assert "<pre" not in html
        assert "tool-expanded-content" in html  # body 仍存在（走参数表格）

    def test_failure_with_diff_still_no_pre(self):
        """失败 + 有 diff → 仍不渲染 <pre 文本体"""
        html = render_tool_block("write", {"path": "x.py"}, result="错误", success=False, diff=_DIFF)
        assert "<pre" not in html

    def test_success_renders_text_body(self):
        """成功（无特殊渲染）→ 文本体走 <pre 渲染"""
        html = render_tool_block("websearch", {"query": "x"}, result="搜索结果", success=True)
        assert "<pre" in html


class TestReconstructDiffFromOperations:
    """D9：edit 工具 operations 参数重建伪 diff（历史消息 diff 缺失 fallback）"""

    def _render_tool_block_content(self, content: str) -> str:
        from app.widgets.message_card import _render_tool_block_content

        return _render_tool_block_content(content)

    def test_reconstruct_from_operations(self):
        """args 含 operations:[{op,anchor,lines}] + 无 diff → 重建伪 diff 分支（HTML 含锚点/+行）"""
        content = (
            "name: edit\n"
            'args: {"file_path": "a.py", "operations": [{"op": "replace", "anchor": "def f():", '
            '"lines": ["def g(): return 2"]}]}\n'
            "result: done\n"
            "success: true"
        )
        html = self._render_tool_block_content(content)
        # 重建伪 diff → 走插件闭包 .tool-diff-inline 渲染
        assert "tool-diff-inline" in html
        # 文件头 = 重建 diff 的 +++ 文件路径
        assert "a.py" in html
        # 统计：+1 行（lines 1 条）
        assert ">+1<" in html

    def test_reconstruct_delete_operation(self):
        """delete 操作 → 重建含删除行标记"""
        content = (
            "name: edit\n"
            'args: {"file_path": "b.py", "operations": [{"op": "delete", "anchor": "old line", "lines": null}]}\n'
            "result: done\n"
            "success: true"
        )
        html = self._render_tool_block_content(content)
        assert "tool-diff-inline" in html

    def test_no_operations_no_reconstruct(self):
        """无 operations → 不误触发重建（无 diff 渲染）"""
        content = "name: edit\nargs: {\"file_path\": \"a.py\"}\nresult: done\nsuccess: true"
        html = self._render_tool_block_content(content)
        assert "tool-diff-inline" not in html
        assert "diff-seg" not in html

    def test_non_edit_tool_no_reconstruct(self):
        """非 edit 工具（metadata 无 reconstruct_diff）→ 即使有 operations 也不重建"""
        content = (
            "name: write\n"
            'args: {"file_path": "a.py", "operations": [{"op": "replace", "lines": ["x"]}]}\n'
            "result: done\n"
            "success: true"
        )
        html = self._render_tool_block_content(content)
        assert "tool-diff-inline" not in html
