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
    - Question 强制覆盖：Question 显示时同时关闭其他所有卡片
    - 不同位置可共存：Top 的卡片和 Bottom 的卡片可以同时显示
    - 无自动恢复：关闭卡片不会自动恢复其他卡片
    """
    
    _instance = None

    @classmethod
    def get_instance(cls) -> "CardManager":
        if cls._instance is None:
            cls._instance = object.__new__(cls)
            cls._instance.__init_state()
        return cls._instance

    def __init__(self):
        # 避免重复初始化
        pass

    def __init_state(self):
        """初始化实例状态"""
        self._cards = {
            ContainerType.TOP: {},
            ContainerType.BOTTOM: {},
        }
        # card_id -> container_type
        self._card_containers: Dict[str, ContainerType] = {}
        # card_id -> 是否强制覆盖卡片（Question）
        self._override_cards = {"question"}
        # 当前可见的卡片（按容器分组）
        self._visible_cards = {
            ContainerType.TOP: None,
            ContainerType.BOTTOM: None,
        }
        # 回调函数
        self._shown_callbacks: Dict[str, List[Callable]] = {}
        self._hidden_callbacks: Dict[str, List[Callable]] = {}

    def _ensure_state_initialized(self):
        """确保状态已初始化"""
        if not hasattr(self, '_visible_cards') or self._visible_cards is None:
            self.__init_state()

    def register_card(
        self,
        container_type: ContainerType,
        card_id: str,
        card_widget,
    ):
        """注册卡片到管理器"""
        self._ensure_state_initialized()
        
        if container_type not in self._cards:
            self._cards[container_type] = {}
        
        if card_id in self._card_containers:
            logger.warning(f"[CardManager] 卡片 {card_id} 已注册，将被覆盖")
        
        self._cards[container_type][card_id] = card_widget
        self._card_containers[card_id] = container_type
        
        logger.debug(f"[CardManager] 注册卡片: {card_id} (容器:{container_type.value})")

    def unregister_card(self, card_id: str):
        """注销卡片"""
        self._ensure_state_initialized()
        
        if card_id not in self._card_containers:
            return
        
        container_type = self._card_containers[card_id]
        if card_id in self._cards.get(container_type, {}):
            del self._cards[container_type][card_id]
        del self._card_containers[card_id]
        
        # 清除可见状态
        if self._visible_cards.get(container_type) == card_id:
            self._visible_cards[container_type] = None
        
        logger.debug(f"[CardManager] 注销卡片: {card_id}")

    def show_card(self, card_id: str, *, override_all: bool = False):
        """显示指定卡片
        
        Args:
            card_id: 卡片ID
            override_all: 是否强制覆盖所有卡片（用于 Question）
        """
        self._ensure_state_initialized()
        
        if card_id not in self._card_containers:
            logger.warning(f"[CardManager] 未注册的卡片: {card_id}")
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards[container_type][card_id]
        
        # 隐藏同位置其他卡片（互斥规则）
        self._hide_same_container_cards(container_type, exclude_card_id=card_id)
        
        # Question 特殊处理：强制关闭所有其他卡片
        if override_all or card_id in self._override_cards:
            self._hide_all_cards()
        
        # 显示卡片
        card_widget.setVisible(True)
        self._visible_cards[container_type] = card_id
        
        # 触发回调
        self._emit_shown(card_id)
        
        logger.debug(f"[CardManager] 显示卡片: {card_id} (容器:{container_type.value})")

    def hide_card(self, card_id: str):
        """隐藏指定卡片"""
        self._ensure_state_initialized()
        
        if card_id not in self._card_containers:
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards[container_type][card_id]
        
        card_widget.setVisible(False)
        
        if self._visible_cards.get(container_type) == card_id:
            self._visible_cards[container_type] = None
        
        # 触发回调
        self._emit_hidden(card_id)
        
        logger.debug(f"[CardManager] 隐藏卡片: {card_id}")

    def toggle_card(self, card_id: str):
        """切换卡片显示状态"""
        self._ensure_state_initialized()
        
        if self.is_card_visible(card_id):
            self.hide_card(card_id)
        else:
            self.show_card(card_id)

    def _emit_shown(self, card_id: str):
        """触发显示回调"""
        if card_id in self._shown_callbacks:
            for callback in self._shown_callbacks[card_id]:
                callback(card_id)
        if "any" in self._shown_callbacks:
            for callback in self._shown_callbacks["any"]:
                callback(card_id)

    def _emit_hidden(self, card_id: str):
        """触发隐藏回调"""
        if card_id in self._hidden_callbacks:
            for callback in self._hidden_callbacks[card_id]:
                callback(card_id)
        if "any" in self._hidden_callbacks:
            for callback in self._hidden_callbacks["any"]:
                callback(card_id)

    def on_card_shown(self, card_id: str, callback: Callable):
        """注册卡片显示回调"""
        if card_id not in self._shown_callbacks:
            self._shown_callbacks[card_id] = []
        self._shown_callbacks[card_id].append(callback)

    def on_card_hidden(self, card_id: str, callback: Callable):
        """注册卡片隐藏回调"""
        if card_id not in self._hidden_callbacks:
            self._hidden_callbacks[card_id] = []
        self._hidden_callbacks[card_id].append(callback)

    def _hide_all_cards(self):
        """隐藏所有卡片"""
        for container_type in ContainerType:
            self._hide_same_container_cards(container_type)

    def _hide_same_container_cards(self, container_type: ContainerType, exclude_card_id: str = None):
        """隐藏同容器的所有卡片"""
        container_cards = self._cards.get(container_type, {})
        for card_id in list(container_cards.keys()):
            if card_id != exclude_card_id and self.is_card_visible(card_id):
                self.hide_card(card_id)

    def get_visible_card(self, container_type: ContainerType) -> Optional[str]:
        """获取容器中当前可见的卡片ID"""
        self._ensure_state_initialized()
        return self._visible_cards.get(container_type)

    def is_card_visible(self, card_id: str) -> bool:
        """检查卡片是否可见"""
        self._ensure_state_initialized()
        
        if card_id not in self._card_containers:
            return False
        container_type = self._card_containers[card_id]
        return self._visible_cards.get(container_type) == card_id

    def show_question_card(self, card_id: str = "question"):
        """显示 Question 卡片（强制覆盖所有）"""
        self.show_card(card_id, override_all=True)

    def hide_question_card(self, card_id: str = "question"):
        """隐藏 Question 卡片（不恢复其他卡片）"""
        self.hide_card(card_id)

    def restore_after_hide(self, closed_card_id: str = None):
        """此方法已废弃，保留兼容性但不做任何事"""
        # 简化逻辑：不自动恢复任何卡片
        pass