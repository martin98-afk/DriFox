# -*- coding: utf-8 -*-
from typing import Dict, Optional
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QSizePolicy
from PyQt5.QtCore import QTimer
from loguru import logger

from app.widgets.cards.card_manager import CardManager, ContainerType


class CardContainer(QWidget):
    """通用卡片容器 - 每个容器只管理一个位置的卡片
    
    功能：
    - 管理同位置多张卡片的显示/隐藏
    - 单卡片互斥：同一时间只显示一张
    - 动态展开/收起容器
    - 只响应自己容器内的卡片事件
    """
    
    def __init__(self, container_type: ContainerType):
        super().__init__()
        self._container_type = container_type
        self._cards: Dict[str, QWidget] = {}
        self._card_manager: Optional[CardManager] = None
        self._setup_ui()
    
    def _setup_ui(self):
        """初始化UI"""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.setMinimumHeight(0)
        self.setMaximumHeight(0)  # 默认折叠
        
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
    
    @property
    def container_type(self) -> ContainerType:
        return self._container_type
    
    def bind_card_manager(self, card_manager: CardManager):
        """绑定 CardManager"""
        self._card_manager = card_manager
    
    def _on_card_shown(self, card_id: str):
        """某张卡片被显示"""
        if card_id not in self._cards:
            return
        # 展开容器
        self._update_container_size()
    
    def _on_card_hidden(self, card_id: str):
        """某张卡片被隐藏"""
        if card_id not in self._cards:
            return
        # 检查容器内是否还有可见卡片
        self._update_container_size()
    
    def _update_container_size(self):
        """更新容器大小：有可见卡片则展开，否则折叠"""
        has_visible = any(w.isVisible() for w in self._cards.values())
        if has_visible:
            # 展开 - 用 QTimer 延迟计算高度，等待卡片渲染完成
            QTimer.singleShot(0, self._expand)
        else:
            self._collapse()
    
    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片到容器，并注册专属回调"""
        if card_id in self._cards:
            logger.warning(f"[CardContainer] 卡片 {card_id} 已存在，将被替换")
        
        self._cards[card_id] = card_widget
        self._layout.addWidget(card_widget)
        card_widget.setVisible(False)
        
        # 注册此卡片专属的回调
        if self._card_manager:
            self._card_manager.on_card_shown(card_id, self._on_card_shown)
            self._card_manager.on_card_hidden(card_id, self._on_card_hidden)
    
    def remove_card(self, card_id: str):
        """从容器移除卡片"""
        if card_id not in self._cards:
            return
        
        widget = self._cards[card_id]
        self._layout.removeWidget(widget)
        del self._cards[card_id]
        
        if len(self._cards) == 0:
            self.setMaximumHeight(0)
    
    def _expand(self):
        """展开容器"""
        total_height = 0
        for widget in self._cards.values():
            if widget.isVisible():
                # 使用 widget.height() 获取实际高度，优先于 sizeHint
                h = widget.height()
                if h <= 0:
                    h = widget.sizeHint().height()
                total_height += h
        
        if total_height > 0:
            total_height += 4  # 微小边距
            self.setMaximumHeight(total_height + 10)
            self.setMinimumHeight(0)
        else:
            self.setMaximumHeight(500)  # 回退高度
        
        self.updateGeometry()
        logger.debug(f"[CardContainer] {self._container_type.value} 展开: 高度={total_height}")
    
    def _collapse(self):
        """收起容器"""
        self.setMinimumHeight(0)
        self.setMaximumHeight(0)
        self.updateGeometry()
        logger.debug(f"[CardContainer] {self._container_type.value} 收起")


class TopCardContainer(CardContainer):
    def __init__(self):
        super().__init__(ContainerType.TOP)


class BottomCardContainer(CardContainer):
    def __init__(self):
        super().__init__(ContainerType.BOTTOM)
