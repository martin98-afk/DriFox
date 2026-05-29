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
if not MEM_DIAG_ENABLED:
    os.environ["MEM_DIAG"] = "0"
elif os.environ.get("MEM_DIAG") == "0":
    # 如果用户通过命令行显式设了 MEM_DIAG=0，尊重命令行
    pass
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
    import platform
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    from loguru import logger
    from app.utils import icons_rc

    # ========== 必须在创建 QApplication 之前设置 Qt 属性 ==========
    # 这些设置必须在任何 Qt 模块导入之前或 QApplication 创建之前完成

    # DPI 缩放设置
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    # OpenGL 共享上下文（解决 QtWebEngineWidgets 导入问题）
    if platform.system() != "Darwin":
        QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings)
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

    # ========== 导入可能触发 WebEngine 的模块（在 QApplication 创建之前）==========
    # 提前导入，确保在 app 创建之前触发
    from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401

    from app.utils.utils import get_app_data_dir
    from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401


    # 迁移旧版本数据（打包版从安装目录迁到用户 home 目录）
    from app.utils.utils import migrate_app_data_if_needed
    migrate_app_data_if_needed()

    # 设置日志 (使用统一路径获取方法，DMG 只读时也需要可写)
    log_dir = get_app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "llm_chatter.log",
        rotation="10 MB",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )

    # 单独的内存诊断日志文件（仅含 [MEM] / [MEM-LEAK] 标签的日志）
    # 用于排查工具迭代中的内存泄漏，与业务日志分离便于查看
    # 由 main.py 顶部的 MEM_DIAG_ENABLED 控制总开关
    if MEM_DIAG_ENABLED:
        logger.add(
            log_dir / "mem_diag.log",
            rotation="10 MB",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            filter=lambda r: "[MEM]" in r["message"],
        )

    # 创建应用
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Drifox")
    app.setApplicationDisplayName("Drifox")
    
    # 禁用默认退出行为（让最后一个窗口隐藏到托盘而不是退出）
    app.setQuitOnLastWindowClosed(False)

    # ========== 单实例检查 ==========
    # 在 QApplication 创建之后立即检查，
    # 避免在已运行的情况下重复创建窗口
    from app.core.single_instance import SingleInstanceGuard
    _guard = SingleInstanceGuard("Drifox")
    if not _guard.try_lock():
        # 已有实例在运行 → 通知其显示窗口，然后本实例退出
        _guard.request_show_window()
        _guard.cleanup()
        return

    # 设置主题
    from qfluentwidgets import Theme, setTheme
    setTheme(Theme.DARK)
    # 获取全局字体配置
    try:
        from app.utils.config import Settings
        font_family = Settings.get_instance().llm_font_family.value
    except Exception:
        try:
            font_family = Settings.get_instance().canvas_font_selected.value
        except Exception:
            font_family = "Segoe UI"

    # 启动时同步开机自启注册表状态
    try:
        from app.utils.startup_manager import sync_auto_start_from_config
        sync_auto_start_from_config()
    except Exception:
        logger.exception("[AutoStart] main.py 中调用 sync_auto_start_from_config 失败")

    try:
        from app.utils.design_tokens import scale_font_size
        tooltip_font_size = scale_font_size(12)
    except Exception:
        tooltip_font_size = 12

    app.setStyleSheet(f"""
            QToolTip {{
                color: #ffffff;
                background-color: rgba(30, 30, 32, 240);
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 2px 2px;
                font-size: {tooltip_font_size}px;
                font-family: '{font_family}';
            }}
        """)
    # 创建并显示窗口 - 直接使用 ToolPopupDialog
    logger.info("LLM Chatter 启动中...")

    from app.main_widget import OpenAIChatToolWindow
    from PyQt5.QtWidgets import QWidget

    # 创建模拟的 homepage
    class FakePage(QWidget):
        def __init__(self):
            super().__init__()
            from app.utils.config import Settings
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
    chat_window = OpenAIChatToolWindow(fake_page, None)

    # 延迟创建 ToolPopupDialog 和初始化 TrayManager
    # 等 chat_window 的 __init__ 完成后再创建弹窗，避免窗口渲染阻塞主线程
    from PyQt5.QtCore import QTimer

    def _activate_window(window):
        """激活窗口：显示 + 置前 + 还原（从最小化/隐藏恢复）"""
        window.show()
        window.activateWindow()
        window.raise_()
        if window.isMinimized():
            window.showNormal()

    def _show_popup():
        # TrayManager 懒初始化（首次访问时才创建 QSystemTrayIcon）
        from app.tray_manager import TrayManager
        TrayManager.get_instance()

        # ToolPopupDialog 构造（包含 QSettings 读取、布局构建）
        from app.tool_popup import ToolPopupDialog
        popup = ToolPopupDialog(chat_window, None)
        popup.setWindowTitle("Drifox")

        # 跳过历史会话恢复
        chat_window._skip_restore_history = True

        # 连接单实例信号：当其他实例启动时，激活本窗口
        _guard.show_requested.connect(lambda: _activate_window(popup))

        popup.show()
        logger.info("LLM Chatter 启动成功")

    # 应用退出时清理单实例资源（共享内存 + IPC 服务器）
    app.aboutToQuit.connect(_guard.cleanup)

    # 等 chat_window.__init__ 完成后再创建弹窗
    QTimer.singleShot(0, _show_popup)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()