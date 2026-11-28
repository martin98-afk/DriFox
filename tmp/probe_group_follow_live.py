# -*- coding: utf-8 -*-
"""一次性探针：真实 WebEngine 下验证分组流式工具栏的跟随态。

验的是 group_dom.GROUP_JS 的修复：
  1. 内容增长 → 分组面板（.dfx-group-body[data-live]）自动贴底；
  2. 非程序 scroll（布局/重排引起，距底 >4px）不再永久关闭跟随；
  3. 跟随关闭后内容增长不打扰用户；滚回容差内恢复跟随。
"""
from __future__ import annotations

import json
import sys
import traceback

from PyQt5.QtCore import QEventLoop, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

OUT = {"ok": False, "steps": []}


def log(name, value):
    OUT["steps"].append([name, value])
    print(f"[probe] {name}: {value}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.utils.config import Settings
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()
    cfg = Settings.get_instance()
    cfg.set(cfg.ui_compact_tool_area, True, save=False)

    window = QScrollArea()
    window.setWidgetResizable(True)
    container = QWidget()
    window.setWidget(container)
    layout = QVBoxLayout(container)
    layout.addSpacing(600)
    card = MessageCard("assistant", parent=container)
    layout.addWidget(card, alignment=Qt.AlignTop)
    window.resize(860, 420)
    window.show()
    card.ensure_rendered()

    def wait_until(fn, timeout=30000, step=50):
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(step)
        timer.timeout.connect(lambda: loop.quit() if fn() else None)
        timer.start()
        QTimer.singleShot(timeout, loop.quit)
        loop.exec_()
        timer.stop()
        return bool(fn())

    def js(expr, timeout=8000):
        box = []
        loop = QEventLoop()

        def _cb(result):
            box.append(result)
            loop.quit()

        card.viewer.page().runJavaScript(expr, _cb)
        QTimer.singleShot(timeout, loop.quit)
        loop.exec_()
        return box[0] if box else None

    if not wait_until(lambda: bool(card.viewer and card.viewer._is_js_ready), timeout=40000):
        log("js_ready", False)
        return 1
    log("js_ready", True)

    log("tol", js("typeof _GROUP_FOLLOW_TOL === 'number' ? _GROUP_FOLLOW_TOL : null"))
    log("fn", js("typeof _followGroupTail"))

    panel_sel = "document.querySelector('.dfx-group-body[data-live]')"
    card.start_streaming_anim()
    card.append_reasoning(("长思考内容用于检验分组滚动窗口。" * 8 + "\n") * 30 + "第一段思考结尾。")
    if not wait_until(lambda: bool(js(f"!!{panel_sel}")), timeout=15000):
        log("panel", None)
        return 1
    log("panel", "created")

    def panel_state():
        return js(
            f"(function(){{var p={panel_sel};if(!p)return null;"
            "return [p.scrollTop,p.scrollHeight-p.clientHeight,p._followTail===true?1:0,p.clientHeight>0?1:0];})()"
        )

    wait_until(lambda: (panel_state() or [0, 1, 0, 0])[1] > 0, timeout=15000)
    log("after_first_stream", panel_state())

    # 1) 继续增长：RO 应补滚贴底
    card.append_reasoning("继续思考到最新一行。" * 120)
    ok_bottom = wait_until(
        lambda: (lambda s: s is not None and abs(s[1] - s[0]) <= 2)(panel_state()), timeout=15000
    )
    log("follow_after_growth", panel_state())
    log("follow_after_growth_ok", ok_bottom)

    # 2) 非程序 scroll：距底 10px（旧 4px 阈值会判离开底部）
    js(
        f"(function(){{var p={panel_sel};"
        "p.scrollTop=p.scrollHeight-p.clientHeight-10;"
        "p.dispatchEvent(new Event('scroll'));})()"
    )
    log("foreign_scroll_10px", panel_state())

    # 3) 再增长：跟随仍生效则应拉回底部
    card.append_reasoning("第三段继续增长。" * 120)
    ok_recover = wait_until(
        lambda: (lambda s: s is not None and abs(s[1] - s[0]) <= 2)(panel_state()), timeout=15000
    )
    log("recover_after_foreign_scroll", panel_state())
    log("recover_after_foreign_scroll_ok", ok_recover)

    # 4) 真离底 120px：应停止跟随且后续增长不打扰
    js(
        f"(function(){{var p={panel_sel};"
        "p.scrollTop=Math.max(0,p.scrollHeight-p.clientHeight-120);"
        "p.dispatchEvent(new Event('scroll'));})()"
    )
    log("user_away_120px", panel_state())
    card.append_reasoning("第四段继续增长。" * 120)
    wait_until(lambda: False, timeout=1200)  # 等一拍
    log("away_kept", panel_state())

    OUT["ok"] = bool(
        OUT["steps"]
        and dict((k, v) for k, v in OUT["steps"]).get("follow_after_growth_ok")
        and dict((k, v) for k, v in OUT["steps"]).get("recover_after_foreign_scroll_ok")
    )
    return 0 if OUT["ok"] else 2


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 3
    print("RESULT " + json.dumps(OUT, ensure_ascii=False))
    sys.exit(code)
