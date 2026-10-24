# -*- coding: utf-8 -*-
"""复现 v2：修复后仍闪烁——定位残留闪烁拍 + 帧级观测 DOM 消失。

场景全流程（简洁模式 + 长消息）：
  阶段1 长正文（差量基线）
  阶段2 思考块：<think>未闭合 → 累积 → 闭合+正文（触发 _needs_full_render 全量拍）
  阶段3 工具运行框注入 → 工具活动期正文（应走降载路径）
  阶段4 工具结果落地：markdown 出现 <tool> 闭合段 + pending 移除（全量拍？）

帧级观测（rAF）：每帧记录 #content-placeholder children 数 / innerText 长度 /
body.scrollHeight，捕捉「消失帧」（children 或文本骤降后恢复）。

运行：python tests/debug/compact_flash_repro2.py
"""

import sys
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import CodeWebViewer  # noqa: E402

INSTRUMENT_JS = """
(function() {
    window._ev = [];
    window._frames = [];
    window._probeSeq = 0;
    var cp0 = document.getElementById('content-placeholder');
    if (cp0 && cp0.firstElementChild && !cp0.firstElementChild.dataset.probeId) {
        cp0.firstElementChild.dataset.probeId = String(++window._probeSeq);
    }
    // rAF 帧观测：捕捉消失帧
    function _loop() {
        var cp = document.getElementById('content-placeholder');
        var kids = cp ? cp.children.length : -1;
        var txt = cp ? (cp.innerText || '').length : -1;
        var sh = document.body ? document.body.scrollHeight : -1;
        var f = window._frames[window._frames.length - 1];
        if (!f || f[1] !== kids || f[2] !== txt || f[3] !== sh) {
            window._frames.push([Math.round(performance.now()), kids, txt, sh]);
        }
        requestAnimationFrame(_loop);
    }
    requestAnimationFrame(_loop);
    function hook(name) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        window[name] = function() {
            var cp = document.getElementById('content-placeholder');
            var first = cp ? cp.firstElementChild : null;
            var idBefore = first ? first.dataset.probeId : null;
            window._ev.push([Math.round(performance.now()), 'BEFORE:' + name, 'probe=' + (idBefore || 'none')]);
            var r = orig.apply(this, arguments);
            var cp2 = document.getElementById('content-placeholder');
            var first2 = cp2 ? cp2.firstElementChild : null;
            var idAfter = first2 ? first2.dataset.probeId : null;
            if (!idAfter && first2) { idAfter = String(++window._probeSeq); first2.dataset.probeId = idAfter; }
            window._ev.push([Math.round(performance.now()), 'AFTER:' + name,
                             'probe=' + (idAfter || 'none'), 'kids=' + (cp2 ? cp2.children.length : -1)]);
            return r;
        };
    }
    ['updateContent', 'updateContentAppend', 'updateTailHtml', 'reorganizeContent'].forEach(hook);
    window._ev.push([0, 'INSTRUMENTED']);
    window._stats = function() {
        var uc = 0, uca = 0, uth = 0, rc = 0, rebuilt = 0, prevProbe = '1';
        window._ev.forEach(function(row) {
            var tag = String(row[1] || '');
            if (tag.indexOf('AFTER:updateContent') === 0 && tag !== 'AFTER:updateContentAppend') {
                uc++;
                var m = String(row[2] || '');
                var cur = m.split('=')[1];
                if (cur && cur !== prevProbe) rebuilt++;
                prevProbe = cur || prevProbe;
            }
            if (tag === 'AFTER:updateContentAppend') uca++;
            if (tag === 'AFTER:updateTailHtml') uth++;
            if (tag === 'AFTER:reorganizeContent') rc++;
        });
        // 消失帧检测：children 或文本长度骤降超过 30% 后恢复
        var drops = [];
        for (var i = 1; i < window._frames.length; i++) {
            var a = window._frames[i-1], b = window._frames[i];
            if (a[1] > 3 && b[1] < a[1] * 0.7) drops.push([b[0], 'kids ' + a[1] + '→' + b[1]]);
            if (a[2] > 100 && b[2] < a[2] * 0.7) drops.push([b[0], 'text ' + a[2] + '→' + b[2]]);
            if (a[3] > 0 && b[3] < a[3] * 0.5) drops.push([b[0], 'scrollH ' + a[3] + '→' + b[3]]);
        }
        return JSON.stringify({uc: uc, uca: uca, uth: uth, rc: rc, rebuilt: rebuilt,
                               drops: drops, frameCount: window._frames.length});
    };
})()
"""

