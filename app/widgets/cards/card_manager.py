# -*- coding: utf-8 -*-
from enum import Enum
from typing import Dict, List, Optional, Callable
from loguru import logger


class ContainerType(Enum):
    TOP = "top"      # chatscroll 上方
    BOTTOM = "bottom"  # chatscroll 下方


class CardManager:
    """中央卡片管理器 - 统一管理所有卡片的显示状态
    
    规则：
    - 系统卡片组全局互斥：所有标记为 system 的卡片，一次只能显示一张
    - 非系统卡片同容器互斥：Tool/SubAgent 等同容器内互斥
    - 不同容器可共存（如 Top 的 Todo + Bottom 的 Tool）
    - Question 强制覆盖所有
    """
    
    _instance = None

    @classmethod
    def get_instance(cls) -> "CardManager":
        if cls._instance is None:
            cls._instance = object.__new__(cls)
            cls._instance.__init_state()
        return cls._instance

    def __init__(self):
        pass

    def __init_state(self):
        self._cards = {
            ContainerType.TOP: {},
            ContainerType.BOTTOM: {},
        }
        self._card_containers: Dict[str, ContainerType] = {}
        self._override_cards = {"question"}
        self._system_cards: set = set()  # 标记为"系统卡片"的 ID
        self._visible_cards = {
            ContainerType.TOP: None,
            ContainerType.BOTTOM: None,
        }
        self._shown_callbacks: Dict[str, List[Callable]] = {}
        self._hidden_callbacks: Dict[str, List[Callable]] = {}
    
    def _ensure_state_initialized(self):
        if not hasattr(self, '_visible_cards') or not isinstance(self._visible_cards, dict) or ContainerType.TOP not in self._visible_cards:
            self.__init_state()

    def register_card(self, container_type: ContainerType, card_id: str, card_widget, system_card: bool = False):
        """注册卡片到管理器
        
        Args:
            container_type: 容器类型
            card_id: 卡片标识
            card_widget: 控件
            system_card: 是否为系统卡片（系统卡片全局互斥）
        """
        self._ensure_state_initialized()
        if container_type not in self._cards:
            self._cards[container_type] = {}
        if card_id in self._card_containers:
            logger.warning(f"[CardManager] 卡片 {card_id} 已注册，将被覆盖")
        self._cards[container_type][card_id] = card_widget
        self._card_containers[card_id] = container_type
        if system_card:
            self._system_cards.add(card_id)
        logger.debug(f"[CardManager] 注册卡片: {card_id} (容器:{container_type.value}, 系统卡片:{system_card})")

    def show_card(self, card_id: str):
        """显示指定卡片"""
        self._ensure_state_initialized()
        if card_id not in self._card_containers:
            logger.warning(f"[CardManager] 未注册的卡片: {card_id}")
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards.get(container_type, {}).get(card_id)
        if card_widget is None:
            return
        
        # 如果卡片已经可见，不做任何事
        if self._visible_cards.get(container_type) == card_id:
            return
        
        # 系统卡片：全局互斥（隐藏所有其他系统卡片）
        if card_id in self._system_cards:
            self._hide_system_cards(exclude_card_id=card_id)
            # 同时隐藏同容器非系统卡片
            self._hide_same_container_cards(container_type, exclude_card_id=card_id)
        else:
            # 非系统卡片：同容器互斥
            self._hide_same_container_cards(container_type, exclude_card_id=card_id)
            # 非系统卡片显示时，如果系统卡片可见则隐藏（让系统卡片优先变成互斥）
            # 但 Question 特殊：强制关闭所有
            if card_id in self._override_cards:
                self._hide_all_cards()
        
        # 显示卡片
        card_widget.setVisible(True)
        self._visible_cards[container_type] = card_id
        
        # 触发回调
        if card_id in self._shown_callbacks:
            for cb in self._shown_callbacks[card_id]:
                cb(card_id)
        
        logger.debug(f"[CardManager] 显示卡片: {card_id} (容器:{container_type.value})")

    def hide_card(self, card_id: str):
        """隐藏指定卡片"""
        self._ensure_state_initialized()
        if card_id not in self._card_containers:
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards.get(container_type, {}).get(card_id)
        if card_widget is None:
            return
        
        if self._visible_cards.get(container_type) != card_id:
            return
        
        card_widget.setVisible(False)
        self._visible_cards[container_type] = None
        
        # 触发回调
        if card_id in self._hidden_callbacks:
            for cb in self._hidden_callbacks[card_id]:
                cb(card_id)
        
        logger.debug(f"[CardManager] 隐藏卡片: {card_id}")

    def toggle_card(self, card_id: str):
        """切换卡片显示状态"""
        self._ensure_state_initialized()
        if card_id not in self._card_containers:
            return
        if self.is_card_visible(card_id):
            self.hide_card(card_id)
        else:
            self.show_card(card_id)

    def on_card_shown(self, card_id: str, callback: Callable):
        if card_id not in self._shown_callbacks:
            self._shown_callbacks[card_id] = []
        self._shown_callbacks[card_id].append(callback)

    def on_card_hidden(self, card_id: str, callback: Callable):
        if card_id not in self._hidden_callbacks:
            self._hidden_callbacks[card_id] = []
        self._hidden_callbacks[card_id].append(callback)

    def _hide_system_cards(self, exclude_card_id: str = None):
        """隐藏所有系统卡片（跨容器）"""
        for card_id in list(self._system_cards):
            if card_id != exclude_card_id and self.is_card_visible(card_id):
                self.hide_card(card_id)

    def _hide_all_cards(self):
        """隐藏所有卡片（跨容器）"""
        for container_type in ContainerType:
            self._hide_same_container_cards(container_type)

    def _hide_same_container_cards(self, container_type: ContainerType, exclude_card_id: str = None):
        """隐藏同容器的所有卡片"""
        for card_id in list(self._cards[container_type].keys()):
            if card_id != exclude_card_id and self._visible_cards.get(container_type) == card_id:
                card_widget = self._cards[container_type][card_id]
                card_widget.setVisible(False)
                self._visible_cards[container_type] = None
                if card_id in self._hidden_callbacks:
                    for cb in self._hidden_callbacks[card_id]:
                        cb(card_id)

    def get_visible_card(self, container_type: ContainerType) -> Optional[str]:
        if not hasattr(self, '_visible_cards') or self._visible_cards is None:
            return None
        return self._visible_cards.get(container_type)

    def is_card_visible(self, card_id: str) -> bool:
        if card_id not in self._card_containers:
            return False
        container_type = self._card_containers[card_id]
        if not hasattr(self, '_visible_cards') or self._visible_cards is None:
            return False
        return self._visible_cards.get(container_type) == card_id
