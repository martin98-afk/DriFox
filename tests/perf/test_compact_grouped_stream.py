"""真实 WebEngine 回放：观察正文切批、旧按钮身份和迟到工具归属。"""
import os

import pytest

pytestmark = pytest.mark.perf


def test_compact_grouped_stream(qapp, qtbot, compact_output_plugin):
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
        js("""window._legacyFrames=0; (function sample(){if(document.getElementById('tool-section').getBoundingClientRect().height>0)window._legacyFrames++;if(!window._endProbe)requestAnimationFrame(sample)})()""")
        card.start_streaming_anim()
        first = ('长思考内容用于检验分组滚动窗口。' * 12 + '\n') * 40 + '第一段思考结尾。'
        card.append_reasoning(first)
        live = "document.querySelector('.dfx-group-body .v2-live-text.think-compact')"
        qtbot.waitUntil(lambda: bool(js(f'!!{live}')), timeout=5000)
        qtbot.wait(1100)
        addition = '继续思考到最新一行。' * 100
        card.append_reasoning(addition)
        qtbot.waitUntil(lambda: js(f'{live} && {live}.textContent') == first + addition, timeout=5000)
        live_box = js(f'[{live}.clientHeight,{live}.scrollHeight,{live}.scrollTop]')
        height, content_height, top = live_box
        assert 0 < height <= 96 and content_height > height and abs(content_height-height-top) <= 2, live_box
        card.update_tool_streaming('late-1', 'read', {'path': 'first.txt'})
        qtbot.waitUntil(lambda: js(states) == ['true'], timeout=10000)
        card.append_text('第一段正文。\n\n')
        qtbot.waitUntil(lambda: js(states) == ['false'], timeout=10000)
        js(f"window.firstButton={buttons}[0]")
        hidden = "firstButton.nextElementSibling.hasAttribute('data-group-hidden')"
        qtbot.waitUntil(lambda: bool(js(hidden)), timeout=4000)
        if not js("matchMedia('(prefers-reduced-motion: reduce)').matches"):
            for opening in (True, False):
                js("""window._heights=[]; window._sampled=false;
                  (function(){var end=performance.now()+320;firstButton.click();
                  function sample(){_heights.push(firstButton.nextElementSibling.getBoundingClientRect().height);
                    if(performance.now()<end)requestAnimationFrame(sample);else window._sampled=true;}sample()})()""")
                qtbot.waitUntil(lambda: bool(js('window._sampled')), timeout=4000)
                heights = js('window._heights')
                assert len(set(heights)) >= 3, heights
                assert all((b-a if opening else a-b) >= -1 for a,b in zip(heights, heights[1:])), heights
            js('firstButton.click();setTimeout(()=>firstButton.click(),55)')
            qtbot.wait(300)
            assert js(hidden) and js(states) == ['false']
        js('firstButton.click()')
        card.start_new_thinking_block()
        card.append_reasoning('第二段思考。')
        qtbot.waitUntil(lambda: js(states) == ['true', 'true'], timeout=10000)
        card.append_text('第二段正文。\n\n')
        qtbot.waitUntil(lambda: js(states) == ['true', 'false'], timeout=10000)
        settings = compact_output_plugin.get_settings_cards()[0].widget_class()
        qtbot.addWidget(settings)
        settings._choices['original'].click()
        another = MessageCard('assistant', parent=window)
        assert another._output_mode.id == 'original' and card._output_mode.id == 'compact'
        card.append_tool_result(tool_call_id='late-1', tool_name='read', arguments={'path': 'first.txt'}, result='晚到结果', success=False)
        qtbot.waitUntil(lambda: js('firstButton.textContent') == '读取了 1 个文件，1 个失败', timeout=10000)
        style = js("""(function(){var p=Array.from(document.querySelectorAll('#content-placeholder p')).find(n=>n.textContent.includes('第一段正文'));
          firstButton.focus(); var s=getComputedStyle(firstButton), l=getComputedStyle(firstButton.querySelector('.dfx-group-label')), t=getComputedStyle(p);
          return [parseFloat(t.fontSize)-parseFloat(l.fontSize),l.color===t.color,parseFloat(l.opacity),s.outlineStyle,s.backgroundColor];})()""")
        assert style == [1, True, 0.76, 'none', 'rgba(0, 0, 0, 0)'], style
        qtbot.waitUntil(lambda: bool(js("document.querySelector('[data-tool-call-id=\"late-1\"]:not(.tool-streaming-block)')" + ' !== null')), timeout=10000)
        assert js(f"firstButton === {buttons}[0]")
        assert js(f"{buttons}[0].nextElementSibling.querySelector('[data-tool-call-id=\"late-1\"]') !== null")
        qtbot.wait(320)
        preview = "firstButton._panel.querySelector('[data-block-kind=reasoning] .think-compact')"
        assert js(f'{preview}.innerText.length < 180 && {preview}.offsetHeight > 0')
        card.finish_streaming()
        qtbot.waitUntil(lambda: js(states) == ['true', 'false'], timeout=10000)
        qtbot.waitUntil(lambda: js("!firstButton._panel.hasAttribute('data-live')"), timeout=5000)
        assert js(f'{preview}.innerText.length < 180 && {preview}.offsetHeight > 0')
        assert js('window._endProbe=true;window._legacyFrames') == 0
    finally:
        cfg.set(cfg.ui_compact_tool_area, old, save=False)
