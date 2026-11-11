# -*- coding: utf-8 -*-
"""探针：gitee 弹窗深浅切换（兜底链）vs 系统配置切换（batched 链）的 icon 反色问题

用户症状：启动后直接从 gitee 弹窗切深浅，所有 icon 反色（浅色主题配浅色 icon）；
从系统配置切换正常；系统配置切过一次后 gitee 也正常。

跑法（PowerShell）：
  $env:PROBE_SKIP_WEBENGINE="1"; uv run python tests/debug/probe_gitee_theme_toggle.py
"""

import os
import tempfile

os.environ["DRIFOX_DATA_DIR"] = tempfile.mkdtemp(prefix="drifox_gitee_theme_probe_")
# 真实桌面环境：不设 offscreen，main_widget 的 WebEngine 才能完整构建
os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
print("=== PROBE START ===", flush=True)

from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

apply_render_env(default_config_path())

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

app = QApplication.instance() or QApplication([])

SKIP_WE = os.environ.get("PROBE_SKIP_WEBENGINE") == "1"
if not SKIP_WE:
    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile(app)
    print("webengine profile ok", flush=True)

from app.utils.config import Settings  # noqa: E402
from app.utils.theme_manager import theme_manager  # noqa: E402

settings = Settings.get_instance()


def pump(ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# ── 打点：setTheme（源头模块）在启动期/切换期的调用 ───────
import qfluentwidgets.common.style_sheet as _ss  # noqa: E402

_orig_settheme = _ss.setTheme
_settheme_calls = []


def _patched_settheme(*a, **kw):
    import traceback

    stack = "".join(traceback.format_stack(limit=6)[:-1])
    short = " <- ".join(line.strip().replace("File ", "") for line in stack.splitlines() if "drifox" in line or "DriFox" in line)[:300]
    _settheme_calls.append(a)
    print(f"  >>> setTheme{a[1:] if len(a) > 1 else ''} from: {short}", flush=True)
    return _orig_settheme(*a, **kw)


_ss.setTheme = _patched_settheme
# 顶层 re-export 同步（函数内 from qfluentwidgets import setTheme 走顶层）
import qfluentwidgets  # noqa: E402

qfluentwidgets.setTheme = _patched_settheme

# ── 打点：dispatch_refresh 与各 target ───────────────────
_orig_dispatch = theme_manager.dispatch_refresh
_dispatch_targets = []


def _patched_dispatch():
    _dispatch_targets.clear()
    # 记录 targets 注册顺序
    for ref in theme_manager._refresh_targets:
        w = ref()
        _dispatch_targets.append(type(w).__name__ if w is not None else "dead")
    print(f"  >>> dispatch_refresh targets(注册顺序)={_dispatch_targets}", flush=True)
    return _orig_dispatch()


theme_manager.dispatch_refresh = _patched_dispatch


def _patch(owner, name: str, tag: str):
    orig = getattr(owner, name)

    def inner(*a, **kw):
        print(f"  >>> CALL {tag}", flush=True)
        return orig(*a, **kw)

    setattr(owner, name, inner)


from app.widgets.tab_manager_window import TabManagerWindow  # noqa: E402

_patch(TabManagerWindow, "refresh_theme", "TMW.refresh_theme")
_panel_cls = type(tm_panel := None)  # 占位，真正 patch 在 TMW 创建后

# ── 启动（打点已就位，观察启动期 setTheme） ───────────────
print("\n=== 启动 TabManagerWindow ===", flush=True)
tm = TabManagerWindow.create_instance()
tm.resize(1400, 900)
tm.show()
pump(400)

_panel_cls = type(tm._tab_panel)
if hasattr(_panel_cls, "refresh_theme"):
    _patch(_panel_cls, "refresh_theme", "tab_panel.refresh_theme")
if hasattr(_panel_cls, "refresh_style"):
    _patch(_panel_cls, "refresh_style", "tab_panel.refresh_style")
try:
    from app.widgets.custom_title_bar import CustomTitleBar

    _patch(CustomTitleBar, "refresh_style", "titleBar.refresh_style")
except Exception as e:
    print(f"titleBar patch skip: {e}", flush=True)

print("\n=== 启动后状态 ===", flush=True)


def snapshot(tag: str) -> None:
    from qfluentwidgets.common.config import isDarkTheme, qconfig

    print(
        f"[{tag}] ui_theme_style={settings.ui_theme_style.value} ui_light_mode={settings.ui_light_mode.value} "
        f"tm_is_light={theme_manager.is_light_theme()} qfluent_isDark={isDarkTheme()} qconfig.theme={qconfig.theme}",
        flush=True,
    )


snapshot("initial")
print(f"启动期 setTheme 调用次数: {len(_settheme_calls)}", flush=True)
_settheme_calls.clear()

# ── 构建 main_widget（跳过 WebEngine 时可能降级成功） ─────
print("\n=== 构建 main_widget ===", flush=True)
w = None
try:
    from PySide6.QtWidgets import QWidget

    class _FakePage(QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = settings

        def isActiveWindow(self):
            return True

        @property
        def workflow_name(self):
            return "ui_tests"

        @property
        def global_variables_changed(self):
            class _FakeSignal:
                def connect(self, *a, **k):
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

    from app.main_widget import OpenAIChatToolWindow

    _patch(OpenAIChatToolWindow, "refresh_theme", "main_widget.refresh_theme")
    w = OpenAIChatToolWindow(_FakePage())
    tm.add_window(w)
    pump(500)
    print("main_widget 构建成功", flush=True)
except Exception as e:
    print(f"main_widget 构建失败（继续，用 TMW-only 验证）: {type(e).__name__}: {e}", flush=True)

snapshot("after-boot")
_settheme_calls.clear()

# ── 场景 A：gitee 弹窗兜底切换（设置卡未构建） ────────────
print("\n=== 场景 A：gitee 弹窗切换（设置卡未构建 → 兜底链） ===", flush=True)
row = getattr(tm._tab_panel, "_gitee_account_row", None)
assert row is not None, "tab_panel 上找不到 _gitee_account_row"

from app.widgets.cards.settings.gitee_card import _GiteeMorePopup  # noqa: E402
from app.widgets.cards.global_card_controller import get_global_card_controller  # noqa: E402

ctrl = get_global_card_controller()
print(f"controller={ctrl is not None} _settings_popup={getattr(ctrl, '_settings_popup', 'NA')!r}", flush=True)

popup = _GiteeMorePopup(row)
popup.show()
pump(100)

from qfluentwidgets.common.config import isDarkTheme, qconfig  # noqa: E402

before_theme = settings.ui_theme_style.value
popup._on_theme_mode_changed("dark")
pump(800)

print(f"切换前 theme={before_theme} → 切换后 theme={settings.ui_theme_style.value}", flush=True)
print(f"qconfig.theme={qconfig.theme} isDark={isDarkTheme()} tm_is_light={theme_manager.is_light_theme()}", flush=True)
snapshot("after-A")

# ── 场景 B：切回（对照） ─────────────────────────────────
print("\n=== 场景 B：gitee 弹窗切回 ===", flush=True)
popup2 = _GiteeMorePopup(row)
popup2.show()
pump(100)
popup2._on_theme_mode_changed("light")  # 再反转
pump(800)
snapshot("after-B")

# ── 场景 C：构建设置卡后走 return 分支（= 系统配置链） ────
print("\n=== 场景 C：设置卡构建后，gitee 弹窗切换（走 valueChanged→batched 链） ===", flush=True)
if ctrl is not None:
    ctrl.ensure_settings_popup()
    pump(400)
    print(f"_settings_popup built: {ctrl._settings_popup is not None}", flush=True)
    popup3 = _GiteeMorePopup(row)
    popup3.show()
    pump(100)
    popup3._on_theme_mode_changed("dark")
    pump(1000)
    snapshot("after-C")

print("\n=== PROBE END ===", flush=True)
app.quit()
