# -*- coding: utf-8 -*-
"""
LLM Chatter 主入口
以独立弹窗模式启动，无需 FluentWindow 框架
"""

import os
import sys
import warnings

from qfluentwidgets import setFontFamilies

warnings.filterwarnings("ignore")
os.environ["PYTHONIOENCODING"] = "utf-8"

# ========== 内存诊断开关 ==========
# 设为 False 可禁用所有 [MEM] 诊断日志和 mem_diag.log 文件
# 关闭后 Worker 内也不再执行内存快照和自适应 GC 日志
MEM_DIAG_ENABLED = False

# 同步给 Worker（Worker 读取 MEM_DIAG 环境变量）
if MEM_DIAG_ENABLED:
    os.environ["MEM_DIAG"] = "1"
else:
    os.environ.pop("MEM_DIAG", None)  # 不设任何值，Worker 默认启用

# ========== tracemalloc 深度追踪 ==========
# 当 MEM_DIAG_ENABLED=True 且需要定位具体内存分配热点时，设为 True
# 会增加运行时开销，仅在排查时开启
# 可通过命令行 MEM_TRACE=1 python main.py 临时覆盖
MEM_TRACE_ENABLED = False
if MEM_TRACE_ENABLED:
    os.environ["MEM_TRACE"] = "1"
else:
    os.environ.pop("MEM_TRACE", None)

# 添加项目根目录到 Python 路径
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)


