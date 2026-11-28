"""真实 WebEngine 回放：观察正文切批、旧按钮身份和迟到工具归属。"""
import os

import pytest

pytestmark = pytest.mark.perf


def test_compact_grouped_stream(qapp, qtbot):
    if os.environ.get('DRIFOX_VISIBLE_CADENCE') != '1' or qapp.platformName() == 'offscreen':
        pytest.skip('需开启真窗 WebEngine 回放')
    from PyQt5.QtWidgets import QVBoxLayout, QWidget
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.utils.config import Settings
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    cfg = Settings.get_instance()
    old = cfg.ui_compact_tool_area.value
    window = QWidget()
    qtbot.addWidget(window)
    try:
        cfg.set(cfg.ui_compact_tool_area, True, save=False)
        layout = QVBoxLayout(window)
        card = MessageCard('assistant', parent=window)
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

        buttons = "Array.from(document.querySelectorAll('.dfx-group-button'))"
        states = f"{buttons}.map(b=>b.getAttribute('aria-expanded'))"
        card.start_streaming_anim()
        card.append_reasoning('第一段思考。')
        card.update_tool_streaming('late-1', 'read', {'path': 'first.txt'})
        qtbot.waitUntil(lambda: js(states) == ['true'], timeout=10000)
        card.append_text('第一段正文。\n\n')
        qtbot.waitUntil(lambda: js(states) == ['false'], timeout=10000)
        js(f"window.firstButton={buttons}[0]; firstButton.click()")
        card.start_new_thinking_block()
        card.append_reasoning('第二段思考。')
        qtbot.waitUntil(lambda: js(states) == ['true', 'true'], timeout=10000)
        card.append_text('第二段正文。\n\n')
        qtbot.waitUntil(lambda: js(states) == ['true', 'false'], timeout=10000)
        card.append_tool_result(tool_call_id='late-1', tool_name='read', arguments={}, result='晚到结果', success=True)
        qtbot.waitUntil(lambda: bool(js("document.querySelector('[data-tool-call-id=\"late-1\"]:not(.tool-streaming-block)')" + ' !== null')), timeout=10000)
        assert js(f"firstButton === {buttons}[0]")
        assert js(f"{buttons}[0].nextElementSibling.nextElementSibling.querySelector('[data-tool-call-id=\"late-1\"]') !== null")
        card.finish_streaming()
        qtbot.waitUntil(lambda: js(states) == ['true', 'false'], timeout=10000)
    finally:
        cfg.set(cfg.ui_compact_tool_area, old, save=False)
