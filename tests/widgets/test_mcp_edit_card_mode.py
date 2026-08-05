# -*- coding: utf-8 -*-
"""回归测试：MCP 编辑卡（MCPEditCard）的编辑模式默认行为

新增行为：
1. 编辑已有服务器（server_data 非空）→ 默认进入 JSON 模式（_json_mode=True，stack 显示 JSON 页）。
2. 新增服务器（server_data 为空）→ 默认表单模式。
3. 类型为 http/sse 时，headers 输入框所在行应在表单布局中拉伸填满剩余空间
   （form_layout.stretchFactor(headers_row) == 1，且 headersEdit 取消 100px 上限）。
4. 从 JSON 模式切回表单模式时，必须调用 _on_type_changed 重新同步字段显隐
   （修复此前切换回表单时 http 字段显隐错乱的既有 bug）。

这些测试构造真实的 MCPEditCard 实例（需要 QApplication），验证布局与状态字段。
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QStackedWidget


def _headers_stretch(card):
    """读取 headers 行在表单布局中的拉伸系数（QLayout 用 stretch(index) 查询）"""
    idx = card._form_layout.indexOf(card._headers_row)
    return card._form_layout.stretch(idx)


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _ensure_qapp()


def test_edit_mode_defaults_to_json():
    """编辑已有服务器时默认 JSON 模式"""
    from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

    card = MCPEditCard(server_data={"name": "mini", "type": "http", "url": "https://x/mcp"})
    assert card._json_mode is True
    assert card._stack.currentIndex() == 1  # JSON 页
    # 模式回调按钮应反映 JSON 激活态
    assert card.modeChanged is not None


def test_add_mode_defaults_to_form():
    """新增服务器时默认表单模式"""
    from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

    card = MCPEditCard(server_data=None)
    assert card._json_mode is False
    assert card._stack.currentIndex() == 0  # 表单页


def test_http_headers_stretch():
    """http 类型：headers 行拉伸填满剩余空间"""
    from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

    card = MCPEditCard(server_data={"name": "mini", "type": "http", "url": "https://x/mcp"})
    # 显式切到表单页验证字段显隐与拉伸（模拟用户点「表单」）
    card._json_mode = False
    card._on_type_changed("http")

    assert _headers_stretch(card) == 1
    # 取消 100px 上限（QWIDGETSIZE_MAX = 16777215）
    assert card.headersEdit.maximumHeight() == 16777215
    # 显式可见性状态（headless 下 isVisible() 受父级未显示影响，改用 WA_WState_Hidden 显式标志）
    assert card.headersEdit.testAttribute(Qt.WA_WState_Hidden) is False
    # stdio-only 字段应隐藏
    assert card.commandEdit.testAttribute(Qt.WA_WState_Hidden) is True
    assert card.urlEdit.testAttribute(Qt.WA_WState_Hidden) is False


def test_stdio_headers_not_stretched():
    """stdio 类型：headers 行不拉伸、且隐藏"""
    from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

    card = MCPEditCard(server_data={"name": "fs", "type": "stdio", "command": "npx", "args": []})
    card._json_mode = False
    card._on_type_changed("stdio")

    assert _headers_stretch(card) == 0
    assert card.headersEdit.maximumHeight() == 100
    assert card.headersEdit.testAttribute(Qt.WA_WState_Hidden) is True
    assert card.commandEdit.testAttribute(Qt.WA_WState_Hidden) is False


def test_toggle_back_to_form_resyncs_visibility():
    """从 JSON 切回表单必须重新同步字段显隐（修复显隐错乱 bug）"""
    from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

    card = MCPEditCard(server_data={"name": "mini", "type": "http", "url": "https://x/mcp"})
    assert card._json_mode is True

    # 模拟用户点击「表单」按钮
    card._toggle_mode()

    assert card._json_mode is False
    assert card._stack.currentIndex() == 0  # 表单页
    # http 字段应正确显隐
    assert card.urlEdit.testAttribute(Qt.WA_WState_Hidden) is False
    assert card.commandEdit.testAttribute(Qt.WA_WState_Hidden) is True
    assert card.headersEdit.testAttribute(Qt.WA_WState_Hidden) is False
    assert _headers_stretch(card) == 1


def test_stdio_with_url_stays_stdio_in_form():
    """回归：stdio 服务器若同时带有 url 字段，切到表单后类型必须仍是 stdio（不应误判为 sse）

    根因：_build_json_from_data 对 stdio 省略 type，但保留 url；切回表单时
    _normalize_server_data 的旧规则 `elif 'url' in data: type='sse'` 会把带 url 的
    stdio 服务器误判成 sse。修复后优先级改为 command 存在即 stdio。
    """
    from app.widgets.cards.settings.mcp_setting_card import MCPEditCard

    card = MCPEditCard(
        server_data={
            "name": "my-fs",
            "type": "stdio",
            "command": "npx",
            "args": [],
            "url": "https://example.com/leftover",  # 模板/复制配置常见残留
            "_source": "x",
        }
    )
    assert card._json_mode is True  # 编辑默认 JSON 模式
    # 模拟用户点击「表单」按钮
    card._toggle_mode()
    assert card._json_mode is False
    assert card.typeCombo.currentText() == "stdio", "带 url 的 stdio 服务器被误判为 sse"
    # 显隐也应符合 stdio：command 可见、url 隐藏
    assert card.commandEdit.testAttribute(Qt.WA_WState_Hidden) is False
    assert card.urlEdit.testAttribute(Qt.WA_WState_Hidden) is True

