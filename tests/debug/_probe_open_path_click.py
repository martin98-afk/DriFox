# -*- coding: utf-8 -*-
"""实机链路探针：工具卡里的文件地址点击 → console 消息 → openFileRequested。

覆盖：骨架 click 委托抢断顺序、encodeURIComponent 编码、Python 侧 unquote、
信号派发。不接宿主，所以不会真的打开任何程序。

用法：python tests/debug/_probe_open_path_click.py
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
os.environ["DRIFOX_QT_RENDERER"] = "1"

from PyQt5.QtCore import QCoreApplication, QEventLoop, Qt, QTimer  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

import orjson as json  # noqa: E402

from app.widgets.card_viewers import CodeWebViewer  # noqa: E402
from app.widgets.render_helpers import render_tool_block  # noqa: E402
from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile()


def _pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def main() -> int:
    block = render_tool_block(
        "edit",
        {"path": "C:/工作 目录/app/main.py", "description": "改点东西"},
        "ok",
        True,
        tool_call_id="tc-probe",
    )
    if "data-open-path" not in block:
        print("[FAIL] 渲染产物没有 data-open-path")
        return 1

    viewer = CodeWebViewer(light=False)
    viewer.setAttribute(Qt.WA_DontShowOnScreen, True)
    viewer.resize(900, 500)
    viewer.show()
    captured = []
    viewer.openFileRequested.connect(captured.append)

    viewer._load_skeleton()
    deadline = 200
    while not viewer._is_js_ready and deadline > 0:
        _pump(100)
        deadline -= 1
    if not viewer._is_js_ready:
        print("[FAIL] 骨架未就绪（_is_js_ready 超时）")
        return 1
    print("[ok] 骨架就绪")

    viewer.page().runJavaScript(
        "document.getElementById('content-placeholder').innerHTML = "
        + json.dumps(block).decode("utf-8")
        + ";"
    )
    _pump(300)

    viewer.page().runJavaScript(
        "(function(){var el=document.querySelector('[data-open-path]');"
        "if(!el)return 'NO_EL';el.click();return el.getAttribute('data-open-path');})();",
        lambda r: print(f"[js] 点击目标属性: {r!r}"),
    )
    _pump(600)

    if captured == ["C:/工作 目录/app/main.py"]:
        print(f"[ok] 收到 openFileRequested: {captured}")
        return 0
    print(f"[FAIL] 信号未按预期到达: {captured}")
    return 1


if __name__ == "__main__":
    code = main()
    print("RESULT:", "OK" if code == 0 else "FAIL")
    sys.exit(code)
