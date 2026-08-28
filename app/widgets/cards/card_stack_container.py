# -*- coding: utf-8 -*-
"""CardStackContainer — 停靠区多卡堆叠容器（Pivot 切换条 + QStackedWidget）

与 CardContainer（单卡互斥 + 展开动画）互补：仅服务 DOCK 容器的堆叠声明卡。
宿主（TabManagerWindow）在 LEFT/RIGHT 停靠区并行挂 CardContainer（非堆叠卡）
与 CardStackContainer（堆叠卡）；两容器互不感知，CardManager 统一裁决。
"""

from typing import Dict, Optional

from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import Pivot

from app.widgets.cards.card_manager import CardManager, ContainerType


class CardStackContainer(QWidget):
    """堆叠卡容器：可见集来自 CardManager.get_visible_cards"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._window_id: str = ""
        self._container_type: Optional[ContainerType] = None
        self._cm: Optional[CardManager] = None
        self._widgets: Dict[str, QWidget] = {}

        self.setObjectName("cardStackContainer")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._pivot = Pivot(self)
        self._pivot.setObjectName("cardStackPivot")
        self._stack = QStackedWidget(self)
        self._layout.addWidget(self._pivot)
        self._layout.addWidget(self._stack, 1)
        self._pivot.currentItemChanged.connect(self._on_pivot_changed)
        self.hide()

    # ── 上下文与卡片管理 ──

    def set_container_context(self, window_id: str, container_type: ContainerType) -> None:
        self._window_id = window_id
        self._container_type = container_type
        self._cm = CardManager.get_instance()

    def attach_card(self, card_id: str, widget: QWidget) -> None:
        if card_id in self._widgets:
            return
        self._widgets[card_id] = widget
        self._pivot.addItem(routeKey=card_id, text=card_id, onClick=lambda k=card_id: self._on_pivot_changed(k))
        self._stack.addWidget(widget)
        self.sync_from_manager()

    def detach_card(self, card_id: str) -> None:
        w = self._widgets.pop(card_id, None)
        if w is not None:
            self._stack.removeWidget(w)
        # qfluentwidgets Pivot 无 removeItem API：清理 items 列表后重建（最小化实现）
        try:
            self._pivot.clear()
        except AttributeError:
            # 老版本无 clear：尝试 removeWidget
            try:
                self._pivot.removeWidget()
            except Exception:
                pass
        # 重新 addItem
        for cid, cw in self._widgets.items():
            self._pivot.addItem(routeKey=cid, text=cid, onClick=lambda k=cid: self._on_pivot_changed(k))
        self.sync_from_manager()

    # ── 状态同步 ──

    def sync_from_manager(self) -> None:
        """按 CardManager 可见集重建显示状态"""
        if self._cm is None or self._container_type is None:
            return
        visible = self._cm.get_visible_cards(self._window_id, self._container_type)
        for cid, w in list(self._widgets.items()):
            w.setVisible(cid in visible)
        if not visible:
            self.hide()
            return
        self.show()
        active = (
            self._cm._window_data.get(self._window_id, {})
            .get("dock_active_cards", {})
            .get(self._container_type)
        )
        # target 必须在 self._widgets 内（防止 active 指向已 detach 的卡）
        target = active if active in self._widgets else visible[-1]
        if target is None or target not in self._widgets:
            return
        idx = self._stack.indexOf(self._widgets[target])
        if idx >= 0:
            self._stack.setCurrentIndex(idx)
        try:
            self._pivot.setCurrentItem(target)
        except Exception:
            pass
        # pivot 条仅在 >1 卡时展示
        self._pivot.setVisible(len(visible) > 1)

    def count(self) -> int:
        return self._stack.count()

    def active_card_id(self) -> Optional[str]:
        w = self._stack.currentWidget()
        for cid, cw in self._widgets.items():
            if cw is w:
                return cid
        return None

    def _on_pivot_changed(self, card_id: str) -> None:
        if card_id not in self._widgets:
            return
        self._stack.setCurrentWidget(self._widgets[card_id])
        if self._cm is not None and self._window_id:
            self._cm.set_active_card(card_id, self._window_id)
