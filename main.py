# -*- coding: utf-8 -*-
"""
LLM Chatter 主入口
以独立弹窗模式启动，无需 FluentWindow 框架
"""
import os
import sys
import warnings

from qfluentwidgets import setFontFamilies

from app.tool_popup import ToolPopupDialog

warnings.filterwarnings("ignore")
os.environ["PYTHONIOENCODING"] = "utf-8"

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

    # 设置日志 (使用统一路径获取方法，DMG 只读时也需要可写)
    log_dir = get_app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "llm_chatter.log",
        rotation="10 MB",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"
    )

    # 创建应用
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Drifox")
    app.setApplicationDisplayName("Drifox")

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
        pass

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

    # 初始化全局 TrayManager（必须在创建任何 ToolPopupDialog 之前）
    from app.tray_manager import TrayManager
    TrayManager.get_instance()

    # 使用 ToolPopupDialog 包装
    popup = ToolPopupDialog(chat_window, None)
    popup.setWindowTitle("Drifox")

    # 跳过历史会话恢复
    chat_window._skip_restore_history = True

    popup.show()

    logger.info("LLM Chatter 启动成功")

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()