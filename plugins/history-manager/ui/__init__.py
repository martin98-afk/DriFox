# -*- coding: utf-8 -*-
"""history-manager 插件 UI 注册入口

职责：
- 注册对话左侧停靠区浮动卡「历史会话」（``card_id="history-manager"``，
  ``container="left"``，默认常驻显示），页面自带 ``HistoryCard``（搜索 /
  项目选择折叠面板 / 会话列表）与自拉数据逻辑（``HistoryManager`` 全局单例）。
  项目选择面板复用宿主 ``ProjectSelectorCardContent``（首行「全部项目」），
  面板数据的拉取/展开由宿主标题栏项目 icon 经服务入口驱动（见下）。
  左侧栏顶部系统插件区自动派生按钮（浮动卡兼容映射）。
- 注册 ``HistoryService``（``SERVICE_NAME="history"``）：宿主 ``MainWidget``
  通过 ``_history_card`` / ``_history_popup_card`` 代理属性读取页面与卡片，
  会话操作仍由窗口方法实现（页内信号转发）。

注册后自动获得 ``/history`` 命令（``UIPluginRegistry`` 命令账本联动注册）。
"""

from loguru import logger

_PLUGIN_NAME = "history-manager"


class HistoryService:
    """历史会话服务门面（页面引用 + 刷新入口）

    宿主侧用法（``MainWidget``）::

        svc = UIPluginRegistry.get_instance().get_service("history")
        svc.page   # HistoryPage（工作台页）
        svc.card   # HistoryCard（会话列表）
    """

    SERVICE_NAME = "history"

    def __init__(self) -> None:
        self._page = None

    def bind_page(self, page) -> None:
        self._page = page

    @property
    def page(self):
        return self._page

    @property
    def card(self):
        return self._page.card if self._page is not None else None

    def refresh(self) -> None:
        if self._page is not None:
            self._page.refresh()

    def set_opacity(self, opacity: float) -> None:
        if self._page is not None:
            self._page.set_opacity(opacity)

    # ── 项目选择面板代理（宿主标题栏项目 icon / 命令入口驱动） ──

    def open_project_selector(self) -> None:
        """展开项目选择面板（并刷新面板数据）"""
        if self._page is not None and hasattr(self._page, "open_project_selector"):
            self._page.open_project_selector()

    def collapse_project_selector(self) -> None:
        """收起项目选择面板（切项目 / 归档项目后）"""
        if self._page is not None and hasattr(self._page, "collapse_project_selector"):
            self._page.collapse_project_selector()

    def refresh_project_selector_data(self) -> None:
        """刷新项目选择面板数据（项目增删 / 导入导出后）"""
        if self._page is not None and hasattr(self._page, "refresh_project_selector_data"):
            self._page.refresh_project_selector_data()


def register_ui(registry) -> None:
    """历史会话页 ui 组件注册入口（被 UIPluginRegistry.load_plugin 调用）

    Args:
        registry: UIPluginRegistry 单例
    """
    from .history_page import HistoryPage

    service = HistoryService()

    class _BoundHistoryPage(HistoryPage):
        """构造即回绑服务的页面（服务→页面引用由插件自身维护）"""

        def __init__(self, parent=None, context=None):
            super().__init__(parent, context)
            service.bind_page(self)

    try:
        registry.register_floating_card(
            plugin_name=_PLUGIN_NAME,
            card_id="history-manager",
            widget_class=_BoundHistoryPage,
            container="left",
            title="历史会话",
            default_visible=True,  # 左侧停靠区常驻显示
        )
        logger.info("[history-manager] 已注册左侧浮动卡: history-manager（历史会话）")
    except Exception as e:
        logger.warning(f"[history-manager] 注册浮动卡失败（历史列表将缺失）: {e}")

    try:
        registry.register_service(HistoryService.SERVICE_NAME, service, plugin_name=_PLUGIN_NAME)
        logger.info("[history-manager] 已注册 HistoryService")
    except Exception as e:
        logger.warning(f"[history-manager] 注册 HistoryService 失败: {e}")
