# -*- coding: utf-8 -*-
"""CodeWebViewer 右键复制选中优先逻辑测试。

修复背景：大模型卡片（CodeWebViewer，QWebEngineView）右键菜单「复制」
原先直接复制全文，未处理页面选中文本；现应优先复制 DOM 选区，
无选中时降级复制全文（与用户卡片 PlainTextViewer 行为一致）。

测试环境无法创建真实 QWebEngineView，因此用 object.__new__ 绕过 __init__，
仅 mock page() 与 get_plain_text()，并隔离 win32clipboard 模块。
"""

import sys
from unittest.mock import MagicMock, patch

from app.widgets.message_card import CodeWebViewer


def _make_viewer(selected: str, full_text: str) -> CodeWebViewer:
    """构造绕过 __init__ 的 CodeWebViewer，注入 mock page 与全文文本。"""
    viewer = CodeWebViewer.__new__(CodeWebViewer)

    class _FakePage:
        def selectedText(self):
            return selected

    viewer.page = lambda: _FakePage()
    viewer.get_plain_text = lambda: full_text
    return viewer


def _capture_clipboard_text(viewer: CodeWebViewer) -> str:
    """调用 _copy_to_clipboard，拦截写入 win32clipboard 的文本。"""
    fake_w32 = MagicMock()
    with patch.dict(sys.modules, {"win32clipboard": fake_w32}):
        viewer._copy_to_clipboard()
    assert fake_w32.SetClipboardText.call_count == 1
    return fake_w32.SetClipboardText.call_args[0][0]


def test_copy_prefers_selection():
    """有选中文本时，右键复制只复制选中内容而非全文。"""
    viewer = _make_viewer(selected="选中的文字", full_text="完整内容很长很长")
    assert _capture_clipboard_text(viewer) == "选中的文字"


def test_copy_normalizes_paragraph_separator():
    """WebEngine 选区中的块级分隔符 \\u2029 应规范化为 \\n。"""
    viewer = _make_viewer(selected="第一行\u2029第二行", full_text="全文")
    assert _capture_clipboard_text(viewer) == "第一行\n第二行"


def test_copy_falls_back_to_full_text_when_no_selection():
    """无选中文本时，降级复制全文（原有行为）。"""
    viewer = _make_viewer(selected="", full_text="完整内容")
    assert _capture_clipboard_text(viewer) == "完整内容"


def test_copy_empty_full_text_does_not_write():
    """选中与全文均为空时，不应写入剪贴板。"""
    viewer = _make_viewer(selected="", full_text="")
    fake_w32 = MagicMock()
    with patch.dict(sys.modules, {"win32clipboard": fake_w32}):
        viewer._copy_to_clipboard()
    fake_w32.SetClipboardText.assert_not_called()
