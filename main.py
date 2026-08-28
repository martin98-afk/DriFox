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
# 内存治理(#21)：禁用 pypinyin 对 PINYIN_DICT/PHRASES_DICT 的二次 .copy()，省 ~一半拼音库内存
# 必须在任何 pypinyin 相关 import 之前设置（main.py 为进程启动最早期入口）
os.environ.setdefault("PYPINYIN_NO_DICT_COPY", "1")
os.environ["PYTHONIOENCODING"] = "utf-8"

# ========== Chromium 进程治理（WebEngine 内存占用的根因）==========
# 必须在 QApplication 创建之前设置：QtWebEngine 在首次初始化时读取该环境变量，
# 之后修改无效（这也是它必须放在 main.py 最顶部的原因）。
#
# 背景：每张消息卡片正文是一个独立的 QWebEngineView，Chromium 默认进程模型下
# 会为每张卡片派生独立 renderer 进程（各约数十 MB）。长对话滚动过程中进程数
# 随卡片数单调增长 —— 这是"长时间运行内存溢出"的主要来源。
#
# --renderer-process-limit：硬性封顶 renderer 进程总数，达到上限后 Chromium
#   自动复用已有进程而非继续派生，把内存曲线从"线性增长"压成"恒定上限"。
#   注意：不使用 --process-per-site —— 它会使所有同源卡片共享同一进程，与
#   现有的「按 PID kill 离屏 renderer」回收机制冲突（kill 一个会误伤全部卡片）。
#
# 其余开关均为本地 setHtml 渲染场景下的纯开销，关闭后无功能损失。
#
# 覆盖方式：用 setdefault，外部若已设置 QTWEBENGINE_CHROMIUM_FLAGS 则以其为准
# （便于调试或快速回退，例如 QTWEBENGINE_CHROMIUM_FLAGS="" 即完全禁用本组开关）。
_CHROMIUM_FLAGS = (
    "--renderer-process-limit=6"  # renderer 进程硬上限（核心）
    # PySide6 迁移注记：Qt6 的 Chromium（108+）在 --disable-gpu 后回退 SwiftShader
    # 软件光栅化；若再加 --disable-software-rasterizer 会把唯一回退路径也禁掉，
    # 导致所有 QWebEngineView 内容空白（Qt5/Chromium 83 无此问题）。
    # 故 Qt6 下不再传 --disable-software-rasterizer。
    " --disable-gpu"  # 聊天正文无 WebGL/视频需求，省掉 GPU 进程常驻内存
    " --disable-gpu-compositing"  # 纯软件合成：绕开 DirectComposition 视觉树（窗口闪没的扰动源）
    # Windows 遮挡计算误判：Chromium 会把宿主窗口判定为"被遮挡"而 cloak 掉
    # → 主窗口整个消失后重现（setHtml/首次渲染时触发，QtWebEngine on Windows
    # 已知问题）。两个 feature 名都禁，覆盖新旧 Chromium 版本。
    " --disable-features=CalculateNativeWinOcclusion,NativeWindowOcclusionTracking"
    " --disable-backgrounding-occluded-windows"  # 配套：被误判遮挡时也不降速/挂起
    " --disable-dev-shm-usage"  # 避免容器/小 /dev/shm 环境下的渲染异常
    " --disable-extensions"
    " --disable-background-networking"  # 纯本地渲染，不需要后台网络服务
    " --disable-background-timer-throttling"  # 隐藏 tab 的计时器节流会拖慢流式渲染
    " --js-flags=--max-old-space-size=128"  # 限制单 renderer JS 堆，防单页膨胀
)
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", _CHROMIUM_FLAGS)

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
    from PySide6.QtCore import QCoreApplication, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    if _qt_pp:
        logger.info(f"[EnvCleanup] QT_PLUGIN_PATH 已清理: {_qt_pp}")

    # ========== 必须在创建 QApplication 之前设置 Qt 属性 ==========
    # 这些设置必须在任何 Qt 模块导入之前或 QApplication 创建之前完成

    # DPI 缩放设置
    # 迁移注记：Qt6 高 DPI 永远启用，AA_EnableHighDpiScaling/AA_UseHighDpiPixmaps
    # 已无效果（Qt6 中设置会触发弃用警告），仅保留 rounding policy。
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # 共享 OpenGL contexts —— Qt6 WebEngine 官方要求（tests/conftest.py 同款）。
    # 缺失时 WebEngine 首次初始化 Chromium 会重设 GL context，扰动原生窗口层级，
    # 表现为主窗口短暂 HIDE 再 SHOW（启动闪烁）。
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

    # ========== 导入可能触发 WebEngine 的模块（在 QApplication 创建之前）==========
    # 必须在 QApplication 创建之前导入所有 QWebEngine 类，
    # 否则后续模块（如 message_card.py）中延迟导入会导致：
    #   ImportError: QtWebEngineWidgets must be imported before a QCoreApplication instance is created
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
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

        # 设置日志（全量 all.log + 按子系统拆分的分文件，见 app/core/logging_setup.py）
        try:
            from app.core.logging_setup import setup_logging
            from app.utils.utils import get_app_data_dir

            setup_logging(get_app_data_dir() / "logs", mem_diag_enabled=MEM_DIAG_ENABLED)
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

        # 预导入 openai resources 子模块（chat/responses 等）
        # 必须在任何 worker 线程启动前完成：openai SDK 懒加载 + Python 3.14
        # import 锁死锁检测，多线程首次并发访问 client.chat/client.responses
        # 会抛 _ModuleLock deadlock。
        try:
            from app.utils.http_client import preload_openai_resources

            preload_openai_resources()
            logger.debug("[DeferredStartup] openai resources 子模块预导入完成")
        except Exception:
            logger.exception("[DeferredStartup] openai resources 预导入失败（非致命）")

        # [PERF] WebEngine 预热已前移到 _show_popup 中窗口显示前同步完成
        # （Qt6 迁移：Chromium 首次初始化的 native 扰动发生在显示前，避免启动闪烁），
        # 此处不再重复预热。

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
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

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
    # if not _guard.try_lock():
    #     _guard.request_show_window()
    #     _guard.cleanup()
    #     return

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

    from PySide6.QtWidgets import QWidget
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

    def _activate_window(window):
        """激活窗口：显示 + 置前 + 还原"""
        window.show()
        window.activateWindow()
        window.raise_()
        if window.isMinimized():
            window.showNormal()

    def _show_popup():
        from app.utils.config import Settings

        # 多窗口模式已暂时下线：无论配置如何，一律以 Tab 管理器模式启动。
        # 配置项 enable_tab_manager 与 ToolPopupDialog 路径暂保留，便于未来回退。
        settings = Settings.get_instance()
        if not settings.enable_tab_manager.value:
            settings.enable_tab_manager.value = True
            settings.save()
            logger.info("检测到多窗口模式配置，已强制修正为 Tab 管理器模式")

        # ── Tab 模式 ──
        from app.widgets.tab_manager_window import TabManagerWindow, _apply_window_topmost

        tm = TabManagerWindow.create_instance()
        # 首个 ChatWindow 必须在 TabManagerWindow 创建之后构造：
        # TabManagerWindow.__init__ 里 PluginHostService.ensure_started() 同步完成
        # PluginManager 扫描；若先构造本窗口，其 setup_ui 的 _load_all_ui_plugins 与
        # 首帧 singleShot(0) 重试都会早于 ensure_started 执行（pm 未就绪静默 return），
        # 此后无人再触发 UI 插件装载 → 主窗口插件内容（卡片/侧边栏/输入按钮）全部缺失。
        chat_window = OpenAIChatToolWindow(fake_page)
        tm.add_window(chat_window)
        _guard.show_requested.connect(lambda: _activate_window(tm))
        # 强制提前物化原生窗口句柄：Qt6 WebEngine 首次渲染（setHtml）会在顶层
        # HWND 下创建 Chromium 原生子窗口，若顶层句柄彼时才物化，会触发原生
        # 层级扰动 → 主窗口短暂 HIDE 再 SHOW（启动闪烁，hideEvent 已插桩实证）。
        tm.winId()
        _apply_window_topmost(tm)
        # ── 同步预热 Chromium（Qt6 迁移关键）──
        # 本机 GPU 探测失败重试（GLES3 fallback / shared context 虚拟化失败）期间，
        # 首次 setHtml 会让主窗口短暂 HIDE（插桩实证：hideEvent 栈在 setHtml 内）。
        # 把首次初始化 + setHtml 挪到窗口显示之前同步完成，抖动发生在显示前，不可见。
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView as _QWebEngineView
            from PySide6.QtCore import QEventLoop as _QEventLoop, QTimer as _QTimer

            _preheat = _QWebEngineView()
            _loop = _QEventLoop()
            _preheat.loadFinished.connect(_loop.quit)
            _preheat.setHtml("<html><body></body></html>")
            _QTimer.singleShot(2000, _loop.quit)  # 兜底：Chromium 异常时不无限等
            _loop.exec()
            app._preheat_webengine = _preheat  # 保持引用防 GC
            logger.debug("[Startup] WebEngine 同步预热完成（Chromium 已就绪）")
        except Exception:
            logger.exception("[Startup] WebEngine 同步预热失败（非致命）")
        tm.show()
        logger.info("DriFox 以 Tab 管理器模式启动")

    # 应用退出时清理
    app.aboutToQuit.connect(_guard.cleanup)
    from app.core.store.session_store import SessionStore

    app.aboutToQuit.connect(SessionStore.mark_clean_shutdown)

    # ── M2 修复：全 app 退出（托盘退出等）不走任何 MainWidget.closeEvent ──
    # _quit_application 直接 quit()，TabManagerWindow.cleanup() 从未被执行：
    # 各 tab 的完整清理链（流式中断消息 finalize 收集落库 / backend 收尾 /
    # 桌宠停止）全部跳过，仅靠 _on_app_about_to_quit 的脏会话保存兜底。
    # 此处显式驱动 cleanup()（幂等）：逐窗 close() 走完整 closeEvent 链。
    # 尽力而为语义：finalize 为 daemon 线程自同步落盘，不等 join。
    def _teardown_tab_windows():
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            _tm = TabManagerWindow.get_instance()
            if _tm is not None:
                _tm.cleanup()
        except Exception:
            logger.warning("[M2] 退出时驱动 TabManagerWindow.cleanup 失败", exc_info=True)

    app.aboutToQuit.connect(_teardown_tab_windows)

    # 调度：主窗口先创建 → 再弹窗 → 最后执行延迟启动
    QTimer.singleShot(0, _show_popup)
    QTimer.singleShot(0, _deferred_startup)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
