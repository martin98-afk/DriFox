"""真窗回归：正文流式途中插入工具完成，正文可见增长与工具块原位转换。

覆盖两件事实（后台单测只覆盖数据层，不覆盖真实绘制）：
  1. 流式运行行 → 完成块是**原位转换**：块顶/标题行列位置与运行行一致
     （≤1px），容器内不残留流式块、不出现重复块；
  2. 转换发生在正文投递中途时，正文可见文字不得倒退或长时间停顿。

坐标与文字均取自 Chromium 真实布局；offscreen / 后台窗口不代表绘制，
须 DRIFOX_VISIBLE_CADENCE=1 且真实前台窗口运行。
"""
import json
import os
import time

import pytest

pytestmark = pytest.mark.perf

GEOM_JS = """(function(){var el=document.querySelector('[data-tool-call-id="t1"]');
    if(!el)return null;
    var r=el.getBoundingClientRect();
    var tc=document.getElementById('tool-content');
    var cr=tc?tc.getBoundingClientRect():null;
    var title=el.querySelector('span[style*="nowrap"]');
    var tr=title?title.getBoundingClientRect():null;
    return {x:Math.round(r.x*10)/10,y:Math.round(r.y*10)/10,
        relY:cr?Math.round((r.top-cr.top)*10)/10:null,
        h:Math.round(r.height*10)/10,
        titleL:tr?Math.round(tr.left*10)/10:null,
        titleT:tr?Math.round(tr.top*10)/10:null,
        titleRelX:tr?Math.round((tr.left-r.left)*10)/10:null,
        titleRelY:tr?Math.round((tr.top-r.top)*10)/10:null,
        streaming:el.classList.contains('tool-streaming-block'),
        collapsible:el.classList.contains('cm-collapsible')};})()"""

TOOL_DOM_JS = """(function(){var tc=document.getElementById('tool-content');
    if(!tc)return '{"missing":true}';
    return JSON.stringify({streaming:tc.querySelectorAll('.tool-streaming-block').length,
        blocks:tc.querySelectorAll('.tool-block').length,
        expanded:Array.prototype.map.call(tc.querySelectorAll('.tool-block'),
            function(el){return el.getAttribute('data-expanded');})});})()"""


def test_tool_complete_interleave(qapp, qtbot):
    if os.environ.get("DRIFOX_VISIBLE_CADENCE") != "1" or qapp.platformName() == "offscreen":
        pytest.skip("需显式开启真窗回放")
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
        qtbot.waitUntil(lambda: bool(result) or result == [None], timeout=5000)
        return result[0]

    card.raise_()
    card.activateWindow()
    card.start_streaming_anim()
    sample_origin = time.perf_counter()
    assert js("""(function(){var start=performance.now(), prev=0, stamps=[], minLen=0, back=[];
        window._icStart=start;window._icDone=false;window._icStamps=stamps;
        window._icBack=back;
        function tick(){var n=document.getElementById('content-placeholder');
        var size=n?n.textContent.trim().length:0;
        if(size<minLen)back.push([performance.now()-start,minLen,size]);
        if(size>minLen)minLen=size;
        if(size>prev)stamps.push([performance.now()-start,size-prev]);
        prev=size;
        if(!window._icDone)requestAnimationFrame(tick);}tick();return true;})()""")
    echo_ms = (time.perf_counter() - sample_origin) * 1000

    pieces = ["交错%03d段" % i for i in range(24)]
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
    feed()
    timer.start(30)
    expected = "".join(pieces)

    # 正文投递途中插工具：运行行先到，完成块后到，正文不中断
    qtbot.waitUntil(lambda: len(sent) >= 6, timeout=15000)
    card.update_tool_streaming("t1", "bash", {"command": "git log --oneline -3"})
    qtbot.waitUntil(lambda: (js(GEOM_JS) or {}).get("streaming") is True, timeout=5000)
    qtbot.wait(200)
    before = js(GEOM_JS)

    qtbot.waitUntil(lambda: len(sent) >= 12, timeout=15000)
    card.append_tool_result(
        tool_name="bash",
        arguments={"command": "git log --oneline -3"},
        result="5213b557c 参数化紧凑几何测试\n2183d6e6e 优化会话查询",
        success=True,
        tool_call_id="t1",
    )
    qtbot.waitUntil(lambda: (js(GEOM_JS) or {}).get("streaming") is False, timeout=5000)
    qtbot.wait(200)
    after = js(GEOM_JS)

    qtbot.waitUntil(lambda: len(sent) == len(pieces), timeout=15000)

    def complete():
        text = js("document.getElementById('content-placeholder').textContent") or ""
        return text.count(expected) == 1

    qtbot.waitUntil(complete, timeout=5000)
    stamps = js("window._icDone=true;window._icStamps")
    backs = js("window._icBack")
    tool_dom = json.loads(js(TOOL_DOM_JS) or "{}")

    gaps = sorted(b[0] - a[0] for a, b in zip(stamps, stamps[1:]))
    span = (sent[-1] - sent[0]) * 1000
    print(
        f"可见增长帧={len(stamps)} 间隔中位={gaps[len(gaps)//2]:.0f}ms "
        f"P95={gaps[int(.95*len(gaps))]:.0f}ms 最长={max(gaps):.0f}ms "
        f"对时误差≤{echo_ms:.0f}ms 投递跨度={span:.0f}ms 正文倒退={len(backs)} "
        f"运行行=({before['x']},rel{before['relY']},h{before['h']},标题+{before['titleRelX']}/+{before['titleRelY']}) "
        f"完成块=({after['x']},rel{after['relY']},h{after['h']},标题+{after['titleRelX']}/+{after['titleRelY']}) "
        f"工具区 {tool_dom}",
        flush=True,
    )

    card.finish_streaming()

    # 原位转换：块在工具区内的相对位置与标题行列位置不位移；容器内无流式残留、无重复块
    assert after["streaming"] is False
    assert after["collapsible"] is True
    assert abs(after["x"] - before["x"]) <= 1, "完成块横向位移"
    assert abs(after["relY"] - before["relY"]) <= 1, "完成块在工具区内的位置变了"
    assert abs(after["titleRelX"] - before["titleRelX"]) <= 1, "完成瞬间标题横向跳动"
    assert abs(after["titleRelY"] - before["titleRelY"]) <= 1, "完成瞬间标题纵向跳动"
    assert tool_dom.get("streaming") == 0, f"流式块残留：{tool_dom}"
    assert tool_dom.get("blocks") == 1, f"完成块重复：{tool_dom}"
    # v2 通道顺序由 _content_data 块列表决定：完成块须落在正文块之间，不得沉底
    kinds = [(b or {}).get("type") for b in card._content_data]
    assert kinds.count("tool_result") == 1, f"工具完成块缺失：{kinds}"
    idx = kinds.index("tool_result")
    assert 0 < idx < len(kinds) - 1, f"工具完成块顺序异常：{kinds}"
    # 正文在工具完成前后不得倒退，也不得出现明显长停顿
    assert backs == [], f"正文可见文字倒退：{backs[:3]}"
    assert max(gaps) <= 300, f"最长可见停顿 {max(gaps):.0f}ms"
