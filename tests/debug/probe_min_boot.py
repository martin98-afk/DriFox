# -*- coding: utf-8 -*-
"""最小启动二分：定位完整启动探针段错误的位置"""
import os
import tempfile
import sys

os.environ["DRIFOX_DATA_DIR"] = tempfile.mkdtemp(prefix="drifox_min_probe_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
print("step1: env ok", flush=True)

from app.utils.render_env import apply_render_env, default_config_path

_RS = apply_render_env(default_config_path())
print(f"step2: render_env ok {_RS}", flush=True)

from PySide6.QtCore import QCoreApplication, Qt

if _RS.get("use_open_gles", True):
    QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
if _RS.get("share_gl_contexts", True):
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
print("step3: QApplication ok", flush=True)

SKIP_WE = os.environ.get("PROBE_SKIP_WEBENGINE") == "1"
if not SKIP_WE:
    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile(app)
    print("step4: webengine profile ok", flush=True)
else:
    print("step4: SKIPPED (PROBE_SKIP_WEBENGINE=1)", flush=True)

from app.utils.config import Settings

settings = Settings.get_instance()
print(f"step5: Settings ok ui_theme_style={settings.ui_theme_style.value} ui_light_mode={settings.ui_light_mode.value}", flush=True)

from app.utils.theme_manager import theme_manager

print(f"step6: theme_manager is_light={theme_manager.is_light_theme()}", flush=True)

from qfluentwidgets.common.config import isDarkTheme, qconfig

print(f"step7: qfluent isDark={isDarkTheme()} qconfig.theme={qconfig.theme}", flush=True)

from app.widgets.tab_manager_window import TabManagerWindow

print("step8: imports ok", flush=True)

tm = TabManagerWindow.create_instance()
print("step9: TMW created", flush=True)
tm.resize(1400, 900)
tm.show()
print("step10: TMW shown", flush=True)

import time

end = time.time() + 1.0
while time.time() < end:
    app.processEvents()
    time.sleep(0.02)
print("step11: pumped", flush=True)
app.quit()
print("ALL OK", flush=True)
