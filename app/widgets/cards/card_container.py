# -*- coding: utf-8 -*-
from typing import Dict, Optional
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QSizePolicy, QApplication
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
        self._window_id: Optional[str] = None  # 多窗口隔离
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
    
    def bind_card_manager(self, card_manager: CardManager, window_id: str):
        """绑定 CardManager（多窗口隔离）"""
        self._card_manager = card_manager
        self._window_id = window_id
    
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
            # 延迟展开 - 等父布局完成后再计算高度
            QTimer.singleShot(50, self._expand)
        else:
            self._collapse()
    
    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片到容器，并注册专属回调"""
        self._cards[card_id] = card_widget
        self._layout.addWidget(card_widget)
        card_widget.setVisible(False)
        
        # 注册此卡片专属的回调（传入 window_id 用于多窗口隔离）
        if self._card_manager and self._window_id:
            self._card_manager.on_card_shown(self._window_id, card_id, self._on_card_shown)
            self._card_manager.on_card_hidden(self._window_id, card_id, self._on_card_hidden)
        
        # 连接卡片内部高度变化信号 → 容器重新展开（支持拖拽、自适应等动态高度）
        if hasattr(card_widget, 'heightChanged'):
            card_widget.heightChanged.connect(self._expand)
    
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
        # 强制刷新布局，确保卡片已获得正确的宽度和高度
        self.layout().activate()
        QApplication.processEvents()

        total_height = 0
        for widget in self._cards.values():
            if widget.isVisible():
                h = widget.height()
                if h <= 0:
                    h = widget.sizeHint().height()
                if h <= 0:
                    h = widget.minimumHeight()
                if h <= 0:
                    h = 400
                total_height += h

        if total_height > 0:
            self.setMaximumHeight(total_height + 4)
        else:
            self._collapse()

        # 再次延迟展开 - 处理动态添加的内容（仅高度变化时应用，避免抖动）
        QTimer.singleShot(50, self._delayed_expand)
    
    def _delayed_expand(self):
        """延迟展开 - 处理动态添加的内容（仅高度变化时应用，避免抖动）"""
        total_height = 0
        for widget in self._cards.values():
            if widget.isVisible():
                h = widget.height()
                if h <= 0:
                    h = widget.sizeHint().height()
                if h <= 0:
                    h = 400
                total_height += h
        if total_height > 0:
            new_max = total_height + 4
            # 仅当高度变化超过 2px 时才应用，避免微小抖动
            if abs(new_max - self.maximumHeight()) > 2:
                self.setMaximumHeight(new_max)
                self.updateGeometry()
        else:
            self._collapse()
    
    def _collapse(self):
        """收起容器"""
        self.setMinimumHeight(0)
        self.setMaximumHeight(0)
        self.updateGeometry()

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
    
    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片并修正底部圆角为直角，与下方输入框视觉融合"""
        # 将卡片底部圆角置零（覆盖宽泛的 border-radius 规则）
        old = card_widget.styleSheet() or ""
        card_widget.setStyleSheet(old + """
        
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
        
        """)
        super().add_card(card_id, card_widget)
    
    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片并修正底部圆角，使其与下方输入框视觉融合"""
        # 修正卡片底部圆角为直角
        card_widget.setProperty("bottomCard", True)
        card_widget.style().unpolish(card_widget)
        card_widget.style().polish(card_widget)
        super().add_card(card_id, card_widget)
