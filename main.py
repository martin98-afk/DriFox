# -*- coding: utf-8 -*-
"""
LLM Chatter 主入口
以独立弹窗模式启动，无需 FluentWindow 框架
"""

import os
import sys
import time
import warnings

# ========== 开机自启提权 helper（最早处理，不加载 Qt）==========
# 拨动自启开关时主进程通过 runas 以管理员身份把本进程再次拉起，
# 命令行携带 --configure-auto-start=on|off。helper 写完 HKLM 注册表
# 立即退出，成败通过 --startup-error-file 回传（见 app/utils/startup_manager.py）。
# 必须放在 qfluentwidgets/Qt 导入之前：helper 进程无需也不应加载 GUI 栈。
if os.name == "nt" and any(arg.startswith("--configure-auto-start=") for arg in sys.argv[1:]):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app.utils.startup_manager import maybe_handle_startup_helper

    sys.exit(maybe_handle_startup_helper(sys.argv[1:]) or 0)

from qfluentwidgets import setFontFamilies

warnings.filterwarnings("ignore")
# 内存治理(#21)：禁用 pypinyin 对 PINYIN_DICT/PHRASES_DICT 的二次 .copy()，省 ~一半拼音库内存
# 必须在任何 pypinyin 相关 import 之前设置（main.py 为进程启动最早期入口）
os.environ.setdefault("PYPINYIN_NO_DICT_COPY", "1")
os.environ["PYTHONIOENCODING"] = "utf-8"

# ========== 渲染配置 → 环境变量（QtWebEngine 首次初始化前一次性生效）==========
# 原 main.py 硬编码的 QT_OPENGL / QT_ANGLE_PLATFORM / QTWEBENGINE_CHROMIUM_FLAGS
# 已配置化：设置界面「渲染与性能」→ app.config [Render] 组，重启生效。
# app/utils/render_env.py 在 Qt 加载前裸 JSON 读取该组并换算环境变量，档位语义、
# 旧检测链（DRIFOX_SOFTWARE_RENDER / DRIFOX_ENABLE_WEBGL → ~/.drifox 标记文件）、
# 外部环境变量优先（setdefault）与平台限定（macOS 强设 d3d11 黑屏）见其模块注释。
# 返回值里还有两个「Qt 属性类」设置（AA_UseOpenGLES / AA_ShareOpenGLContexts）：
# 它们不是环境变量，只能在 QApplication 创建前 setAttribute，故由 main() 取用。
from app.utils.render_env import apply_render_env, default_config_path

RENDER_SETTINGS = apply_render_env(default_config_path())

# 防御：QSG_RHI* 残留会让场景图走 RHI D3D11 合成，WebEngine 的 GL 纹理接不上 → 整块黑。
for _env_key in ("QSG_RHI", "QSG_RHI_BACKEND"):
    os.environ.pop(_env_key, None)


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

