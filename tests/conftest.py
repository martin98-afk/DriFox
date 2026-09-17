# -*- coding: utf-8 -*-
"""pytest 全局 fixtures"""

import os

import pytest

from loguru import logger

# ⚠️ 必须模块级（收集期生效）：pytest 收集测试文件即触发 app 模块链式
# import → provider 加载 → codebuddy._bootstrap 启动守护线程；session
# fixture 首测试前才运行，届时线程已在 urlopen 阻塞（退出期竞态 0x8001010D）。
# codebuddy._bootstrap 读该变量跳过线程启动。
os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"


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
def _disable_codebuddy_refresh_loop():
    """codebuddy 守护线程 urlopen/DNS 与解释器退出竞态（0x8001010D）→ 测试环境禁用

    codebuddy._bootstrap 读 DRIFOX_NO_CODEBUDDY_REFRESH=1 时跳过线程启动；
    避免测试进程退出期被后台网络请求击中（Windows fatal exception）。
    """
    import os

    os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"


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


@pytest.fixture()
def log_capture():
    """捕获 loguru WARNING+ 日志记录（自 12 个安全审计/守卫类测试上收）。"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)
