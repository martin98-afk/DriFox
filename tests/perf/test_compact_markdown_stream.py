"""真窗回放：简洁模式分块 markdown 流式渲染，段尾暂停不得残留源码。

回归目标：最后一段短 markdown 紧跟在已渲染内容后（增长不足 512、距上次渲染
不足 1.5s）时命中降载门限，段尾暂停无后续 chunk 曾导致源码滞留到 finish。
"""
import os

import pytest

pytestmark = pytest.mark.perf

LONG = "这是一段中文长段落用于检验流式 markdown 渲染并让活动块进入尾态通道。" * 16

# raw = 不得残留在 DOM 的 markdown 源码字面；html = 必须出现的渲染产物。
SCENARIOS = {
    "tail_short_inline": {
        "chunks": [
            "开头有 `code_a` 行内代码。" + LONG,
            "\n补充 **加粗重点** 和 `code_b` 结束。",
        ],
        "raw": ["**加粗重点**", "`code_b`"],
        "html": ["<strong>加粗重点</strong>", "<code>code_b</code>"],
    },
    "tail_short_quote": {
        "chunks": [
            "开头有 **加粗开头** 文字。" + LONG,
            "\n\n> 引用文字结尾",
        ],
        "raw": ["> 引用文字结尾"],
        "html": ["<blockquote>", "引用文字结尾"],
    },
    "tail_short_list": {
        "chunks": [
            "开头有 **加粗开头** 文字。" + LONG,
            "\n\n1. 第一项\n2. 第二项",
        ],
        "raw": ["1. 第一项", "2. 第二项"],
        "html": ["<ol>", "<li>第一项</li>", "<li>第二项</li>"],
    },
}


@pytest.mark.parametrize("name", list(SCENARIOS.keys()))
def test_compact_markdown_stream(qapp, qtbot, compact_output_plugin, name):
    if os.environ.get("DRIFOX_VISIBLE_CADENCE") != "1" or qapp.platformName() == "offscreen":
        pytest.skip("需开启真窗 WebEngine 回放")
    from PyQt5.QtWidgets import QVBoxLayout, QWidget
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.utils.config import Settings
    from app.widgets.message_card import MessageCard

    scenario = SCENARIOS[name]
    init_shared_web_profile()
    cfg = Settings.get_instance()
    old = cfg.ui_compact_tool_area.value
    window = QWidget()
    qtbot.addWidget(window)
    try:
        cfg.set(cfg.ui_compact_tool_area, True, save=False)
        layout = QVBoxLayout(window)
        card = MessageCard("assistant", parent=window)
        layout.addWidget(card)
        window.resize(800, 700)
        window.show()
        card.ensure_rendered()
        qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready), timeout=30000)

        def js(expr):
            results = []
            card.viewer.page().runJavaScript(expr, results.append)
            qtbot.waitUntil(lambda: bool(results), timeout=5000)
            return results[0]

        def assert_rendered(dom, stage):
            for raw in scenario["raw"]:
                assert raw not in dom, f"{stage} 残留源码 {raw!r}: {dom[-600:]}"
            for html in scenario["html"]:
                assert html in dom, f"{stage} 缺少渲染产物 {html!r}: {dom[-600:]}"

        dom_expr = "document.getElementById('content-placeholder').innerHTML"

        card.start_streaming_anim()
        for chunk in scenario["chunks"]:
            card.append_text(chunk)
            qtbot.wait(120)
        # 段尾暂停：超过降载时间窗 1.5s，且不再有后续 chunk 触发渲染。
        qtbot.wait(2200)
        assert_rendered(js(dom_expr), "段尾暂停")

        card.finish_streaming()
        qtbot.wait(600)
        assert_rendered(js(dom_expr), "finish 后")
    finally:
        cfg.set(cfg.ui_compact_tool_area, old, save=False)
