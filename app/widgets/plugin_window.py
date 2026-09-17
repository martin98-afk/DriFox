# -*- coding: utf-8 -*-
"""plugin_window.py — 插件独立弹窗窗口壳

复用主程序窗口外观（qframelesswindow.FramelessWindow + CustomTitleBar
精简模式），供 UIPluginRegistry.register_window 扩展点托管插件内容页。

生命周期：
- 应用退出：main.py aboutToQuit 链调 registry.destroy_all_windows()
- 插件卸载：unload_plugin 逐窗 close_window()
- 用户手动关闭：closeEvent 从 registry._open_windows 摘除实例
静默销毁，无 on_close 通知；插件侧如需清理，在其 widget 析构中自理。
"""

from typing import Any, Dict, Optional

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qframelesswindow import FramelessWindow

from loguru import logger

from app.utils.utils import get_icon
from app.widgets.custom_title_bar import CustomTitleBar

#: 窗口壳默认窗口标题（未提供 title 时）
_DEFAULT_TITLE = "插件窗口"


class PluginWindow(FramelessWindow):
    """插件独立弹窗壳：CustomTitleBar(minimal=True) + 内容页填满客户区"""

    def __init__(self, window_info, context: Optional[Dict[str, Any]] = None, registry=None):
        """创建插件弹窗

        Args:
            window_info: UIPluginRegistry.WindowInfo（含 widget_class/title/
                         icon_path/几何/context_provider）
            context: open_window 传入的可选宿主上下文（仅存供查询，不绑定生命周期）
            registry: UIPluginRegistry 实例（closeEvent 摘除引用用）
        """
        super().__init__()
        self._info = window_info
        self._registry = registry
        self.plugin_context: Dict[str, Any] = dict(context or {})

        # 无边框 + 精简标题栏（无侧栏开关/tab/工作台开关）
        self.setTitleBar(CustomTitleBar(self, minimal=True))
        self.titleBar.set_window_title(window_info.title or _DEFAULT_TITLE)
        self._apply_window_icon(window_info.icon_path)
        self.setWindowTitle(window_info.title or _DEFAULT_TITLE)
        self.setObjectName("pluginWindowRoot")
        self._apply_window_style()

        # 内容页：插件 widget_class 实例填满客户区（构造契约对齐浮动卡：
        # 优先 parent= 关键字，TypeError 回退位置参数；context provider 注入
        # 对齐 _show_floating_card 的 set_context_provider/set_context/_card_context）
        content = self._build_content(window_info.widget_class, window_info.context_provider)
        self._content = content
        # ⚠️ FramelessWindow 的 titleBar 是浮动子控件（不参与布局，靠 resizeEvent
        # 定位在顶部）——布局顶部必须留出标题栏高度，否则内容覆盖标题栏区域；
        # 且 content 晚于 titleBar 创建、z-order 更高，必须再 raise_ 一次，
        # 否则标题栏被盖住（无法拖动、点不到最小化/关闭）。
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, self.titleBar.height(), 0, 0)
        lay.setSpacing(0)
        lay.addWidget(content)
        self.titleBar.raise_()

        # 几何
        self.resize(max(int(window_info.width), 1), max(int(window_info.height), 1))
        min_w, min_h = int(window_info.min_width), int(window_info.min_height)
        if min_w > 0 or min_h > 0:
            self.setMinimumSize(max(min_w, 0), max(min_h, 0))

        # 主题刷新注册（弱引用，窗口销毁自动失效）
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.register_refresh_target(self)
        except Exception:
            pass

    # ── 内部 ──

    def _apply_window_style(self) -> None:
        """窗口整体背景（主题色；refresh_theme 复用）

        FramelessWindow 默认不绘制背景，不设会透出桌面/黑底。
        用 objectName 选择器限定本窗口，避免 QSS 级联影响插件内容页子控件。
        """
        try:
            from app.utils.design_tokens import Colors

            self.setStyleSheet(
                f"#pluginWindowRoot {{ background: {Colors.CONTENT_BG}; }}"
            )
        except Exception:
            pass

    def _apply_window_icon(self, icon_path: str) -> None:
        """标题栏图标：插件提供路径则用之，否则回退 DriFox 图标"""
        icon = None
        if icon_path:
            try:
                icon = QIcon(str(icon_path))
                if icon.isNull():
                    icon = None
            except Exception:
                icon = None
        if icon is None:
            try:
                icon = get_icon("drifox")
            except Exception:
                icon = None
        if icon is not None:
            self.titleBar.set_window_icon(icon)

    def _build_content(self, widget_class: type, context_provider=None) -> QWidget:
        """创建内容页；widget_class 初始化失败时退回空 QWidget（不炸弹窗）

        context_provider: 可选拉模型上下文提供者（无参回调），构造后按
        浮动卡契约注入（set_context_provider / set_context / _card_context）。
        """
        try:
            try:
                page = widget_class(parent=self)
            except TypeError:
                page = widget_class(self)
        except Exception as e:
            logger.warning(
                f"[PluginWindow] 内容页 {getattr(widget_class, '__name__', '?')} 构建失败: {e}"
            )
            return QWidget(self)
        if context_provider is not None:
            try:
                if hasattr(page, "set_context_provider") and callable(page.set_context_provider):
                    page.set_context_provider(context_provider)
                elif hasattr(page, "set_context") and callable(page.set_context):
                    page.set_context(context_provider())
                else:
                    page._card_context = context_provider()
                    page._card_context_provider = context_provider
            except Exception as e:
                logger.debug(f"[PluginWindow] context 注入失败: {e}")
        # 数据/主题加载入口对齐浮动卡激活路径：插件模板的 show_card() 负责
        # 拉数据 + 应用主题（如 git-panel 的 _apply_latest_theme + _async_refresh），
        # 不调用则弹窗只建骨架（样式丢失 + 数据空白）。
        show_card = getattr(page, "show_card", None)
        if callable(show_card):
            try:
                show_card()
            except Exception as e:
                logger.warning(f"[PluginWindow] 内容页 show_card 失败: {e}")
        return page

    def refresh_theme(self) -> None:
        """主题切换刷新（ThemeManager.register_refresh_target 契约）"""
        try:
            from app.utils.design_tokens import Colors

            Colors.refresh()
        except Exception:
            pass
        try:
            self.titleBar.refresh_style()
        except Exception:
            pass
        self._apply_window_style()
        # 主题级联到内容页（插件页若实现 refresh_theme 则同步刷新）
        content = getattr(self, "_content", None)
        refresh_theme = getattr(content, "refresh_theme", None)
        if callable(refresh_theme):
            try:
                refresh_theme()
            except Exception:
                pass

    def closeEvent(self, event) -> None:
        """手动关闭：从 registry 摘除实例（与 close_window 幂等）"""
        reg = self._registry
        if reg is not None and getattr(self, "_info", None) is not None:
            try:
                reg._open_windows.pop(self._info.window_id, None)
            except Exception:
                pass
        super().closeEvent(event)