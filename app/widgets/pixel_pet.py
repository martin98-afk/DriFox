# -*- coding: utf-8 -*-
"""
像素小狐桌宠 — 超迷你互动吉祥物

状态：
  idle     - 空闲呼吸（默认）
  thinking - 思考中（歪头 + 点点）
  streaming- 回复中（说话 + 摇尾）
  question - 等待用户回答（歪头疑惑）
  success  - 任务完成（跳跃 + 撒花）
  error    - 出错了（发抖 + 泪滴）
  sleeping - 长时间无活动（闭眼 + Zzz）

交互：
  · 点击 → 弹跳反馈
  · 拖拽 → 窗口内自由移动

使用：
  pet = PixelPetWidget(parent)
  pet.set_state("thinking")       # 切换到思考状态
  pet.set_state("idle")           # 恢复正常
"""

import logging
from pathlib import Path

from PyQt5.QtCore import (
    QPoint,
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt5.QtGui import QMouseEvent, QPainter, QPixmap
from PyQt5.QtWidgets import QWidget

logger = logging.getLogger(__name__)

# =============================================================================
# 配置常量
# =============================================================================

FRAME_SIZE = 16  # 每帧像素尺寸
SCALE = 3  # 渲染倍数 → 48×48 显示
DISPLAY_SIZE = FRAME_SIZE * SCALE  # 48
SPRITESHEET = Path(__file__).parent / "pet_sprites.png"

# 状态 → 行索引
STATE_ROWS = {
    "idle": 0,
    "thinking": 1,
    "streaming": 2,
    "question": 3,
    "success": 4,
    "error": 5,
    "sleeping": 6,
}

# 每状态帧数
FRAMES_PER_STATE = 8  # 8帧实现流畅动画

# 帧间隔（ms，缩短间隔提升流畅度减少抽搐感）
FRAME_INTERVALS = {
    "idle": 180,
    "thinking": 140,
    "streaming": 80,
    "question": 140,
    "success": 100,
    "error": 70,
    "sleeping": 280,
}

# 空闲超过此时间自动进入睡眠 (ms)
SLEEP_TIMEOUT_MS = 60_000  # 1分钟

# 成功/错误状态持续后恢复 idle (ms)
RECOVER_MS = 2500


class PixelPetWidget(QWidget):
    """像素小狐桌宠 — 浮动在主窗口上的互动吉祥物"""

    # 信号 — 仅用于通知状态变化（可选由外部监听）
    state_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self._current_state = "idle"
        self._frame_index = 0
        self._spritesheet: QPixmap | None = None
        self._dragging = False
        self._drag_offset = QPoint()

        # 加载 spritesheet
        self._load_spritesheet()

        # 外观
        self.setFixedSize(DISPLAY_SIZE, DISPLAY_SIZE)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.PointingHandCursor)

        # 强力 Z-order：定时 + 父控件事件拦截双重保障
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._force_raise)
        self._raise_timer.start(50)  # 高频检查

        # 在父控件上安装事件过滤器，任何子控件变化时拉自己到顶层
        if parent:
            parent.installEventFilter(self)

        # 动画 Timer
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._advance_frame)
        self._frame_timer.setInterval(FRAME_INTERVALS["idle"])
        self._frame_timer.start()

        # 睡眠检测 Timer
        self._sleep_timer = QTimer(self)
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(self._enter_sleep)

        # 恢复 Timer（成功/错误 → idle）
        self._recover_timer = QTimer(self)
        self._recover_timer.setSingleShot(True)
        self._recover_timer.timeout.connect(lambda: self.set_state("idle"))

        # 启动睡眠倒计时
        self._sleep_timer.start(SLEEP_TIMEOUT_MS)

        # 始终确保自己在父控件最上层
        if parent:
            self.raise_()

        # 上次 ai_state（用于检测 streaming→idle 触发 success）
        self._last_ai_state = "idle"

        logger.debug("[PixelPet] 初始化完成，状态: idle")

    # ═══════════════════════════════════════════════════════════
    # Spritesheet 加载
    # ═══════════════════════════════════════════════════════════

    def _load_spritesheet(self) -> None:
        """加载像素 spritesheet 图片"""
        try:
            if SPRITESHEET.exists():
                sp = QPixmap(str(SPRITESHEET))
                if sp.isNull():
                    logger.warning(f"[PixelPet] spritesheet 无效: {SPRITESHEET}")
                    self._spritesheet = None
                else:
                    self._spritesheet = sp
                    logger.debug(f"[PixelPet] spritesheet 加载成功: {SPRITESHEET}")
            else:
                logger.warning(f"[PixelPet] spritesheet 不存在: {SPRITESHEET}")
                self._spritesheet = None
        except Exception as e:
            logger.warning(f"[PixelPet] 加载 spritesheet 失败: {e}")
            self._spritesheet = None

    # ═══════════════════════════════════════════════════════════
    # 信号驱动 — 接收 main_widget 的 AI 状态变化
    # ═══════════════════════════════════════════════════════════

    def _on_ai_state_changed(self, ai_state: str) -> None:
        """接收 main_widget.ai_state_changed 信号

        ai_state: idle / thinking / streaming / question / error
        内部自动处理：
          - streaming→idle 时短暂展示 success 再回 idle
          - idle 超时自动进入 sleeping
        """
        if ai_state == self._last_ai_state:
            return

        prev = self._last_ai_state
        self._last_ai_state = ai_state

        if ai_state == "idle":
            # 从活跃状态回到 idle：先短暂展示 success（跳一下）
            if prev in ("streaming", "thinking"):
                self.set_state("success")
                # recover_timer 会把 success→idle
            else:
                self.set_state("idle")
        elif ai_state == "error":
            self.set_state("error")
            # recover_timer 会把 error→idle，但保持 _last_ai_state = "error"
        else:
            self.set_state(ai_state)

    def set_state(self, state: str) -> None:
        """切换桌宠动画状态（内部调用）"""
        if state not in STATE_ROWS:
            logger.warning(f"[PixelPet] 未知状态: {state}")
            return

        if state == self._current_state:
            return

        old = self._current_state
        self._current_state = state
        self._frame_index = 0

        # 更新帧间隔
        self._frame_timer.setInterval(FRAME_INTERVALS.get(state, 400))

        # 特殊状态处理
        if state in ("success", "error"):
            self._recover_timer.start(RECOVER_MS)
            self._play_bounce()
        elif state == "question":
            self._play_bounce_small()
        elif state == "sleeping":
            self._sleep_timer.stop()
        else:
            self._reset_sleep_timer()

        self.state_changed.emit(state)
        self.update()
        logger.debug(f"[PixelPet] 状态切换: {old} → {state}")

    def get_state(self) -> str:
        return self._current_state

    def _reset_sleep_timer(self) -> None:
        """重置睡眠倒计时"""
        if self._current_state in ("success", "error", "sleeping"):
            return  # 这些状态自己管理计时
        self._sleep_timer.stop()
        self._sleep_timer.start(SLEEP_TIMEOUT_MS)

    def _enter_sleep(self) -> None:
        """进入睡眠状态（仅 idle 时）"""
        if self._current_state == "idle":
            self.set_state("sleeping")
        elif self._current_state == "sleeping":
            pass  # 已在睡眠
        else:
            # 其他状态不自动睡眠，重新倒计时
            self._reset_sleep_timer()

    def wake_up(self) -> None:
        """从睡眠中唤醒"""
        if self._current_state == "sleeping":
            self.set_state("idle")

    # ═══════════════════════════════════════════════════════════
    # 帧动画
    # ═══════════════════════════════════════════════════════════

    def _advance_frame(self) -> None:
        """推进到下一帧"""
        self._frame_index = (self._frame_index + 1) % FRAMES_PER_STATE
        self.update()

    def _current_frame_rect(self) -> QRect:
        """获取当前帧在 spritesheet 中的矩形区域"""
        row = STATE_ROWS.get(self._current_state, 0)
        x = self._frame_index * FRAME_SIZE
        y = row * FRAME_SIZE
        return QRect(x, y, FRAME_SIZE, FRAME_SIZE)

    # ═══════════════════════════════════════════════════════════
    # 绘制
    # ═══════════════════════════════════════════════════════════

    def paintEvent(self, event: object) -> None:
        """绘制当前帧的像素狐狸"""
        # 每次绘制前确保自己在最上层（对抗 layout 管理的兄弟控件遮挡）
        self.raise_()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.LosslessImageRendering, True)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)

        if self._spritesheet and not self._spritesheet.isNull():
            src = self._current_frame_rect()
            # 最近邻缩放 — 保持像素锐利
            painter.drawPixmap(self.rect(), self._spritesheet, src)
        else:
            # fallback：无 spritesheet 时画一个简易图标
            painter.fillRect(self.rect(), Qt.transparent)

        painter.end()

    # ═══════════════════════════════════════════════════════════
    # 动效
    # ═══════════════════════════════════════════════════════════

    def _play_bounce(self) -> None:
        """播放弹跳动画（降低高度减少突兀感）"""
        anim = QPropertyAnimation(self, b"geometry", self)
        geo = self.geometry()
        anim.setDuration(350)
        anim.setKeyValueAt(0, geo)
        anim.setKeyValueAt(0.2, QRect(geo.x(), geo.y() - 2, geo.width(), geo.height()))
        anim.setKeyValueAt(0.5, QRect(geo.x(), geo.y() - 3, geo.width(), geo.height()))
        anim.setKeyValueAt(0.8, QRect(geo.x(), geo.y() - 1, geo.width(), geo.height()))
        anim.setKeyValueAt(1, geo)
        # 非阻塞启动
        anim.start()
        # 保持引用防止 GC
        if not hasattr(self, "_animations"):
            self._animations: list = []
        self._animations.append(anim)

    def _play_bounce_small(self) -> None:
        """提问状态小弹跳（比 success 更低更柔和）"""
        anim = QPropertyAnimation(self, b"geometry", self)
        geo = self.geometry()
        anim.setDuration(250)
        anim.setKeyValueAt(0, geo)
        anim.setKeyValueAt(0.3, QRect(geo.x(), geo.y() - 2, geo.width(), geo.height()))
        anim.setKeyValueAt(0.6, QRect(geo.x(), geo.y() - 1, geo.width(), geo.height()))
        anim.setKeyValueAt(1, geo)
        anim.start()
        if not hasattr(self, "_animations"):
            self._animations: list = []
        self._animations.append(anim)

    def _click_bounce(self) -> None:
        """点击时的小弹跳反响"""
        if self._current_state == "sleeping":
            self.wake_up()
            return

        self._reset_sleep_timer()
        anim = QPropertyAnimation(self, b"geometry", self)
        geo = self.geometry()
        anim.setDuration(250)
        anim.setKeyValueAt(0, geo)
        anim.setKeyValueAt(0.3, QRect(geo.x(), geo.y() - 5, geo.width(), geo.height()))
        anim.setKeyValueAt(0.6, QRect(geo.x(), geo.y() - 1, geo.width(), geo.height()))
        anim.setKeyValueAt(1, geo)
        anim.start()
        if not hasattr(self, "_animations"):
            self._animations: list = []
        self._animations.append(anim)
        logger.debug("[PixelPet] 点击弹跳")

    # ═══════════════════════════════════════════════════════════
    # 鼠标事件 — 拖拽 & 点击
    # ═══════════════════════════════════════════════════════════

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        """开始拖拽"""
        if event is None:
            return
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_offset = event.pos()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        """拖拽移动"""
        if event is None:
            return
        if self._dragging:
            new_pos = self.mapToParent(event.pos()) - self._drag_offset
            # 约束在父控件内
            if self.parent():
                pw = self.parent().width()
                ph = self.parent().height()
                new_x = max(0, min(new_pos.x(), pw - self.width()))
                new_y = max(0, min(new_pos.y(), ph - self.height()))
                self.move(new_x, new_y)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        """结束拖拽"""
        if event is None:
            return
        if event.button() == Qt.LeftButton:
            was_dragging = self._dragging
            self._dragging = False
            self.setCursor(Qt.PointingHandCursor)

            # 如果没有明显移动 → 当作点击
            if was_dragging and self._drag_offset and event.pos():
                dx = abs(event.pos().x() - self._drag_offset.x())
                dy = abs(event.pos().y() - self._drag_offset.y())
                if dx < 4 and dy < 4:
                    self._click_bounce()

    # ═══════════════════════════════════════════════════════════
    # Z-order 保障
    # ═══════════════════════════════════════════════════════════

    def _force_raise(self) -> None:
        """定时器回调：强制提到最上层（对抗 layout 管理的兄弟控件）"""
        if self.parent():
            self.raise_()

    def eventFilter(self, obj: object, event: object) -> bool:
        """拦截父控件事件：任何 resize/paint 时确保自己在顶层"""
        # 父控件发生任何变化时都尝试拉自己到顶
        self.raise_()
        return super().eventFilter(obj, event)

    # ═══════════════════════════════════════════════════════════
    # 生命周期
    # ═══════════════════════════════════════════════════════════

    def resize_handle(self, parent_width: int, parent_height: int) -> None:
        """父窗口大小变化时调用，保持右下角位置"""
        if not self._dragging:
            self.move(
                parent_width - self.width() - 12,
                parent_height - self.height() - 64,
            )

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self.raise_()

    def cleanup(self) -> None:
        """释放资源"""
        self._frame_timer.stop()
        self._sleep_timer.stop()
        self._recover_timer.stop()
        self._raise_timer.stop()
        logger.debug("[PixelPet] 已清理")
