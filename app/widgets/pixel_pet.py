# -*- coding: utf-8 -*-
"""
像素小狐桌宠 v2 — 更流畅、更智能、更多样

状态：
  idle          - 空闲呼吸（默认）
  thinking      - 思考中（眼球转动 + 思考点）
  streaming     - 回复中（说话 + 摇尾）
  question      - 等待用户回答（歪头 + 问号弹跳）
  success       - 任务完成（星星眼 + 烟花 + 爱心）
  error         - 😭 出错了（大哭 + 双侧泪痕 + 抖动 + 红色边框）
  sleeping      - 长时间无活动（闭眼 + Zzz），有概率自动醒来
  writing       - 正在写作/编码（眼镜 + 笔尖点动）
  thinking_hard - 深度思考（流汗 + 快速转眼）
  excited       - 连续成功兴奋（跳跃 + 音符 + 星星眼）
  confused      - 疑惑不解（问号表情）
  surprised     - 惊讶一瞥
  dizzy         - 眩晕冒金星
  crying        - 持续大哭（与 error 同帧，更持久的哭泣）
  reading       - ★ 看书（胸前打开的书 + 翻页动画 + 偶尔抬头思考，空闲随机触发）
  music         - ★ 戴耳机听音乐（头顶耳机 + 音符飘出 + 律动，空闲随机触发）
  wakeup        - ★ 睡醒过渡（闭眼→哈欠→伸懒腰→清醒，睡眠醒来时播放）

子状态（无需独立 spritesheet 行）：
  napping       - 深夜沉睡（sleeping 帧 + 更慢间隔）
  greeting      - 入场欢迎（位置动画 + success 帧）

交互：
  · 单击 → 随机反馈（抬头/歪头/蹭手）
  · 拖拽 → 惯性滑动
  · 快速连击 → 蹭蹭动画（眯眼 + 爱心）
  · 长时间不理 → 主动吸引注意
  · 单击睡眠中的桌宠 → 播放 wakeup 动画后苏醒

使用：
  pet = PixelPetWidget(parent)
  pet.set_state("thinking")       # 切换到思考状态
  pet.set_state("idle")           # 恢复正常
"""

import math
import random
import sys
from pathlib import Path

import shiboken6 as sip
from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget
from loguru import logger


# =============================================================================
# 配置常量
# =============================================================================

FRAME_SIZE = 16  # 每帧像素尺寸
SCALE = 3  # 渲染倍数 → 48×48 显示
DISPLAY_SIZE = FRAME_SIZE * SCALE  # 48
BADGE_HEIGHT = 18  # ★ 顶部情绪徽章预留高度（像素）

# 状态 → 行索引（扩展为 15 行）
# Row 0: 基础待机      Row 1: 眨眼环顾     Row 2: 专注/严肃
# Row 3: 享受/闭眼     Row 4: 眩晕冒金星   Row 5: 流汗惊恐
# Row 6: 疑惑问号      Row 7: 专注写作     Row 8: 尴尬小紧张
# Row 9: 兴奋魔法特效  Row 10: 挣扎扭动    Row 11: 警示紧张
# Row 12: 看书阅读     Row 13: 戴耳机听音乐 Row 14: 睡醒过渡
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
    "reading": 12,      # ★ 看书（Row 12 胸前书+翻页）
    "dragging": 10,      # ★ 挣扎（Row 10 扭动 + 嘴张）
    "warning": 11,       # ★ 警示（Row 11 紧张 + 头顶"!"号）
    "confused": 6,       # ★ 疑惑（Row 6 问号），与 sleeping 共用行
    "surprised": 8,      # ★ 惊讶（Row 8 尴尬小紧张），与 thinking_hard 共用行
    "dizzy": 4,          # ★ 眩晕（Row 4 冒金星），与 success 共用行
    "crying": 5,         # ★ 大哭（Row 5 流汗惊恐），与 error 共用行
    "music": 13,         # ★ 戴耳机听音乐（Row 13 耳机 + 音符 + 律动）
    "wakeup": 14,        # ★ 睡醒过渡（Row 14 闭眼→哈欠→伸懒腰→清醒）
    "sleep": 15,         # ★ 入睡过渡（Row 15 站立→哈欠→眯眼→趴下→入睡）
}

# 睡眠子状态映射（深夜用 napping 表现更深度的睡眠）
SLEEP_SUB_MAP = {
    "napping": "sleeping",
    "deep_sleep": "sleeping",
}

# 每状态帧数（从 8→12）
FRAMES_PER_STATE = 12

# 帧率上限保护：最短帧间隔（≈30fps）。当前 FRAME_INTERVALS 最小 80ms，
# 此处为无操作化安全网，防止后续误将帧间隔调到极小值导致 paintEvent 无节流超频重绘。
_MIN_FRAME_INTERVAL_MS = 33

# 帧间隔（ms，支持 (min,max) 范围随机取值）
FRAME_INTERVALS = {
    "idle": (200, 350),        # 空闲：缓慢自然呼吸
    "thinking": 200,            # 思考：中等节奏，有深思感
    "streaming": 150,           # 说话：自然语速（12帧×150ms=1.8s循环）
    "question": (220, 300),     # 提问：带疑惑感，不能太快
    "success": 120,             # 成功：轻快喜悦
    "error": 100,               # 错误：颤抖但可看清
    "sleeping": 200,            # 睡眠：缓慢
    "writing": 200,             # 写作：专注节奏
    "thinking_hard": 250,       # 深度思考：稍快但不鬼畜
    "excited": 110,             # 兴奋：活泼但不过度
    "reading": 250,            # ★ 看书：缓慢有节奏，沉浸阅读
    "dragging": 80,             # ★ 挣扎：快速帧（比 error 还快，传达慌张）
    "warning": 150,             # ★ 警示：适中节奏，传达警觉感
    "confused": (250, 350),     # ★ 疑惑：缓慢困惑，问号浮现
    "surprised": 90,            # ★ 惊讶：快速闪现
    "dizzy": 130,               # ★ 眩晕：冒金星节奏
    "crying": 150,              # ★ 哭泣：比正常 error 稍慢
    "music": 220,               # ★ 听音乐：舒缓律动节奏
    "wakeup": 180,              # ★ 睡醒：渐进苏醒，不急不缓
    "sleep": 180,               # ★ 入睡：缓慢犯困，渐入梦乡
    # 子状态
    "napping": 200,
}

