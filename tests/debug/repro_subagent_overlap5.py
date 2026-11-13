# -*- coding: utf-8 -*-
"""复现诊断（第五轮）：窄容器验证完成折叠框 header 的 flex 收缩溢出叠印。

假设：render_helpers._head_spans 第一个 span（icon+中文名容器）写了
flex: 0 1 auto; min-width: 0 —— 容器宽度不足时该 span 被收缩到内容宽以下，
内部 nowrap 的中文名溢出（overflow visible）叠印到右侧 preview 上。
流式块与 inline 卡同位置都是 flex: 0 0 auto（不可缩），故只有完成折叠框叠印。

本轮在真骨架下用两种容器宽度（宽=1300 对照 / 窄=560 触发收缩）各渲染一次，
量测 cn_name.right 与 preview.x 判定叠印。

运行：uv run python tests/debug/repro_subagent_overlap5.py
"""

import json
import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

ARGS = {
    "tasks": [
        {
            "agent": "explore",
            "description": "独立复核 DriFox 消息卡片 HTML 区偶发纯白背景的根因链。已知证据：… + 3~5 条可核对的证据卡",
        }
    ]
}
TID = "call_repro_5"

MD = (
    "让一个只读子智能体交叉验证因果链。\n\n"
    "<tool>\n"
    "name: subagent_para\n"
    "args: " + json.dumps(ARGS) + "\n"
    "result: (已分派 1 个子任务)\n"
    "success: true\n"
    "tool_call_id: " + TID + "\n"
    "</tool>\n\n"
    "等待复核结果。\n"
)

MEASURE_JS = """
(function() {
  function r(el) {
    var b = el.getBoundingClientRect();
    return {x: Math.round(b.x), right: Math.round(b.right), w: Math.round(b.width),
            top: Math.round(b.y), bottom: Math.round(b.bottom)};
  }
  var el = document.querySelector('[data-tool-call-id="__TID__"]');
  if (!el) return {err: 'no block'};
  var nameEl = null, prevEl = null;
  var spans = el.querySelectorAll('span');
  for (var i = 0; i < spans.length; i++) {
    var t = (spans[i].textContent || '').trim();
    var cs = getComputedStyle(spans[i]);
    if (t === '分发任务' && spans[i].children.length === 0 && !nameEl && cs.whiteSpace === 'nowrap') nameEl = spans[i];
    if (t.indexOf('独立复核') === 0 && spans[i].children.length === 0 && !prevEl) prevEl = spans[i];
  }
  var btn = el.querySelector('.cm-collapsible__summary');
  var first = btn ? btn.children[0] : null;
  var out = {
    blockW: r(el).w,
    nameRect: nameEl ? r(nameEl) : null,
    prevRect: prevEl ? r(prevEl) : null,
    firstSpan: first ? {rect: r(first), overflow: getComputedStyle(first).overflow, flex: getComputedStyle(first).flex} : null
  };
  return out;
})()
""".replace("__TID__", TID)


def main() -> None:
    app = QApplication(sys.argv)

    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile()

    from app.widgets.message_card import CodeWebViewer

    viewer = CodeWebViewer(light=True)
    viewer.show()

    results = {}

    def measure(width: int, cb) -> None:
        def on_measure(res) -> None:
            results[width] = res
            if not isinstance(res, dict) or "err" in res:
                print(f"[w={width}] 量测失败: {res}")
            else:
                n, p = res["nameRect"], res["prevRect"]
                fs = res.get("firstSpan") or {}
                if n and p:
                    overlap = n["right"] - p["x"]
                    v_int = min(n["bottom"], p["bottom"]) - max(n["top"], p["top"])
                    verdict = "叠印" if overlap > 0 and v_int > 0 else "正常"
                    print(
                        f"[w={width}] 块宽={res['blockW']} 首span={fs.get('rect')} flex={fs.get('flex')} overflow={fs.get('overflow')}\n"
                        f"        名.right={n['right']} 预.x={p['x']} 预.top={p['top']} 名.top={n['top']} → {verdict}（overlap={overlap}px）"
                    )
                else:
                    print(f"[w={width}] 未定位 span：name={bool(n)} prev={bool(p)}")
            cb()

        viewer.page().runJavaScript(MEASURE_JS, 0, on_measure)

    def phase(width: int, cb) -> None:
        viewer.resize(width, 400)
        viewer._markdown_text = MD
        viewer._streaming = False
        viewer._perform_update()

        def go():
            measure(width, cb)

        QTimer.singleShot(1100, go)

    def run() -> None:
        phase(1300, lambda: QTimer.singleShot(200, lambda: phase(560, finish)))

    def finish() -> None:
        print("\n[done] 两档宽度量测完成")
        QTimer.singleShot(120, app.quit)

    QTimer.singleShot(1000, run)
    QTimer.singleShot(20000, app.quit)
    app.exec_()


if __name__ == "__main__":
    main()
