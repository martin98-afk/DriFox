# -*- coding: utf-8 -*-
from enum import Enum
from typing import Dict, List, Optional, Tuple
from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger


class ContainerType(Enum):
    TOP = "top"      # chatscroll 上方
    BOTTOM = "bottom"  # chatscroll 下方


class CardManager(QObject):
    """中央卡片管理器 - 统一管理所有卡片的显示状态
    
    规则：
    - 同位置互斥：Top/Bottom 各自只能显示一个卡片
    - Question 强制覆盖：Question 显示时同时关闭其他所有卡片
    - 不同位置可共存：Top 的卡片和 Bottom 的卡片可以同时显示
    - 恢复机制：高优先级卡片关闭后恢复之前的卡片状态
    """
    
    # 信号：卡片显示/隐藏时触发
    cardShown = pyqtSignal(str)      # card_id
    cardHidden = pyqtSignal(str)     # card_id
    
    _instance = None

    @classmethod
    def get_instance(cls) -> "CardManager":
        if cls._instance is None:
            cls._instance = CardManager()
        return cls._instance

    def __init__(self):
        super().__init__()
        # 容器类型 -> {card_id: widget}
        self._cards: Dict[ContainerType, Dict[str, object]] = {
            ContainerType.TOP: {},
            ContainerType.BOTTOM: {},
        }
        # card_id -> container_type
        self._card_containers: Dict[str, ContainerType] = {}
        # card_id -> 是否强制覆盖卡片（Question）
        self._override_cards: set = {"question"}
        # card_id -> 之前的可见状态（用于恢复）
        self._previous_visible_state: Dict[str, bool] = {}
        # 当前可见的卡片（按容器分组）
        self._visible_cards: Dict[ContainerType, Optional[str]] = {
            ContainerType.TOP: None,
            ContainerType.BOTTOM: None,
        }

    def register_card(
        self,
        container_type: ContainerType,
        card_id: str,
        card_widget,
    ):
        """注册卡片到管理器
        
        Args:
            container_type: 容器类型 (TOP/BOTTOM)
            card_id: 卡片唯一标识
            card_widget: 卡片控件对象
        """
        if card_id in self._card_containers:
            logger.warning(f"[CardManager] 卡片 {card_id} 已注册，将被覆盖")
        
        self._cards[container_type][card_id] = card_widget
        self._card_containers[card_id] = container_type
        
        logger.debug(f"[CardManager] 注册卡片: {card_id} (容器:{container_type.value})")

    def unregister_card(self, card_id: str):
        """注销卡片"""
        if card_id not in self._card_containers:
            return
        
        container_type = self._card_containers[card_id]
        if card_id in self._cards[container_type]:
            del self._cards[container_type][card_id]
        del self._card_containers[card_id]
        
        logger.debug(f"[CardManager] 注销卡片: {card_id}")

    def show_card(self, card_id: str, *, force: bool = False):
        """显示指定卡片
        
        Args:
            card_id: 卡片ID
            force: 是否强制显示（True=不执行恢复机制，直接显示）
        """
        if card_id not in self._card_containers:
            logger.warning(f"[CardManager] 未注册的卡片: {card_id}")
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards[container_type][card_id]
        
        # 保存当前可见状态（用于恢复）
        if not force:
            self._save_visible_state()
        
        # 隐藏同位置其他卡片
        self._hide_same_container_cards(container_type, exclude_card_id=card_id)
        
        # Question 特殊处理：强制关闭所有其他卡片
        if card_id in self._override_cards:
            self._hide_all_cards()
        
        # 显示卡片
        card_widget.setVisible(True)
        self._visible_cards[container_type] = card_id
        
        self.cardShown.emit(card_id)
        logger.debug(f"[CardManager] 显示卡片: {card_id} (容器:{container_type.value})")

    def hide_card(self, card_id: str):
        """隐藏指定卡片"""
        if card_id not in self._card_containers:
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards[container_type][card_id]
        
        card_widget.setVisible(False)
        
        if self._visible_cards[container_type] == card_id:
            self._visible_cards[container_type] = None
        
        self.cardHidden.emit(card_id)
        logger.debug(f"[CardManager] 隐藏卡片: {card_id}")

    def toggle_card(self, card_id: str):
        """切换卡片显示状态"""
        if self.is_card_visible(card_id):
            self.hide_card(card_id)
        else:
            self.show_card(card_id)

    def _hide_all_cards(self):
        """隐藏所有卡片"""
        for container_type in ContainerType:
            self._hide_same_container_cards(container_type)

    def _hide_same_container_cards(self, container_type: ContainerType, exclude_card_id: str = None):
        """隐藏同容器的所有卡片"""
        for card_id in list(self._cards[container_type].keys()):
            if card_id != exclude_card_id and self.is_card_visible(card_id):
                self.hide_card(card_id)

    def get_visible_card(self, container_type: ContainerType) -> Optional[str]:
        """获取容器中当前可见的卡片ID"""
        return self._visible_cards[container_type]

    def is_card_visible(self, card_id: str) -> bool:
        """检查卡片是否可见"""
        if card_id not in self._card_containers:
            return False
        return self._visible_cards[self._card_containers[card_id]] == card_id

    def restore_after_hide(self, excluded_card_id: str = None):
        """恢复之前被隐藏的卡片
        
        当高优先级卡片关闭后调用，恢复之前显示的低优先级卡片
        """
        for card_id, was_visible in self._previous_visible_state.items():
            if was_visible and card_id != excluded_card_id:
                self.show_card(card_id, force=True)
        
        self._previous_visible_state.clear()
        logger.debug(f"[CardManager] 恢复卡片: {[k for k, v in self._previous_visible_state.items() if v]}")

    def _save_visible_state(self):
        """保存当前可见状态"""
        self._previous_visible_state.clear()
        for container_type in ContainerType:
            card_id = self._visible_cards[container_type]
            if card_id:
                self._previous_visible_state[card_id] = True