"""真实 WebEngine 回放：观察正文切批、旧按钮身份和迟到工具归属。"""
import os

import pytest

pytestmark = pytest.mark.perf


def test_compact_grouped_stream(qapp, qtbot, compact_output_plugin):
    if os.environ.get('DRIFOX_VISIBLE_CADENCE') != '1' or qapp.platformName() == 'offscreen':
        pytest.skip('需开启真窗 WebEngine 回放')
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.utils.config import Settings
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    cfg = Settings.get_instance()
    old = cfg.ui_compact_tool_area.value
    window = QScrollArea()
    window.setWidgetResizable(True)
    qtbot.addWidget(window)
    try:
        cfg.set(cfg.ui_compact_tool_area, True, save=False)
        container = QWidget()
        window.setWidget(container)
        layout = QVBoxLayout(container)
        layout.addSpacing(800)
        card = MessageCard('assistant', parent=container)
        layout.addWidget(card, alignment=Qt.AlignTop)
        window.resize(800, 400)
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
                bar = window.verticalScrollBar()
                bar.setValue(bar.maximum())
                positions = []
                bar.valueChanged.connect(positions.append)
                viewer_heights = []
                card.heightChanged.connect(viewer_heights.append)
                js("""window._heights=[]; window._sampled=false;
                  (function(){var end=performance.now()+320;firstButton.click();
                  function sample(){_heights.push(firstButton.nextElementSibling.getBoundingClientRect().height);
                    if(performance.now()<end)requestAnimationFrame(sample);else window._sampled=true;}sample()})()""")
                qtbot.waitUntil(lambda: bool(js('window._sampled')), timeout=4000)
                heights = js('window._heights')
                card.heightChanged.disconnect(viewer_heights.append)
                bar.valueChanged.disconnect(positions.append)
                if opening:
                    assert len(set(heights)) >= 3, heights
                    assert len(set(viewer_heights)) >= 3, viewer_heights
                else:
                    assert set(heights) == {0}, heights
                    assert len(set(viewer_heights)) == 1, viewer_heights
                    assert positions == [bar.maximum()], positions
                    def text_viewport_y():
                        origin = card.viewer.mapTo(window.viewport(), QPoint()).y()
                        return origin + js("document.querySelector('[data-block-kind=text]').getBoundingClientRect().top")
                    landed = text_viewport_y()
                    for _ in range(6):
                        qtbot.wait(50)
                        assert abs(text_viewport_y() - landed) <= 1
                assert all((b-a if opening else a-b) >= -1 for a,b in zip(heights, heights[1:])), heights
                assert all((b-a if opening else a-b) >= -2 for a,b in zip(viewer_heights, viewer_heights[1:])), viewer_heights
                assert abs(card.viewer.height() - js('document.body.scrollHeight + 2')) <= 2
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
        styles = js("""(function(){
          firstButton.focus();
          var probe = document.createElement('span');
          probe.style.color = 'var(--text-muted)';
          firstButton.parentNode.appendChild(probe);
          var base = getComputedStyle(probe);
          var result = Array.from(document.querySelectorAll('.dfx-group-button'), function(button) {
            var label = getComputedStyle(button.querySelector('.dfx-group-label'));
            var s = getComputedStyle(button);
            return [parseFloat(label.fontSize) / parseFloat(base.fontSize), label.color === base.color,
                    label.opacity, s.outlineStyle, s.backgroundColor, button.offsetHeight];
          });
          probe.remove();
          return result;
        })()""")
        assert styles[0] == styles[1]
        assert styles[0][:-1] == [pytest.approx(0.82, abs=0.001), True, '1', 'none', 'rgba(0, 0, 0, 0)']
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
        qtbot.wait(350)
        overhead = card.height() - card.viewer.height()
        for expanded in ('false', 'true'):
            js('firstButton.click()')
            qtbot.wait(320)
            assert js("firstButton.getAttribute('aria-expanded')") == expanded
            assert abs(card.viewer.height() - js('document.body.scrollHeight + 2')) <= 2
            assert abs(card.height() - card.viewer.height() - overhead) <= 2
    finally:
        cfg.set(cfg.ui_compact_tool_area, old, save=False)


