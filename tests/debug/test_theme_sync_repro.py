# -*- coding: utf-8 -*-
"""[DEBUG-thm] 复现：config_sync 下载写回 ThemeMode 后 setTheme 幂等短路

假设：config_sync._reload_settings_on_main_thread 全量写回时直接
`item.value = v`（不经 qconfig.set），把 qfluentwidgets 全局 themeMode
内存值提前改掉；150ms 后 _delayed_theme_refresh → dispatch_refresh →
setTheme(Theme.X) 命中 qconfig.set 的 `if item.value == value: return`
幂等短路：
  - qconfig._cfg._theme（qconfig.theme 内部态，图标/样式表生成依据）不更新
  - themeChanged 不发射
  - updateStyleSheet 仍执行，但用旧 theme 生成样式表 → 组件被重设旧主题样式
→ 图标/字体颜色停留旧主题，窗口背景等（项目自绘 Colors 链）正常切换。
"""

import sys

from PyQt5.QtCore import QCoreApplication, Qt

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)  # noqa: F841

from qfluentwidgets import Theme, qconfig, setTheme  # noqa: E402

from app.utils.config import Settings  # noqa: E402

cfg = Settings.get_instance()

fired_settings = []
cfg.themeChanged.connect(lambda t: fired_settings.append(str(t)))

print(f"[0] 初始 themeMode={cfg.themeMode.value!r} qconfig.theme={qconfig.theme}")

# ── 步骤 1：模拟本地深色（正常手动切到深色后的状态）──
setTheme(Theme.DARK)
print(f"[1] setTheme(DARK)  → themeChanged(Settings)={fired_settings} qconfig.theme={qconfig.theme}")

# ── 步骤 2：模拟 config_sync 写回云端 [QFluentWidgets] ThemeMode ──
fired_settings.clear()
cfg.themeMode.value = "Theme.LIGHT"
print(f"[2] .value='Theme.LIGHT' → themeChanged={fired_settings} themeMode={cfg.themeMode.value!r} qconfig.theme={qconfig.theme}")

# ── 步骤 3：模拟 150ms 后 dispatch_refresh → setTheme(LIGHT) ──
fired_settings.clear()
setTheme(Theme.LIGHT)
ok = fired_settings and qconfig.theme == Theme.LIGHT
print(f"[3] setTheme(LIGHT)  → themeChanged={fired_settings} qconfig.theme={qconfig.theme}")
print(
    "[VERDICT] "
    + (
        "BUG 复现：setTheme 幂等短路，qconfig.theme 停留旧主题（图标/QSS 用旧主题生成）"
        if not ok
        else "假设不成立"
    )
)

# ── 对照组：手动切换（themeMode 未被提前改写）──
cfg.themeMode.value = Theme.DARK
fired_settings.clear()
setTheme(Theme.LIGHT)
print(f"[4] 对照-手动切换   → themeChanged={fired_settings} qconfig.theme={qconfig.theme}")
