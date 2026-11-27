"""真窗逐帧测量 WebEngine 视口与实际文档内容的有符号差（空白 / 裁切）。"""
import os

import pytest

pytestmark = pytest.mark.perf


@pytest.mark.parametrize('collapse', [False, True], ids=['prose', 'collapse'])
def test_compact_stream_geometry(qapp, qtbot, collapse):
    if os.environ.get('DRIFOX_VISIBLE_CADENCE') != '1' or qapp.platformName() == 'offscreen':
        pytest.skip('需要真实前台窗口')
    from PyQt5.QtWidgets import QWidget, QVBoxLayout, QScrollArea
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    view = QScrollArea()
    qtbot.addWidget(view)
    view.setWidgetResizable(True)
    body = QWidget()
    lay = QVBoxLayout(body)
    card = MessageCard('assistant', parent=body)
    lay.addWidget(card)
    lay.addStretch()
    view.setWidget(body)
    view.resize(640, 580)
    view.show()
    card.ensure_rendered()
    qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready), timeout=20000)
    page = card.viewer.page()

    def js(script):
        values = []
        page.runJavaScript(script, values.append)
        qtbot.waitUntil(lambda: bool(values), timeout=5000)
        return values[0]

    assert js('window._toolCompactMode')
    card.start_streaming_anim()
    if collapse:
        card.append_text('测量正文内元素收起后的气泡底边。')
        qtbot.waitUntil(lambda: js("document.querySelector('#content-placeholder').innerText").count('底边') == 1)
        js("var box=document.createElement('div');box.style.height='300px';document.querySelector('#content-placeholder').appendChild(box)")
        qtbot.waitUntil(lambda: js('Math.abs(window.innerHeight-document.body.scrollHeight-2)<=3'))
        js('''box.remove();window._collapseLag=null;var t0=performance.now();requestAnimationFrame(function tick(){
            if(window.innerHeight-document.body.scrollHeight<=5)window._collapseLag=performance.now()-t0;
            else requestAnimationFrame(tick);});''')
        qtbot.waitUntil(lambda: js('window._collapseLag') is not None, timeout=2500)
        collapse_ms = js('window._collapseLag')
        print(f'300px 元素收起后空白收回耗时={collapse_ms:.0f}ms', flush=True)
        card.finish_streaming()
        assert collapse_ms < 300
        return
    js('''window._gapSamples=[];requestAnimationFrame(function tick(){
        var c=document.querySelector('#content-placeholder');
        if(c.innerText.length>40) window._gapSamples.push([performance.now(),window.innerHeight,
            document.body.scrollHeight,c.innerText.length]);
        if(!window._stopGap)requestAnimationFrame(tick);});''')
    text = '段落会逐步换行，阅读位置应该跟随真实内容。'
    expected = []
    for i in range(140):
        chunk = text * (4 if i % 7 == 0 else 2) + ('\n\n' if i % 11 == 10 else '')
        expected.append(chunk)
        card.append_text(chunk)
        qtbot.wait(24)
    qtbot.wait(600)
    frames = js('window._stopGap=true;window._gapSamples')
    assert len(frames) > 50
    cap = card.viewer.body_max_height_cap()
    assert frames[-1][3] > 6000
    gaps = [viewport - min(doc + 2, cap) for _, viewport, doc, _ in frames if doc + 2 < cap]
    assert len(gaps) > 50
    clip = sorted(max(0, -g) for g in gaps)
    blank = sorted(max(0, g) for g in gaps)
    print(f'采样={len(gaps)} 空白P95/峰值={blank[int(.95*len(gaps))]}/{blank[-1]} '
          f'裁切P95/峰值={clip[int(.95*len(gaps))]}/{clip[-1]} 终态差={gaps[-1]}', flush=True)
    qtbot.waitUntil(lambda: js("document.querySelector('#content-placeholder').innerText").count(text)
                    == ''.join(expected).count(text), timeout=5000)
    card.finish_streaming()
    qtbot.waitUntil(lambda: js("document.querySelector('#content-placeholder').innerText").count(text)
                    == ''.join(expected).count(text), timeout=5000)
    assert frames[-1][1] == cap and frames[-1][2] > cap
    line_px = js("parseFloat(getComputedStyle(document.querySelector('#content-placeholder p') || document.querySelector('#content-placeholder')).lineHeight)") or 27
    assert clip[int(.95*len(gaps))] <= 2 * round(line_px)
    assert blank[int(.95*len(gaps))] <= 2
