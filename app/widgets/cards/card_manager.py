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
    - 同位置互斥：Top/Bottom 各自只能显示一个卡片
    - Question 强制覆盖：Question 显示时同时关闭所有其他卡片
    - 不同位置可共存：Top 的卡片和 Bottom 的卡片可以同时显示
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
        """初始化实例状态"""
        self._cards = {
            ContainerType.TOP: {},
            ContainerType.BOTTOM: {},
        }
        self._card_containers: Dict[str, ContainerType] = {}
        self._override_cards = {"question"}
        self._visible_cards = {
            ContainerType.TOP: None,
            ContainerType.BOTTOM: None,
        }
        # 每张卡片的显示/隐藏回调
        self._shown_callbacks: Dict[str, List[Callable]] = {}
        self._hidden_callbacks: Dict[str, List[Callable]] = {}
    
    def _ensure_state_initialized(self):
        if not hasattr(self, '_visible_cards') or self._visible_cards is None:
            self.__init_state()

    def register_card(self, container_type: ContainerType, card_id: str, card_widget):
        """注册卡片到管理器"""
        self._ensure_state_initialized()
        if container_type not in self._cards:
            self._cards[container_type] = {}
        if card_id in self._card_containers:
            logger.warning(f"[CardManager] 卡片 {card_id} 已注册，将被覆盖")
        self._cards[container_type][card_id] = card_widget
        self._card_containers[card_id] = container_type
        logger.debug(f"[CardManager] 注册卡片: {card_id} (容器:{container_type.value})")

    def show_card(self, card_id: str):
        """显示指定卡片（同容器互斥）"""
        self._ensure_state_initialized()
        if card_id not in self._card_containers:
            logger.warning(f"[CardManager] 未注册的卡片: {card_id}")
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards[container_type][card_id]
        
        # 如果卡片已经可见，不做任何事
        if self._visible_cards[container_type] == card_id:
            return
        
        # 隐藏同容器其他卡片
        self._hide_same_container_cards(container_type, exclude_card_id=card_id)
        
        # Question 特殊处理：关闭所有其他卡片
        if card_id in self._override_cards:
            for ct in ContainerType:
                if ct != container_type:
                    self._hide_same_container_cards(ct)
        
        # 显示卡片
        card_widget.setVisible(True)
        self._visible_cards[container_type] = card_id
        
        # 触发此卡片专属的回调
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
        card_widget = self._cards[container_type][card_id]
        
        # 如果已经不可见，不做任何事
        if self._visible_cards[container_type] != card_id:
            return
        
        card_widget.setVisible(False)
        self._visible_cards[container_type] = None
        
        # 触发此卡片专属的回调
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
        """注册卡片显示回调（每张卡片独立）"""
        if card_id not in self._shown_callbacks:
            self._shown_callbacks[card_id] = []
        self._shown_callbacks[card_id].append(callback)

    def on_card_hidden(self, card_id: str, callback: Callable):
        """注册卡片隐藏回调（每张卡片独立）"""
        if card_id not in self._hidden_callbacks:
            self._hidden_callbacks[card_id] = []
        self._hidden_callbacks[card_id].append(callback)

    def _hide_same_container_cards(self, container_type: ContainerType, exclude_card_id: str = None):
        """隐藏同容器的所有卡片"""
        for card_id in list(self._cards[container_type].keys()):
            if card_id != exclude_card_id and self._visible_cards.get(container_type) == card_id:
                card_widget = self._cards[container_type][card_id]
                card_widget.setVisible(False)
                self._visible_cards[container_type] = None
                # 触发回调
                if card_id in self._hidden_callbacks:
                    for cb in self._hidden_callbacks[card_id]:
                        cb(card_id)

    def get_visible_card(self, container_type: ContainerType) -> Optional[str]:
        """获取容器中当前可见的卡片ID"""
        if not hasattr(self, '_visible_cards') or self._visible_cards is None:
            return None
        return self._visible_cards.get(container_type)

    def is_card_visible(self, card_id: str) -> bool:
        """检查卡片是否可见"""
        if card_id not in self._card_containers:
            return False
        container_type = self._card_containers[card_id]
        if not hasattr(self, '_visible_cards') or self._visible_cards is None:
            return False
        return self._visible_cards.get(container_type) == card_id
