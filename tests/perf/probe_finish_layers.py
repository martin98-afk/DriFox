"""Windows 真窗收尾探针；由 probe_windows_sessions.py finish 在隔离进程运行。"""
import time

from PyQt5.QtCore import Qt, QTimer

from test_stream_render_bench_web import CHUNK_CHARS, FEED_INTERVAL_MS, _build_payload


def finish(tm, first, drain, emit):
    payload = _build_payload()
    tail = 'ENDOFANSWER'
    payload = payload[:-len(tail)] + tail
    for cycle in range(1, 4):
        window = tm.spawn_tab(first, new_session=True)
        assert window is not None
        drain(1200)
        card = window._append_assistant_message()
        card.ensure_rendered()
        viewer = card.viewer
        deadline = time.monotonic() + 20
        while not (viewer._is_js_ready and card.isVisible()) and time.monotonic() < deadline:
            drain(30)
        assert viewer._is_js_ready and card.isVisible(), (viewer._is_js_ready, window.isVisible(), card.isVisible())
        card.start_streaming_anim()
        for offset in range(0, len(payload), CHUNK_CHARS):
            card.append_text(payload[offset:offset + CHUNK_CHARS])
            drain(FEED_INTERVAL_MS)
        drain(800)  # 与单卡基线相同：测已排空的收尾，不把末批积压算进来

        def js(source):
            result = []
            viewer.page().runJavaScript(source, result.append)
            deadline = time.monotonic() + 5
            while not result and time.monotonic() < deadline:
                drain(5)
            assert result, '页面未应答'
            return result[0]

        assert js('''(() => {
            if (!window.patchBlocks) return false;
            window.probePatch = [];
            const patch = window.probeOriginalPatch = window.patchBlocks;
            window.patchBlocks = function(...args) {
                const t = performance.now();
                try { return patch.apply(this, args); }
                finally { probePatch.push(performance.now() - t); }
            };
            return true;
        })()''')
        clock = time.perf_counter
        solidify = card._solidify_text_blocks
        data_ms = 0.0

        def timed_solidify():
            nonlocal data_ms
            start = clock()
            try:
                return solidify()
            finally:
                data_ms += (clock() - start) * 1000

        card._solidify_text_blocks = timed_solidify
        bar = window.chat_scroll_area.verticalScrollBar()
        changes, ranges, heights, ticks = [], [], [], []
        t0 = clock()

        def ms():
            return round((clock() - t0) * 1000, 2)

        def value(v):
            changes.append([ms(), v])

        def bounds(lo, hi):
            ranges.append([ms(), hi])

        def height(h):
            heights.append([ms(), h])

        def tick():
            ticks.append(ms())

        bar.valueChanged.connect(value)
        bar.rangeChanged.connect(bounds)
        viewer._page.heightReported.connect(height)
        timer = QTimer()
        timer.setTimerType(Qt.PreciseTimer)
        timer.timeout.connect(tick)
        timer.start(5)
        gap_before = bar.maximum() - bar.value()
        t0 = clock()
        card.finish_streaming()
        sync_ms = ms()
        drain(1800)
        timer.stop()
        bar.valueChanged.disconnect(value)
        bar.rangeChanged.disconnect(bounds)
        viewer._page.heightReported.disconnect(height)
        dom = js("(() => { window.patchBlocks = window.probeOriginalPatch; delete window.probeOriginalPatch; return [window.probePatch, document.body.innerText.split('ENDOFANSWER').length-1]; })()")
        assert dom[1] == 1 and ''.join(b['text'] for b in card._content_data if b.get('type') == 'text') == payload
        emit(cycle=cycle, chars=len(payload), finish_ms=sync_ms, data_solidify_ms=round(data_ms, 3),
             patch_calls_ms=dom[0], height_reports=heights, scroll_values=changes, scroll_ranges=ranges,
             scroll_max=bar.maximum(), gap_before=gap_before, gap_after=bar.maximum() - bar.value(),
             qt_tick_max_ms=round(max(b-a for a, b in zip([0, *ticks], ticks)), 2))
        del card._solidify_text_blocks
        tm._on_tab_close_requested(len(tm._windows) - 1)
        drain(1000)
