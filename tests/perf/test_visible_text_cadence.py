"""真窗回放连续无标点正文，记录浏览器实际可见文字增长的间隔。

只测就绪后的卡片，绕过网络与模型；后台窗口或离屏环境不能代表前台绘制。
"""
import os
import time

import pytest

pytestmark = pytest.mark.perf


@pytest.mark.parametrize('batch', [1, 8], ids=['uniform', 'bursty'])
def test_visible_text_cadence(qapp, qtbot, batch, compact_output_plugin):
    if os.environ.get("DRIFOX_VISIBLE_CADENCE") != "1" or qapp.platformName() == "offscreen":
        pytest.skip("需显式开启真窗可见文字回放")
    from PyQt5.QtCore import Qt, QTimer
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget
    area = QScrollArea()
    qtbot.addWidget(area)
    area.setWidgetResizable(True)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.addSpacing(600)
    card = MessageCard('assistant', parent=body)
    layout.addWidget(card)
    area.setWidget(body)
    area.resize(580, 420)
    area.show()
    card.ensure_rendered()
    qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready), timeout=30000)
    page = card.viewer.page()

    def js(script):
        result = []
        page.runJavaScript(script, result.append)
        qtbot.waitUntil(lambda: bool(result), timeout=5000)
        return result[0]

    card.raise_()
    card.activateWindow()
    card.start_streaming_anim()
    card.append_reasoning('准备回放正文。')
    qtbot.waitUntil(lambda: bool(js("document.querySelector('.dfx-group-button') !== null")), timeout=5000)
    scrollbar = area.verticalScrollBar()
    scrollbar.setValue(max(0, scrollbar.maximum()-70))
    positions = []
    scrollbar.valueChanged.connect(positions.append)
    sample_origin = time.perf_counter()
    assert js("""(function(){var start=performance.now(), prev=0, stamps=[];
        window._sampleStart=start;window._sampleDone=false;window._sampleStamps=stamps;window._groupTops=[];
        function tick(){var n=document.querySelector('[data-block-kind=text]');var size=n?n.innerText.trim().length:0;
        var group=document.querySelector('.dfx-group-button'); if(group)window._groupTops.push([group.getBoundingClientRect().top,document.body.scrollTop,window.scrollY,document.body.className,window.innerHeight,document.body.scrollHeight]);
        if(size>prev)stamps.push([performance.now()-start,size-prev]);prev=size;
        if(!window._sampleDone)requestAnimationFrame(tick);}tick();return true;})()""")
    echo_ms = (time.perf_counter() - sample_origin) * 1000
    pieces = ["样本%03d段落" % i for i in range(120)]
    pieces = [''.join(pieces[i:i+batch]) for i in range(0, len(pieces), batch)]
    sent = []
    timer = QTimer(card)
    timer.setTimerType(Qt.PreciseTimer)

    def feed():
        i = len(sent)
        sent.append(time.perf_counter())
        card.append_text(pieces[i])
        if i + 1 == len(pieces):
            timer.stop()

    timer.timeout.connect(feed)
    feed()  # 两种输入都立即发首批，首字延迟不包含定时器的首次等待。
    timer.start(25 * batch)
    qtbot.waitUntil(lambda: len(sent) == len(pieces), timeout=15000)
    expected = ''.join(pieces)

    def complete():
        text = js("document.getElementById('content-placeholder').textContent") or ''
        return text.count(expected) == 1 and all(text.count(piece) == 1 for piece in pieces)

    # 流结束前就应完整显示，不得依靠 finish_streaming 的整块重渲自愈。
    qtbot.waitUntil(complete, timeout=5000)
    times = js("window._sampleDone=true;window._sampleStamps")
    assert len(times) >= 2
    gaps = sorted(b[0]-a[0] for a,b in zip(times,times[1:]))
    jumps = sorted(t[1] for t in times)
    span = (sent[-1]-sent[0])*1000
    origin_to_send = (sent[0] - sample_origin) * 1000
    first = times[0][0] - origin_to_send
    tail = times[-1][0] - origin_to_send - span
    print(f"可见增长帧={len(times)} 首批到首帧≈{first:.0f}ms 末批到末帧≈{tail:.0f}ms 对时误差≤{echo_ms:.0f}ms 首帧增字={times[0][1]} 间隔中位={gaps[len(gaps)//2]:.0f}ms P95={gaps[int(.95*len(gaps))]:.0f}ms 最长={max(gaps):.0f}ms 单帧增字P95={jumps[int(.95*len(jumps))]} 最大={max(jumps)}", flush=True)
    samples = js('window._groupTops')
    assert not positions, positions
    assert max(s[0] for s in samples)-min(s[0] for s in samples) <= 1, (min(samples), max(samples))
    if batch > 1:
        assert times[0][1] < len(pieces[0]), '首批正文不应整段跳出'
    card.finish_streaming()
    qtbot.waitUntil(complete, timeout=5000)
