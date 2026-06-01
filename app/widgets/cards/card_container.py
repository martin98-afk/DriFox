# -*- coding: utf-8 -*-
from typing import Dict, Optional
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QSizePolicy
from PyQt5.QtCore import QEvent, QTimer
from loguru import logger

from app.widgets.cards.card_manager import CardManager, ContainerType


class CardContainer(QWidget):
    """通用卡片容器 - 每个容器只管理一个位置的卡片
    
    功能：
    - 管理同位置多张卡片的显示/隐藏
    - 单卡片互斥：同一时间只显示一张
    - 动态展开/收起容器（放开/限制最大高度，让 Qt 自然布局）
    - 只响应自己容器内的卡片事件
    """
    
    # 展开时放开最大高度限制，让 Qt 布局系统自动计算合适高度
    _EXPAND_MAX = 16777215

    def __init__(self, container_type: ContainerType):
        super().__init__()
        self._container_type = container_type
        self._cards: Dict[str, QWidget] = {}
        self._card_manager: Optional[CardManager] = None
        self._window_id: Optional[str] = None  # 多窗口隔离
        self._expand_timer: Optional[QTimer] = None  # 防抖展开定时器
        self._setup_ui()
    
    def _setup_ui(self):
        """初始化UI"""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
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
    
    def _is_expanded(self) -> bool:
        """容器是否已展开"""
        return self.maximumHeight() >= self._EXPAND_MAX

    def _on_card_shown(self, card_id: str):
        """某张卡片被显示"""
        if card_id not in self._cards:
            return
        self._schedule_expand()
    
    def _on_card_hidden(self, card_id: str):
        """某张卡片被隐藏"""
        if card_id not in self._cards:
            return
        self._schedule_expand()
    
    def _schedule_expand(self):
        """防抖调度：有可见卡片则展开，否则折叠"""
        # 窗口拖拽过程中跳过容器展开/折叠，防止布局级联干扰
        try:
            from app.tool_popup import ToolPopupDialog
            if ToolPopupDialog._any_window_dragging:
                return
        except ImportError:
            pass
        has_visible = any(w.isVisible() for w in self._cards.values())
        if self._is_expanded() and has_visible:
            return  # 已展开且有可见卡片，不再重复触发布局
        # 取消上次未执行的防抖
        if self._expand_timer:
            self._expand_timer.stop()
        else:
            self._expand_timer = QTimer(self)
            self._expand_timer.setSingleShot(True)
            self._expand_timer.setInterval(0)
            self._expand_timer.timeout.connect(self._do_expand)
        
        if has_visible:
            # ⚡ 有可见卡片：立即展开，不等到 timer 触发
            # 否则父容器高度为 0 时，卡片虽然 setVisible(True) 但在屏幕上看不见
            self._do_expand()
        else:
            # 无可见卡片：通过 timer 折叠（延迟一点没关系）
            self._expand_timer.start()
    
    def _do_expand(self):
        """执行展开/折叠"""
        has_visible = any(w.isVisible() for w in self._cards.values())
        if has_visible:
            # 放开最大高度限制，让 Qt 布局系统自动计算合适高度
            self.setMaximumHeight(self._EXPAND_MAX)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(0)
    
    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片到容器，并注册专属回调"""
        self._cards[card_id] = card_widget
        self._layout.addWidget(card_widget)
        card_widget.setVisible(False)
        card_widget.installEventFilter(self)
        
        # 注册此卡片专属的回调（传入 window_id 用于多窗口隔离）
        if self._card_manager and self._window_id:
            self._card_manager.on_card_shown(self._window_id, card_id, self._on_card_shown)
            self._card_manager.on_card_hidden(self._window_id, card_id, self._on_card_hidden)
        
        # 连接卡片内部高度变化信号 → 容器重新展开（支持拖拽、自适应等动态高度）
        if hasattr(card_widget, 'heightChanged'):
            card_widget.heightChanged.connect(self._schedule_expand)
    
    def remove_card(self, card_id: str):
        """从容器移除卡片"""
        if card_id not in self._cards:
            return
        
        widget = self._cards[card_id]
        widget.removeEventFilter(self)
        self._layout.removeWidget(widget)
        del self._cards[card_id]
        
        if len(self._cards) == 0:
            self.setMaximumHeight(0)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize and obj in self._cards.values():
            self._schedule_expand()
        return super().eventFilter(obj, event)


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
        """添加卡片并修正底部圆角，使其与下方输入框视觉融合"""
        # 修正卡片底部圆角为直角
        card_widget.setProperty("bottomCard", True)
        card_widget.style().unpolish(card_widget)
        card_widget.style().polish(card_widget)
        super().add_card(card_id, card_widget)
