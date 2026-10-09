# -*- coding: utf-8 -*-
"""测量：工具框「流式态 → 完成态」三形态的实际高度差（一次性诊断脚本）。

背景（2026-10-10 用户报告）：工具运行框转完成态时卡片轻微上下变动。

三形态：
  1. streaming   tool-streaming-block[data-streaming=true]  spinner+preview（运行中）
  2. comp-preview 同结构 data-streaming=false（finish_tool_streaming 参数接收完成）
  3. comp-box    cm-collapsible.tool-block（append_tool_result 后的折叠框）

不经过 MessageCard/共享 profile（与运行中的主程序隔离），独立 off-the-record
profile + 最小页面（内联盒模型为主，页面级只注入 .cm-collapsible 相关规则与
全局字体）。分三档 preview 长度取样。

运行：python tests/debug/measure_tool_block_height_delta.py
"""

import json
import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets.card_render_core import _render_tool_streaming_block  # noqa: E402
from app.widgets.render_helpers import render_tool_block  # noqa: E402

# 与真实页面同源的少量页面级 CSS（其余盒模型均为块 HTML 内联 style）
PAGE_CSS = """
    body {
        margin: 0; padding: 16px;
        background: #1e2229; color: #e8eaf0;
    }
    .cm-collapsible {
        overflow: hidden;
        transform: translateZ(0);
        backface-visibility: hidden;
        contain: layout style;
    }
    .cm-collapsible__chevron {
        flex: 0 0 auto;
        width: 6px; height: 6px;
        border-right: 1.5px solid currentColor;
        border-bottom: 1.5px solid currentColor;
        transform: rotate(45deg);
        transform-origin: center;
        transition: transform 180ms ease;
        margin-left: 2px; opacity: 0.85;
    }
    .cm-collapsible__body {
        height: 0; opacity: 0; overflow: hidden;
    }
    .cm-collapsible__summary:focus-visible {
        box-shadow: inset 0 0 0 1px rgba(102, 198, 255, 0.28);
    }
"""


def _make_three(preview: str, tool_name: str = "search", tool_call_id: str = "tool_measure"):
    """同一工具的三形态 HTML"""
    streaming = _render_tool_streaming_block(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        preview=preview,
        char_count=len(preview),
        completed=False,
    )
    completed_preview = _render_tool_streaming_block(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        preview=preview,
        char_count=len(preview),
        completed=True,
    )
    completed_box = render_tool_block(
        tool_name=tool_name,
        tool_args={"q": preview},
        result=preview,
        success=True,
        collapsed=True,
        tool_call_id=tool_call_id,
    )
    return {
        "streaming": streaming,
        "compPreview": completed_preview,
        "compBox": completed_box,
    }


def _measure_js(htmls: dict) -> str:
    """量测 JS：逐段塞进离屏容器，量实际盒高。"""
    parts = ",\n".join(f"{k}: measure({json.dumps(v)})" for k, v in htmls.items())
    return (
        "(function(){"
        "  var host = document.getElementById('__measure_host');"
        "  if (!host) {"
        "    host = document.createElement('div');"
        "    host.id = '__measure_host';"
        "    host.style.cssText = 'position:absolute;left:-99999px;top:0;width:640px;';"
        "    document.body.appendChild(host);"
        "  }"
        "  function measure(html) {"
        "    host.innerHTML = html;"
        "    var el = host.firstElementChild;"
        "    if (!el) return {err:'no element'};"
        "    var r = el.getBoundingClientRect();"
        "    var cs = getComputedStyle(el);"
        "    var out = {h: Math.round(r.height*100)/100, pad: cs.padding};"
        "    var kids = [];"
        "    for (var i=0;i<el.children.length;i++){"
        "      var c = el.children[i];"
        "      kids.push((c.className||c.tagName).toString().slice(0,32)+':'+Math.round(c.getBoundingClientRect().height*100)/100);"
        "    }"
        "    out.kids = kids;"
        "    return out;"
        "  }"
        f"  return {{{parts}}};"
        "})()"
    )


CASES = [
    ("短 preview（一行内）", "检索 DriFox"),
    ("中 preview（约 2 行内）", "搜索关键词：DriFox 流式高度追踪与工具框完成态转换机制"),
    (
        "超长 preview（完成态折 2 行）",
        "这是一段刻意拉长的工具参数预览文本，用于验证完成态的行数钳制行为："
        "当预览内容超过可用宽度时，流式态强制 nowrap 单行省略号截断，"
        "而完成态使用 -webkit-line-clamp: 2 允许折成两行显示，"
        "两者盒高因此在长文本场景下出现方向相反的差异。",
    ),
]


def main() -> None:
    app = QApplication(sys.argv)

    from PyQt5.QtWebEngineWidgets import QWebEngineView

    view = QWebEngineView()
    view.resize(760, 560)
    view.show()

    results = []
    state = {"ready": False, "i": 0}

    def on_measure(label: str, r) -> None:
        if not isinstance(r, dict) or "streaming" not in r:
            print(f"[{label}] 量测失败：{r}")
            QTimer.singleShot(80, run_next)
            return
        s, cp, cb = r["streaming"], r["compPreview"], r["compBox"]
        if any("err" in x for x in (s, cp, cb)):
            print(f"[{label}] 元素缺失：{r}")
            QTimer.singleShot(80, run_next)
            return
        d12 = round(cp["h"] - s["h"], 2)
        d23 = round(cb["h"] - cp["h"], 2)
        results.append((label, s["h"], cp["h"], cb["h"]))
        print(
            f"[{label}]\n"
            f"    流式     offsetHeight={s['h']}px  padding={s['pad']}\n"
            f"    完成预览 offsetHeight={cp['h']}px  (流式→完成预览 Δ={d12:+.2f})\n"
            f"    完成折叠 offsetHeight={cb['h']}px  padding={cb['pad']}  (完成预览→完成折叠 Δ={d23:+.2f})\n"
            f"    流式→完成折叠 总差 Δ={round(cb['h'] - s['h'], 2):+.2f}px\n"
            f"    子块高度: {json.dumps(s.get('kids', []), ensure_ascii=False)} | "
            f"{json.dumps(cb.get('kids', []), ensure_ascii=False)}"
        )
        QTimer.singleShot(80, run_next)

    def run_next() -> None:
        if state["i"] >= len(CASES):
            finish()
            return
        label, preview = CASES[state["i"]]
        state["i"] += 1
        three = _make_three(preview)
        view.page().runJavaScript(
            _measure_js(three),
            lambda r=None, L=label: on_measure(L, r),
        )

    def finish() -> None:
        print("\n===== 汇总（px）=====")
        print(f"{'场景':<30}{'流式':>9}{'完成预览':>10}{'完成折叠':>10}")
        for label, sh, cph, cbh in results:
            print(f"{label:<30}{sh:>9.2f}{cph:>10.2f}{cbh:>10.2f}")
        QTimer.singleShot(120, app.quit)

    def start() -> None:
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{PAGE_CSS}"
            "body, button, span, div { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; }"
            "</style></head><body><div id='mount'></div></body></html>"
        )
        view.setHtml(html)
        view.loadFinished.connect(lambda _ok: (state.__setitem__("ready", True), QTimer.singleShot(200, run_next)))

    QTimer.singleShot(0, start)
    app.exec_()


if __name__ == "__main__":
    main()
