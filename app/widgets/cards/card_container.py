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
        """某张卡片被显示 → 移除高度限制，让布局自然展开"""
        if card_id not in self._cards:
            return
        # 延迟一帧执行，等待卡片渲染完成
        QTimer.singleShot(0, self._release_height)
    
    def _on_card_hidden(self, card_id: str):
        """某张卡片被隐藏 → 若无可见卡片则折叠"""
        if card_id not in self._cards:
            return
        self._update_container_size()
    
    def _update_container_size(self):
        """更新容器大小：有可见卡片则释放高度，否则折叠"""
        has_visible = any(w.isVisible() for w in self._cards.values())
        if has_visible:
            QTimer.singleShot(0, self._release_height)
        else:
            self._collapse()
    
    def _release_height(self):
        """移除高度限制，让布局自然决定容器高度"""
        if any(w.isVisible() for w in self._cards.values()):
            self.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
            self.updateGeometry()
    
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
    """下方卡片容器 - 底部直角设计，与输入框视觉融合"""
    
    def __init__(self):
        super().__init__(ContainerType.BOTTOM)
        self.setStyleSheet("""
            BottomCardContainer {
                background: transparent;
                border: none;
            }
        """)
