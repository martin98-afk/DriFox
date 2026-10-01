# -*- coding: utf-8 -*-
"""pytest 全局 fixtures"""

import os
from pathlib import Path

import pytest

from loguru import logger

# ⚠️ 必须模块级（收集期生效）：pytest 收集测试文件即触发 app 模块链式
# import → provider 加载 → codebuddy._bootstrap 启动守护线程；session
# fixture 首测试前才运行，届时线程已在 urlopen 阻塞（退出期竞态 0x8001010D）。
# codebuddy._bootstrap 读该变量跳过线程启动。
os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"

# ⚠️ 同理必须模块级（早于任何 Settings 单例构造）：把测试进程的 app_data_dir
# 隔离到仓库内 .drifox-test/（已被 /.drifox*/ gitignore 覆盖），杜绝测试写入
# 真实用户配置 .drifox/app.config（D3 实证污染过 Tools/OffBehavior 等键）。
# setdefault：外部显式指定 DRIFOX_DATA_DIR 时尊重外部值。
os.environ.setdefault(
    "DRIFOX_DATA_DIR", str(Path(__file__).resolve().parent.parent / ".drifox-test")
)


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
    from PySide6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


@pytest.fixture(scope="session", autouse=True)
def _ensure_context_tiers_loaded():
    """测试期加载 system-context 内置 tier 链（session 级，幂等）。

    上下文压缩已迁到 ContextPipeline：build_messages / UI 估算不再直接调
    compactor，而是跑 registry 里的 tier 链。单个测试进程不会走真实启动链的
    warmup_runtime_components()，故此 fixture 补一次扫描，让依赖真实 cascade
    行为的用例（如 tool 结果截断集成测试）拿到非空链。
    """
    try:
        from app.plugins.loaders.runtime_component_loader import (
            _make_budget_resolver_loader,
            _make_context_tier_loader,
        )

        _make_context_tier_loader().scan_roots()
        _make_budget_resolver_loader().scan_roots()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _no_runtime_watcher_threads():
    """禁用 rt-watcher-* 后台热重载线程（session 级，进程内不还原）。

    背景：backend._create_engines 等路径会 ensure_*_watcher() 拉起 11 个
    rt-watcher-* 守护线程，轮询插件目录并在**后台线程**调 reload_plugin/
    unload_plugin——测试进程里组件文件无真实变更时也可能因基线竞态触发
    全量重载，非主线程 import UI 模块直接 qFatal abort
    （Fatal Python error: Aborted，Thread [rt-watcher-engines]，2026-10-02
    tests/plugins 全量 39% 处稳定复现）。
    测试环境只需要 scan_roots 静态扫描，不需要热重载；掐掉 start() 保留
    ensure_* 内的 scan_now 语义。生产 main.py 不经过本 conftest，不受影响。
    """
    try:
        from app.plugins.loaders.runtime_component_loader import _RuntimeWatcher

        _orig_start = _RuntimeWatcher.start

        def _no_start(self):
            pass

        _RuntimeWatcher.start = _no_start
        yield
        _RuntimeWatcher.start = _orig_start
    except Exception:
        yield


@pytest.fixture(scope="session")
def qapp(_setup_qt_attributes):
    """PySide6 QApplication 单例（Phase F：UIModule 测试需要 Qt 事件循环）"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def log_capture():
    """捕获 loguru WARNING+ 日志记录（自 12 个安全审计/守卫类测试上收）。"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


@pytest.fixture()
def plugin_enabled():
    """把插件名临时加入 Settings.enabled_plugins（P0-1 加载过滤适配），返回恢复函数。

    build P0-1 后 load_plugin_tools 以 Settings.enabled_plugins 为准过滤插件；
    临时插件名不在白名单会被跳过。本 fixture 自动加入并在测试结束后恢复原值。
    （30f26d89 重构时误删导致 15+ 测试文件 ERROR，7c0a6b1a 后由 F1 恢复）
    """

    def _enable(plugin_name: str):
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = list(cfg.enabled_plugins.value or [])
        if plugin_name not in saved:
            cfg.enabled_plugins.value = saved + [plugin_name]

        def restore():
            cfg.enabled_plugins.value = saved

        return restore

    return _enable
