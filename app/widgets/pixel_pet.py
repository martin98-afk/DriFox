# -*- coding: utf-8 -*-
"""
像素小狐桌宠 v2 — 更流畅、更智能、更多样

状态：
  idle          - 空闲呼吸（默认）
  thinking      - 思考中（眼球转动 + 思考点）
  streaming     - 回复中（说话 + 摇尾）
  question      - 等待用户回答（歪头 + 问号弹跳）
  success       - 任务完成（星星眼 + 烟花 + 爱心）
  error         - 出错了（发抖 + 泪滴）
  sleeping      - 长时间无活动（闭眼 + Zzz）
  writing       - 正在写作/编码（眼镜 + 笔尖点动）
  thinking_hard - 深度思考（流汗 + 快速转眼）
  excited       - 连续成功兴奋（跳跃 + 音符 + 星星眼）

子状态（无需独立 spritesheet 行）：
  napping       - 深夜沉睡（sleeping 帧 + 更慢间隔）
  greeting      - 入场欢迎（位置动画 + success 帧）

交互：
  · 单击 → 随机反馈（抬头/歪头/蹭手）
  · 拖拽 → 惯性滑动
  · 快速连击 → 蹭蹭动画（眯眼 + 爱心）
  · 长时间不理 → 主动吸引注意

使用：
  pet = PixelPetWidget(parent)
  pet.set_state("thinking")       # 切换到思考状态
  pet.set_state("idle")           # 恢复正常
"""

import logging
import random
from pathlib import Path

from PyQt5.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QPoint,
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
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

# 状态 → 行索引（扩展为 10 行）
STATE_ROWS = {
    "idle": 0,
    "thinking": 1,
    "streaming": 2,
    "question": 3,
    "success": 4,
    "error": 5,
    "sleeping": 6,
    "writing": 7,
    "thinking_hard": 8,
    "excited": 9,
}

# 每状态帧数（从 8→12）
FRAMES_PER_STATE = 12

# 帧间隔（ms，支持 (min,max) 范围随机取值）
FRAME_INTERVALS = {
    "idle": (200, 350),        # 空闲：缓慢自然呼吸
    "thinking": 200,            # 思考：中等节奏，有深思感
    "streaming": 150,           # 说话：自然语速（12帧×150ms=1.8s循环）
    "question": (220, 300),     # 提问：带疑惑感，不能太快
    "success": 120,             # 成功：轻快喜悦
    "error": 100,               # 错误：颤抖但可看清
    "sleeping": 400,            # 睡眠：缓慢
    "writing": 200,             # 写作：专注节奏
    "thinking_hard": 150,       # 深度思考：稍快但不鬼畜
    "excited": 110,             # 兴奋：活泼但不过度
    # 子状态
    "napping": 500,
}

# 空闲超过此时间自动进入睡眠 (ms)
SLEEP_TIMEOUT_MS = 60_000  # 1 分钟

# 成功/错误状态持续后恢复 idle (ms)
RECOVER_MS = 2500

# 深夜时段（napping）
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 6

# 空闲行为配置
IDLE_BEHAVIOR_INTERVAL_MS = 10_000  # 每隔 10s 检查一次随机行为
IDLE_BEHAVIOR_DURATION_MS = 2000   # 行为持续约 2s
ATTENTION_TIMEOUT_MS = 300_000     # 5 分钟无交互 → 主动吸引注意

# 惯性滑动参数
INERTIA_DECAY = 0.92
INERTIA_MIN_VELOCITY = 0.5