# ========== 崩溃捕获（VEH → minidump + C 栈，仅 Windows）==========
# Intel 核显驱动崩溃发生在 native 层，Python traceback 看不到调用链；
# 此捕获器在致命异常时自动落 logs/crash/*.dmp + 模块!RVA 级 C 栈日志。
# 回退：DRIFOX_NO_VEH=1 跳过安装。确认驱动修复稳定后可移除本段。
if os.name == "nt":
    try:
        from app.utils.veh_minidump import install as _install_veh

        _install_veh()
    except Exception:
        pass  # 捕获器失败绝不阻塞启动


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

    # 启动分段打点：壳前各段（巨型 import / 主窗口构造）历史上有累计 ~3s
    # 的无日志空窗，逐段 DEBUG 打点便于定位后续优化目标（行为零变更）
    import time as _sm_time

    _sm_seg = _sm_time.perf_counter()

    def _smark(label: str, level: str = "debug") -> None:
        """启动分段打点。

        [T29 C1] level 参数：关键分界点（壳可见 / import 完成 / 首窗就绪）用
        "info" 提升可见性——普通用户的默认日志级别即可看到启动耗时分布，
        无需开 DEBUG。
        """
        nonlocal _sm_seg
        _now = _sm_time.perf_counter()
        msg = f"[StartupMark] {label} 耗时 {(_now - _sm_seg) * 1000:.0f}ms"
        if level == "info":
            logger.info(msg)
        else:
            logger.debug(msg)
        _sm_seg = _now

    if _qt_pp: 
        logger.info(f"[EnvCleanup] QT_PLUGIN_PATH 已清理: {_qt_pp}")

    # ========== 必须在创建 QApplication 之前设置 Qt 属性 ==========
    # 这些设置必须在任何 Qt 模块导入之前或 QApplication 创建之前完成

    # DPI 缩放设置
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    # OpenGL 走 ANGLE(D3D11)：绕开 Intel OpenGL ICD 缺陷路径（见文件顶部说明）。
    # 必须在 QApplication 与 WebEngine 导入之前设置。
    # 由 RenderBackend 推导（见 render_env.compute_settings）：hardware / software
    # 走 ANGLE 才需要；software_gl 是 Mesa llvmpipe 桌面 GL 兜底档，强制 ES 反而
    # 与「最慢最稳」的初衷冲突 —— 故该档位下不设这个属性。
    if RENDER_SETTINGS.get("use_open_gles", True):
        QApplication.setAttribute(Qt.AA_UseOpenGLES)
    # [MEM] 共享 GL 上下文：默认每个 QWebEngineView 会创建自己的 OpenGL 上下文，
    # 并发对话下 40+ 张消息卡 = 40+ 个独立上下文，每个都要独立的命令缓冲与合成
    # 表面后备存储。开启后所有 view 复用同一上下文，per-view 常驻开销下降
    # （实测 12 个 view 总增量 250MB → 218MB，约 -12.7%）。
    # 必须在 QApplication 创建之前设置（benchmarks/README.md 同样要求此项）。
    # [Render] ShareGLContexts 可关：多卡共用上下文被怀疑与卡片/图表闪烁相关，
    # 出问题时关掉即可验证是否由它引起。
    if RENDER_SETTINGS.get("share_gl_contexts", True):
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

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

    # [T24 R4] 日志 + 原生崩溃捕获前置（壳显示后立即执行；幂等）。
    # 背景：setup_logging / faulthandler 原在 _deferred_startup 中（事件循环后），
    # 而 T7 把 main_widget 级联 import 移进了 _show_popup —— 壳已显示但 import
    # 期间（~3s）若崩溃，既无日志也无 dump，构成取证盲窗。现在提前到 tm.show()
    # 之后立即执行；_deferred_startup 中保留同函数调用（幂等，覆盖其它路径）。
    _early_forensics_state = {"done": False}

    def _setup_early_forensics():
        """启用日志与 faulthandler（幂等）。

        顺序：数据迁移 → 日志 → 崩溃捕获。迁移必须先于日志——打包版迁移会
        rmtree 目标目录后重建，若日志先开，句柄会指向被删除的文件。
        """
        if _early_forensics_state["done"]:
            return
        _early_forensics_state["done"] = True
        try:
            from app.utils.utils import migrate_app_data_if_needed

            migrate_app_data_if_needed()
        except Exception:
            logger.exception("[EarlyForensics] migrate_app_data_if_needed 失败")
        try:
            from app.core.logging_setup import setup_logging
            from app.utils.utils import get_app_data_dir

            setup_logging(get_app_data_dir() / "logs", mem_diag_enabled=MEM_DIAG_ENABLED)
        except Exception:
            pass
        try:
            from app.core.crash_handler import install_crash_handler
            from app.utils.utils import get_app_data_dir

            install_crash_handler(get_app_data_dir() / "logs")
        except Exception:
            pass

    def _deferred_startup():
        """在事件循环启动后执行的非关键初始化"""
        # 分段计时：本函数整体在主线程串行执行，任一步骤拖慢都会顺延后续步骤
        # （历史上 openai resources 预导入独占 ~4s 无从察觉），逐段打点便于定位
        import time as _time

        _seg_t = _time.perf_counter()

        def _mark(label: str) -> None:
            nonlocal _seg_t
            _now = _time.perf_counter()
            logger.debug(f"[DeferredStartup] {label} 耗时 {(_now - _seg_t) * 1000:.0f}ms")
            _seg_t = _now

        _mark("enter")

        # [T24 R4] 迁移/日志/崩溃捕获已提前到壳显示后（_setup_early_forensics，
        # 见 _show_popup 内调用）。此处保留兜底调用：非 _show_popup 启动路径
        # （如测试直接调 _deferred_startup）仍需保证日志与 faulthandler 就绪；
        # 幂等 → 已执行过则为空操作。
        _setup_early_forensics()
        _mark("early_forensics(fallback)")

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
        _mark("init_shared_web_profile")

        # 启动后台 RSS 采样器：把 psutil 进程表遍历从主线程搬走。
        # 采样结果供 B4 强回收阈值判定使用（原为每 content chunk 同步采样，
        # 单次 20-80ms，是流式卡顿主因之一）。
        try:
            from app.core.rss_sampler import rss_sampler

            rss_sampler.ensure_started()
        except Exception:
            logger.exception("[DeferredStartup] rss_sampler 启动失败（降级为同步采样）")

        # 预热纯 Qt 块级渲染器（仅灰度开启时）。
        # [PERF] markdown_block_viewer 已从启动关键路径移除（延迟导入），
        # 开启灰度的实例在这里补热，避免首张 assistant 卡片渲染时抖动 ~340ms。
        try:
            from app.utils.config import Settings

            if Settings.get_instance().qt_message_renderer.value:
                from app.widgets.message_card import prewarm_markdown_block_viewer

                prewarm_markdown_block_viewer()
        except Exception:
            logger.exception("[DeferredStartup] prewarm_markdown_block_viewer 失败")

        # 预导入 openai resources 子模块（chat/responses 等）
        # 必须在任何 worker 线程启动前完成：openai SDK 懒加载 + Python 3.14
        # import 锁死锁检测，多线程首次并发访问 client.chat/client.responses
        # 会抛 _ModuleLock deadlock。
        try:
            # [PERF] 冷导入实测 4-5s（`import openai` 3.5s + resources 1.9s），
            # 改后台线程顺序导入：死锁只在多线程并发导入不同模块时出现，
            # 单线程串行走完不会触发，主线程不再冻结这段
            from app.utils.http_client import preload_openai_resources_async

            preload_openai_resources_async()
            logger.debug("[DeferredStartup] openai resources 子模块预导入已转后台线程")
        except Exception:
            logger.exception("[DeferredStartup] openai resources 预导入失败（非致命）")
        _mark("openai_preload_async")

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

            # [MEM] 预热完成后销毁视图本身：Chromium 基础设施（browser process /
            # profile）此时已初始化完毕且随 app 级 profile 常驻，空白 renderer
            # 约占 15-30MB 无保留价值。延时 5s：覆盖启动窗口，之后首张真实卡片
            # 的 renderer 已就位，销毁无副作用。
            def _release_preheat():
                try:
                    _preheat_view.deleteLater()
                    app._preheat_webengine = None
                except Exception:
                    pass

            QTimer.singleShot(5000, _release_preheat)
            logger.debug("[DeferredStartup] WebEngine 预热视图已创建（5s 后释放）")
        except Exception:
            logger.exception("[DeferredStartup] WebEngine 预热失败（非致命）")
        _mark("webengine_preheat")

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

        # 崩溃取证自检（仅调试）：DRIFOX_CRASH_TEST=1 时在 faulthandler 安装后
        # 触发真实 SIGSEGV，验证 crash log/WER 链路。正常运行永不设置此变量。
        # ⚠️ 必须在主线程触发：Windows CRT 的 signal handler 只在主线程路由
        # 硬件异常，子线程触发时 faulthandler 不落盘（实测）。
        if os.environ.get("DRIFOX_CRASH_TEST") == "1":
            logger.warning("[CrashHandler] DRIFOX_CRASH_TEST=1，3 秒后触发测试性段错误")
            import faulthandler as _fh

            _fh._sigsegv()

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
    from app.utils.config import Settings

    # [T24 R2] 二次启动 show 请求的接入口必须在拿到单实例锁后**立即**注册。
    # 此前注册在 _show_popup 末尾（首窗 add_window 之后）：T7 把 main_widget
    # 级联 import（~3s）后移到该点之前，窗口期内的二次启动请求会因
    # show_requested 无接收者而静默丢弃（用户感知：双击图标无反应）。
    # 现在改为：锁即注册；窗口未就绪时先记 pending，首窗就绪后补激活。
    _show_window_state: dict = {"window": None, "wanted": False}

    def _activate_window(window):
        """激活窗口：显示 + 置前 + 还原"""
        window.show()
        window.activateWindow()
        window.raise_()
        if window.isMinimized():
            window.showNormal()

    def _on_show_requested():
        """二次启动 show 请求：窗口就绪则激活，未就绪先记 pending（T24 R2）。"""
        win = _show_window_state["window"]
        if win is None:
            _show_window_state["wanted"] = True
            return
        _activate_window(win)

    _guard = SingleInstanceGuard("Drifox")
    # 开关关闭时不取锁，允许多实例并行（改动重启生效）
    if Settings.get_instance().enable_single_instance.value and not _guard.try_lock():
        _guard.request_show_window()
        _guard.cleanup()
        return

    # [T24 R2] 锁就绪 → 立即接上 show 请求入口，覆盖后续 import 窗口期
    _guard.show_requested.connect(_on_show_requested)

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

    _smark("pre_import（单实例/主题/字体）")

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
    _smark("fake_page")

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

        _smark("import_tab_manager")
        tm = TabManagerWindow.create_instance()
        _smark("tab_manager_create")
        # [体验] 置顶 hint 必须在首次 show 之前应用：setWindowFlags 在窗口已可见时
        # 会销毁并重建 native 窗口，表现为「窗口出现后闪一下（消失又出现）」。
        # 未 show 时改 flags 不触发重建，后续 tm.show() 一次性显示。
        _apply_window_topmost(tm)
        # 批5 壳先行：先显示壳窗口（空态占位「正在准备会话…」）→ 应用级服务启动
        # → 进程级预热（SessionStore/StorageRegistry/内置工具链）→ 首窗构造 → add_window。
        # [PERF 2026-09-14] GatewayService/PluginHostService.ensure_started() 从
        # TabManagerWindow.__init__ 挪到此处：插件发现 + tools/agents/hooks 注册
        # 实测 ~1.1s，不再挡在壳窗口出现之前。时序约束不变：首个 ChatWindow 仍
        # 必须在 ensure_started 之后构造——若先构造首窗，其 _load_all_ui_plugins
        # 会因 pm 未就绪静默 return 且无人重试，主窗口插件内容（卡片/侧边栏/
        # 输入按钮）全部缺失。
        tm.show()
        tm.show_boot_placeholder()
        # [T24 R6] 同步重绘一次，让壳在 import 阻塞前真正画出来。
        # show() 是异步的（实际绘制等事件循环的 expose），而紧随其后的
        # main_widget 级联 import 会阻塞主线程 ~3s，期间事件循环不跑 →
        # 窗口始终未绘制（用户看到白屏/空窗而非「正在准备会话…」）。
        # repaint() 强制同步绘制当前帧且**不处理事件队列**：
        # - 方案 a（processEvents）实测会吸入 _deferred_startup 的 singleShot(0)，
        #   使其在 _show_popup 中途重入执行（打断启动时序约束），故不采用；
        # - 方案 b（占位文本前置）实测无效：占位在 show 前后设置都一样，
        #   回调内 paintEvent 根本不被调用（paint 必须等事件循环）。
        # 兜底：repaint 异常不影响启动主流程。
        try:
            tm.repaint()
        except Exception:
            pass
        # [T24 R4] 壳已可见 → 立即启用日志与崩溃捕获，缩小 import 期的取证盲窗
        _setup_early_forensics()
        _smark("early_forensics")
        # [T29 C1] 关键分界点：壳可见 + 日志/崩溃捕获已就绪（import 盲窗已消除）。
        # info 级别：普通用户日志即可看到「壳可见 → 首窗就绪」的耗时分布。
        _smark("shell_visible_after_logging", level="info")
        from app.core.gateway_service import GatewayService
        from app.core.plugin_host_service import PluginHostService

        _smark("shell_show", level="info")
        GatewayService.get_instance().ensure_started()
        PluginHostService.get_instance().ensure_started()
        _smark("app_services_start")
        from app.utils.preheat import preheat_process_level

        preheat_process_level()
        _smark("preheat_process")
        # [PERF T7] 巨型 import 后移：main_widget 级联 import（message_card/cards/
        # tab_manager_window/qfluentwidgets，历史实测 ~3s）原在顶层，挡住启动壳；
        # 现移到壳显示 + 应用级服务启动 + 进程级预热之后、首窗构造前。
        # 时序安全：WebEngine 已在启动早期预导入；首窗构造本就要求在
        # ensure_started/preheat 之后（见上方时序约束注释），import 与首窗
        # 构造同属此节点，无新增顺序依赖。
        from app.main_widget import OpenAIChatToolWindow

        _smark("import_main_widget", level="info")
        chat_window = OpenAIChatToolWindow(fake_page)
        _smark("first_chat_window", level="info")
        tm.add_window(chat_window)
        tm.remove_boot_placeholder()
        tm._mark_first_window_ready()
        # [T24 R2] 窗口就绪 → 登记到共享状态；若 import 窗口期已有二次启动请求
        # （_on_show_requested 记了 pending），此处立即补激活，不再静默丢失。
        _show_window_state["window"] = tm
        if _show_window_state["wanted"]:
            _show_window_state["wanted"] = False
            _activate_window(tm)
        logger.info("DriFox 以 Tab 管理器模式启动（壳先行 + 进程级预热）")

        # 延迟检测上次原生崩溃 dump：主窗口就绪 8s 后逐条以 InfoBar 提示，不抢首帧。
        # 每条 InfoBar 创建成功即重命名 .reported（显示过就改状态），下次启动不再提示
        def _check_last_crash():
            try:
                from app.core.crash_handler import check_pending_crashes, prompt_crash_report
                from app.utils.utils import get_app_data_dir

                dumps = check_pending_crashes(get_app_data_dir() / "logs")
                for dump in dumps:
                    logger.warning(f"[CrashHandler] 检测到上次崩溃报告: {dump}")
                    prompt_crash_report(dump, parent=tm)
            except Exception:
                pass

        QTimer.singleShot(8000, _check_last_crash)

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

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
