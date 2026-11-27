"""在真实 Qt 列表内回放断续 Markdown，检查用户实际看到的内容，而非中间 HTML。"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf


@pytest.mark.parametrize('long_intro', [False, True], ids=['short', 'long'])
def test_compact_mixed_stream(qapp, qtbot, long_intro):
    if os.environ.get('DRIFOX_VISIBLE_CADENCE') != '1' or qapp.platformName() == 'offscreen':
        pytest.skip('需显式开启真窗 WebEngine 回放')
    from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.utils.config import Settings
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    cfg = Settings.get_instance()
    old = cfg.ui_compact_tool_area.value
    area = QScrollArea()
    qtbot.addWidget(area)
    try:
        cfg.set(cfg.ui_compact_tool_area, True, save=False)
        body = QWidget()
        layout = QVBoxLayout(body)
        card = MessageCard('assistant', parent=body)
        layout.addWidget(card)
        layout.addStretch(1)
        area.setWidgetResizable(True)
        area.setWidget(body)
        area.resize(810, 850)
        area.show()
        card.ensure_rendered()
        qtbot.waitUntil(lambda: bool(card.viewer and card.viewer._is_js_ready), timeout=30000)
        page = card.viewer.page()

        def js(script):
            result = []
            page.runJavaScript(script, result.append)
            qtbot.waitUntil(lambda: bool(result), timeout=5000)
            return result[0]

        assert js('window._toolCompactMode')
        card.start_streaming_anim()
        chunks = [
            '### 本周阅读记录\n\n', '三天读完的页数如下。\n\n',
            '```ech', 'arts\n', '{"tooltip":{},"xAxis":{"type":"category","data":["周一","周二","周三"]},',
            '"yAxis":{"type":"value"},"series":[{"name":"页数","type":"bar","data":[18,32,25]}]}\n',
            '```\n\n', '周二阅读量最高，共 32 页。\n\n',
            '```mermaid\ngraph LR\n A[阅读] --> B[笔记]\n', '```\n\n',
            '```echarts\n{"bad_chart_marker": invalid}\n', '```\n\n',
            '```mermaid\nnot_a_diagram broken_mermaid_marker\n', '```\n\n',
            '普通代码仍应完整显示：\n\n```python\nprint(75)\n```\n\n', '记录结束。\n\n',
            '```svg\n<svg viewBox="truncated_svg_marker',
        ]
        if long_intro:
            chunks.insert(1, '阅读时把值得回看的段落做了标记。' * 50 + '\n\n')
        # 每个浏览器绘制帧取可见正文；不检查 data-*（合法图表本就携带载荷）。
        js("""(function(){window._leaks=[];var root=document.querySelector('#content-placeholder');
          window._chartSwaps=0;var canvas=null;
          function sample(){var next=root.querySelector('.echarts-container canvas');
            if(next){if(canvas&&next!==canvas)window._chartSwaps++;canvas=next;}
            var t=root.innerText;if(/bad_chart_marker|broken_mermaid_marker|truncated_svg_marker|\"xAxis\"|graph LR/.test(t)){
            if(window._leaks.length<4)window._leaks.push(t.slice(-400));}
            if(!window._mixedDone)requestAnimationFrame(sample);}sample();})()""")
        motion = None if long_intro else os.environ.get('DRIFOX_RENDER_MOTION')
        frames = []

        def capture_frame():
            if not motion:
                return
            from io import BytesIO
            from PIL import Image
            from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.WriteOnly)
            assert area.grab().save(buffer, 'PNG')
            frames.append(Image.open(BytesIO(bytes(data))).convert('RGB'))

        for chunk in chunks:
            card.append_text(chunk)
            qtbot.wait(140)
            if chunk.startswith('{"tooltip"'):
                bar = "document.querySelector('.chart-skeleton__bars i')"
                qtbot.waitUntil(lambda: bool(js(f'!!{bar}')), timeout=3000)
                if not js("matchMedia('(prefers-reduced-motion: reduce)').matches"):
                    samples = []
                    for _ in range(5):
                        samples.append(js(f"[getComputedStyle({bar}).transform,{bar}.closest('.chart-skeleton').offsetHeight]"))
                        qtbot.wait(60)
                    assert len({v[0] for v in samples}) > 1, '图表占位符内部的柱条没有动'
                    assert len({v[1] for v in samples}) == 1
            capture_frame()
        js('window._duringStreamSwaps=window._chartSwaps')
        card.finish_streaming()
        root = "document.querySelector('#content-placeholder')"
        qtbot.waitUntil(lambda: '记录结束。' in (js(f'{root}.innerText') or ''), timeout=10000)
        text = js(f'{root}.innerText')
        assert text.count('记录结束。') == 1 and 'print(75)' in text
        qtbot.waitUntil(lambda: bool(js(f"{root}.querySelector('.echarts-container canvas,.echarts-container svg') !== null")), timeout=10000)
        qtbot.waitUntil(lambda: bool(js(f"{root}.querySelector('.mermaid-block svg') !== null")), timeout=10000)
        qtbot.waitUntil(lambda: '尚未闭合' in (js(f'{root}.innerText') or ''), timeout=5000)
        qtbot.waitUntil(lambda: '请检查 Mermaid 语法' in (js(f'{root}.innerText') or ''), timeout=5000)
        assert not js('window._mixedDone=true;window._leaks'), '半截或无效图表源码曾出现在正文'
        assert js('window._duringStreamSwaps') == 0, '后续消息分块不应销毁已有图表画布'
        assert 'bad_chart_marker' not in text
        dest = None if long_intro else os.environ.get('DRIFOX_RENDER_PREVIEW')
        if dest:
            qtbot.wait(500)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            assert area.grab().save(dest)
        if motion:
            capture_frame()
            Path(motion).parent.mkdir(parents=True, exist_ok=True)
            frames[0].save(motion, save_all=True, append_images=frames[1:],
                           duration=[180] * (len(frames) - 1) + [1600], loop=0)
    finally:
        cfg.set(cfg.ui_compact_tool_area, old, save=False)
