# [DEBUG-glctx] 最小复现：OpenGL context 创建失败
# 用法（在项目根目录）：
#   MODE=angle   python tests/debug/gl_context_repro.py   # 复现组：模拟 main.py 的 ANGLE 强制
#   MODE=dynamic python tests/debug/gl_context_repro.py   # 对照组：Qt 默认动态选择 GL 后端
# 信号：stderr 是否出现 "Failed to create OpenGL context"
import os
import sys

MODE = os.environ.get("MODE", "angle")
if MODE == "angle":
    os.environ.setdefault("QT_OPENGL", "angle")
else:
    os.environ.pop("QT_OPENGL", None)

# 与 main.py 保持一致的 Chromium flags（禁 GPU 时的行为差异可能是诱因）
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--renderer-process-limit=6 --disable-gpu --disable-software-rasterizer"
    " --disable-dev-shm-usage --enable-low-end-device-mode",
)

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

if MODE == "angle":
    QApplication.setAttribute(Qt.AA_UseOpenGLES)

# WebEngine 必须在 QApplication 之前导入（与 main.py 一致）
from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: E402,F401

print(f"[repro] MODE={MODE} starting QApplication", flush=True)
app = QApplication(sys.argv)
view = QWebEngineView()
view.resize(400, 300)
view.setHtml("<html><body><h1>repro</h1></body></html>")
view.show()
print("[repro] QWebEngineView shown, spinning event loop 6s", flush=True)


def _done():
    print("[repro] survived 6s, exiting cleanly (NO bug)", flush=True)
    app.quit()


QTimer.singleShot(6000, _done)
sys.exit(app.exec_())
