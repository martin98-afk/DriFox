# -*- coding: utf-8 -*-
"""pytest 全局 fixtures"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _ensure_split_system_plugins_enabled():
    """system 单体拆分为 system-* 系列后的测试环境白名单补录（内存态，不落盘）。

    Provider/ModelAdapter 等启动链 warmup 在 PluginManager 未初始化时按
    Settings.enabled_plugins 白名单过滤插件；无此补录时新拆分插件会被
    整体跳过（注册表为空 → session_header 等声明能力丢失）。
    """
    try:
        from app.utils.config import Settings
        from app.plugins.managers.plugin_manager import PluginManager

        cfg = Settings.get_instance()
        enabled = [n for n in (cfg.enabled_plugins.value or []) if n != "system"]
        missing = [n for n in PluginManager._SPLIT_COMPONENT_TO_PLUGIN.values() if n not in enabled]
        if missing:
            cfg.set(cfg.enabled_plugins, enabled + missing, save=False)
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _setup_qt_attributes():
    """qfluentwidgets SingleDirectionScrollArea 需在 QApplication 创建前设置 Qt::AA_ShareOpenGLContexts"""
    from PyQt5.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


@pytest.fixture(scope="session")
def qapp(_setup_qt_attributes):
    """PyQt5 QApplication 单例（Phase F：UIModule 测试需要 Qt 事件循环）"""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