class PixelPetWidget(QWidget):
    """像素小狐桌宠 v2 — 增强版浮动互动吉祥物"""

    state_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        # ── 核心状态 ──
        self._current_state = "idle"
        self._frame_index = 0
        self._spritesheet: QPixmap | None = None
        self._dragging = False
        self._drag_offset = QPoint()
        self._drag_velocity = QPoint(0, 0)  # 惯性用
        self._inertia_timer = QTimer(self)
        self._inertia_timer.timeout.connect(self._apply_inertia)

        # ── 空闲行为 ──
        self._idle_behavior_active = False
        self._idle_behavior_frame = 0
        self._idle_behavior_type = None
        self._last_interaction_time = 0  # timestamp via QElapsedTimer
        self._click_count = 0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._reset_click_count)
        self._attention_shown = False

        # ── AI 状态联动 ──
        self._last_ai_state = "idle"
        self._thinking_start_time = None
        self._success_streak = 0
        self._error_streak = 0

        # ── 性能 ──
        self._frame_timer_elapsed = QElapsedTimer()
        self._frame_timer_elapsed.start()

        # 加载 spritesheet
        self._load_spritesheet()

        # 外观
        self._current_scale = SCALE
        self._update_display_size()
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.PointingHandCursor)

        # 强力 Z-order
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._force_raise)
        self._raise_timer.start(100)  # 降低频率减负
        if parent:
            parent.installEventFilter(self)

        # 动画 Timer
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._advance_frame)
        self._start_frame_timer("idle")

        # 睡眠检测 + 空闲行为
        self._sleep_timer = QTimer(self)
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(self._enter_sleep)

        self._idle_behavior_timer = QTimer(self)
        self._idle_behavior_timer.setSingleShot(True)
        self._idle_behavior_timer.timeout.connect(self._try_idle_behavior)

        self._attention_timer = QTimer(self)
        self._attention_timer.setSingleShot(True)
        self._attention_timer.timeout.connect(self._seek_attention)

        # 恢复 Timer
        self._recover_timer = QTimer(self)
        self._recover_timer.setSingleShot(True)
        self._recover_timer.timeout.connect(lambda: self.set_state("idle"))

        # 过渡动画跟踪
        self._transition_anim = None
        self._transition_opacity = 1.0

        # 位置动画
        self._position_anim = None

        # 启动计时
        self._reset_sleep_timer()
        self._reset_idle_behavior_timer()
        self._attention_timer.start(ATTENTION_TIMEOUT_MS)
        self._last_interaction_time = 0
        self._interaction_timer = QElapsedTimer()
        self._interaction_timer.start()

        # 初始状态
        self._check_night_mode()
        if parent:
            self.raise_()

        # 入场欢迎（稍后执行，让布局完成）
        QTimer.singleShot(500, self._play_greeting)

        # 监听配置变化
        self._connect_config_signals()

        logger.debug(f"[PixelPet] v2 初始化完成，状态: idle ({DISPLAY_SIZE}×{DISPLAY_SIZE})")

    # ═══════════════════════════════════════════════════════════
    # 尺寸管理
    # ═══════════════════════════════════════════════════════════

    def _update_display_size(self) -> None:
        """根据当前缩放更新显示尺寸"""
        from app.utils.config import Settings
        size_map = {"small": 2, "medium": 3, "large": 4}
        cfg_size = Settings.get_instance().pet_size.value
        self._current_scale = size_map.get(cfg_size, 3)
        self.setFixedSize(FRAME_SIZE * self._current_scale, FRAME_SIZE * self._current_scale)

    def _get_display_scale(self) -> int:
        return self._current_scale

    # ═══════════════════════════════════════════════════════════
    # Spritesheet 加载
    # ═══════════════════════════════════════════════════════════

    def _load_spritesheet(self) -> None:
        try:
            if SPRITESHEET.exists():
                sp = QPixmap(str(SPRITESHEET))
                if sp.isNull():
                    logger.warning(f"[PixelPet] spritesheet 无效: {SPRITESHEET}")
                    self._spritesheet = None
                else:
                    self._spritesheet = sp
                    logger.debug(f"[PixelPet] spritesheet 加载成功 ({sp.width()}×{sp.height()})")
            else:
                logger.warning(f"[PixelPet] spritesheet 不存在: {SPRITESHEET}")
                self._spritesheet = None
        except Exception as e:
            logger.warning(f"[PixelPet] 加载 spritesheet 失败: {e}")
            self._spritesheet = None

    # ═══════════════════════════════════════════════════════════
    # 帧间隔管理
    # ═══════════════════════════════════════════════════════════

    def _get_interval(self, state: str) -> int:
        """获取帧间隔，支持范围随机"""
        interval = FRAME_INTERVALS.get(state, 150)
        if isinstance(interval, (tuple, list)):
            return random.randint(interval[0], interval[1])
        return interval

    def _start_frame_timer(self, state: str) -> None:
        """启动帧 Timer"""
        interval = self._get_interval(state)
        self._frame_timer.setInterval(interval)
        if not self._frame_timer.isActive():
            self._frame_timer.start()

    # ═══════════════════════════════════════════════════════════
    # 信号驱动 — AI 状态变化响应
    # ═══════════════════════════════════════════════════════════

    def _on_ai_state_changed(self, ai_state: str) -> None:
        """接收 main_widget.ai_state_changed 信号"""
        if ai_state == self._last_ai_state:
            return

        prev = self._last_ai_state
        self._last_ai_state = ai_state

        if ai_state == "idle":
            if prev in ("streaming", "thinking"):
                self._success_streak += 1
                if self._success_streak >= 3:
                    self.set_state("excited")
                else:
                    self.set_state("success")
            else:
                self.set_state("idle")
        elif ai_state == "error":
            self._error_streak += 1
            self._success_streak = 0
            self.set_state("error")
        elif ai_state == "thinking":
            self._success_streak = 0
            self.set_state("thinking")
        elif ai_state == "streaming":
            self._success_streak = 0
            self.set_state("streaming")
        elif ai_state == "question":
            self._success_streak = 0
            self.set_state("question")
        else:
            self.set_state(ai_state)

    def set_state(self, state: str) -> None:
        """切换桌宠动画状态"""
        # napping 映射到 sleeping 帧
        if state == "napping":
            state = "sleeping"

        if state not in STATE_ROWS:
            logger.warning(f"[PixelPet] 未知状态: {state}")
            return

        if state == self._current_state:
            return

        old_state = self._current_state
        self._current_state = state

        # 帧索引重置
        if state == "idle":
            self._frame_index = random.randint(0, FRAMES_PER_STATE - 1)
            self._idle_behavior_active = False
        else:
            self._frame_index = 0

        # 更新帧间隔
        self._start_frame_timer(state)

        # 特殊状态处理
        if state in ("success", "error"):
            self._recover_timer.start(RECOVER_MS)
        elif state == "question":
            self._play_bounce_small()
        elif state == "sleeping":
            self._sleep_timer.stop()
        else:
            self._reset_sleep_timer()

        # 过渡动画（非紧急切换）
        if old_state not in ("error",) and state not in ("error",):
            self._play_transition(old_state, state)
        else:
            # error 瞬间切换
            self.update()

        # thinking_hard 检测
        if state == "thinking":
            self._check_thinking_hard()

        # 重置空闲行为计时
        self._reset_idle_behavior_timer()
        self._reset_attention_timer()

        self.state_changed.emit(state)
        logger.debug(f"[PixelPet] 状态切换: {old_state} → {state}")

    def get_state(self) -> str:
        return self._current_state

    # ═══════════════════════════════════════════════════════════
    # 思考深度检测
    # ═══════════════════════════════════════════════════════════

    def _check_thinking_hard(self) -> None:
        """启动延迟检测：5秒后如果还在 thinking，升级为 thinking_hard"""
        self._thinking_hard_checker = QTimer(self)
        self._thinking_hard_checker.setSingleShot(True)
        self._thinking_hard_checker.timeout.connect(self._do_upgrade_thinking)
        self._thinking_hard_checker.start(5000)

    def _do_upgrade_thinking(self) -> None:
        """5秒后检查是否需要升级为 thinking_hard"""
        if self._current_state == "thinking":
            self.set_state("thinking_hard")

    # ═══════════════════════════════════════════════════════════
    # 过渡动画
    # ═══════════════════════════════════════════════════════════

    def _play_transition(self, old_state: str, new_state: str) -> None:
        """状态切换时的微过渡：Y轴小弹跳 + 透明度呼吸"""
        self._transition_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._transition_anim.setDuration(120)
        self._transition_anim.setKeyValueAt(0, self.windowOpacity())
        self._transition_anim.setKeyValueAt(0.5, 0.85)
        self._transition_anim.setKeyValueAt(1, 1.0)
        self._transition_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._transition_anim.start()
        if not hasattr(self, "_animations"):
            self._animations = []
        self._animations.append(self._transition_anim)

    # ═══════════════════════════════════════════════════════════
    # 睡眠/唤醒/深夜模式
    # ═══════════════════════════════════════════════════════════

    def _reset_sleep_timer(self) -> None:
        if self._current_state in ("success", "error", "sleeping"):
            return
        self._sleep_timer.stop()
        self._sleep_timer.start(SLEEP_TIMEOUT_MS)

    def _enter_sleep(self) -> None:
        if self._current_state == "idle":
            if self._is_night_time():
                self.set_state("napping")  # 映射到 sleeping
            else:
                self.set_state("sleeping")

    def _check_night_mode(self) -> None:
        """检查当前是否为深夜，自动进入 napping"""
        if self._is_night_time() and self._current_state == "idle":
            self.set_state("napping")

    def _is_night_time(self) -> bool:
        """判断当前是否为深夜时段 23:00~06:00"""
        from datetime import datetime
        h = datetime.now().hour
        return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR

    def wake_up(self) -> None:
        if self._current_state in ("sleeping",):
            self.set_state("idle")
        elif self._current_state in ("napping",):
            self.set_state("greeting")  # 映射到 idle

    # ═══════════════════════════════════════════════════════════
    # 入场动画
    # ═══════════════════════════════════════════════════════════

    def _play_greeting(self) -> None:
        """入场欢迎：从下方弹入"""
        if self.parent() and self._current_state == "idle":
            pw, ph = self.parent().width(), self.parent().height()
            target_y = ph - self.height() - self._get_input_area_height() - 8
            target_x = pw - self.width() - 12
            self.move(target_x, ph)  # 从底部开始
            anim = QPropertyAnimation(self, b"geometry", self)
            anim.setDuration(400)
            start_geo = QRect(target_x, ph, self.width(), self.height())
            end_geo = QRect(target_x, target_y, self.width(), self.height())
            anim.setStartValue(start_geo)
            anim.setEndValue(end_geo)
            anim.setEasingCurve(QEasingCurve.OutBack)
            anim.start()
            if not hasattr(self, "_animations"):
                self._animations = []
            self._animations.append(anim)
            # 短暂显示 success 帧作为欢迎
            self.set_state("success")
            logger.debug("[PixelPet] 入场欢迎动画")

    # ═══════════════════════════════════════════════════════════
    # 帧动画
    # ═══════════════════════════════════════════════════════════

    def _advance_frame(self) -> None:
        """推进到下一帧，含空闲行为逻辑"""
        if self._current_state == "idle" and not self._idle_behavior_active:
            # 空闲时偶尔停留增加自然感（权重停留）
            if random.random() < 0.10:
                return  # 10% 概率停留一帧
        self._frame_index = (self._frame_index + 1) % FRAMES_PER_STATE
        self.update()

    def _current_frame_rect(self) -> QRect:
        row = STATE_ROWS.get(self._current_state, 0)
        x = self._frame_index * FRAME_SIZE
        y = row * FRAME_SIZE
        return QRect(x, y, FRAME_SIZE, FRAME_SIZE)

    # ═══════════════════════════════════════════════════════════
    # 空闲行为系统
    # ═══════════════════════════════════════════════════════════

    def _reset_idle_behavior_timer(self) -> None:
        from app.utils.config import Settings
        behavior = Settings.get_instance().pet_idle_behavior.value
        if behavior == "minimal":
            return  # 无空闲行为
        interval = IDLE_BEHAVIOR_INTERVAL_MS
        if behavior == "active":
            interval = 5000  # 活跃模式更频繁
        self._idle_behavior_timer.stop()
        self._idle_behavior_timer.start(interval)

    def _try_idle_behavior(self) -> None:
        """尝试触发随机空闲行为"""
        if self._current_state != "idle" or self._idle_behavior_active:
            self._reset_idle_behavior_timer()
            return

        behaviors = [
            ("blink", 40),       # 40% 眨眨眼（高频率）
            ("look_around", 20), # 20% 四处张望
            ("yawn", 12),        # 12% 打哈欠
            ("stretch", 8),      # 8% 伸懒腰
            ("groom", 8),        # 8% 理毛
            ("tail_play", 7),    # 7% 玩尾巴
            ("head_tilt", 5),    # 5% 歪头看用户
        ]

        r = random.randint(1, 100)
        cumulative = 0
        chosen = None
        for name, weight in behaviors:
            cumulative += weight
            if r <= cumulative:
                chosen = name
                break

        if not chosen:
            self._reset_idle_behavior_timer()
            return

        self._idle_behavior_active = True
        self._idle_behavior_type = chosen
        self._idle_behavior_frame = 0

        # 不同行为持续不同的帧数
        duration_map = {
            "blink": 3,          # 快速眨眼
            "look_around": 8,    # 左右看
            "yawn": 6,           # 哈欠
            "stretch": 8,        # 伸懒腰
            "groom": 10,         # 理毛
            "tail_play": 8,      # 玩尾巴
            "head_tilt": 6,      # 歪头
        }
        behavior_frames = duration_map.get(chosen, 6)

        # 播放行为动画（重写帧索引循环）
        self._play_behavior_frames(chosen, behavior_frames)
        logger.debug(f"[PixelPet] 空闲行为: {chosen}")

    def _play_behavior_frames(self, behavior: str, total_frames: int) -> None:
        """播放指定空闲行为的帧序列"""
        # 对于空闲行为，我们利用现有的 sprite 帧做重新排序
        # 而不是真的画新帧
        # 但不改变 _frame_index，因为 idle 的 12 帧已经包含了各种动作
        # 我们通过_advance_frame来控制步伐
        # 行为结束后恢复
        self._idle_behavior_frame = 0

        def behavior_step():
            if self._current_state != "idle":
                self._idle_behavior_active = False
                self._reset_idle_behavior_timer()
                return
            self._idle_behavior_frame += 1
            self._frame_index = (self._frame_index + 1) % FRAMES_PER_STATE
            self.update()
            if self._idle_behavior_frame >= total_frames:
                self._idle_behavior_active = False
                self._idle_behavior_type = None
                self._reset_idle_behavior_timer()
                return
            # 行为期间用更快的帧间隔
            QTimer.singleShot(100, behavior_step)

        QTimer.singleShot(50, behavior_step)

    # ═══════════════════════════════════════════════════════════
    # 注意力系统
    # ═══════════════════════════════════════════════════════════

    def _reset_attention_timer(self) -> None:
        self._attention_timer.stop()
        self._attention_timer.start(ATTENTION_TIMEOUT_MS)
        self._attention_shown = False

    def _seek_attention(self) -> None:
        """长时间无交互，主动吸引注意"""
        if self._current_state in ("idle", "sleeping") and not self._attention_shown:
            self._attention_shown = True
            # 小跳一下吸引注意
            self._play_bounce()
            logger.debug("[PixelPet] 主动吸引注意")

    # ═══════════════════════════════════════════════════════════
    # 绘制
    # ═══════════════════════════════════════════════════════════

    def paintEvent(self, event: object) -> None:
        self.raise_()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.LosslessImageRendering, True)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)

        if self._spritesheet and not self._spritesheet.isNull():
            src = self._current_frame_rect()
            painter.drawPixmap(self.rect(), self._spritesheet, src)
        else:
            painter.fillRect(self.rect(), Qt.transparent)

        painter.end()

    # ═══════════════════════════════════════════════════════════
    # 动效
    # ═══════════════════════════════════════════════════════════

    def _play_bounce(self) -> None:
        """弹跳动画"""
        anim = QPropertyAnimation(self, b"geometry", self)
        geo = self.geometry()
        anim.setDuration(350)
        anim.setKeyValueAt(0, geo)
        anim.setKeyValueAt(0.2, QRect(geo.x(), geo.y() - 2, geo.width(), geo.height()))
        anim.setKeyValueAt(0.5, QRect(geo.x(), geo.y() - 3, geo.width(), geo.height()))
        anim.setKeyValueAt(0.8, QRect(geo.x(), geo.y() - 1, geo.width(), geo.height()))
        anim.setKeyValueAt(1, geo)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        if not hasattr(self, "_animations"):
            self._animations = []
        self._animations.append(anim)

    def _play_bounce_small(self) -> None:
        """小弹跳（比 bounce 更低更柔和）"""
        anim = QPropertyAnimation(self, b"geometry", self)
        geo = self.geometry()
        anim.setDuration(250)
        anim.setKeyValueAt(0, geo)
        anim.setKeyValueAt(0.3, QRect(geo.x(), geo.y() - 2, geo.width(), geo.height()))
        anim.setKeyValueAt(0.6, QRect(geo.x(), geo.y() - 1, geo.width(), geo.height()))
        anim.setKeyValueAt(1, geo)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        if not hasattr(self, "_animations"):
            self._animations = []
        self._animations.append(anim)

    def _play_cuddle(self) -> None:
        """快速连击 → 蹭蹭动画"""
        self._reset_interaction_timer()
        anim = QPropertyAnimation(self, b"geometry", self)
        geo = self.geometry()
        anim.setDuration(400)
        anim.setKeyValueAt(0, geo)
        anim.setKeyValueAt(0.15, QRect(geo.x(), geo.y() - 1, geo.width(), geo.height()))
        anim.setKeyValueAt(0.3, QRect(geo.x() + 2, geo.y(), geo.width(), geo.height()))
        anim.setKeyValueAt(0.5, QRect(geo.x(), geo.y() - 1, geo.width(), geo.height()))
        anim.setKeyValueAt(0.7, QRect(geo.x() - 1, geo.y(), geo.width(), geo.height()))
        anim.setKeyValueAt(1, geo)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        if not hasattr(self, "_animations"):
            self._animations = []
        self._animations.append(anim)
        # 短暂闪烁爱心效果（改变状态到 success 帧再回来）
        if self._current_state == "idle":
            old = self._current_state
            self._frame_index = 4  # success 的爱心帧
            self.update()
            QTimer.singleShot(400, lambda: self.update())
        logger.debug("[PixelPet] 蹭蹭~")

    def _reset_interaction_timer(self) -> None:
        self._interaction_timer.restart()

    # ═══════════════════════════════════════════════════════════
    # 鼠标事件 — 增强交互
    # ═══════════════════════════════════════════════════════════

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_offset = event.pos()
            self._drag_velocity = QPoint(0, 0)
            self._inertia_timer.stop()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if self._dragging:
            new_pos = self.mapToParent(event.pos()) - self._drag_offset
            if self.parent():
                pw = self.parent().width()
                ph = self.parent().height()
                new_x = max(0, min(new_pos.x(), pw - self.width()))
                new_y = max(0, min(new_pos.y(), ph - self.height()))
                # 计算速度用于惯性
                if hasattr(self, "_last_pos"):
                    self._drag_velocity = QPoint(
                        new_x - self._last_pos.x(),
                        new_y - self._last_pos.y(),
                    )
                self._last_pos = QPoint(new_x, new_y)
                self.move(new_x, new_y)
                self._reset_interaction_timer()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.LeftButton:
            was_dragging = self._dragging
            self._dragging = False

            # 惯性滑动
            vel = self._drag_velocity
            if was_dragging and (abs(vel.x()) > 3 or abs(vel.y()) > 3):
                self._inertia_velocity = vel
                self._inertia_timer.start(16)  # ~60fps
            else:
                self.setCursor(Qt.PointingHandCursor)

            # 点击检测（仅在拖拽距离很小时视为点击）
            if was_dragging and self._drag_offset:
                dx = abs(event.pos().x() - self._drag_offset.x())
                dy = abs(event.pos().y() - self._drag_offset.y())
                if dx < 4 and dy < 4:
                    self._handle_click()
            self._reset_interaction_timer()

    def _apply_inertia(self) -> None:
        """惯性滑动衰减"""
        if not hasattr(self, "_inertia_velocity"):
            self._inertia_timer.stop()
            return
        vx = int(self._inertia_velocity.x() * INERTIA_DECAY)
        vy = int(self._inertia_velocity.y() * INERTIA_DECAY)
        self._inertia_velocity = QPoint(vx, vy)

        if abs(vx) < INERTIA_MIN_VELOCITY and abs(vy) < INERTIA_MIN_VELOCITY:
            self._inertia_timer.stop()
            self.setCursor(Qt.PointingHandCursor)
            return

        if self.parent():
            pw = self.parent().width()
            ph = self.parent().height()
            new_x = max(0, min(self.x() + vx, pw - self.width()))
            new_y = max(0, min(self.y() + vy, ph - self.height()))
            self.move(new_x, new_y)

    def _handle_click(self) -> None:
        """点击反馈：不同区域不同反应"""
        if self._current_state == "sleeping":
            self.wake_up()
            return

        self._reset_interaction_timer()
        self._reset_sleep_timer()

        # 快速连击检测
        self._click_count += 1
        self._click_timer.stop()
        self._click_timer.start(600)  # 600ms 内点击超过 3 次算连击

        if self._click_count >= 3:
            self._play_cuddle()
            self._click_count = 0
            return

        # 随机反应
        reactions = [
            self._play_bounce,           # 弹跳
            self._play_bounce_small,     # 小弹跳
            self._play_head_tilt,        # 歪头
        ]
        random.choice(reactions)()
        logger.debug(f"[PixelPet] 点击反馈 (count={self._click_count})")

    def _play_head_tilt(self) -> None:
        """歪头反应"""
        if self._current_state != "idle":
            self._play_bounce_small()
            return
        # 临时切换到 question 状态一帧再回来
        old_frame = self._frame_index
        self._frame_index = 6  # question 歪头帧
        self.update()
        QTimer.singleShot(300, lambda: self._restore_idle_frame(old_frame))

    def _restore_idle_frame(self, old_frame: int) -> None:
        if self._current_state == "idle":
            self._frame_index = old_frame
            self.update()

    def _reset_click_count(self) -> None:
        self._click_count = 0

    # ═══════════════════════════════════════════════════════════
    # Z-order 保障
    # ═══════════════════════════════════════════════════════════

    def _force_raise(self) -> None:
        """定时器回调：强制提到最上层 + 检测父窗口尺寸变化"""
        p = self.parent()
        if p:
            self.raise_()
            pw, ph = p.width(), p.height()
            if hasattr(self, "_last_parent_size"):
                if (pw, ph) != self._last_parent_size:
                    self.resize_handle(pw, ph)
            self._last_parent_size = (pw, ph)

    def eventFilter(self, obj: object, event: object) -> bool:
        self.raise_()
        return super().eventFilter(obj, event)

    # ═══════════════════════════════════════════════════════════
    # 位置自适应
    # ═══════════════════════════════════════════════════════════

    def _get_input_area_height(self) -> int:
        """动态获取底部输入区域高度"""
        if not self.parent():
            return 120
        try:
            # 查找底部输入区域控件
            for child in self.parent().findChildren((QWidget,), options=Qt.FindChildrenRecursively):
                name = child.objectName() or ""
                if "input" in name.lower() or "bottom" in name.lower():
                    if child.isVisible() and child.y() > self.parent().height() // 2:
                        return child.height() + 20
        except Exception:
            pass
        return 120

    def resize_handle(self, parent_width: int, parent_height: int) -> None:
        """父窗口大小变化时平滑移动"""
        if self._dragging:
            return
        input_h = self._get_input_area_height()
        margin = 12
        target_x = parent_width - self.width() - margin
        target_y = parent_height - self.height() - input_h - 8

        # 如果距离当前位置较远则动画移动
        if abs(self.x() - target_x) > 20 or abs(self.y() - target_y) > 20:
            self._animate_to(target_x, target_y, duration=300)
        else:
            self.move(target_x, target_y)

    def _animate_to(self, x: int, y: int, duration: int = 300) -> None:
        """平滑移动到目标位置"""
        self._position_anim = QPropertyAnimation(self, b"geometry", self)
        self._position_anim.setDuration(duration)
        self._position_anim.setStartValue(self.geometry())
        self._position_anim.setEndValue(QRect(x, y, self.width(), self.height()))
        self._position_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._position_anim.start()
        if not hasattr(self, "_animations"):
            self._animations = []
        self._animations.append(self._position_anim)

    # ═══════════════════════════════════════════════════════════
    # 尺寸变化响应
    # ═══════════════════════════════════════════════════════════

    def on_pet_size_changed(self) -> None:
        """配置中 pet_size 变化时调用"""
        old_scale = self._current_scale
        self._update_display_size()
        # 重新定位
        if self.parent():
            QTimer.singleShot(100, lambda: self.resize_handle(
                self.parent().width(), self.parent().height()
            ))

    # ═══════════════════════════════════════════════════════════
    # 配置信号连接
    # ═══════════════════════════════════════════════════════════

    def _connect_config_signals(self) -> None:
        """监听配置变化，动态响应"""
        try:
            from app.utils.config import Settings
            cfg = Settings.get_instance()
            cfg.pet_size.valueChanged.connect(self.on_pet_size_changed)
            cfg.pet_idle_behavior.valueChanged.connect(self._reset_idle_behavior_timer)
        except Exception as e:
            logger.debug(f"[PixelPet] 配置信号连接失败: {e}")

    # ═══════════════════════════════════════════════════════════
    # 生命周期
    # ═══════════════════════════════════════════════════════════

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self.raise_()

    def cleanup(self) -> None:
        self._frame_timer.stop()
        self._sleep_timer.stop()
        self._recover_timer.stop()
        self._raise_timer.stop()
        self._idle_behavior_timer.stop()
        self._attention_timer.stop()
        self._inertia_timer.stop()
        self._click_timer.stop()
        logger.debug("[PixelPet] v2 已清理")