_FAT = (
    "流式输出持续进行，正文内容不断增长，用来撑起一条足够长的消息。"
    "每个段落都足够长，确保整条消息在简洁模式坞态下形成内部滚动。"
) * 2
PARAS = [f"第{i}段：{_FAT}\n\n" for i in range(1, 13)]

THINK_OPEN = "<think>\n这是推理模型的思考过程，正在分析用户的问题并规划回答结构，需要较长的思考内容来触发真实场景。\n"
THINK_CLOSE = "</think>\n\n思考完成，下面开始正式回答用户的问题。\n\n"
POST_THINK = [f"思考后正文第{i}段：继续输出足够长的内容维持坞态滚动。\n\n" for i in range(1, 5)]

TOOL_MD = (
    "<tool>\n"
    'tool_name: bash\n'
    "tool_call_id: t-result-1\n"
    "arguments:\n  command: ls -la\n"
    "result: |\n  total 64\n  drwxr-xr-x 12 user staff 384\n"
    "</tool>\n\n"
)
POST_TOOL = [f"工具后正文第{i}段：工具结果返回后继续流式输出的内容，观察是否闪烁。\n\n" for i in range(1, 6)]

py_log: list[str] = []


def plog(msg):
    t = time.strftime("%H:%M:%S") + f".{int(time.monotonic()*1000)%1000:03d}"
    line = f"[{t}] {msg}"
    py_log.append(line)
    print(line, flush=True)


