# -*- coding: utf-8 -*-
"""
message_card 工具块解析测试（_render_tool_block_content）

覆盖（T14 补充 4）：
1. result 含独立行 success:/diff:/tool_call_id:/echarts: 字段字样 → 完整渲染不截断
   （行锚定 ^字段: 只认行首声明，取最后匹配）
2. args 含 "result:" 子串 → 不串位（args 与 result 正确分离）
3. 流式中间态（不完整 JSON / 缺字段）→ 不崩溃

运行: python -m pytest tests/widgets/test_message_card_tool_parse.py -v
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.widgets.message_card import _render_tool_block_content


class TestResultContainsFieldKeywords:
    """补充 4-1：result 内容含字段关键字不截断"""

    def test_result_with_success_field_line(self):
        """result 内含 'success: xxx' 独立行 → 不误当字段（行锚定只认行首真字段）"""
        content = (
            "name: write\n"
            'args: {"path": "x.py"}\n'
            "result: 输出包含 success: true 字样但不以行首出现\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        # 完整渲染（不因 result 内的 success 字样截断）
        assert html
        assert "输出包含" in html

    def test_result_with_diff_field_line(self):
        """result 内含 'diff: xxx' 独立行 → 不误当 diff 字段"""
        content = (
            "name: write\n"
            'args: {"path": "x.py"}\n'
            "result: 输出包含 diff: --- 字样\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        assert html
        assert "输出包含" in html

    def test_result_with_tool_call_id_and_echarts_keywords(self):
        """result 内含 tool_call_id:/echarts: 字样 → 完整渲染不截断"""
        content = (
            "name: write\n"
            'args: {"path": "x.py"}\n'
            "result: tool_call_id: call_x echarts: {} 都是普通文本\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        assert html
        assert "都是普通文本" in html

    def test_real_field_last_match_wins(self):
        """流式重复字段 → 取最后行首匹配（success 最终值正确）"""
        content = (
            "name: write\n"
            'args: {"path": "x.py"}\n'
            "result: 中间态\n"
            "success: true\n"
            "result: 最终结果\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        # 不崩溃且包含最终结果
        assert html
        assert "最终结果" in html

    def test_result_interleaved_line_start_fields_not_truncated(self):
        """D4：result 内含行首独立字段行（success: world）→ 完整渲染不截断。

        旧解析器命中第一个行首 success → 截断 result → 「更多内容」缺失（红）；
        新解析器行锚定取最后匹配 → result 完整 → 「更多内容」渲染存在（绿）。
        """
        content = (
            "name: write\n"
            'args: {"path": "x.py"}\n'
            "result: 第一行\n"
            "success: world\n"
            "更多内容\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        assert "更多内容" in html, "result 不得被行内 success 字段字样截断（取最后行首匹配）"
        assert "第一行" in html


class TestArgsWithResultSubstring:
    """补充 4-2：args 含 'result:' 子串不串位"""

    def test_args_value_contains_result_colon(self):
        """args JSON 值含 'result:' → args 与 result 正确分离"""
        content = (
            "name: bash\n"
            'args: {"command": "echo result: 123"}\n'
            "result: 命令输出\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        assert html
        # args 的 command 值完整保留（含 result: 子串）
        assert "echo result" in html
        # result 正常渲染
        assert "命令输出" in html

    def test_args_value_contains_success_substring(self):
        """args 含 'success:' 子串 → 不误当字段"""
        content = (
            "name: bash\n"
            'args: {"command": "echo success: 42"}\n'
            "result: 输出\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        assert html
        assert "success: 42" in html


class TestStreamingIntermediateState:
    """补充 4-3：流式中间态不崩溃"""

    def test_incomplete_json_args(self):
        """args JSON 不完整（未闭合）→ 不崩溃，走智能修复/正则提取"""
        content = (
            "name: write\n"
            'args: {"path": "x.py"\n'
            "result: 部分内容\n"
            "success: true"
        )
        html = _render_tool_block_content(content)
        assert html

    def test_missing_args(self):
        """无 args 字段 → 不崩溃（从整个 content 提取）"""
        content = "name: read\nresult: 内容\nsuccess: true"
        html = _render_tool_block_content(content)
        assert html

    def test_missing_result(self):
        """无 result → 不崩溃"""
        content = 'name: write\nargs: {"path": "x.py"}\nsuccess: true'
        html = _render_tool_block_content(content)
        assert html

    def test_empty_content(self):
        """空内容 → 不崩溃"""
        html = _render_tool_block_content("")
        assert html

    def test_only_name(self):
        """只有 name → 不崩溃"""
        html = _render_tool_block_content("name: bash")
        assert html