# 空闲超过此时间自动进入睡眠 (ms) — 随机化，不再固定间隔
SLEEP_TIMEOUT_MIN_MS = 45_000   # 最早 45s
SLEEP_TIMEOUT_MAX_MS = 120_000  # 最晚 2 分钟

# 成功/错误状态持续后恢复 idle (ms)
RECOVER_MS = 2500

# 深夜时段（napping）
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 6

# 空闲行为配置 — 随机化，间隔加长
IDLE_BEHAVIOR_MIN_MS = 20_000   # 最早 20s
IDLE_BEHAVIOR_MAX_MS = 50_000   # 最晚 50s
ATTENTION_TIMEOUT_MS = 300_000     # 5 分钟无交互 → 主动吸引注意

# ★ 睡眠醒来配置 — 睡着后不会一直睡死，有概率自动醒来
SLEEP_WAKE_CHECK_MIN_MS = 30_000   # 最早 30s 后开始检查是否醒来
SLEEP_WAKE_CHECK_MAX_MS = 90_000   # 最晚 90s 检查一次
SLEEP_WAKE_PROBABILITY = 0.45      # 每次检查有 45% 概率醒来
# 空闲行为持续时长（ms，随机范围）— 多轮循环，沉浸感更强
READING_DURATION_MS = (15_000, 30_000)    # 看书持续 15~30 秒
MUSIC_DURATION_MS = (20_000, 40_000)     # 听音乐持续 20~40 秒
WAKEUP_DURATION_FRAMES = 12        # 睡醒过渡完整一轮（按帧数精确控制，只播一次）
SLEEP_DURATION_FRAMES = 12         # 入睡过渡完整一轮（按帧数精确控制，只播一次）

# 惯性滑动参数
INERTIA_DECAY = 0.92
INERTIA_MIN_VELOCITY = 0.5

# ★ 情绪 emoji 映射（画在桌宠上方，直观显示心情）
STATE_EMOJI = {
    "idle": "😊",
    "thinking": "🤔",
    "streaming": "💬",
    "question": "❓",
    "success": "🎉",
    "error": "😭",
    "sleeping": "💤",
    "writing": "✏️",
    "thinking_hard": "🤔",
    "excited": "✨",
    "dragging": "😖",      # ★ 挣扎：被抓住的窘迫表情
    "warning": "⚠️",       # ★ 警示：重要操作前的提醒
    "confused": "🤷",
    "surprised": "😲",
    "dizzy": "😵",
    "crying": "😭",
    "reading": "📖",       # ★ 看书：开卷有益
    "music": "🎵",         # ★ 听音乐：耳机
    "wakeup": "🌤️",       # ★ 睡醒：清晨阳光
    "sleep": "😴",          # ★ 入睡：犯困睡觉
    "napping": "💤",
}


