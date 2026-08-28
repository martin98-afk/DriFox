# -*- coding: utf-8 -*-
"""复现：流式输出时正文容器上滚后位置无法保持，被拉到固定偏上位置。

场景（坞态 streaming-dock，#content-placeholder max-height:450px 自滚动）：
1. 流式注入长正文 → 正文容器溢出 450px
2. 模拟用户上滚（程序赋值 scrollTop，无 _progScroll 标记 = 等价用户滚动）
3. 继续流式 chunk（增量文本 + 差量/全量渲染）
4. 采样 scrollTop 轨迹：用户上滚后若被拉到固定偏上位置 → 复现 bug

运行：python tests/debug/scroll_position_repro.py
"""

import sys
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.core.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import CodeWebViewer  # noqa: E402

PROBE = """
(function() {
    var cp = document.getElementById('content-placeholder');
    return {
        cpTop: cp ? Math.round(cp.scrollTop) : -1,
        cpMax: cp ? Math.round(cp.scrollHeight - cp.clientHeight) : -1,
        cpH: cp ? cp.clientHeight : -1,
        cpProg: cp ? !!cp._progScroll : null,
        userUp: cp ? !!cp._userScrolledUp : null,
        bodyTop: Math.round(document.body.scrollTop),
        bodyMax: Math.round(document.body.scrollHeight - document.body.clientHeight),
        bodyH: document.body.scrollHeight,
        userWithin: !!window._userScrolledWithin,
        dock: document.body.classList.contains('streaming-dock'),
        inc: document.querySelectorAll('[data-incremental="true"]').length
    };
})()
"""

# 长正文素材：每段触发硬边界（\n\n 结尾）→ 差量渲染；段内句号 → 软边界
_FAT = (
    "流式输出期间内容持续增长，正文容器被限制在四百五十像素高度内自行滚动。"
    "每个段落都足够长以便触发滚动溢出，需要多行文本才能超过容器的最大高度限制。"
    "这里不断补充文字以撑开容器，让滚动条真正出现，从而观察滚动位置的保持情况。"
    "再补充一些文字确保高度足够，多行内容累积之后才能形成有效的滚动范围。"
) * 3
PARAS = [f"第{i}段：{_FAT}\n\n" for i in range(1, 13)]

samples: list[tuple[str, dict]] = []


def probe(viewer, label, cb=None):
    viewer.page().runJavaScript(PROBE, lambda r: (samples.append((label, r)), cb and cb(r)))


