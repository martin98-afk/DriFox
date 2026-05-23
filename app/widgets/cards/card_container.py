# -*- coding: utf-8 -*-
from typing import Dict
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QSizePolicy
from PyQt5.QtCore import pyqtSignal
from loguru import logger

from app.widgets.cards.card_manager import CardManager, ContainerType


class CardContainer(QWidget):
    """通用卡片容器 - 支持嵌入布局的卡片显示
    
    功能：
    - 管理多个卡片的显示/隐藏
    - 单卡片互斥显示（同一时间只显示一个卡片）
    - 支持动态展开/收起
    """
    
    # 卡片关闭信号
    cardClosed = pyqtSignal(str)  # card_id
    
    def __init__(self, container_type: ContainerType):
        super().__init__()
        self._container_type = container_type
        self._cards: Dict[str, QWidget] = {}
        self._setup_ui()
    
    def _setup_ui(self):
        """初始化UI"""
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.setMinimumHeight(0)
        self.setMaximumHeight(0)  # 默认折叠
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        self._content_widget = QWidget(self)
        self._content_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._content_widget.setVisible(False)
        layout.addWidget(self._content_widget)
        
        self._card_layout = QVBoxLayout(self._content_widget)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(8)
    
    @property
    def container_type(self) -> ContainerType:
        return self._container_type
    
    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片到容器"""
        if card_id in self._cards:
            logger.warning(f"[CardContainer] 卡片 {card_id} 已存在，将被替换")
        
        self._cards[card_id] = card_widget
        self._card_layout.addWidget(card_widget)
        card_widget.setVisible(False)
    
    def remove_card(self, card_id: str):
        """从容器移除卡片"""
        if card_id not in self._cards:
            return
        
        widget = self._cards[card_id]
        self._card_layout.removeWidget(widget)
        del self._cards[card_id]
        
        if len(self._cards) == 0:
            self.setMaximumHeight(0)
            self._content_widget.setVisible(False)
    
    def show_card(self, card_id: str):
        """显示指定卡片（隐藏其他同容器卡片）"""
        if card_id not in self._cards:
            return
        
        for cid, widget in self._cards.items():
            if cid != card_id:
                widget.setVisible(False)
        
        widget = self._cards[card_id]
        widget.setVisible(True)
        self._content_widget.setVisible(True)
        self._expand()
    
    def hide_card(self, card_id: str):
        """隐藏指定卡片"""
        if card_id not in self._cards:
            return
        
        widget = self._cards[card_id]
        widget.setVisible(False)
        
        has_visible = any(w.isVisible() for w in self._cards.values())
        if not has_visible:
            self._content_widget.setVisible(False)
            self._collapse()
    
    def _expand(self):
        """展开容器"""
        total_height = 0
        for widget in self._cards.values():
            if widget.isVisible():
                total_height += widget.sizeHint().height() + self._card_layout.spacing()
        
        if total_height > 0:
            total_height += self._card_layout.contentsMargins().top() + self._card_layout.contentsMargins().bottom()
        
        self.setMaximumHeight(total_height if total_height > 0 else 500)
        self.updateGeometry()
    
    def _collapse(self):
        """收起容器"""
        self.setMaximumHeight(0)
        self._content_widget.setVisible(False)
        self.updateGeometry()


class TopCardContainer(CardContainer):
    """上方卡片容器 - chatscroll 上方"""
    
    def __init__(self):
        super().__init__(ContainerType.TOP)


class BottomCardContainer(CardContainer):
    """下方卡片容器 - chatscroll 下方"""
    
    def __init__(self):
        super().__init__(ContainerType.BOTTOM)