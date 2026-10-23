"""[DEBUG-sb1] 验证 `* { scrollbar-width: thin }` 是否禁用 ::-webkit-scrollbar。

背景（PyQt5 → PySide6 渲染差异嫌疑）：
- 卡片骨架 CSS 有给 Firefox 写的 `* { scrollbar-width: thin; scrollbar-color: ... }`。
- Chromium 121+ 实现标准滚动条属性，规范规定：元素一旦设置 scrollbar-width/color，
  该元素 ::-webkit-scrollbar 系列伪元素样式全部失效，回退 UA 渲染。
- QtWebEngine 5.15（Chromium 83）不认识标准属性 → PyQt5 版不受影响。

本脚本对同一内容跑 A/B 两组 CSS，量实际滚动条宽度与 body 可用宽度：
- A 组 = 现状（带 * 规则）
- B 组 = 移除 * 规则
若 A 组滚动条宽度 != 6px 而 B 组 == 6px，假设成立。

运行：uv run python tests/debug/scrollbar_width_probe.py
"""
import json
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: I001 必须先于 QApplication 实例化导入

STYLES_COMMON = """
    html { overflow: hidden; }
    html, body { margin: 0; padding: 0; }
    body {
        padding: 6px 8px 0 14px;
        max-height: 300px;
        overflow-y: scroll;
        overflow-x: hidden;
    }
    body::-webkit-scrollbar-track { background: transparent; }
"""

WEBKIT_SB = """
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; margin: 2px 0; }
    ::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.18); border-radius: 3px; }
    ::-webkit-scrollbar-corner { background: transparent; }
"""

STD_SB = """
    /* Firefox 滚动条（写法照抄 card_viewers.py） */
    * {
        scrollbar-width: thin;
        scrollbar-color: rgba(0, 0, 0, 0.18) transparent;
    }
"""

PROBE_JS = """
(function () {
    var b = document.body;
    return JSON.stringify({
        scrollbarW: b.offsetWidth - b.clientWidth,
        bodyClientW: b.clientWidth,
        bodyOffsetW: b.offsetWidth,
        viewportW: document.documentElement.clientWidth,
        innerW: window.innerWidth,
        longLineW: document.getElementById('longline').offsetWidth
    });
})()
"""


def make_html(std_rule: bool) -> str:
    std = STD_SB if std_rule else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
{STYLES_COMMON}
{WEBKIT_SB}
{std}
</style></head>
<body>
<div id="longline" style="white-space:nowrap;display:inline-block;">ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz</div>
<div>第一行中文内容测试文字换行行为第二行第三行</div>
<div>更多内容填充确保出现滚动条</div><div>更多内容填充确保出现滚动条</div>
<div>更多内容填充确保出现滚动条</div><div>更多内容填充确保出现滚动条</div>
<div>更多内容填充确保出现滚动条</div><div>更多内容填充确保出现滚动条</div>
</body></html>"""


def main() -> int:
    app = QApplication(sys.argv)
    results = {}

    def make_view(tag: str, std_rule: bool) -> QWebEngineView:
        v = QWebEngineView()
        v.resize(600, 400)
        v.hide()
        v.setHtml(make_html(std_rule), )

        def probe() -> None:
            v.page().runJavaScript(PROBE_JS, lambda r: _collect(tag, r))

        QTimer.singleShot(2500, probe)
        return v

    def _collect(tag: str, r: str) -> None:
        try:
            results[tag] = json.loads(r) if r else {"error": "empty result"}
        except (ValueError, TypeError):
            results[tag] = {"error": f"unparseable: {r!r}"}
        if len(results) >= 2:
            _finish()

    def _finish() -> None:
        print("[DEBUG-sb1] probe results:")
        print(json.dumps(results, indent=2, ensure_ascii=False))
        a = results.get("A_with_std_rule") or {}
        b = results.get("B_webkit_only") or {}
        sa, sb = a.get("scrollbarW"), b.get("scrollbarW")
        print(f"[DEBUG-sb1] scrollbarW: A={sa} B={sb} （期望 6px；A!=6 且 B==6 → 假设成立）")
        if isinstance(sa, int) and isinstance(sb, int):
            if sa != 6 and sb == 6:
                print("[DEBUG-sb1] VERDICT: 假设成立 —— * { scrollbar-width } 禁用了 webkit 自定义滚动条")
            elif sa == 6 and sb == 6:
                print("[DEBUG-sb1] VERDICT: 假设不成立 —— 两种写法滚动条都是 6px")
            else:
                print("[DEBUG-sb1] VERDICT: 需进一步分析（两种都 != 6 或反向）")
        else:
            print("[DEBUG-sb1] VERDICT: 探针未取到数据（页面加载失败？）")
        app.exit(0)

    va = make_view("A_with_std_rule", True)
    vb = make_view("B_webkit_only", False)

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(app.exit)  # 15s 兜底
    guard.start(15000)
    app.exec()
    va.deleteLater()
    vb.deleteLater()
    return 0


if __name__ == "__main__":
    sys.exit(main())
