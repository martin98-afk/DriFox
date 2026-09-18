# -*- coding: utf-8 -*-
"""tests/ui 脚手架（S3b）：A 档全链 UI 测试固定设施。

S2 硬约束（全部融入，见各 fixture/docstring）：
- QtWebEngineWidgets 先于 QApplication import（本模块顶部即 import）
- 窗口一律 ``place_offscreen``（禁 showMinimized）
- 全程零 runJavaScript（渲染证据用高度回报信号 + grab 像素）
- DRIFOX_DATA_DIR 指向临时目录（不碰用户 ~/.drifox，也不碰仓库 .drifox）

运行方式：``pytest tests/ui -m ui``（默认收集已排除 tests/ui）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# ── 会话开始前完成环境隔离：必须先于任何 app 模块 import ──
_UI_DATA_DIR = Path(tempfile.mkdtemp(prefix="drifox_ui_tests_"))
os.environ["DRIFOX_DATA_DIR"] = str(_UI_DATA_DIR)

# 渲染前置链（与 main.py 同源）：QtWebEngine 首次初始化前必须换算好
# QT_OPENGL / QT_ANGLE_PLATFORM / QTWEBENGINE_CHROMIUM_FLAGS——S2 POC 与
# S3b 实测：漏掉这步 QWebEngineProfile 构造直接 AV（access violation）
from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

_RENDER_SETTINGS = apply_render_env(default_config_path())

# QtWebEngineWidgets 必须先于 QApplication 创建（S2 注意点 1）；
# 此处 import 即满足「先于 app 构建链中的 QApplication」时序
from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401,E402

pytest_plugins = ["pytestqt"]


@pytest.fixture(scope="session")
def ui_app(request):
    """session 级：完整主窗口（含插件栈），不 exec_，由 qtbot 泵事件。

    构建链与 main.py 一致：Settings → FakePage → TabManagerWindow →
    OpenAIChatToolWindow → add_window。数据目录已被顶部 DRIFOX_DATA_DIR
    隔离，网关无配置自动跳过（S2 P7 验证）。
    """
    from PyQt5.QtCore import QCoreApplication, Qt

    # AA 属性必须在 QApplication 创建前设置（main.py 同款双属性）
    if _RENDER_SETTINGS.get("use_open_gles", True):
        QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
    if _RENDER_SETTINGS.get("share_gl_contexts", True):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    assert app is not None

    from app.utils.config import Settings
    from app.core.infra.webengine_profile import init_shared_web_profile
    from qfluentwidgets import setFontFamilies, setTheme, Theme

    init_shared_web_profile(app)  # main.py 启动链同款：卡片渲染前置依赖
    settings = Settings.get_instance()
    try:
        setTheme(Theme.LIGHT if settings.theme_mode.value == "light" else Theme.DARK)
    except Exception:  # noqa: BLE001 — 主题失败不阻断
        setTheme(Theme.DARK)
    setFontFamilies([settings.llm_font_family.value])

    from PyQt5.QtWidgets import QWidget

    class _FakePage(QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = settings
            setFontFamilies([self.cfg.llm_font_family.value])

        def isActiveWindow(self):
            return True

        @property
        def workflow_name(self):
            return "ui_tests"

        @property
        def global_variables_changed(self):
            class _FakeSignal:
                def connect(self, *args, **kwargs):
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

    from app.widgets.tab_manager_window import TabManagerWindow
    from app.main_widget import OpenAIChatToolWindow
    from tools.ui_driver import place_offscreen

    page = _FakePage()
    tm = TabManagerWindow.create_instance()
    chat_window = OpenAIChatToolWindow(page)
    tm.add_window(chat_window)
    tm.show()
    place_offscreen(tm)  # A 档：真实可见但物理出屏（禁 showMinimized）

    yield {"tm": tm, "chat_window": chat_window, "app": app, "data_dir": _UI_DATA_DIR}

    # 会话收尾：走 TabManager 完整清理（保存/网关停机/桌宠停止）
    try:
        tm.cleanup()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def ui_session(ui_app, qtbot):
    """function 级：预置 20 轮（≥20 批）文本大会话并切为当前，返回主窗口。"""
    mw = ui_app["chat_window"]
    sm = mw.session_manager
    session = sm.create_new_session()
    for i in range(20):
        session.messages.append({"role": "user", "content": f"问题 {i}：请回复一段较长的说明文本。" * 3})
        session.messages.append({"role": "assistant", "content": f"回答 {i}：" + "这是用于撑起批次与滚动的填充内容。" * 20})
    mw._display_current_session()
    mw._release_inactive_session_messages()
    qtbot.wait(800)  # 等批次构建/懒渲染首屏落定
    return mw


@pytest.fixture()
def mem_sampler(qtbot):
    """内存与容器采样器：sample() 单次采样 / samples() 聚合中位数口径。"""
    import statistics

    from tools.ui_driver import observe

    class _Sampler:
        def __init__(self):
            self.points: list[dict] = []

        def sample(self, tag: str = "") -> dict:
            snap = observe.memory()
            snap["tag"] = tag
            self.points.append(snap)
            return snap

        def median_private(self, drop_first: bool = True) -> float:
            """Private Bytes 中位数（S1-r 建议⑤：≥5 采样、排除首帧）。"""
            pts = self.points[1:] if (drop_first and len(self.points) > 1) else self.points
            values = [p["main_private_mb"] for p in pts if isinstance(p.get("main_private_mb"), (int, float))]
            return statistics.median(values) if values else float("nan")

    return _Sampler()


@pytest.fixture()
def ui_shot(tmp_path):
    """widget.grab 存 tmp，返回保存路径（断言可另加像素多样性检查）。"""

    def _shot(widget, name: str) -> Path:
        pm = widget.grab()
        target = Path(tmp_path) / f"{name}.png"
        pm.save(str(target))
        return target

    return _shot
