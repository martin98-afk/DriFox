# -*- coding: utf-8 -*-
"""复现：简洁模式下工具/思考块出现后，正文被整页重建（闪烁）+ 流式打字机中断。

假设链（待验证）：
H1 工具块注入 → _tool_dom_dirty/pending → _has_active_tool_dom()=True
   → 差量快路径让位 → 流式每拍走全量渲染（_sequence_render）
   → _apply_render_result → updateContent 整页替换 #content-placeholder
   → 长消息 HTML 大、解析慢 → 正文消失再出现（闪烁）。
H3 updateTailHtml/_twReset 丢弃打字机缓冲 → 流式逐字揭示变整段蹦字。
H4 思考块未闭合 → _needs_full_render=True（6981 行）→ 同样每拍全量。

判定信号（JS probe）：
- 给正文首块打 data-probe-id（自增），hook updateContent 前后检查该 id 元素
  是否被移除重建 → 正文 DOM 身份重建 = 闪烁。
- 统计 updateContent / updateContentAppend / updateTailHtml / _twReset 调用次数。
- Python 侧 patch _perform_update/_sequence_render/_render_tail_inline 记录路径。

运行：python tests/debug/compact_flash_repro.py
"""

import sys
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import CodeWebViewer  # noqa: E402

# ── JS 插桩：hook 渲染入口 + 正文节点身份探针 ──
INSTRUMENT_JS = """
(function() {
    window._ev = [];           // 事件轨迹
    window._probeSeq = 0;
    var cp0 = document.getElementById('content-placeholder');
    if (cp0 && cp0.firstElementChild && !cp0.firstElementChild.dataset.probeId) {
        cp0.firstElementChild.dataset.probeId = String(++window._probeSeq);
    }
    window._markProbe = function() {
        var cp = document.getElementById('content-placeholder');
        if (cp && cp.firstElementChild && !cp.firstElementChild.dataset.probeId) {
            cp.firstElementChild.dataset.probeId = String(++window._probeSeq);
        }
    };
    window._checkProbe = function(tag) {
        var cp = document.getElementById('content-placeholder');
        var first = cp ? cp.firstElementChild : null;
        var id = first ? first.dataset.probeId : null;
        var kids = cp ? cp.children.length : -1;
        window._ev.push([Math.round(performance.now()), tag,
                         'probe=' + (id || 'GONE'), 'kids=' + kids]);
    };
    function hook(name) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        window[name] = function() {
            window._checkProbe('BEFORE:' + name);
            var r = orig.apply(this, arguments);
            window._markProbe();
            window._checkProbe('AFTER:' + name);
            return r;
        };
    }
    ['updateContent', 'updateContentAppend', 'updateTailHtml', 'reorganizeContent'].forEach(hook);
    if (typeof window._twReset === 'function') {
        var _twr = window._twReset;
        window._twReset = function() {
            window._ev.push([Math.round(performance.now()), 'TW_RESET', 'typewriter 缓冲被丢弃']);
            return _twr.apply(this, arguments);
        };
    }
    window._ev.push([0, 'INSTRUMENTED', 'ok']);
    window._stats = function() {
        var names = {}; var probeGone = 0; var twReset = 0;
        window._ev.forEach(function(row) {
            var tag = String(row[1] || '');
            if (tag.indexOf('AFTER:') === 0) {
                var n = tag.slice(6);
                names[n] = (names[n] || 0) + 1;
                if (n.indexOf('updateContent') === 0 && row[2] === 'probe=GONE') probeGone++;
            }
            if (tag === 'TW_RESET') twReset++;
        });
        // probeSeq = 正文首节点被打标总数；>1 说明首节点身份多次重建（整页替换直接证据）
        return JSON.stringify({names: names, probeGone: probeGone, twReset: twReset,
                               probeSeq: window._probeSeq || 0});
    };
})()
"""

# 长正文素材（简洁模式 + 消息长）
_FAT = (
    "流式输出持续进行，正文内容不断增长，用来撑起一条足够长的消息。"
    "每个段落都足够长，确保整条消息在简洁模式坞态下形成内部滚动。"
    "继续补充文字以增加内容高度，让正文容器真正溢出并出现滚动范围。"
) * 2
PARAS = [f"第{i}段：{_FAT}\n\n" for i in range(1, 15)]

TAIL_PARAS = [
    "工具运行期间继续输出的正文段落A，用于观察工具活动期的渲染路径。\n\n",
    "工具运行期间继续输出的正文段落B，观察正文是否被整页重建。\n\n",
    "工具运行期间继续输出的正文段落C，观察打字机是否还逐字揭示。\n\n",
    "工具运行期间继续输出的正文段落D。\n\n",
    "工具运行期间继续输出的正文段落E。\n\n",
]

py_log: list[str] = []


def plog(msg):
    t = time.strftime("%H:%M:%S") + f".{int(time.monotonic()*1000)%1000:03d}"
    line = f"[{t}] {msg}"
    py_log.append(line)
    print(line)