def main():
    app = QApplication(sys.argv)
    init_shared_web_profile()
    viewer = CodeWebViewer(light=True)
    viewer.contentHeightChanged.connect(lambda h: viewer.setFixedHeight(h))
    viewer.resize(620, 900)
    viewer.show()

    state = {"phase": "wait", "idx": 0}

    _orig = viewer._perform_update

    import app.widgets.card_viewers as _cv

    _orig_extract = _cv._extract_closed_segments
    _orig_boundary = viewer._has_reached_clean_boundary
    _orig_tailfn = viewer._render_tail_inline
    _orig_seqr = viewer._sequence_render
    _orig_apply = viewer._apply_render_result

    def extract_patched(md_tail):
        r = _orig_extract(md_tail)
        if r[1]:
            plog(f"    _extract_closed_segments: {len(r[1])} 段 stable+{r[0]}")
        else:
            plog("    _extract_closed_segments: 0 段")
        return r

    def boundary_patched(md):
        r = _orig_boundary(md)
        plog(f"    clean_boundary={r}")
        return r

    def tail_patched():
        plog("    → _render_tail_inline")
        return _orig_tailfn()

    def seq_patched(md, compact):
        plog(f"    → _sequence_render 提交 seq={viewer._render_seq} inflight={viewer._render_inflight}")
        return _orig_seqr(md, compact)

    def apply_patched(seq, html):
        plog(f"    ← _apply_render_result seq={seq} cur_seq={viewer._render_seq} html={'None' if html is None else len(html)}")
        return _orig_apply(seq, html)

    _cv._extract_closed_segments = extract_patched
    viewer._has_reached_clean_boundary = boundary_patched
    viewer._render_tail_inline = tail_patched
    viewer._sequence_render = seq_patched
    viewer._apply_render_result = apply_patched

    def perform_patched():
        plog(f"_perform_update: dirty={viewer._tool_dom_dirty} "
             f"pending={len(getattr(viewer, '_injected_pending_tools', set()) or set())} "
             f"nf={viewer._needs_full_render} active={viewer._has_active_tool_dom()} "
             f"md={len(viewer._markdown_text)} vis={viewer.isVisible()} "
             f"jsready={viewer._is_js_ready} deferred={viewer._render_deferred} "
             f"timer={viewer._render_timer.isActive()} inflight={viewer._render_inflight}")
        return _orig()

    viewer._perform_update = perform_patched

    _orig_append_chunk = viewer.append_chunk

    def append_patched(text):
        _orig_append_chunk(text)
        plog(f"    append_chunk(+{len(text)}) md={len(viewer._markdown_text)} "
             f"timer={viewer._render_timer.isActive()} interval={viewer._render_timer.interval()}")

    viewer.append_chunk = append_patched

    def report():
        plog("── 采样 ──")

        def dump(s):
            import json as _json
            if not s:
                plog("统计为空")
                app.quit()
                return
            d = _json.loads(s)
            print(f"  updateContent: {d['uc']}  Append: {d['uca']}  TailHtml: {d['uth']}  reorg: {d['rc']}", flush=True)
            print(f"  正文首节点身份重建: {d['rebuilt']}", flush=True)
            print(f"  帧观测样本: {d['frameCount']}", flush=True)
            print(f"  消失帧(骤降): {len(d['drops'])}", flush=True)
            for row in d["drops"][:12]:
                print(f"    {row}", flush=True)
            app.quit()

        viewer.page().runJavaScript("window._stats ? window._stats() : null", dump)

    def run_scene():
        plog("── 阶段1: 简洁模式 + 长正文 ──")
        viewer._streaming = True
        viewer.page().runJavaScript(
            "window._toolCompactMode=true; if(typeof _setStreamingDock==='function')_setStreamingDock(true);"
        )

        def pump1():
            i = state["idx"]
            if i < len(PARAS):
                viewer.append_chunk(PARAS[i])
                state["idx"] += 1
                QTimer.singleShot(120, pump1)
            else:
                QTimer.singleShot(600, think_open)

        def think_open():
            plog("── 阶段2: 思考块开启（未闭合，静默累积）──")
            viewer.append_chunk(THINK_OPEN)
            # 思考内容累积（append_text 侧静默，这里直接喂 markdown 模拟累积）
            def think_accum():
                plog("思考累积中（无渲染拍）")
                QTimer.singleShot(300, think_close)
            QTimer.singleShot(300, think_accum)

        def think_close():
            plog("── 阶段2b: 思考闭合 + 正文（应触发全量拍=闪烁嫌疑①）──")
            viewer.append_chunk(THINK_CLOSE)

            def pump2():
                i = state.get("p2", 0)
                if i < len(POST_THINK):
                    viewer.append_chunk(POST_THINK[i])
                    state["p2"] = i + 1
                    QTimer.singleShot(500, pump2)
                else:
                    QTimer.singleShot(600, tool_stream)
            QTimer.singleShot(500, pump2)

        def tool_stream():
            plog("── 阶段3: 工具运行框注入 + 活动期正文（应走降载）──")
            viewer._tool_dom_dirty = True
            viewer._tool_dom_dirty_gen = getattr(viewer, "_tool_dom_dirty_gen", 0) + 1
            viewer._injected_pending_tools = getattr(viewer, "_injected_pending_tools", set()) | {"t-result-1"}
            viewer.page().runJavaScript(
                "var ts=document.getElementById('tool-section'); ts.style.display='';"
                "var tc=document.getElementById('tool-content');"
                "tc.insertAdjacentHTML('beforeend',"
                "'<div class=\"tool-streaming-block\" data-tool-call-id=\"t-result-1\" data-streaming=\"true\" data-order=\"99\">'"
                "+'<div class=\"tool-block-header\">⏳ bash · ls -la 运行中…</div></div>');"
                "if (window._toolCompactMode && typeof reorganizeContent==='function') reorganizeContent();"
            )

            def pump3():
                i = state.get("p3", 0)
                if i < len(POST_TOOL):
                    plog(f"工具活动期 chunk {i+1}/{len(POST_TOOL)}")
                    viewer.append_chunk(POST_TOOL[i])
                    state["p3"] = i + 1
                    QTimer.singleShot(600, pump3)
                else:
                    QTimer.singleShot(600, tool_result)
            QTimer.singleShot(300, pump3)

        def tool_result():
            plog("── 阶段4: 工具结果落地（markdown 出现 <tool> 闭合段，闪烁嫌疑②）──")
            viewer.append_chunk(TOOL_MD)
            # 模拟 remove_tool_streaming：结果已落地，pending 移除
            try:
                viewer._injected_pending_tools.discard("t-result-1")
            except Exception:
                pass

            def pump4():
                i = state.get("p4", 0)
                if i < 6:
                    viewer.append_chunk(f"工具落地后继续流式第{i+1}段：观察哨兵是否周期性补渲导致闪烁。\n\n")
                    state["p4"] = i + 1
                    QTimer.singleShot(1200, pump4)
                else:
                    QTimer.singleShot(2000, report)

            QTimer.singleShot(300, pump4)

        pump1()

    state["ready"] = False

    def poll():
        if state["ready"]:
            return
        if getattr(viewer, "_is_js_ready", False):
            state["ready"] = True
            viewer.page().runJavaScript(INSTRUMENT_JS)
            QTimer.singleShot(150, run_scene)
        else:
            QTimer.singleShot(100, poll)

    QTimer.singleShot(100, poll)
    QTimer.singleShot(45000, lambda: (plog("超时"), report()))
    app.exec()


if __name__ == "__main__":
    main()
