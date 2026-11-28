"""在真实 WebEngine 布局中验证图表落高与用户滚动的时序。"""

from app.core.infra.webengine_profile import init_shared_web_profile
from app.widgets.card_viewers import CodeWebViewer


def test_chart_growth_keeps_follow_and_respects_reading(qtbot):
    init_shared_web_profile()
    viewer = CodeWebViewer()
    qtbot.addWidget(viewer)
    viewer.show()
    qtbot.waitUntil(lambda: viewer._is_js_ready, timeout=10000)
    js = viewer._run_js_sync
    assert js("""
        window.b = document.body;
        window.cp = document.getElementById('content-placeholder');
        b.style.maxHeight = '240px';
        cp.innerHTML = '<div style="height:900px"></div><div id="chart"></div>';
        window.chartBlock = document.getElementById('chart');
        window.gap = () => b.scrollHeight - b.clientHeight - b.scrollTop;
        b.scrollTop = b.scrollHeight;
        b.scrollTop > 0
    """)

    # 图表闭合的这一批没有文字追加，也必须跟随。
    assert js("chartBlock.style.height='600px'; _v2AfterPatch(true, false); gap() <= 1")
    js("b.dispatchEvent(new WheelEvent('wheel', {deltaY:-120})); b.scrollTop=200")
    qtbot.waitUntil(lambda: bool(js("window._userScrolledWithin")))
    assert js("chartBlock.style.height='800px'; _v2AfterPatch(true, false); b.scrollTop === 200")

    # 滚到旧底部的同一帧内容又增高，此时 scroll 事件尚未投递。
    js("""
        b.dispatchEvent(new WheelEvent('wheel', {deltaY:120}));
        b.scrollTop = b.scrollHeight;
        chartBlock.style.height = '1200px';
    """)
    qtbot.waitUntil(lambda: bool(js("!window._userScrolledWithin && gap() <= 1")))

    # 流式结束后才收到嵌入图表的尺寸，验证真实的异步高度更新入口。
    assert js("""
        _setStreamingDock(false);
        chartBlock._dfxPendingH = 1500;
        _dfxApplyWidgetH(chartBlock);
        gap() <= 1
    """)

    js("""
        window.diagram = document.createElement('div');
        diagram.className = 'mermaid-block mermaid-pending';
        diagram.setAttribute('data-mermaid-src', btoa('graph TD; A-->B; B-->C; C-->D; D-->E; E-->F; F-->G;'));
        cp.appendChild(diagram);
        b.scrollTop = b.scrollHeight;
        renderMermaidBlocks([diagram]);
    """)
    qtbot.waitUntil(lambda: bool(js("diagram.querySelector('svg') !== null")), timeout=10000)
    assert js("gap() <= 1")
