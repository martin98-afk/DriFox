# -*- coding: utf-8 -*-
"""复现：关闭标签页报 TypeError: native Qt signal is not callable

链路：TabManagerWindow.create_instance → add_window → _close_window_at(0)
装 sys.excepthook 抓事件循环内抛出的异常并打印完整 traceback。

跑法：.venv/Scripts/python.exe tests/debug/repro_close_tab_signal_call.py
"""

import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ["DRIFOX_DATA_DIR"] = tempfile.mkdtemp(prefix="drifox_repro_close_")

from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

apply_render_env(default_config_path())

from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

if True:
    QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(app)

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.utils.config import Settings  # noqa: E402
from app.widgets.tab_manager_window import TabManagerWindow  # noqa: E402

settings = Settings.get_instance()

# ── 异常捕获：打印完整 traceback ──────────────────────────
_seen = []

def _hook(etype, value, tb):
    stack = "".join(traceback.format_exception(etype, value, tb))
    if stack not in _seen:
        _seen.append(stack)
        print("\n=== UNHANDLED EXCEPTION ===", flush=True)
        print(stack, flush=True)

sys.excepthook = _hook


class _FakePage(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = settings

    def isActiveWindow(self):
        return True

    @property
    def workflow_name(self):
        return "repro"

    @property
    def global_variables_changed(self):
        class _FakeSignal:
            def connect(self, *args, **kwargs):
                pass

            def disconnect(self, *args, **kwargs):
                pass

        return _FakeSignal()

    def setUpdatesEnabled(self, enabled):
        pass

    def update(self):
        pass

    def show_splitter(self):
        pass

    def hide_splitter(self):
        pass


def pump(ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def main():
    page = _FakePage()
    tm = TabManagerWindow.create_instance()
    tm.resize(1400, 900)
    tm.show()
    pump(300)

    print("=== 构造窗口 ===", flush=True)
    w = OpenAIChatToolWindow(page)
    tm.add_window(w)
    pump(300)
    print(f"窗口数: {tm.window_count}", flush=True)

    print("=== 关闭标签页 ===", flush=True)
    try:
        tm._close_window_at(0)
        print("_close_window_at 同步段无异常", flush=True)
    except Exception:
        traceback.print_exc()

    pump(800)
    if not _seen:
        print("\n=== 未捕获到异常（复现失败或症状在别的路径） ===", flush=True)
    tm.close()
    pump(300)


if __name__ == "__main__":
    main()
