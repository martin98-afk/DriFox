# -*- coding: utf-8 -*-
"""复现：标签页隐藏期间卡内（WebEngine body）跟随态是否被误置 / 切回后是否贴底。

贴近真实结构：CodeWebViewer 作为子控件放进 QStackedWidget 的一页（模拟
TabManagerWindow._content_area 的页），用 setCurrentIndex 切换来 hide/show。

场景：
1. viewer 所在页为当前页，注入长内容（body 溢出 → 可滚）
2. setCurrentIndex(1) 切走（等价切标签页）
3. 隐藏期间继续注入内容（等价后台流式/补渲）
4. 采样 window._userScrolledWithin / body 几何
5. setCurrentIndex(0) 切回，再采样，判断卡内是否贴底

运行：python tests/debug/probe_tab_hidden_calibration.py [--raw]
"""
import sys

from PyQt5.QtCore import QEventLoop, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QLabel, QStackedWidget, QVBoxLayout, QWidget

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
QApplication.setAttribute(Qt.AA_UseOpenGLES, True)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import CodeWebViewer  # noqa: E402

PROBE = """
(function() {
    var b = document.body;
    var cp = document.getElementById('content-placeholder');
    if (!b) return {err: 'no body'};
    return {
        userWithin: window._userScrolledWithin === true,
        bodyTop: Math.round(b.scrollTop),
        bodyMax: Math.round(b.scrollHeight - b.clientHeight),
        bodyH: b.scrollHeight,
        bodyClient: b.clientHeight,
        cpUp: cp ? cp._userScrolledUp === true : null,
        cpTop: cp ? Math.round(cp.scrollTop) : -1,
        cpMax: cp ? Math.round(cp.scrollHeight - cp.clientHeight) : -1,
        dock: b.classList.contains('streaming-dock')
    };
})()
"""

_FAT = (
    "这是一段用于撑开卡片内部滚动区的填充文本，需要足够长才能让 body 出现滚动条。"
    "内容持续增长时卡内视口应当保持在底部，除非用户主动上滚离开。"
) * 4

samples: list = []


def drain(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def probe(viewer, label: str) -> None:
    done = {"ok": False}
    box = {}

    def _cb(res):
        box["r"] = res
        done["ok"] = True

    viewer.page().runJavaScript(PROBE, _cb)
    deadline = 80
    while not done["ok"] and deadline > 0:
        drain(50)
        deadline -= 1
    res = box.get("r")
    samples.append((label, res))
    if not isinstance(res, dict):
        print(f"  {label}: <无返回值 {res!r}>")
        return
    gap = res["bodyMax"] - res["bodyTop"]
    print(
        f"  {label}: userWithin={res['userWithin']} bodyTop={res['bodyTop']} "
        f"max={res['bodyMax']} gap={gap} h={res['bodyH']} clientH={res['bodyClient']} "
        f"cpUp={res['cpUp']} cpTop={res['cpTop']} cpMax={res['cpMax']} dock={res['dock']}"
    )


def main() -> None:
    app = QApplication(sys.argv)
    init_shared_web_profile()

    stack = QStackedWidget()
    page0 = QWidget()
    page1 = QWidget()
    lay0 = QVBoxLayout(page0)
    lay0.setContentsMargins(0, 0, 0, 0)

    viewer = CodeWebViewer(light=False)
    lay0.addWidget(viewer)
    QVBoxLayout(page1).addWidget(QLabel("other page"))
    stack.addWidget(page0)
    stack.addWidget(page1)
    stack.resize(820, 640)
    stack.show()
    drain(500)

    for _ in range(80):
        if getattr(viewer, "_is_js_ready", False):
            break
        drain(100)
    print(f"JS 就绪: {viewer._is_js_ready}  viewer.isVisible={viewer.isVisible()}")

    # 1) 注入长内容（模拟正常流式）
    viewer._markdown_text = _FAT
    viewer._streaming = True
    viewer._schedule_render(immediate=True)
    drain(1000)
    probe(viewer, "1-初始注入")

    viewer._markdown_text = _FAT + "\n\n" + _FAT
    viewer._schedule_render(immediate=True)
    drain(1000)
    probe(viewer, "2-补一段")

    # 2) 切走（等价切标签页）
    stack.setCurrentIndex(1)
    drain(500)
    probe(viewer, "3-切走后")

    # 3) 隐藏期间继续增长（等价后台流式/补渲）
    viewer._markdown_text += "\n\n" + _FAT
    viewer._schedule_render(immediate=True)
    drain(1400)
    probe(viewer, "4-隐藏中增长")

    viewer._markdown_text += "\n\n" + _FAT
    viewer._schedule_render(immediate=True)
    drain(1400)
    probe(viewer, "5-隐藏中再增长")

    # 4) 切回
    stack.setCurrentIndex(0)
    drain(1200)
    probe(viewer, "6-切回后")
    drain(1200)
    probe(viewer, "7-切回后再等")

    print()
    print("─── 判定 ───")
    mis = [lb for lb, r in samples if isinstance(r, dict) and lb.startswith(("4", "5")) and r["userWithin"]]
    if mis:
        print(f"  ✅ 误置复现：隐藏期间 userWithin=true（用户从未滚动）→ {mis}")
    else:
        print("  ❌ 未复现跟随态误置")

    last = samples[-1][1] if samples and isinstance(samples[-1][1], dict) else None
    if last:
        gap = last["bodyMax"] - last["bodyTop"]
        if gap > 24:
            print(f"  ✅ 复现「切回后卡内未贴底」：距底 {gap}px")
        else:
            print(f"  ❌ 未复现「切回后卡内未贴底」：距底 {gap}px")

    viewer.cleanup()


if __name__ == "__main__":
    main()