def main():
    """启动 LLM Chatter"""

    # ========== 环境清理：避免与已安装 Drifox.app 的 Qt 冲突 ==========
    # macOS 上如果通过 Login Items 启动了已打包的 Drifox.app，其启动脚本可能
    # 将 QT_PLUGIN_PATH 设置为 App bundle 内 PyQt5 的 plugin 路径。该路径
    # 指向的 QtCore 与开发环境 .venv 中的 PyQt5 QtCore 不是同一份二进制，
    # 导致在 QApplication 创建时加载两套 Qt 框架 → 类重复注册 → 最终触发
    # CoreFoundation __CFDataValidateRange 断言失败（SIGABRT）。
    # 开发环境下 PyQt5 会自动感知自身 plugin 路径，无需外部 QT_PLUGIN_PATH。
    _qt_pp = os.environ.pop("QT_PLUGIN_PATH", "")

    from loguru import logger
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import QApplication

    if _qt_pp:
        logger.info(f"[EnvCleanup] QT_PLUGIN_PATH 已清理: {_qt_pp}")

    # ========== 必须在创建 QApplication 之前设置 Qt 属性 ==========
    # 这些设置必须在任何 Qt 模块导入之前或 QApplication 创建之前完成

    # DPI 缩放设置
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    # ========== 导入可能触发 WebEngine 的模块（在 QApplication 创建之前）==========
    # 必须在 QApplication 创建之前导入所有 QWebEngine 类，
    # 否则后续模块（如 message_card.py）中延迟导入会导致：
    #   ImportError: QtWebEngineWidgets must be imported before a QCoreApplication instance is created
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineSettings  # noqa: F401

    # 创建应用 — 尽早创建 QApplication，让 Qt 事件循环尽快就绪
    app = QApplication(sys.argv)

    # 注册 Qt 资源文件中的图标 — 在 QApp 就绪后尽快导入
    from app.utils import icons_rc  # noqa: F401
    from app.utils import icons_light_rc  # noqa: F401

    app.setStyle("Fusion")
    app.setApplicationName("Drifox")
    app.setApplicationDisplayName("Drifox")

    # ========== 延迟启动的非关键 I/O 操作 ==========
    # 以下操作不阻塞首帧渲染，放到一次性定时器中执行

    def _deferred_startup():
        """在事件循环启动后执行的非关键初始化"""
        # 迁移旧版本数据
        try:
            from app.utils.utils import migrate_app_data_if_needed

            migrate_app_data_if_needed()
        except Exception:
            logger.exception("[DeferredStartup] migrate_app_data_if_needed 失败")

        # 设置日志
        try:
            from app.utils.utils import get_app_data_dir

            log_dir = get_app_data_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            logger.add(
                log_dir / "llm_chatter.log",
                rotation="10 MB",
                level="DEBUG",
                format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            )
        except Exception:
            pass

        # 单独的内存诊断日志文件
        if MEM_DIAG_ENABLED:
            try:
                from app.utils.utils import get_app_data_dir

                log_dir = get_app_data_dir() / "logs"
                logger.add(
                    log_dir / "mem_diag.log",
                    rotation="10 MB",
                    level="DEBUG",
                    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
                    filter=lambda r: "[MEM]" in r["message"],
                )
            except Exception:
                pass

        # 同步开机自启注册表状态
        try:
            from app.utils.startup_manager import sync_auto_start_from_config

            sync_auto_start_from_config()
        except Exception:
            logger.exception("[DeferredStartup] sync_auto_start_from_config 失败")

        # 初始化共享 WebEngine Profile（轻量，不启动 Chromium 进程）
        # [PERF] 从主线程关键路径移到这里，首帧不再阻塞
        try:
            from app.core.webengine_profile import init_shared_web_profile

            init_shared_web_profile(parent=app)
        except Exception:
            logger.exception("[DeferredStartup] init_shared_web_profile 失败")

        # [PERF] 预热 WebEngine Chromium 进程：创建隐藏 QWebEngineView 并加载空白页，
        # 让 Chromium 浏览器进程/GPU 进程提前初始化。欢迎卡片创建 QWebEngineView 时
        # 可复用已就绪的进程基础设施，避免首帧后突发 200-500ms 主线程阻塞。
        try:
            from PyQt5.QtWebEngineWidgets import QWebEngineView

            _preheat_view = QWebEngineView()
            _preheat_view.setHtml("<html><body></body></html>")
            _preheat_view.hide()
            # 保持引用，防止 GC 回收导致进程退出
            app._preheat_webengine = _preheat_view
            logger.debug("[DeferredStartup] WebEngine 预热视图已创建")
        except Exception:
            logger.exception("[DeferredStartup] WebEngine 预热失败（非致命）")

        # 后台同步 models.dev 最新模型元数据（不阻塞 UI）
        def _sync_models_dev():
            try:
                from app.core.models_dev_sync import load_dynamic_models

                result = load_dynamic_models()
                dynamic_count = sum(len(v) for v in result.provider_models.values())
                logger.info(
                    f"[DeferredStartup] models.dev 同步完成: {dynamic_count} 个动态模型, "
                    f"from_cache={result.from_cache}, fetched_at={result.fetched_at}"
                )
            except Exception:
                logger.exception("[DeferredStartup] models.dev 同步失败")

        try:
            import threading

            threading.Thread(target=_sync_models_dev, daemon=True).start()
        except Exception:
            logger.exception("[DeferredStartup] 启动 models.dev 后台同步线程失败")

    # 禁用 Qt 的 qFatal 默认行为（abort），改为记录 ERROR 日志
    from loguru import logger as _logger
    from PyQt5.QtCore import QtMsgType, qInstallMessageHandler

    def _qt_message_handler(msg_type, msg_context, msg_text):
        if msg_type == QtMsgType.QtFatalMsg:
            _logger.error(f"[QtFatal] {msg_text}")
        elif msg_type == QtMsgType.QtCriticalMsg:
            _logger.error(f"[QtCritical] {msg_text}")

    qInstallMessageHandler(_qt_message_handler)

    # 全局 Python 异常钩子（兜底）
    import traceback as _traceback

    def _pyqt_exception_hook(exc_type, exc_val, exc_tb):
        _logger.error(f"[UnhandledException] {exc_type.__name__}: {exc_val}")
        _logger.error("".join(_traceback.format_exception(exc_type, exc_val, exc_tb)))

    sys.excepthook = _pyqt_exception_hook

    # sys.unraisablehook
    def _unraisable_hook(unraisable):
        msg = getattr(unraisable.exc_value, "args", (str(unraisable.exc_value),))
        err_msg = msg[0] if msg else str(unraisable.exc_value)
        _logger.error(f"[UnraisableException] {unraisable.exc_type.__name__}: {err_msg}")
        if unraisable.object:
            _logger.error(f"  Object: {unraisable.object!r}")
        _logger.error(f"  Err: {unraisable.err_msg}")

    sys.unraisablehook = _unraisable_hook

    # 禁用默认退出行为
    app.setQuitOnLastWindowClosed(False)

    # ========== 单实例检查 ==========
    from app.core.single_instance import SingleInstanceGuard

    _guard = SingleInstanceGuard("Drifox")
    if not _guard.try_lock():
        _guard.request_show_window()
        _guard.cleanup()
        return

    # 设置 qfluentwidgets 主题 — 跟随 DriFox 主题的 mode
    from qfluentwidgets import Theme, setTheme

    try:
        from app.utils.theme_manager import theme_manager

        if theme_manager.is_light_theme():
            setTheme(Theme.LIGHT)
        else:
            setTheme(Theme.DARK)
    except Exception:
        setTheme(Theme.DARK)

    # 获取全局字体配置
    from app.utils.config import Settings

    try:
        settings = Settings.get_instance()
        font_family = settings.llm_font_family.value
    except Exception:
        try:
            font_family = Settings.get_instance().canvas_font_selected.value
        except Exception:
            font_family = "Segoe UI"

    # 注意：不在这里设置 QToolTip/ToolTip 样式，
    # 由 Colors.refresh() 在主题加载后用主题色统一设置。
    # 见 design_tokens.py: Colors.refresh() step 5。
    # 创建并显示窗口
    logger.info("LLM Chatter 启动中...")

    from PyQt5.QtWidgets import QWidget
    from app.main_widget import OpenAIChatToolWindow

    class FakePage(QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = Settings.get_instance()
            setFontFamilies([self.cfg.llm_font_family.value])

        def isActiveWindow(self):
            return True

        @property
        def workflow_name(self):
            return "standalone_llm_chatter"

        @property
        def global_variables_changed(self):
            class FakeSignal:
                def connect(self, *args, **kwargs):
                    pass

            return FakeSignal()

        def setUpdatesEnabled(self, enabled):
            pass

        def update(self):
            pass

        def show_splitter(self):
            pass

        def hide_splitter(self):
            pass

    fake_page = FakePage()
    chat_window = OpenAIChatToolWindow(fake_page)

    def _activate_window(window):
        """激活窗口：显示 + 置前 + 还原"""
        window.show()
        window.activateWindow()
        window.raise_()
        if window.isMinimized():
            window.showNormal()

    def _show_popup():
        from app.tool_popup import ToolPopupDialog

        popup = ToolPopupDialog(chat_window, None)
        popup.setWindowTitle("Drifox")
        chat_window._skip_restore_history = True
        _guard.show_requested.connect(lambda: _activate_window(popup))
        popup.show()
        logger.info("LLM Chatter 启动成功")

    # 应用退出时清理
    app.aboutToQuit.connect(_guard.cleanup)
    from app.core.store.session_store import SessionStore

    app.aboutToQuit.connect(SessionStore.mark_clean_shutdown)

    # 调度：主窗口先创建 → 再弹窗 → 最后执行延迟启动
    QTimer.singleShot(0, _show_popup)
    QTimer.singleShot(0, _deferred_startup)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
