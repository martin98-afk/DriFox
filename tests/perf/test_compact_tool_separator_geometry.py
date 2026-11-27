"""测量贴底时折叠栏相对 WebView 可见底边的位置；排除整张卡随正文增高的正常移动。"""
import os

import pytest

pytestmark = pytest.mark.perf


def test_tool_separator_stays_at_landed_bottom(qapp, qtbot):
    if os.environ.get('DRIFOX_VISIBLE_CADENCE') != '1' or qapp.platformName() == 'offscreen':
        pytest.skip('需要前台真窗')
    from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    area = QScrollArea()
    qtbot.addWidget(area)
    area.setWidgetResizable(True)
    body = QWidget()
    layout = QVBoxLayout(body)
    card = MessageCard('assistant', parent=body)
    layout.addWidget(card)
    area.setWidget(body)
    area.resize(650, 540)
    area.show()
    card.ensure_rendered()
    qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready), timeout=20000)
    bar = area.verticalScrollBar()
    bar.rangeChanged.connect(lambda _, high: bar.setValue(high))

    def js(script):
        values = []
        card.viewer.page().runJavaScript(script, values.append)
        qtbot.waitUntil(lambda: bool(values), timeout=5000)
        return values[0]

    card.start_streaming_anim()
    card.append_reasoning('先核对各段输出。')
    card.append_text('这是一段用于测量折叠栏位置的文字。' * 36 + '\n\n')
    qtbot.waitUntil(lambda: bar.maximum() > 50 and js("document.querySelector('#tool-section').offsetHeight>0"))
    js("var s=document.querySelector('#tool-section');if(s.dataset.collapsed!=='true')document.querySelector('#tool-separator').click();")
    qtbot.wait(450)
    assert js("s.dataset.collapsed==='true' && s.previousElementSibling.id==='content-placeholder'")
    js('''window._dockRows=[];requestAnimationFrame(function loop(){
        var r=s.getBoundingClientRect(); window._dockRows.push([window.innerHeight-r.bottom,r.height,window.innerHeight,document.body.scrollHeight]);
        if(!window._stopDock)requestAnimationFrame(loop);});''')
    for i in range(110):
        card.append_text('正文持续换行，折叠栏应当保持稳定。' * 3 + ('\n\n' if i % 9 == 8 else ''))
        qtbot.wait(27)
    qtbot.wait(400)
    rows = js('window._stopDock=true;window._dockRows')
    assert all(r[1] > 0 for r in rows)
    settled = rows[-1][0]
    errors = sorted(abs(r[0]-settled) for r in rows)
    print(f'折叠栏 {len(rows)} 帧，底边基准 {settled:.0f}px，偏离 P95={errors[int(len(errors)*.95)]:.0f}px，峰值={errors[-1]:.0f}px', flush=True)
    assert errors[int(len(errors)*.95)] <= 2
    js("window._userScrolledWithin=true;document.body.scrollTop=0;reportHeight()")
    assert js("getComputedStyle(s).position!=='sticky'")
    js("window._userScrolledWithin=false;document.body.scrollTop=document.body.scrollHeight;reportHeight()")
    assert js("getComputedStyle(s).position==='sticky'")
    js("document.querySelector('#tool-separator').click()")
    assert js("s.dataset.collapsed==='false'")
    card.finish_streaming()
    qtbot.waitUntil(lambda: js("getComputedStyle(s).position!=='sticky' && s.nextElementSibling.id==='content-placeholder'"), timeout=5000)