class PixelPetWidget(QWidget):
    """像素小狐桌宠 v2 — 增强版浮动互动吉祥物"""

    state_changed = Signal(str)

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

        # ★ 状态机增强
        self._state_before_drag = None       # 拖拽前保存的状态
        self._drag_pending_state = None      # ★ 延迟切换状态（从睡眠拖拽时首次移动才切 dragging）
        self._thinking_hard_checker = None   # 思考深度检测计时器引用
        self._wakeup_frame_count = 0         # ★ 睡醒过渡帧计数（走完一轮自动回 idle，不循环）
        self._sleep_frame_count = 0          # ★ 入睡过渡帧计数（走完一轮自动切 sleeping，不循环）

        # ── 错误状态持久化（重试时保持报错） ──
        self._error_persist = False

        # ── 抖动动画（error 状态下更显眼） ──
        self._shake_intensity = 3
        self._shake_frame = 0
        self._shake_original_pos = None
        self._shake_timer = QTimer(self)
        self._shake_timer.timeout.connect(self._apply_shake)

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
        self._raise_timer.start(5000)  # 5秒一次足够维持 Z-order
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

        # ★ 睡眠醒来计时器 — 睡着后定时检查是否自动醒来
        self._sleep_wake_timer = QTimer(self)
        self._sleep_wake_timer.setSingleShot(True)
        self._sleep_wake_timer.timeout.connect(self._check_sleep_wake)

        self._idle_behavior_timer = QTimer(self)
        self._idle_behavior_timer.setSingleShot(True)
        self._idle_behavior_timer.timeout.connect(self._try_idle_behavior)

        self._attention_timer = QTimer(self)
        self._attention_timer.setSingleShot(True)
        self._attention_timer.timeout.connect(self._seek_attention)

        # 恢复 Timer
        self._recover_timer = QTimer(self)
        self._recover_timer.setSingleShot(True)
        self._recover_timer.timeout.connect(self._on_recover)

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

        # ★ 情绪徽章动画
        self._badge_alpha = 0.0       # 0~1.0 淡入淡出
        self._badge_bounce_frame = 0  # 徽章弹跳帧
        self._badge_font = None       # 懒加载

        # ★ 状态过渡粒子
        self._particles = []          # [(x, y, alpha, life), ...]
        self._particle_timer = QTimer(self)
        self._particle_timer.timeout.connect(self._update_particles)
        self._particle_timer.setInterval(50)

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
        """根据当前缩放更新显示尺寸（含顶部徽章空间）"""
        from app.utils.config import Settings
        size_map = {"small": 2, "medium": 3, "large": 4}
        cfg_size = Settings.get_instance().pet_size.value
        self._current_scale = size_map.get(cfg_size, 3)
        self.setFixedSize(
            FRAME_SIZE * self._current_scale,
            FRAME_SIZE * self._current_scale + BADGE_HEIGHT
        )

    def _get_display_scale(self) -> int:
        return self._current_scale

    # ═══════════════════════════════════════════════════════════
    # Spritesheet 加载
    # ═══════════════════════════════════════════════════════════

    def _load_spritesheet(self) -> None:
        """加载 spritesheet：优先文件系统 → fallback Qt 内嵌资源"""
        info = self._resolve_spritesheet()
        source, path = info["source"], info["path"]

        try:
            if source == "theme":
                sp = QPixmap(str(path))
            else:
                sp = QPixmap(path)  # Qt 资源路径如 ":/icons/pet.png"

            if sp.isNull():
                logger.warning(f"[PixelPet] spritesheet 无效: {path}")
                self._spritesheet = None
            else:
                self._spritesheet = sp
                logger.debug(
                    f"[PixelPet] spritesheet 加载成功 ({sp.width()}×{sp.height()}) "
                    f"source={source}"
                )
        except Exception as e:
            logger.exception(f"[PixelPet] 加载 spritesheet 失败: {e}")
            self._spritesheet = None

    @staticmethod
    def _resolve_spritesheet() -> dict:
        """解析桌宠 spritesheet 来源。

        Returns:
            dict: {"source": "theme", "path": Path} 或
                  {"source": "builtin", "path": ":/icons/pet.png"}
        """
        try:
            from app.utils.theme_manager import theme_manager
            pet_cfg = theme_manager.get_theme_pet(
                theme_manager.get_current_theme_id()
            )
            if pet_cfg and pet_cfg.get("image"):
                return {"source": "theme", "path": pet_cfg["image"]}
        except Exception:
            logger.debug("[PixelPet] 主题系统未就绪，使用内嵌默认桌宠")

        return {"source": "builtin", "path": ":/icons/pet.png"}

    def refresh_pet(self) -> None:
        """响应主题热重载：重新加载 spritesheet。

        由 theme_manager.dispatch_refresh() 通过 refresh_theme() 调用。
        """
        logger.debug("[PixelPet] refresh_pet: 重新加载 spritesheet")
        self._spritesheet = None
        self._load_spritesheet()
        self.update()

    def refresh_theme(self) -> None:
        """主题刷新统一入口（ThemeManager.dispatch_refresh 调用）。

        与 refresh_pet 等价：重新加载 spritesheet 以应用主题配色。
        桌宠下沉为 TabManagerWindow 全局浮层后，由 TabManager 统一注册刷新目标。
        """
        self.refresh_pet()

    # ═══════════════════════════════════════════════════════════
    # 帧间隔管理
    # ═══════════════════════════════════════════════════════════

    def _get_interval(self, state: str) -> int:
        """获取帧间隔，支持范围随机；下限 _MIN_FRAME_INTERVAL_MS 帧率上限保护"""
        interval = FRAME_INTERVALS.get(state, 150)
        if isinstance(interval, (tuple, list)):
            interval = random.randint(interval[0], interval[1])
        return max(interval, _MIN_FRAME_INTERVAL_MS)

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
            # ★ 重试中：如果已经是 error 状态，开启持久化模式
            if self._current_state == "error":
                self._error_persist = True
                # 重试期间加速抖动，更显慌张
                self._shake_intensity = min(6, 3 + self._error_streak)
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

        # ★ 离开空闲行为状态(reading/music)时清理标志，防止 state_step 闭包残留
        if old_state in ("reading", "music") and state not in ("reading", "music"):
            self._idle_behavior_active = False
            self._idle_behavior_type = None

        # ★ AI 高优先级状态：停止恢复计时器，防止后续竞态覆盖
        if state in ("thinking", "streaming", "question", "error"):
            self._recover_timer.stop()

        # ★ 离开 thinking/thinking_hard 时销毁思考深度检测计时器
        if old_state in ("thinking", "thinking_hard") and state not in ("thinking", "thinking_hard"):
            self._stop_thinking_hard_timer()

        # ★ 离开 sleeping/napping 时停止睡眠醒来计时器（避免唤醒到已醒状态）
        if old_state in ("sleeping", "napping") and state not in ("sleeping", "napping", "wakeup"):
            self._sleep_wake_timer.stop()

        # ★ 离开 sleep（入睡过渡）时停止帧计数
        if old_state == "sleep" and state not in ("sleep", "sleeping", "napping"):
            self._sleep_frame_count = 0

        # ★ 错误持续：离开错误状态时清除持久化标志
        if state != "error":
            self._error_persist = False

        # ★ 错误状态：启动抖动
        if state == "error":
            self._start_shake()
        elif old_state == "error":
            # 离开错误状态：停止抖动
            self._stop_shake()

        # 帧索引重置
        if state == "idle":
            self._frame_index = random.randint(0, FRAMES_PER_STATE - 1)
            self._idle_behavior_active = False
        else:
            self._frame_index = 0

        # 更新帧间隔
        self._start_frame_timer(state)

        # 特殊状态处理
        if state in ("success", "error", "warning"):
            self._recover_timer.start(RECOVER_MS)
        elif state == "question":
            self._play_bounce_small()
        elif state == "sleeping":
            self._sleep_timer.stop()
            # ★ 睡眠时暂停 raise_timer，降低帧率
            self._raise_timer.stop()
            self._frame_timer.setInterval(500)
            # ★ 启动睡眠醒来检查 — 睡着后不会一直睡死
            self._start_sleep_wake_timer()
        else:
            self._reset_sleep_timer()
            # ★ 非睡眠状态确保 raise_timer 恢复（如果没有被 hide 的话）
            if not self._raise_timer.isActive() and self.isVisible():
                self._raise_timer.start(5000)

        # ★ 快乐状态触发粒子效果
        if state in ("success", "excited", "surprised"):
            self._spawn_particles(8)

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
        # ★ 先销毁旧计时器，避免泄漏
        self._stop_thinking_hard_timer()
        self._thinking_hard_checker = QTimer(self)
        self._thinking_hard_checker.setSingleShot(True)
        self._thinking_hard_checker.timeout.connect(self._do_upgrade_thinking)
        self._thinking_hard_checker.start(5000)

    def _stop_thinking_hard_timer(self) -> None:
        """销毁 thinking_hard 检测计时器（离开 thinking 时调用）"""
        if self._thinking_hard_checker is not None:
            try:
                self._thinking_hard_checker.stop()
            except Exception:
                pass
            self._thinking_hard_checker = None

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

    def _on_recover(self) -> None:
        """恢复计时器回调：重置宠物状态和 AI 状态跟踪，防止状态卡死"""
        # ★ 安全检测：只有当前还是 success/error/warning 才恢复
        # 如果状态已经被 AI 信号切换为 thinking/streaming 等，跳过恢复
        if self._current_state not in ("success", "error", "warning"):
            logger.debug(f"[PixelPet] 恢复跳过：当前状态 {self._current_state} 无需恢复")
            return

        self._last_ai_state = "idle"
        if self._current_state == "success":
            self.set_state("idle")
        elif self._current_state == "error":
            # ★ 错误持久化模式：重试中不自动恢复
            if self._error_persist:
                logger.debug("[PixelPet] 错误持久化中，跳过自动恢复")
                self._recover_timer.start(RECOVER_MS)
                return
            self.set_state("idle")
        elif self._current_state == "warning":
            self.set_state("idle")

    # ═══════════════════════════════════════════════════════════
    # 睡眠/唤醒/深夜模式
    # ═══════════════════════════════════════════════════════════

    def _reset_sleep_timer(self) -> None:
        if self._current_state in ("success", "error", "sleeping"):
            return
        self._sleep_timer.stop()
        self._sleep_timer.start(random.randint(SLEEP_TIMEOUT_MIN_MS, SLEEP_TIMEOUT_MAX_MS))

    def _enter_sleep(self) -> None:
        if self._current_state == "idle":
            # ★ 先播放入睡过渡动画，结束后自动切到 sleeping
            self._play_idle_to_sleep()

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
        """唤醒桌宠 — 单击睡眠中的桌宠时调用，播放 wakeup 过渡动画后苏醒"""
        if self._current_state in ("sleeping", "napping"):
            # ★ 停止睡眠醒来计时器（已被手动唤醒）
            self._sleep_wake_timer.stop()
            # 播放睡醒过渡动画，结束后自动回到 idle
            self._play_wakeup_to_idle()
        elif self._current_state in ("greeting",):
            self._raise_timer.start(5000)

    def _start_sleep_wake_timer(self) -> None:
        """★ 启动睡眠醒来检查计时器（随机 30~90s 后检查）"""
        self._sleep_wake_timer.stop()
        delay = random.randint(SLEEP_WAKE_CHECK_MIN_MS, SLEEP_WAKE_CHECK_MAX_MS)
        self._sleep_wake_timer.start(delay)
        logger.debug(f"[PixelPet] 睡眠醒来检查已排程: {delay}ms 后")

    def _check_sleep_wake(self) -> None:
        """★ 睡眠中定时检查是否自动醒来 — 有概率播放 wakeup 动画后回到 idle"""
        if self._current_state not in ("sleeping", "napping"):
            return
        # 按概率决定是否醒来
        if random.random() < SLEEP_WAKE_PROBABILITY:
            logger.debug("[PixelPet] 睡眠中自动醒来~")
            self._play_wakeup_to_idle()
        else:
            # 没醒，重新排程下一次检查
            self._start_sleep_wake_timer()

    def _play_wakeup_to_idle(self) -> None:
        """★ 播放睡醒过渡动画（wakeup），完成后回到 idle 状态

        通过帧计数器精确控制：走完 WAKEUP_DURATION_FRAMES 帧后由
        _advance_frame 自动切回 idle，不依赖时间估算，避免动画循环
        重复播放"醒"的动作。
        """
        # 切换到 wakeup 状态播放苏醒动画
        self._current_state = "wakeup"
        self._frame_index = 0
        self._wakeup_frame_count = 0  # ★ 帧计数归零，由 _advance_frame 计数推进
        self._start_frame_timer("wakeup")
        # 唤醒时恢复 raise_timer 和正常帧率
        if not self._raise_timer.isActive() and self.isVisible():
            self._raise_timer.start(5000)
        self.update()
        self.state_changed.emit("wakeup")

    def _play_idle_to_sleep(self) -> None:
        """★ 播放入睡过渡动画（sleep），完成后切换到 sleeping 状态

        与 _play_wakeup_to_idle 对称，是入睡的必经过渡步骤。
        通过帧计数器精确控制：走完 SLEEP_DURATION_FRAMES 帧后由
        _advance_frame 自动切到 sleeping，不循环播放。
        """
        # 切换到 sleep 状态播放入睡动画
        self._current_state = "sleep"
        self._frame_index = 0
        self._sleep_frame_count = 0  # ★ 帧计数归零，由 _advance_frame 计数推进
        self._start_frame_timer("sleep")
        self.update()
        self.state_changed.emit("sleep")

    # ═══════════════════════════════════════════════════════════
    # 入场动画
    # ═══════════════════════════════════════════════════════════

    def _play_greeting(self) -> None:
        """入场欢迎：从下方弹入"""
        if self.parent() and self._current_state == "idle":
            pw, ph = self.parent().width(), self.parent().height()
            target_x = pw - self.width() - 8
            # 计算目标 Y：站在发送按钮上
            btn_top = self._get_send_button_top()
            if btn_top is not None:
                target_y = btn_top - 42
            else:
                target_y = ph - self.height() - 100
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
        """推进到下一帧，含空闲行为逻辑 + 粒子更新"""
        # ★ 驱动情绪徽章弹跳
        self._badge_bounce_frame += 1

        # ★ 粒子更新合并到主帧循环
        if self._particles:
            self._update_particles()
            if self._particle_timer.isActive():
                self._particle_timer.stop()  # 确保独立 timer 不运行

        if self._current_state == "idle" and not self._idle_behavior_active:
            # 空闲时偶尔停留增加自然感（权重停留）
            if random.random() < 0.10:
                return  # 10% 概率停留一帧

        # ★ wakeup（睡醒过渡）：精确播放一轮后回到 idle，避免循环重复"醒"动作
        if self._current_state == "wakeup":
            self._wakeup_frame_count += 1
            if self._wakeup_frame_count >= WAKEUP_DURATION_FRAMES:
                self._wakeup_frame_count = 0
                self.set_state("idle")
                return
            self._frame_index = (self._frame_index + 1) % FRAMES_PER_STATE
            self.update()
            return

        # ★ sleep（入睡过渡）：精确播放一轮后切换到 sleeping，避免循环重复"睡"动作
        if self._current_state == "sleep":
            self._sleep_frame_count += 1
            if self._sleep_frame_count >= SLEEP_DURATION_FRAMES:
                self._sleep_frame_count = 0
                # 入睡后根据深夜时段决定 napping 还是 sleeping
                if self._is_night_time():
                    self.set_state("napping")
                else:
                    self.set_state("sleeping")
                return
            self._frame_index = (self._frame_index + 1) % FRAMES_PER_STATE
            self.update()
            return

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
        self._idle_behavior_timer.stop()
        self._idle_behavior_timer.start(random.randint(IDLE_BEHAVIOR_MIN_MS, IDLE_BEHAVIOR_MAX_MS))

    def _try_idle_behavior(self) -> None:
        """尝试触发随机空闲行为 — 只保留看书和听音乐（有独立动画的行为）"""
        if self._current_state != "idle" or self._idle_behavior_active:
            self._reset_idle_behavior_timer()
            return

        behaviors = [
            ("reading", 50),          # 看书（胸前打开的书 + 翻页动画）
            ("music", 50),            # 戴耳机听音乐（沉浸律动）
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

        # 行为持续时长（ms，随机范围）
        duration_map = {
            "reading": READING_DURATION_MS,
            "music": MUSIC_DURATION_MS,
        }
        dur_range = duration_map.get(chosen, (3000, 6000))
        duration_ms = random.randint(dur_range[0], dur_range[1])

        # 切换到对应状态的 spritesheet 行
        state_override_map = {
            "reading": "reading",
            "music": "music",
        }

        override_state = state_override_map.get(chosen)
        if override_state:
            self._play_behavior_with_state(override_state, duration_ms)
        logger.debug(f"[PixelPet] 空闲行为: {chosen} ({duration_ms}ms)")

    def _play_behavior_with_state(self, state_name: str, duration_ms: int) -> None:
        """★ 播放需要临时切换状态的空闲行为（玩耍、听音乐、写作等）

        基于时间控制持续时间，帧动画自然循环（mod 12），到时间后恢复 idle。
        停止主帧定时器，由 state_step 按状态对应间隔独立推进帧，避免双重推进。
        """
        if state_name not in STATE_ROWS:
            self._idle_behavior_active = False
            return

        old_state = self._current_state
        # 保存当前帧索引以便恢复
        old_frame = self._frame_index
        # 切换到目标状态
        self._current_state = state_name
        self._frame_index = 0
        # ★ 停止主帧定时器，改由 state_step 独立按状态间隔推进，避免双重推进
        self._frame_timer.stop()
        self.update()
        self.state_changed.emit(state_name)

        # ★ 使用状态对应的帧间隔（reading=250 / music=220 / writing=200 ...）
        step_interval = self._get_interval(state_name)
        elapsed = QElapsedTimer()
        elapsed.start()

        def state_step():
            # ★ widget 已被销毁时跳过（如窗口关闭后触发的残留 singleShot）
            if not sip.isValid(self):
                return
            # ★ 状态被外部(AI信号等)改变时中止行为，避免闭包覆写新状态
            if self._current_state != state_name:
                self._idle_behavior_active = False
                self._idle_behavior_type = None
                return
            if elapsed.elapsed() >= duration_ms:
                # 恢复为 idle
                self._current_state = old_state
                self._frame_index = old_frame
                self._start_frame_timer("idle")
                self._idle_behavior_active = False
                self._idle_behavior_type = None
                self.update()
                self.state_changed.emit(old_state)
                self._reset_idle_behavior_timer()
                return
            self._frame_index = (self._frame_index + 1) % FRAMES_PER_STATE
            self.update()
            QTimer.singleShot(step_interval, state_step)

        QTimer.singleShot(step_interval, state_step)

    # ═══════════════════════════════════════════════════════════
    # 绘制
    # ═══════════════════════════════════════════════════════════

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.LosslessImageRendering, True)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        # ★ 清屏（用全透明填充，防止拖拽时残留像素）
        painter.fillRect(self.rect(), Qt.transparent)

        # ★ 情绪 emoji 徽章（画在顶部预留区域）
        self._draw_emotion_badge(painter)

        # ★ 绘制 sprite（下移 BADGE_HEIGHT，避开徽章区域）
        if self._spritesheet and not self._spritesheet.isNull():
            src = self._current_frame_rect()
            sprite_rect = QRect(0, BADGE_HEIGHT, self.width(), self.height() - BADGE_HEIGHT)
            painter.drawPixmap(sprite_rect, self._spritesheet, src)
        else:
            painter.fillRect(self.rect(), Qt.transparent)

        # ★ 错误状态：脉冲红色边框
        if self._current_state == "error":
            alpha = 80 + int(40 * math.sin(self._shake_frame * 0.4))
            pen = QPen(QColor(255, 60, 60, alpha), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            # 边框区域也要下移，包住 sprite 区域
            error_rect = QRect(0, BADGE_HEIGHT, self.width(), self.height() - BADGE_HEIGHT)
            painter.drawRect(error_rect.adjusted(0, 0, -1, -1))

        # ★ 持久化错误：更强烈的双边框
        if self._current_state == "error" and self._error_persist:
            outer_alpha = 60 + int(40 * math.sin(self._shake_frame * 0.3 + 1.0))
            pen2 = QPen(QColor(255, 30, 30, outer_alpha), 1)
            painter.setPen(pen2)
            outer_rect = QRect(-2, BADGE_HEIGHT - 2, self.width() + 4, self.height() - BADGE_HEIGHT + 4)
            painter.drawRect(outer_rect)

        # ★ 粒子效果（相对 sprite 区域定位）
        for px, py, pa, plife in self._particles:
            if plife <= 0:
                continue
            c = QColor(255, 235, 59, int(pa * 255))
            painter.setPen(Qt.NoPen)
            painter.setBrush(c)
            size = 2 + int(plife * 2)
            painter.drawEllipse(QPointF(px, py), size, size)

        painter.end()

    def _draw_emotion_badge(self, painter: QPainter) -> None:
        """★ 在桌宠头顶预留区域绘制情绪 emoji"""
        # 获取基础 emoji
        emoji = STATE_EMOJI.get(self._current_state)
        if not emoji:
            return

        # ★ 重试递进：error 状态下根据重试次数升级 emoji
        if self._current_state == "error":
            if self._error_streak <= 2:
                emoji = "😭"
            elif self._error_streak <= 4:
                emoji = "😱"
            else:
                emoji = "💀"

        # 空闲/睡眠等平常状态不显示
        quiet_states = {"idle", "sleeping", "napping", "writing", "thinking"}
        if self._current_state in quiet_states:
            target_alpha = 0.0
        else:
            target_alpha = 0.85

        # 平滑过渡 alpha
        diff = target_alpha - self._badge_alpha
        self._badge_alpha += diff * 0.15
        if abs(diff) < 0.01:
            self._badge_alpha = target_alpha

        if self._badge_alpha < 0.05:
            return

        # 懒加载字体 — 按平台选择系统 emoji 字体
        # 修复:旧代码硬编码 "Segoe UI Emoji" 是 Windows 专属字体,
        # 在 macOS / Linux 上不存在;同时 setStyleStrategy(NoFontMerging)
        # 关闭了 Qt 的字体回退,导致 macOS 上桌宠头顶 emoji 渲染为空白。
        if self._badge_font is None:
            if sys.platform == "darwin":
                emoji_family = "Apple Color Emoji"
            elif sys.platform == "win32":
                emoji_family = "Segoe UI Emoji"
            else:
                # Linux 桌面字体差异大,常见为 Noto Color Emoji
                emoji_family = "Noto Color Emoji"
            self._badge_font = QFont(emoji_family, 10)
            # 不设 NoFontMerging,允许 Qt 在字符缺失时自动回退到其他字体

        # 徽章弹跳偏移（基于帧计数器平滑）
        bounce = math.sin(self._badge_bounce_frame * 0.08) * 1.5

        painter.save()
        painter.setOpacity(self._badge_alpha)
        painter.setFont(self._badge_font)

        # 绘制在顶部 BADGE_HEIGHT 区域（居中显示）
        bw = self.width()
        rect = QRect(0, int(bounce * 0.5), bw, BADGE_HEIGHT)
        painter.drawText(rect, Qt.AlignHCenter | Qt.AlignVCenter, emoji)

        painter.restore()

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
            self._frame_index = 4  # success 的爱心帧
            self.update()
            QTimer.singleShot(400, lambda: self.update())
        logger.debug("[PixelPet] 蹭蹭~")

    # ═══════════════════════════════════════════════════════════
    # ★ 抖动动画 — 错误状态时让桌宠更显眼
    # ═══════════════════════════════════════════════════════════

    def _start_shake(self, intensity: int | None = None) -> None:
        """启动位置抖动效果（error 状态下视觉增强）"""
        if intensity is not None:
            self._shake_intensity = intensity
        self._shake_frame = 0
        self._shake_original_pos = QPoint(self.x(), self.y())
        self._shake_timer.start(40)  # 25fps 抖动

    def _stop_shake(self) -> None:
        """停止抖动，恢复原始位置"""
        self._shake_timer.stop()
        if self._shake_original_pos is not None:
            # 限幅防止越界
            new_x = max(0, min(self._shake_original_pos.x(),
                               (self.parent().width() if self.parent() else 9999) - self.width()))
            new_y = max(0, min(self._shake_original_pos.y(),
                               (self.parent().height() if self.parent() else 9999) - self.height()))
            self.move(new_x, new_y)
            self._shake_original_pos = None
        self._shake_frame = 0

    def _apply_shake(self) -> None:
        """执行单帧抖动偏移"""
        if self._current_state != "error":
            self._stop_shake()
            return
        self._shake_frame += 1
        if self._shake_original_pos is None:
            self._shake_original_pos = QPoint(self.x(), self.y())
        intensity = self._shake_intensity
        # 随机偏移，模拟颤抖
        offset_x = random.randint(-intensity, intensity)
        offset_y = random.randint(-intensity, intensity)
        if self.parent():
            new_x = max(0, min(
                self._shake_original_pos.x() + offset_x,
                self.parent().width() - self.width()
            ))
            new_y = max(0, min(
                self._shake_original_pos.y() + offset_y,
                self.parent().height() - self.height()
            ))
            self.move(new_x, new_y)
        else:
            self.move(self._shake_original_pos.x() + offset_x,
                      self._shake_original_pos.y() + offset_y)

    # ═══════════════════════════════════════════════════════════
    # ★ 粒子效果 — 状态过渡时的视觉效果
    # ═══════════════════════════════════════════════════════════

    def _spawn_particles(self, count: int = 6) -> None:
        """★ 在当前桌宠位置生成星星粒子"""
        cx, cy = self.width() // 2, self.height() // 2
        for _ in range(count):
            px = cx + random.randint(-12, 12)
            py = cy + random.randint(-12, 12)
            self._particles.append([px, py, 0.6 + random.random() * 0.4, 1.0])
        # 粒子更新已合并到主帧循环 _advance_frame，不再启动独立 timer
        self.update()

    def _update_particles(self) -> None:
        """★ 更新粒子状态（衰减 + 清除）
        已合并到主帧循环 _advance_frame，此方法保留供 cleanup 调用"""
        if not self._particles:
            self._particle_timer.stop()
            return
        alive = []
        for p in self._particles:
            p[3] -= 0.08  # life 衰减
            p[2] -= 0.04  # alpha 衰减
            p[1] -= 0.5   # 轻微上飘
            if p[3] > 0 and p[2] > 0:
                alive.append(p)
        self._particles = alive
        self.update()
        if not alive:
            self._particle_timer.stop()

    # ═══════════════════════════════════════════════════════════
    # ★ 注意力吸引系统（v2 — 多阶段、更可爱）
    # ═══════════════════════════════════════════════════════════

    def _reset_attention_timer(self) -> None:
        self._attention_timer.stop()
        self._attention_timer.start(ATTENTION_TIMEOUT_MS)
        self._attention_shown = False

    def _seek_attention(self) -> None:
        """长时间无交互，主动吸引注意 — 多阶段可爱吸引"""
        if self._current_state not in ("idle", "sleeping") or self._attention_shown:
            return
        self._attention_shown = True

        # 随机选择一种吸引注意的方式
        style = random.choice(["bounce", "emoji_wave", "double_bounce", "spin_look"])
        logger.debug(f"[PixelPet] 主动吸引注意 (style={style})")

        if style == "bounce":
            self._play_bounce()
        elif style == "emoji_wave":
            self._play_bounce_small()
            # 切换为 surprised 表情一瞬
            QTimer.singleShot(100, lambda: self._flash_state("surprised", 3))
        elif style == "double_bounce":
            self._play_bounce()
            QTimer.singleShot(400, self._play_bounce_small)
        elif style == "spin_look":
            self._play_bounce()
            # 快速左右张望
            for d in [6, 9, 3, 9]:
                QTimer.singleShot(d * 50, self.update)

    def _flash_state(self, state_name: str, frames: int = 3) -> None:
        """★ 快速闪烁切换到指定状态再回来（用于表情反馈）"""
        if state_name not in STATE_ROWS:
            return
        old_state = self._current_state
        self._frame_index = 0

        def flash_step(count):
            # ★ 状态被外部(AI信号等)改变时中止闪烁，避免闭包覆写新状态
            if self._current_state != state_name and self._current_state != old_state:
                return
            if count >= frames:
                # 完成后务必恢复原始状态
                self._current_state = old_state
                self.update()
                return
            # 交替显示目标状态和原始状态
            self._current_state = state_name if count % 2 == 0 else old_state
            self.update()
            QTimer.singleShot(100, lambda: flash_step(count + 1))

        flash_step(0)

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
            # ★ 保存拖拽前的状态（用于松手后恢复）
            prev = self._current_state
            if prev in ("sleeping", "napping"):
                # ★ 从睡眠被拖拽：松手后回 idle（不播 wakeup），延迟切 dragging
                #   让轻点仍能触发 wake_up()，只有真正拖动时才进挣扎
                self._state_before_drag = "idle"
                self._sleep_wake_timer.stop()
                self._drag_pending_state = "dragging"
            else:
                self._state_before_drag = prev
                self._drag_pending_state = None
                self.set_state("dragging")

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if self._dragging:
            # ★ 从睡眠开始的拖拽：首次移动时才切换到 dragging 状态
            if self._drag_pending_state:
                self.set_state(self._drag_pending_state)
                self._drag_pending_state = None
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

            # ★ 清理延迟切换标记（轻点时未触发拖动，标记还在）
            self._drag_pending_state = None

            # ★ 松手后恢复拖拽前的状态（而非强制 idle）
            if was_dragging and self._current_state == "dragging":
                prev_state = self._state_before_drag or "idle"
                self._state_before_drag = None
                self.set_state(prev_state)

            # 点击检测（仅在拖拽距离很小时视为点击）
            if was_dragging and self._drag_offset:
                dx = abs(event.pos().x() - self._drag_offset.x())
                dy = abs(event.pos().y() - self._drag_offset.y())
                if dx < 4 and dy < 4:
                    self._handle_click()
            self._reset_interaction_timer()

    def enterEvent(self, event: object) -> None:
        """★ 鼠标悬停进入 — 桌宠抬头看鼠标"""
        super().enterEvent(event)
        if self._current_state == "idle" and not self._dragging:
            self._flash_state("surprised", 2)

    def leaveEvent(self, event: object) -> None:
        """★ 鼠标离开 — 恢复 idle"""
        super().leaveEvent(event)
        if self._current_state == "idle":
            self.update()

    def _apply_inertia(self) -> None:
        """惯性滑动衰减 — 根据速度动态调整间隔，速度低时降频"""
        if not hasattr(self, "_inertia_velocity"):
            self._inertia_timer.stop()
            return
        vx = int(self._inertia_velocity.x() * INERTIA_DECAY)
        vy = int(self._inertia_velocity.y() * INERTIA_DECAY)
        self._inertia_velocity = QPoint(vx, vy)

        speed = math.sqrt(vx * vx + vy * vy)
        if speed < INERTIA_MIN_VELOCITY:
            self._inertia_timer.stop()
            self.setCursor(Qt.PointingHandCursor)
            return

        # ★ 智能降频：速度越低，间隔越长（保底 16ms = ~60fps）
        adaptive_interval = max(16, min(60, int(60 - speed * 0.5)))
        self._inertia_timer.setInterval(adaptive_interval)

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
        return super().eventFilter(obj, event)

    # ═══════════════════════════════════════════════════════════
    # 位置自适应
    # ═══════════════════════════════════════════════════════════
    # 位置自适应 — 站在发送按钮上
    # ═══════════════════════════════════════════════════════════

    def _get_send_button_top(self):
        """桌宠已下沉为 TabManagerWindow 全局浮层：取当前激活窗的
        input_area.send_btn 在 TabManagerWindow 坐标系下的上边缘 Y 坐标。

        原逻辑读 self.parent().input_area.send_btn（parent 为单个 MainWidget），
        现 parent 为 TabManagerWindow（无 input_area），改为经 TabManagerWindow
        取当前激活窗的 send_btn 并映射到 TabManagerWindow 坐标系（与 pet 自身
        几何坐标系一致）。
        """
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is None:
                return None
            win = tm.get_current_window()
            if win is None:
                return None
            input_area = getattr(win, "input_area", None)
            if input_area is not None and hasattr(input_area, "send_btn"):
                sb = input_area.send_btn
                if sb.isVisible():
                    return sb.mapTo(self.parent() or tm, QPoint(0, 0)).y()
        except Exception:
            pass
        return None

    def reposition_to_active_window(self) -> None:
        """TabManager 切 tab / 窗体缩放时调用：重新定位到当前激活窗的 send_btn。

        复用 resize_handle 的平滑移动逻辑（_animate_to），从旧位置平滑滑到
        新窗口输入框；坐标基于 TabManagerWindow 坐标系（pet 已下沉为全局浮层）。
        拖拽中不打断用户操作。
        """
        if self._dragging:
            return
        p = self.parent()
        if p is None:
            return
        self.resize_handle(p.width(), p.height())

    def resize_handle(self, parent_width: int, parent_height: int) -> None:
        """父窗口大小变化时平滑移动 — 桌宠站在发送按钮上边缘"""
        if self._dragging:
            return

        margin = 8
        target_x = parent_width - self.width() - margin

        # 找到发送按钮，让桌宠脚底站在它上边缘
        btn_top = self._get_send_button_top()
        if btn_top is not None:
            # 像素狐狸脚底在 widget 内的偏移量（16×16 中脚在 y=13，缩放后 13*3=39，+2px 空隙 + 徽章高度）
            feet_offset = 41 + BADGE_HEIGHT
            target_y = btn_top - feet_offset
        else:
            # fallback: 右下角，输入区域上方
            target_y = parent_height - self.height() - 100

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
        except Exception as e:
            logger.debug(f"[PixelPet] 配置信号连接失败: {e}")

    # ═══════════════════════════════════════════════════════════
    # ★ 外部事件响应
    # ═══════════════════════════════════════════════════════════

    def on_user_typing(self) -> None:
        """★ 💬 用户正在输入时调用 — 桌宠显示写作动画（陪用户一起打字）"""
        if self._current_state not in ("idle", "thinking"):
            return
        # 短暂播放 writing 动画（眼镜+笔尖点动），像在陪用户一起写
        self._play_behavior_with_state("writing", 1200)

    def on_drag_started(self) -> None:
        """★ 🖱️ 用户开始拖拽时调用 — 显示被拽的无奈表情"""
        if self._current_state == "idle":
            self._flash_state("crying", 2)

    # ═══════════════════════════════════════════════════════════
    # 生命周期
    # ═══════════════════════════════════════════════════════════

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self.raise_()
        # ★ 显示时恢复 raise_timer（除非在睡觉）
        if self._current_state != "sleeping" and not self._raise_timer.isActive():
            self._raise_timer.start(5000)

    def hideEvent(self, event: object) -> None:
        """★ 隐藏时停止不必要的定时器"""
        super().hideEvent(event)
        self._raise_timer.stop()

    def cleanup(self) -> None:
        self._frame_timer.stop()
        self._sleep_timer.stop()
        self._sleep_wake_timer.stop()
        self._recover_timer.stop()
        self._raise_timer.stop()
        self._idle_behavior_timer.stop()
        self._attention_timer.stop()
        self._inertia_timer.stop()
        self._click_timer.stop()
        self._shake_timer.stop()
        self._particle_timer.stop()
        self._particles.clear()
        self._stop_shake()
        self._stop_thinking_hard_timer()
        self._state_before_drag = None
        self._drag_pending_state = None
        self._sleep_frame_count = 0
        # H4/H5：解除 parent 事件过滤器 + 断开配置信号 + 注销主题刷新目标，
        # 避免桌宠销毁/重建后旧实例仍被事件过滤器与 theme_manager 强引用持有（泄漏）。
        parent = self.parent()
        if parent is not None:
            try:
                parent.removeEventFilter(self)
            except Exception:
                pass
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            cfg.pet_size.valueChanged.disconnect(self.on_pet_size_changed)
        except Exception:
            pass
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.unregister_refresh_target(self)
        except Exception:
            pass
        logger.debug("[PixelPet] v2 已清理")
