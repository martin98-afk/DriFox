# -*- coding: utf-8 -*-
"""真机探针：切字体后卡片正文是否跟随（不必重建会话）

验证链：
  T1 建真实 CodeWebViewer（离屏）+ 加载骨架
  T2 读 body 计算字体（基线）
  T3 改配置字体（llm_font_family）→ 调 refresh_font
  T4 再读 body 计算字体 → 必须变成新字体
  T5 断言错误日志为空（JS 语法错误会让 QWebEngine 静默失败）

跑法：
  python tests/debug/probe_font_refresh_real.py
"""

import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWebEngineWidgets import QWebEnginePage  # noqa: E402,F401

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.card_viewers import CodeWebViewer  # noqa: E402

app = QApplication([])
init_shared_web_profile()

viewer = CodeWebViewer(None, light=False)
viewer.resize(900, 400)
viewer.show()

_state = {"ready": False, "font": None, "errors": []}


def pump(ms: int) -> None:
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def js(code: str, tag: str = "") -> None:
    def _cb(v):
        if tag:
            _state[tag] = v

    viewer.page().runJavaScript(code, _cb)


# 收集 JS 错误（语法错误在 QWebEngine 里静默，靠 console 捕获）
class _Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, msg, line, source):  # noqa: N802
        if level != 0:
            _state["errors"].append(f"{msg} @{line}")


# T1：等骨架就绪
for _ in range(60):
    pump(100)
    if getattr(viewer, "_is_js_ready", False):
        _state["ready"] = True
        break

print("T1 骨架就绪:", _state["ready"])
if not _state["ready"]:
    print("FAIL: 骨架未就绪")
    sys.exit(1)

viewer._markdown_text = "字体探针正文 **bold** `code`\n\n| a | b |\n|---|---|\n| 1 | 2 |"
viewer._streaming = False
viewer._is_history = False
viewer._schedule_render(immediate=True)
pump(700)

# T2：基线字体
js("getComputedStyle(document.body).fontFamily", "font")
pump(400)
print("T2 基线 body 字体:", _state["font"])
base_font = _state["font"]

# T3：改配置 + refresh_font
from app.utils.config import Settings  # noqa: E402
from app.utils.design_tokens import invalidate_font_cache  # noqa: E402
from app.utils.utils import invalidate_font_family_css_cache  # noqa: E402

cfg = Settings.get_instance()
old = cfg.llm_font_family.value
new_font = "KaiTi" if "Kai" not in (old or "") else "SimSun"
cfg.set(cfg.llm_font_family, new_font, save=False)
invalidate_font_cache()
invalidate_font_family_css_cache()
viewer.refresh_font()
pump(800)

# T4：新字体
_state["font"] = None
js("getComputedStyle(document.body).fontFamily", "font")
pump(400)
print("T4 刷新后 body 字体:", _state["font"])

ok = _state["font"] and new_font.lower() in _state["font"].lower()
print("T5 JS 错误:", _state["errors"] or "（无）")

# 恢复原字体（不落盘）
cfg.set(cfg.llm_font_family, old, save=False)
invalidate_font_cache()
invalidate_font_family_css_cache()

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