def test_completed_group_icons_on_replay(qapp, qtbot, compact_output_plugin):
    if os.environ.get('DRIFOX_VISIBLE_CADENCE') != '1' or qapp.platformName() == 'offscreen':
        pytest.skip('需开启真窗 WebEngine 回放')
    from PyQt5.QtCore import QRect
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    card = MessageCard('assistant')
    qtbot.addWidget(card)
    card.resize(640, 400)
    card.set_content([
        {'type': 'reasoning', 'content': '检查文件'},
        {'type': 'text', 'text': '开始执行。'},
        {'type': 'tool_result', 'name': 'bash', 'tool_call_id': 'cmd', 'result': 'ok', 'success': True},
        {'type': 'text', 'text': '命令已完成。'},
        {'type': 'tool_result', 'name': 'unknown_tool', 'tool_call_id': 'other', 'result': 'ok', 'success': True},
    ])
    card.show()
    card.ensure_rendered()
    qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready))
    js = card.viewer._run_js_sync
    groups = "Array.from(document.querySelectorAll('.dfx-group-button[data-group-complete]'))"
    qtbot.waitUntil(lambda: js(f"{groups}.length") == 3)
    boxes = js(f"""{groups}.map(b => {{
        let r = b.getBoundingClientRect(), label = b.querySelector('.dfx-group-label').getBoundingClientRect();
        return [Math.ceil(r.left), Math.ceil(r.top), Math.floor(label.left-r.left)-1, Math.floor(r.height)];
    }})""")
    qtbot.wait(200)
    # 截取图标区域，与隐藏图标后同一位置的像素比较，覆盖 qrc 遮罩读不到的情况。
    before = [card.viewer.grab(QRect(*box)).toImage() for box in boxes]
    js("""var hideIcons = document.createElement('style');
        hideIcons.textContent = '.dfx-group-button::before {visibility:hidden}';
        document.head.appendChild(hideIcons);""")
    qtbot.wait(150)
    try:
        for box, visible in zip(boxes, before):
            assert visible != card.viewer.grab(QRect(*box)).toImage(), box
    finally:
        js('hideIcons.remove()')


def test_live_group_follows_growth_until_scrolled_up(qapp, qtbot, compact_output_plugin):
    if os.environ.get('DRIFOX_VISIBLE_CADENCE') != '1' or qapp.platformName() == 'offscreen':
        pytest.skip('需开启真窗 WebEngine 回放')
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    card = MessageCard('assistant')
    qtbot.addWidget(card)
    card.resize(640, 400)
    card.show()
    card.ensure_rendered()
    qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready))
    js = card.viewer._run_js_sync
    card.start_streaming_anim()

    def append(index):
        card.append_tool_result(tool_call_id=f'read-{index}', tool_name='read',
                                arguments={'path': str(index)}, result='ok', success=True)

    for index in range(16):
        append(index)
    qtbot.waitUntil(lambda: bool(js("document.querySelector('.dfx-group-body [data-tool-call-id=\"read-15\"]') !== null")))
    js("window.panel = document.querySelector('.dfx-group-body[data-live]')")
    at_bottom = 'panel.scrollHeight - panel.clientHeight - panel.scrollTop <= 2'
    qtbot.waitUntil(lambda: bool(js('panel.scrollTop > 50 && ' + at_bottom)))
    # 模拟工具详情异步长高，此时没有新分组补丁。
    js("panel.lastElementChild.lastElementChild.style.paddingBottom = '80px'")
    qtbot.waitUntil(lambda: bool(js(at_bottom)))
    js('panel.scrollTop = 24')
    qtbot.waitUntil(lambda: bool(js('!panel._followTail')))
    append(16)
    qtbot.waitUntil(lambda: bool(js("!!panel.querySelector('[data-tool-call-id=\"read-16\"]')")))
    assert js('panel.scrollTop') == 24
    js('panel.scrollTop = panel.scrollHeight')
    qtbot.waitUntil(lambda: bool(js('panel._followTail')))
    append(17)
    qtbot.waitUntil(lambda: bool(js("!!panel.querySelector('[data-tool-call-id=\"read-17\"]') && " + at_bottom)))
    card.finish_streaming()
