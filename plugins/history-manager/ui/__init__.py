# -*- coding: utf-8 -*-
"""history-manager 插件 UI 注册入口

职责：
- 注册工作台「历史会话」页（``page_id="history"``），页面自带 ``HistoryCard``
  与会话列表交互（原 ``app/widgets/workbench_panel.HistoryPage`` +
  ``app/widgets/cards/settings/history_card.py``）。
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
        registry.register_workbench_tab(
            plugin_name=_PLUGIN_NAME,
            page_id="history",
            label="历史会话",
            widget_class=_BoundHistoryPage,
            priority=10,
            metadata={"source": "system", "order_hint": 20},
        )
        logger.info("[history-manager] 已注册工作台 tab: history（历史会话页）")
    except Exception as e:
        logger.warning(f"[history-manager] 注册 history tab 失败（历史页将缺失）: {e}")

    try:
        registry.register_service(HistoryService.SERVICE_NAME, service, plugin_name=_PLUGIN_NAME)
        logger.info("[history-manager] 已注册 HistoryService")
    except Exception as e:
        logger.warning(f"[history-manager] 注册 HistoryService 失败: {e}")