def main():
    app = QApplication(sys.argv)
    init_shared_web_profile()
    viewer = CodeWebViewer(light=True)
    # 模拟 MessageCard：高度上报 → setFixedHeight（真实链路）
    viewer.contentHeightChanged.connect(lambda h: viewer.setFixedHeight(h))
    viewer.resize(620, 900)
    viewer.show()

    state = {"phase": "wait_ready", "para_idx": 0, "scroll_target": None, "rounds": 0}

    def on_ready():
        state["phase"] = "dock"
        # 简洁模式 + 坞态（真实流式开始时 MessageCard 会注入）
        viewer.page().runJavaScript(
            "window._toolCompactMode=true; _setStreamingDock(true);"
            "var tc=document.getElementById('tool-content');"
            "var ts=document.getElementById('tool-section'); ts.style.display='';"
            # 真实工具块 DOM：已完成折叠 tool-block + 流式中 tool-streaming-block
            # （对齐 _inject_tool_streaming_html / append_tool_result 注入产物）
            "tc.innerHTML='"
            '<div class="tool-block" data-tool-call-id="t-done1" data-streaming="false" data-order="0" data-expanded="false">'
            '<details class="cm-collapsible"><summary class="cm-collapsible__summary">✔ bash · ls -la</summary>'
            '<div class="cm-collapsible__body" style="height:0;opacity:0"><pre>total 64\\ndrwxr-xr-x  12 user  staff  384</pre></div>'
            '</details></div>'
            '<div class="tool-streaming-block" data-tool-call-id="t-run1" data-streaming="true" data-order="1">'
            '<div class="tool-block-header">⏳ write · main.py 运行中…</div>'
            "</div>';"
            "_scrollToolContentToBottom();"
        )
        # ── 插桩：monkey-patch 关键函数 + scroll 事件 + 高频采样 ──
        viewer.page().runJavaScript("""
        window._log = [];
        window._lastSamp = null;
        window.onerror = function(msg, src, line, col) {
            window._log.push([Math.round(performance.now()), 'ERROR', msg + ' @' + line + ':' + col]);
        };
        ['updateContent','updateContentAppend','updateTailHtml','reorganizeContent',
         '_autoScrollStreamingBody','_setStreamingDock','_scrollToolContentToBottom'].forEach(function(name){
            var orig = window[name];
            if (typeof orig === 'function') {
                window[name] = function() {
                    var cp = document.getElementById('content-placeholder');
                    window._log.push([Math.round(performance.now()), 'CALL:' + name + '(' + arguments.length + 'args)',
                                      cp ? Math.round(cp.scrollTop) : -1, cp ? !!cp._userScrolledUp : null]);
                    return orig.apply(this, arguments);
                };
            }
        });
        document.getElementById('content-placeholder').addEventListener('scroll', function() {
            window._log.push([Math.round(performance.now()), 'SCROLL-EVT', Math.round(this.scrollTop),
                              'userUp=' + !!this._userScrolledUp, 'prog=' + !!this._progScroll,
                              'sup=' + !!window._suppressScrollEvent]);
            if (window._suppressScrollEvent) return;
            if (this._progScroll) { this._progScroll = false; return; }
            var _atB = Math.abs(this.scrollHeight - this.scrollTop - this.clientHeight) < 30;
            var _prev = !!this._userScrolledUp;
            this._userScrolledUp = !_atB;
            window._log.push([Math.round(performance.now()), 'SCROLL-JUDGE',
                              'atBottom=' + _atB, 'userUp ' + _prev + '→' + !!this._userScrolledUp]);
        });
        // ── 拦截 scrollTop 写入（定位谁在动滚动条）──
        var _cpEl0 = document.getElementById('content-placeholder');
        var _desc = null, _proto = Object.getPrototypeOf(_cpEl0);
        while (_proto && !_desc) {
            _desc = Object.getOwnPropertyDescriptor(_proto, 'scrollTop');
            if (!_desc) _proto = Object.getPrototypeOf(_proto);
        }
        if (_desc && _desc.set && _desc.get) {
            Object.defineProperty(_cpEl0, 'scrollTop', {
                get: function() { return _desc.get.call(this); },
                set: function(v) {
                    window._log.push([Math.round(performance.now()), 'SET scrollTop', Math.round(v),
                                      'from=' + Math.round(_desc.get.call(this)), 'userUp=' + !!this._userScrolledUp,
                                      'stack=' + (new Error().stack || '').split('\\n')[2].trim().slice(0, 90)]);
                    _desc.set.call(this, v);
                },
                configurable: true
            });
        }
        setInterval(function() {
            var cp = document.getElementById('content-placeholder');
            if (cp && window._lastSamp !== cp.scrollTop) {
                window._log.push([Math.round(performance.now()), 'SAMPLE', Math.round(cp.scrollTop)]);
                window._lastSamp = cp.scrollTop;
            }
        }, 16);
        """)
        QTimer.singleShot(300, start_stream)

    def start_stream():
        state["phase"] = "stream"
        step_stream()

    def step_stream():
        if state["para_idx"] < len(PARAS):
            viewer.append_chunk(PARAS[state["para_idx"]])
            state["para_idx"] += 1
            probe(viewer, f"chunk{state['para_idx']}")
            QTimer.singleShot(280, step_stream)
        else:
            if state["scroll_target"] is None:
                # 全部正文注入完毕 → 模拟用户上滚
                QTimer.singleShot(600, user_scroll_up)
            else:
                state["rounds"] += 1
                if state["rounds"] >= 4:
                    QTimer.singleShot(400, finish)
                    return
                # 用户上滚后继续流式（正文更新）；奇数轮强制全量渲染（模拟工具块/
                # 思考块闭合触发 updateContent 全量 innerHTML 重写路径）
                if state["rounds"] % 2 == 1:
                    viewer._needs_full_render = True
                viewer.append_chunk(f"后续更新段 {state['rounds']}：正文仍在流式追加新内容，观察滚动位置是否被扰动。\n\n")
                probe(viewer, f"post-scroll-update{state['rounds']}" + ("-full" if state["rounds"] % 2 == 1 else ""))
                if state["rounds"] == 2:
                    # 上滚保持验证完成后：用户滚回底部 → 恢复自动跟随验证
                    QTimer.singleShot(400, scroll_back_to_bottom)
                    return
                QTimer.singleShot(400, step_stream)

    def scroll_back_to_bottom():
        viewer.page().runJavaScript(
            "var cp=document.getElementById('content-placeholder');"
            "cp.dispatchEvent(new WheelEvent('wheel', {deltaY: 120, bubbles: true}));"
            "cp.scrollTop=cp.scrollHeight;"
        )
        QTimer.singleShot(300, lambda: probe(viewer, "scrolled-to-bottom", lambda r: None))
        QTimer.singleShot(800, follow_up_check)

    def follow_up_check():
        # 底部后新 chunk → 应自动跟随（cpTop 追到新 max）
        viewer.append_chunk("底部跟随验证段：滚回底部后新内容应自动滚底显示。\n\n")
        QTimer.singleShot(600, lambda: probe(viewer, "after-bottom-follow", lambda r: None))
        QTimer.singleShot(1100, finish)

    def user_scroll_up():
        def _do(r):
            print(f"[user-scroll-up] before: cpTop={r['cpTop']} cpMax={r['cpMax']} userUp={r['userUp']}")
            target = max(0, r["cpTop"] - 800)
            state["scroll_target"] = target
            # 模拟真实滚轮：先派发 wheel（同步标记上滚意图），再 SET scrollTop。
            # 竞争场景：wheel 后**立即**同步执行渲染 JS（不等 scroll 事件派发），
            # 验证 userUp 已同步置位、位置不被拉底覆盖。
            viewer.page().runJavaScript(
                "var cp=document.getElementById('content-placeholder');"
                "cp.dispatchEvent(new WheelEvent('wheel', {deltaY: -120, bubbles: true}));"
                f"cp.scrollTop={target};"
                # 竞争注入：scroll 事件尚未派发时渲染路径抢先执行
                "_autoScrollStreamingBody();"
            )
            QTimer.singleShot(350, lambda: probe(viewer, "after-user-scroll", check_user_scroll))

        probe(viewer, "before-user-scroll", _do)

    def check_user_scroll(r):
        print(f"[after-user-scroll] cpTop={r['cpTop']} (target={state['scroll_target']}) userUp={r['userUp']}")
        if r["userUp"] is not True:
            print("!! 用户上滚未被识别（_userScrolledUp 未置位）——scroll 事件未派发或被吞")
        state["rounds"] = 0
        QTimer.singleShot(300, step_stream)

    def finish():
        viewer.page().runJavaScript(
            "JSON.stringify(window._log || [])",
            lambda s: (dump_log(s), app.quit()),
        )

    def dump_log(s):
        import json as _json

        print("\n===== JS 轨迹日志 =====")
        try:
            for row in _json.loads(s or "[]"):
                print(" ".join(str(x) for x in row))
        except Exception as e:
            print("log parse fail:", e, str(s)[:500])
        print("\n===== 采样轨迹 =====")
        for label, r in samples:
            if not isinstance(r, dict):
                continue
            flag = ""
            tgt = state["scroll_target"]
            if tgt is not None and label.startswith(("post-scroll", "after-user")):
                drift = r["cpTop"] - tgt
                flag = f"  ← drift={drift:+d}" + ("  ❌ 位置被扰动" if abs(drift) > 60 else "  ✓")
            if label in ("scrolled-to-bottom", "after-bottom-follow"):
                atb = abs(r["cpMax"] - r["cpTop"]) < 40
                flag = f"  ← atBottom={atb}" + ("  ✓ 跟随恢复" if atb else "  ❌ 未跟随")
            print(
                f"{label:28s} cpTop={r['cpTop']:5d} cpMax={r['cpMax']:5d} cpH={r['cpH']:4d} "
                f"userUp={str(r['userUp']):5s} bodyH={r['bodyH']:5d} vwH={viewer.height():4d} "
                f"within={str(r['userWithin']):5s}{flag}"
            )
        app.quit()

    viewer.loadFinished.connect(lambda ok: (ok and QTimer.singleShot(800, on_ready)))
    QTimer.singleShot(30000, app.quit)  # 兜底超时
    app.exec()
    print("done")


if __name__ == "__main__":
    main()
