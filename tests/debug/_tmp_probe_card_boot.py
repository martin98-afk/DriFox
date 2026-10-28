# -*- coding: utf-8 -*-
"""临时探针：定位 MessageCard 脚本内启动的崩溃点。"""

import faulthandler
import sys

faulthandler.enable(all_threads=True)

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(app)
print("profile ok", flush=True)

from app.widgets.message_card import MessageCard  # noqa: E402

print("import ok", flush=True)

card = MessageCard(role="assistant")
print("card created", flush=True)

card.resize(700, 520)
card.show()
print("shown", flush=True)

card.start_streaming_anim()
card.ensure_rendered()
print("rendered", flush=True)


def done() -> None:
    v = card.viewer
    print("viewer", v, "js_ready", getattr(v, "_is_js_ready", None), flush=True)
    app.quit()


QTimer.singleShot(2500, done)
QTimer.singleShot(20000, app.quit)
app.exec_()
print("quit ok", flush=True)
sys.stdout.flush()
import os

os._exit(0)
