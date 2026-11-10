# -*- coding: utf-8 -*-
"""MCP ImageContent → 多模态图片块 回归测试（L1/L2 数据契约）。

Run: pytest tests/core/test_mcp_image_content.py -v
"""
from app.tools.result import ToolResult, as_image_list


def test_as_image_list_single_dict():
    assert as_image_list({"mime": "image/png", "data": "aGk="}) == [
        {"mime": "image/png", "data": "aGk="}
    ]


def test_as_image_list_multi():
    payload = [
        {"mime": "image/png", "data": "aGk="},
        {"mime": "image/jpeg", "data": "eA=="},
    ]
    assert as_image_list(payload) == payload


def test_as_image_list_rejects_empty_and_junk():
    assert as_image_list(None) == []
    assert as_image_list({}) == []
    assert as_image_list({"mime": "image/png"}) == []
    assert as_image_list([{"mime": "image/png", "data": "aGk="}, 42, {}]) == [
        {"mime": "image/png", "data": "aGk="}
    ]


def test_tool_result_to_dict_keeps_image_list():
    payload = [{"mime": "image/png", "data": "aGk="}]
    d = ToolResult(True, content="ok", image_data=payload).to_dict()
    assert d["image_data"] == payload


def _dispatch_result(content, is_error=False):
    """走真实分派层，只把 CallToolResult 换成假对象（不碰 session）。"""
    from app.tools.mcp_tools import MCPClientManager

    class _FakeResult:
        pass

    fake = _FakeResult()
    fake.content = content
    fake.isError = is_error
    return MCPClientManager._split_tool_content(fake)


def _content_blocks_single_image():
    from mcp import types as mt

    return [
        mt.TextContent(type="text", text='{"ok":true}'),
        mt.ImageContent(type="image", data="aGk=", mimeType="image/png"),
    ]


def test_split_text_only():
    from mcp import types as mt

    text, images = _dispatch_result([mt.TextContent(type="text", text="hello")])
    assert text == "hello"
    assert images == []


def test_split_single_image():
    text, images = _dispatch_result(_content_blocks_single_image())
    assert text == '{"ok":true}'
    assert images == [{"mime": "image/png", "data": "aGk="}]


def test_split_multi_image_keeps_order():
    from mcp import types as mt

    content = [
        mt.TextContent(type="text", text="封面"),
        mt.ImageContent(type="image", data="aGk=", mimeType="image/png"),
        mt.TextContent(type="text", text="第2帧"),
        mt.ImageContent(type="image", data="eA==", mimeType="image/jpeg"),
    ]
    text, images = _dispatch_result(content)
    assert text == "封面\n第2帧"
    assert images == [
        {"mime": "image/png", "data": "aGk="},
        {"mime": "image/jpeg", "data": "eA=="},
    ]


def test_split_dict_blocks_sdk_agnostic():
    """不同 SDK 小版本可能交回 dict 块，按 type 字符串分派仍生效。"""
    content = [
        {"type": "text", "text": "x"},
        {"type": "image", "data": "eA==", "mimeType": "image/webp"},
    ]
    text, images = _dispatch_result(content)
    assert text == "x"
    assert images == [{"mime": "image/webp", "data": "eA=="}]


def test_split_is_error_with_image_still_returns_images():
    """isError 结果也保留图片载荷，由调用方决定丢弃；本层不吞。"""
    text, images = _dispatch_result(_content_blocks_single_image(), is_error=True)
    assert text == '{"ok":true}'
    assert len(images) == 1


def test_dialog_image_pick_helper():
    """弹窗取多图首张：统一走 helper，避免各写一份判定。"""
    payload = [
        {"mime": "image/png", "data": "aGk="},
        {"mime": "image/jpeg", "data": "eA=="},
    ]
    picked = as_image_list(payload)[:1]
    assert picked == [{"mime": "image/png", "data": "aGk="}]
