# -*- coding: utf-8 -*-
from enum import Enum
from typing import Dict, List, Optional, Tuple
from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger


class ContainerType(Enum):
    TOP = "top"
    BOTTOM = "bottom"


class CardManager(QObject):
    """中央卡片管理器 - 统一管理所有卡片的显示状态和优先级
    
    功能：
    - 注册/注销卡片
    - 按优先级显示/隐藏卡片
    - 同优先级卡片共存，高优先级覆盖低优先级
    - 恢复机制：高优先级卡片关闭后恢复之前的低优先级卡片
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
        # 容器类型 -> {card_id: (widget, priority)}
        self._cards: Dict[ContainerType, Dict[str, Tuple]] = {
            ContainerType.TOP: {},
            ContainerType.BOTTOM: {},
        }
        # card_id -> container_type
        self._card_containers: Dict[str, ContainerType] = {}
        # 优先级 -> [card_ids]（用于同优先级共存）
        self._priority_groups: Dict[int, List[str]] = {}
        # card_id -> 之前的可见状态（用于恢复）
        self._previous_visible_state: Dict[str, bool] = {}
        # 当前可见的卡片（按容器分组）
        self._visible_cards: Dict[ContainerType, List[str]] = {
            ContainerType.TOP: [],
            ContainerType.BOTTOM: [],
        }

    def register_card(
        self,
        container_type: ContainerType,
        card_id: str,
        card_widget,
        priority: int = 50
    ):
        """注册卡片到管理器
        
        Args:
            container_type: 容器类型 (TOP/BOTTOM)
            card_id: 卡片唯一标识
            card_widget: 卡片控件对象
            priority: 优先级，数值越小权限越高（默认50）
        """
        if card_id in self._card_containers:
            logger.warning(f"[CardManager] 卡片 {card_id} 已注册，将被覆盖")
        
        # 存储卡片信息
        self._cards[container_type][card_id] = (card_widget, priority)
        self._card_containers[card_id] = container_type
        
        # 按优先级分组
        if priority not in self._priority_groups:
            self._priority_groups[priority] = []
        if card_id not in self._priority_groups[priority]:
            self._priority_groups[priority].append(card_id)
        
        logger.debug(f"[CardManager] 注册卡片: {card_id} (容器:{container_type.value}, 优先级:{priority})")

    def unregister_card(self, card_id: str):
        """注销卡片"""
        if card_id not in self._card_containers:
            return
        
        container_type = self._card_containers[card_id]
        if card_id in self._cards[container_type]:
            del self._cards[container_type][card_id]
        del self._card_containers[card_id]
        
        # 从优先级分组中移除
        for priority in self._priority_groups:
            if card_id in self._priority_groups[priority]:
                self._priority_groups[priority].remove(card_id)
                break
        
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
        card_widget = self._cards[container_type][card_id][0]
        priority = self._cards[container_type][card_id][1]
        
        # 保存当前可见状态（用于恢复）
        if not force:
            self._save_visible_state()
        
        # 隐藏冲突的卡片（同容器、低优先级）
        self._hide_conflicting_cards(container_type, priority, card_id)
        
        # 显示卡片
        card_widget.setVisible(True)
        
        # 更新可见列表
        if card_id not in self._visible_cards[container_type]:
            self._visible_cards[container_type].append(card_id)
        
        self.cardShown.emit(card_id)
        logger.debug(f"[CardManager] 显示卡片: {card_id} (优先级: {priority})")

    def hide_card(self, card_id: str):
        """隐藏指定卡片"""
        if card_id not in self._card_containers:
            return
        
        container_type = self._card_containers[card_id]
        card_widget = self._cards[container_type][card_id][0]
        
        card_widget.setVisible(False)
        
        # 从可见列表中移除
        if card_id in self._visible_cards[container_type]:
            self._visible_cards[container_type].remove(card_id)
        
        self.cardHidden.emit(card_id)
        logger.debug(f"[CardManager] 隐藏卡片: {card_id}")

    def toggle_card(self, card_id: str):
        """切换卡片显示状态"""
        if self.is_card_visible(card_id):
            self.hide_card(card_id)
        else:
            self.show_card(card_id)

    def hide_all_cards(self, container_type: Optional[ContainerType] = None):
        """隐藏所有卡片"""
        targets = [container_type] if container_type else [ContainerType.TOP, ContainerType.BOTTOM]
        
        for ct in targets:
            for card_id in list(self._visible_cards[ct]):
                self.hide_card(card_id)

    def get_visible_cards(self, container_type: ContainerType) -> List[str]:
        """获取容器中当前可见的所有卡片ID"""
        return list(self._visible_cards[container_type])

    def is_card_visible(self, card_id: str) -> bool:
        """检查卡片是否可见"""
        if card_id not in self._card_containers:
            return False
        return card_id in self._visible_cards[self._card_containers[card_id]]

    def get_card_priority(self, card_id: str) -> int:
        """获取卡片优先级"""
        if card_id not in self._card_containers:
            return -1
        container_type = self._card_containers[card_id]
        return self._cards[container_type][card_id][1]

    def restore_after_hide(self, excluded_card_id: str = None):
        """恢复之前被隐藏的卡片
        
        当高优先级卡片关闭后调用，恢复之前显示的低优先级卡片
        """
        for card_id, was_visible in self._previous_visible_state.items():
            if was_visible and card_id != excluded_card_id:
                self.show_card(card_id, force=True)
        
        self._previous_visible_state.clear()

    def _save_visible_state(self):
        """保存当前可见状态"""
        self._previous_visible_state.clear()
        for container_type in ContainerType:
            for card_id in self._visible_cards[container_type]:
                self._previous_visible_state[card_id] = True

    def _hide_conflicting_cards(self, container_type: ContainerType, priority: int, exclude_card_id: str):
        """隐藏冲突的卡片（同容器、低优先级）
        
        规则：
        - 同优先级共存：不隐藏同优先级的卡片
        - 高优先级覆盖低优先级：隐藏 priority > 当前priority 的卡片
        """
        for card_id, (widget, card_priority) in list(self._cards[container_type].items()):
            if card_id == exclude_card_id:
                continue
            # 只隐藏低优先级的卡片（数值更大的优先级）
            if card_priority > priority and self.is_card_visible(card_id):
                self.hide_card(card_id)