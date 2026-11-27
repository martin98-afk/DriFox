"""真窗回放连续无标点正文，记录浏览器实际可见文字增长的间隔。

只测就绪后的卡片，绕过网络与模型；后台窗口或离屏环境不能代表前台绘制。
"""
import os
import time

import pytest

pytestmark = pytest.mark.perf


def test_visible_text_cadence(qapp, qtbot):
    if os.environ.get("DRIFOX_VISIBLE_CADENCE") != "1" or qapp.platformName() == "offscreen":
        pytest.skip("需显式开启真窗可见文字回放")
    from PyQt5.QtCore import Qt, QTimer
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    card = MessageCard("assistant")
    qtbot.addWidget(card)
    card.resize(900, 700)
    card.show()
    card.ensure_rendered()
    qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready), timeout=30000)
    page = card.viewer.page()

    def js(script):
        result = []
        page.runJavaScript(script, result.append)
        qtbot.waitUntil(lambda: bool(result), timeout=5000)
        return result[0]

    card.start_streaming_anim()
    assert js("""(function(){var n=document.getElementById('content-placeholder');
        if(!n)return false;var start=performance.now(), prev=0, stamps=[];
        window._sampleStamps=stamps;function tick(){var size=n.textContent.trim().length;
        if(size>prev)stamps.push(performance.now()-start);prev=size;
        if(!window._sampleDone)requestAnimationFrame(tick);}tick();return true;})()""")
    pieces = ["样本%03d段落" % i for i in range(120)]
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
    timer.start(25)
    qtbot.waitUntil(lambda: len(sent) == len(pieces), timeout=15000)
    expected = ''.join(pieces)

    def complete():
        text = js("document.getElementById('content-placeholder').textContent") or ''
        return text.count(expected) == 1 and all(text.count(piece) == 1 for piece in pieces)

    # 流结束前就应完整显示，不得依靠 finish_streaming 的整块重渲自愈。
    qtbot.waitUntil(complete, timeout=5000)
    times = js("window._sampleDone=true;window._sampleStamps")
    assert len(times) >= 2
    gaps = sorted(b-a for a,b in zip(times,times[1:]))
    print(f"可见增长帧={len(times)} 首次={times[0]:.0f}ms 间隔中位={gaps[len(gaps)//2]:.0f}ms P95={gaps[int(.95*len(gaps))]:.0f}ms 最长={max(gaps):.0f}ms 投递跨度={(sent[-1]-sent[0])*1000:.0f}ms", flush=True)
    card.finish_streaming()
    qtbot.waitUntil(complete, timeout=5000)