def main():
    app = QApplication(sys.argv)
    init_shared_web_profile()
    viewer = CodeWebViewer(light=True)
    viewer.contentHeightChanged.connect(lambda h: viewer.setFixedHeight(h))
    viewer.resize(620, 900)
    viewer.show()

    state = {"phase": "wait_ready", "chunk": 0}

    # ── Python 侧路径打点 ──
    _orig_perform = viewer._perform_update
    _orig_seq = viewer._sequence_render
    _orig_tail = viewer._render_tail_inline

    def perform_patched():
        plog(f"_perform_update 进入: dirty={viewer._tool_dom_dirty} "
             f"pending={len(getattr(viewer, '_injected_pending_tools', set()) or set())} "
             f"needs_full={viewer._needs_full_render} "
             f"active_dom={viewer._has_active_tool_dom()} "
             f"md_len={len(viewer._markdown_text)}")
        return _orig_perform()

    def seq_patched(md, compact):
        plog(f"  → _sequence_render 全量提交 (md={len(md)}, compact={compact})")
        return _orig_seq(md, compact)

    def tail_patched():
        plog("  → _render_tail_inline 尾部行内渲染")
        return _orig_tail()

    viewer._perform_update = perform_patched
    viewer._sequence_render = seq_patched
    viewer._render_tail_inline = tail_patched

    def on_ready():
        plog("骨架就绪，开始插桩")
        viewer.page().runJavaScript(INSTRUMENT_JS)

        def start_stream():
            plog("── 阶段1: 开启简洁模式 + 灌入长正文（差量快路径基线）──")
            viewer._streaming = True
            viewer.page().runJavaScript(
                "window._toolCompactMode=true; _setStreamingDock(true);"
                "var ts=document.getElementById('tool-section'); ts.style.display='none';"
            )

            def pump_para():
                i = state["chunk"]
                if i < len(PARAS):
                    viewer.append_chunk(PARAS[i])
                    state["chunk"] += 1
                    QTimer.singleShot(120, pump_para)
                else:
                    QTimer.singleShot(800, inject_tool)

            def inject_tool():
                plog("── 阶段2: 注入工具运行块（复现 _inject_tool_streaming_html 的 viewer 侧效果）──")
                # 与 _inject_tool_streaming_html 一致：dirty + pending + JS 注入运行框
                viewer._tool_dom_dirty = True
                viewer._tool_dom_dirty_gen = getattr(viewer, "_tool_dom_dirty_gen", 0) + 1
                viewer._injected_pending_tools = getattr(viewer, "_injected_pending_tools", set()) | {"t-run1"}
                viewer.page().runJavaScript(
                    "var ts=document.getElementById('tool-section'); ts.style.display='';"
                    "var tc=document.getElementById('tool-content');"
                    "tc.insertAdjacentHTML('beforeend',"
                    "'<div class=\"tool-streaming-block\" data-tool-call-id=\"t-run1\" data-streaming=\"true\" data-order=\"99\">'"
                    "+'<div class=\"tool-block-header\">⏳ bash · 复现工具 运行中…</div></div>');"
                    "if (window._toolCompactMode && typeof reorganizeContent==='function') reorganizeContent();"
                )
                state["tail_idx"] = 0

                def pump_tail():
                    j = state["tail_idx"]
                    if j < len(TAIL_PARAS):
                        plog(f"chunk 到达（工具活动期 {j+1}/{len(TAIL_PARAS)}）")
                        viewer.append_chunk(TAIL_PARAS[j])
                        state["tail_idx"] += 1
                        QTimer.singleShot(700, pump_tail)
                    else:
                        QTimer.singleShot(1500, report)

                QTimer.singleShot(200, pump_tail)

            pump_para()

        QTimer.singleShot(150, start_stream)

    def report():
        plog("── 采样 JS 轨迹统计 ──")

        def dump(s):
            import json as _json
            try:
                if not s:
                    plog("统计为空（插桩丢失，仅依据 Python 侧轨迹下结论）")
                    app.quit()
                    return
                data = _json.loads(s)
                names = data.get("names", {})
                probe_gone = data.get("probeGone", 0)
                tw_reset = data.get("twReset", 0)
                probe_seq = data.get("probeSeq", 0)
                print(f"  updateContent 整页替换次数: {names.get('updateContent', 0)}", flush=True)
                print(f"  updateContentAppend 差量追加次数: {names.get('updateContentAppend', 0)}", flush=True)
                print(f"  updateTailHtml 尾部替换次数: {names.get('updateTailHtml', 0)}", flush=True)
                print(f"  reorganizeContent 次数: {names.get('reorganizeContent', 0)}", flush=True)
                print(f"  打字机缓冲重置(_twReset)次数: {tw_reset}", flush=True)
                print(f"  正文首节点身份重建次数(probeSeq): {probe_seq}", flush=True)
                verdict = (
                    "复现 ✓：正文节点被 updateContent 整页反复重建（闪烁根因确认）"
                    if names.get("updateContent", 0) >= 2 and probe_seq >= 2
                    else "未复现：工具活动期未见反复整页替换，需回查假设"
                )
                plog(verdict)
            except Exception as e:
                plog(f"dump 异常: {e!r}")
            app.quit()

        viewer.page().runJavaScript("window._stats ? window._stats() : null", dump)

    # contentReady 在内部 WebPage 上，viewer 外部用 _is_js_ready 轮询
    state["ready_hooked"] = False

    def poll_ready():
        if state.get("ready_hooked"):
            return
        if getattr(viewer, "_is_js_ready", False):
            state["ready_hooked"] = True
            on_ready()
        else:
            QTimer.singleShot(100, poll_ready)

    QTimer.singleShot(100, poll_ready)
    QTimer.singleShot(30000, lambda: (plog("超时退出"), report()))

    app.exec()


if __name__ == "__main__":
    main()
