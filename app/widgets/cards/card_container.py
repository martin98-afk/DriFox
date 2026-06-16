# -*- coding: utf-8 -*-
from typing import Dict, Optional
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QSizePolicy
from PyQt5.QtCore import QEvent, QTimer, QPropertyAnimation, QEasingCurve, Qt
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

    # 卡片可通过 setProperty(NO_ANIMATION_PROP, True) 声明不参与容器的展开/折叠动画
    # 适用场景：卡片自带 resize / 拖拽 等会持续触发 heightChanged 的交互，
    # 容器动画会与这些交互产生约一个动画时长的高度延迟 / 抖动。
    # 声明后，容器高度会直接 snap 到目标值，不再走 QPropertyAnimation。
    NO_ANIMATION_PROP = "noContainerAnimation"

    def __init__(self, container_type: ContainerType):
        super().__init__()
        self._container_type = container_type
        self._cards: Dict[str, QWidget] = {}
        self._card_manager: Optional[CardManager] = None
        self._window_id: Optional[str] = None  # 多窗口隔离
        self._expand_timer: Optional[QTimer] = None  # 防抖展开定时器
        self._expand_animation: Optional[QPropertyAnimation] = None  # 展开/折叠动画
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

    def _should_skip_animation(self) -> bool:
        """当前可见卡片中是否存在声明跳过容器动画的卡片

        用于 resize 等高频高度变化场景，避免容器动画造成视觉延迟。
        """
        for w in self._cards.values():
            if w.isVisible() and w.property(self.NO_ANIMATION_PROP):
                return True
        return False

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
        """防抖调度：有可见卡片则展开，否则折叠

        注意：不在"已展开"时早 return。
        子卡片（如 CommandCard 切 detail 模式）可能在已展开容器内
        通过 setFixedHeight 改变自身高度，需要容器重新跑 _do_expand
        以动画到新高度。_do_expand 内部会用 sizeHint 差异判断避免无谓动画。
        """
        # 窗口拖拽过程中跳过容器展开/折叠，防止布局级联干扰
        try:
            from app.tool_popup import ToolPopupDialog
            if ToolPopupDialog._any_window_dragging:
                return
        except ImportError:
            pass
        has_visible = any(w.isVisible() for w in self._cards.values())
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
        """执行展开/折叠动画（200ms 缓动，OutCubic）

        若当前唯一可见卡片声明了 NO_ANIMATION_PROP（例如自带 resize 的 todo 卡片），
        则跳过动画、直接 snap 到目标高度，避免容器高度在拖拽过程中滞后于卡片内容。
        """
        has_visible = any(w.isVisible() for w in self._cards.values())
        skip_anim = self._should_skip_animation()

        # 取消进行中的动画，避免叠加造成跳变
        if self._expand_animation is not None and self._expand_animation.state() == QPropertyAnimation.Running:
            self._expand_animation.stop()
            try:
                self._expand_animation.finished.disconnect()
            except (TypeError, RuntimeError):
                pass

        # 解除 minHeight 限制，确保折叠动画能跑到 0
        self.setMinimumHeight(0)

        if has_visible:
            # ── 展开：snap 或动画到 layout 算出的自然高度 ──
            # 先放开 maxHeight，让 layout 算出"展开后该有多高"
            self.setMaximumHeight(self._EXPAND_MAX)
            # 强制激活布局：修复 setUpdatesEnabled(False) 期间填充内容后，
            # layout.sizeHint() 可能返回过期缓存值（输入框隐藏但卡片不显示，
            # resize 窗口后恢复正常）。activate() 确保子 widget 的最新内容
            # 被计入容器高度。
            self._layout.activate()
            natural_h = max(
                self._layout.sizeHint().height(),
                self._layout.minimumSize().height(),
            )
            if natural_h <= 0:
                # 兜底：layout 没算出高度（布局管道尚未完成测量），
                # 延迟重试，让 Qt 在下一轮事件循环完成几何计算后再展开
                QTimer.singleShot(0, self._do_expand)
                return
            current_h = self.height()
            # 高度差异 < 2px 跳过动画，避免列表过滤/模式切换时无谓抖动
            if abs(natural_h - current_h) < 2:
                return
            if skip_anim:
                # 声明跳过动画：直接 snap，避免 resize / 拖拽期间高度延迟
                self.setMaximumHeight(natural_h)
                return
            self._animate_height(current_h, natural_h, on_finished=None)
        else:
            # ── 折叠：snap 或动画到 0，结束后锁定 maxHeight=0 ──
            # 折叠前 maxHeight 可能是 _EXPAND_MAX 或动画中间值，确保放开以读取真实 height
            if self.maximumHeight() < self._EXPAND_MAX:
                self.setMaximumHeight(self._EXPAND_MAX)
            current_h = self.height()
            if current_h <= 0:
                self.setMaximumHeight(0)
                return
            if skip_anim:
                self.setMaximumHeight(0)
                return
            self._animate_height(
                current_h,
                0,
                on_finished=lambda: self.setMaximumHeight(0),
            )

    def _animate_height(self, start_h: int, end_h: int, on_finished=None):
        """用 QPropertyAnimation 动画 maximumHeight，200ms OutCubic"""
        self._expand_animation = QPropertyAnimation(self, b"maximumHeight")
        self._expand_animation.setDuration(200)
        self._expand_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._expand_animation.setStartValue(start_h)
        self._expand_animation.setEndValue(end_h)
        if on_finished is not None:
            def _on_done():
                try:
                    on_finished()
                finally:
                    try:
                        if self._expand_animation is not None:
                            self._expand_animation.finished.disconnect(_on_done)
                    except (TypeError, RuntimeError):
                        pass
            self._expand_animation.finished.connect(_on_done)
        self._expand_animation.start()
    
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
