# -*- coding: utf-8 -*-
"""历史会话首屏一次揭示（history-first-paint）。

背景：从历史列表打开会话时，卡片先以起步高度（40px）进布局，WebEngine 再
异步上报真实高度；视口贴底跟随每一次落高，用户看到的就是「整屏骨架 + 一路
抖动着撑开」。本模块把这段中间态整体遮住：等**首屏内**卡片正文落地、首屏
几何连续两拍不变、且视口贴底后，一次揭示。屏外卡片继续后台补齐，不阻塞。

就绪还有一条「整屏高度静止」条件（``LAYOUT_QUIET_GRACE_MS`` 限时）：屏内已
就绪、但屏外卡片刚落高时揭示，落高会把可见内容顶走（实测 224px 台阶）。
加载的初始窗口是有限的一小段，等它静止通常只多两三百毫秒；超出宽限则按
「屏外不阻塞」如期揭示。

职责边界：
- 只做状态机（遮罩 / 轮询 / 超时 / 取消 / 主题重刷），不认识 MessageCard，
  也不碰渲染链。宿主（main_widget）用三个回调提供输入输出。
- 就绪判定**不读** ``_lazy_rendered`` 当「正文已就绪」（它只代表 viewer 已建，
  正文未必落地、高度未必提交）。该标志只用作**反面**信号：未置位 = 屏内卡
  还没派发 viewer，此时禁止走「长时间静止」兜底（见 ``_QUIET_TICKS``）。

宿主回调：
- ``cover_parent() -> QWidget | None``：遮罩的父控件（通常是滚动区 viewport）
- ``state_provider() -> dict | None``：就绪判定输入，键见 ``_read_state``
- ``on_reveal(stick_bottom: bool) -> None``：揭示动作（宿主负责贴底）

环境变量 ``DRIFOX_HISTORY_FIRST_PAINT=0/false/no`` 关闭本特性（import 期固化）。
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional

from PyQt5.QtCore import QObject, Qt, QTimer
from PyQt5.QtWidgets import QLabel, QWidget

# 轮询节奏：25ms 对齐 40Hz 感知帧，一次采样成本 ~O(屏内卡片数)
POLL_INTERVAL_MS = 25
# 慢加载才出提示文字：快加载（一个静默保持周期内就绪，约 250ms）不该闪一行字
HINT_TEXT_DELAY_MS = 320
# 硬超时：慢卡 / 渲染失败不得永久遮挡聊天区
MAX_WAIT_MS = 1500
# 整屏高度需要连续静止这么久才揭示：屏外卡片落高会把可见内容顶走，
# 揭示前必须确认初始加载窗口真的停下来了（实测分批落高间隔 ≤200ms）
LAYOUT_QUIET_HOLD_MS = 200
# 屏内卡片已全部派发 viewer、但高度长时间不动：视为落地失败（如 viewer 停在
# 起步高度），按 25ms × 16 = 400ms 兜底揭示，好过死等 MAX_WAIT_MS
_QUIET_TICKS = 16
HINT_TEXT = "正在加载会话…"


def _load_enabled() -> bool:
    """实验开关读取：缺失/空/未知值一律默认开，显式 0/false/no 关闭。"""
    raw = os.environ.get("DRIFOX_HISTORY_FIRST_PAINT", "").strip().lower()
    return raw not in ("0", "false", "no")


# perf: import 期固化开关，进程内不热变（实验按进程设 env 启动）
ENABLED = _load_enabled()


def _cover_background() -> str:
    """遮罩底色：跟随主题聊天区实底（纯色主题色优先，其次 Colors.CONTENT_BG）。"""
    from app.utils.design_tokens import Colors

    Colors.refresh()
    base = None
    try:
        from app.utils.theme_manager import theme_manager

        bgs = theme_manager.get_theme_backgrounds(theme_manager.get_current_theme_id())
        chat_area = bgs.get("chat_area") or {}
        if chat_area and chat_area.get("enabled", True):
            base = chat_area.get("color")
    except Exception:
        base = None
    return base or Colors.CONTENT_BG


def _hint_color() -> str:
    from app.utils.design_tokens import Colors

    Colors.refresh()
    return Colors.TEXT_MUTED


class _FirstPaintCover(QLabel):
    """首屏遮罩：不透明面板，超时才显示提示文字。

    ⚠️ 不设 ``WA_TransparentForMouseEvents``：一旦声明鼠标穿透，``QWidget.childAt``
    会跳过本层，把下面的骨架卡片重新判成「可见」——逐帧验收/截图的可见性采样
    又会看到成排空白卡。遮罩必须在覆盖语义上真实存在。滚轮仍可用：QLabel 忽略
    wheel 事件，Qt 会继续向父级（滚动区 viewport）传播，用户想中断随时能滚。
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("historyFirstPaintCover")
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.apply_theme()

    def apply_theme(self) -> None:
        """按当前主题重刷底色/字色（主题切换随时可能落在等待窗口内）。"""
        try:
            bg = _cover_background()
            fg = _hint_color()
            self.setStyleSheet(f"#historyFirstPaintCover {{ background-color: {bg}; color: {fg}; font-size: 13px; }}")
        except Exception:
            pass  # 主题系统未就绪：保持上一次样式，不阻塞加载

    def set_hint_visible(self, visible: bool) -> None:
        text = HINT_TEXT if visible else ""
        if self.text() != text:
            self.setText(text)


class HistoryFirstPaintGate(QObject):
    """历史重建期的首屏揭示状态机。

    状态：空闲 → 等待（遮罩已上、轮询中）→ 揭示 / 取消。
    每次 ``begin`` 都会先 ``cancel`` 旧一轮并递增 ``_token``，旧回调与新会话
    之间不存在共享的可写状态；``_tick`` 还额外核对 ``session_id``，两层都拦
    「旧等待把新会话揭出来」。
    """

    def __init__(
        self,
        cover_parent: Callable[[], Optional[QWidget]],
        state_provider: Callable[[], Optional[Dict[str, Any]]],
        on_reveal: Callable[[bool], None],
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._cover_parent = cover_parent
        self._state_provider = state_provider
        self._on_reveal = on_reveal
        self._cover: Optional[_FirstPaintCover] = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._waiting = False
        self._session_id: Optional[str] = None
        self._token = 0
        self._started_at = 0.0
        self._last_visible_sig: Any = None
        self._last_layout_height: Any = None
        self._layout_quiet_since = 0.0
        self._visible_stable_samples = 0
        self._hint_visible = False

    # ── 观测（测试 / 诊断用） ──────────────────────────────
    @property
    def is_waiting(self) -> bool:
        return self._waiting

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def cover_visible(self) -> bool:
        cover = self._cover
        if cover is None:
            return False
        try:
            # 用 isHidden 而非 isVisible：父窗口未 show（离屏/未激活标签页）时
            # isVisible 恒 False，但遮罩确实已挂在视口上，会在首绘时生效。
            return not cover.isHidden()
        except RuntimeError:
            return False

    def hint_text(self) -> str:
        cover = self._cover
        if cover is None:
            return ""
        try:
            return cover.text()
        except RuntimeError:
            return ""

    def cover_style(self) -> str:
        cover = self._cover
        if cover is None:
            return ""
        try:
            return cover.styleSheet()
        except RuntimeError:
            return ""

    # ── 生命周期 ─────────────────────────────────────────
    def begin(self, session_id: Optional[str]) -> None:
        """开始一轮等待：立刻上罩（赶在本轮首绘之前），随后轮询就绪。"""
        self.cancel()
        self._token += 1
        self._session_id = session_id
        self._started_at = time.monotonic()
        self._last_visible_sig = None
        self._last_layout_height = None
        self._layout_quiet_since = self._started_at
        self._visible_stable_samples = 0
        self._hint_visible = False
        if not ENABLED:
            return
        if not self._show_cover():
            return
        self._waiting = True
        self._timer.start()

    def cancel(self) -> bool:
        """撤罩作废且不揭示（切会话 / 宿主清理用）。返回是否取消了一轮等待。"""
        was_waiting = self._waiting
        self._waiting = False
        self._token += 1
        self._timer.stop()
        self._remove_cover()
        return was_waiting

    def reveal(self, stick_bottom: bool = True) -> bool:
        """撤罩并通知宿主（宿主负责贴底）。"""
        if not self._waiting:
            self._remove_cover()
            return False
        self._waiting = False
        self._token += 1
        self._timer.stop()
        self._remove_cover()
        try:
            self._on_reveal(bool(stick_bottom))
        except Exception:
            pass  # 揭示动作失败不影响已撤罩这一事实
        return True

    def on_theme_changed(self) -> None:
        """主题切换：等待中的遮罩就地重刷配色。"""
        cover = self._cover
        if cover is not None:
            cover.apply_theme()

    # ── 内部 ────────────────────────────────────────────
    def _show_cover(self) -> bool:
        try:
            parent = self._cover_parent()
        except Exception:
            parent = None
        if parent is None:
            return False
        try:
            cover = _FirstPaintCover(parent)
            cover.setGeometry(parent.rect())
            cover.show()
            cover.raise_()
        except RuntimeError, AttributeError, TypeError:
            return False
        self._cover = cover
        return True

    def _remove_cover(self) -> None:
        cover, self._cover = self._cover, None
        if cover is None:
            return
        # 先 hide 再断父引用：避免脱离父树瞬间变成独立顶层窗口
        try:
            cover.hide()
            cover.setParent(None)
            cover.deleteLater()
        except RuntimeError:
            pass

    def _sync_cover_geometry(self) -> None:
        cover = self._cover
        if cover is None:
            return
        try:
            parent = cover.parentWidget()
            if parent is not None:
                cover.setGeometry(parent.rect())
            cover.raise_()
        except RuntimeError:
            pass

    def _read_state(self) -> Optional[Dict[str, Any]]:
        try:
            state = self._state_provider()
        except Exception:
            return None
        return state if isinstance(state, dict) else None

    def _elapsed_ms(self, now: Optional[float] = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, (current - self._started_at) * 1000.0)

    def _tick(self) -> None:
        if not self._waiting:
            self._timer.stop()
            return
        self._sync_cover_geometry()
        elapsed = self._elapsed_ms()
        state = self._read_state()
        if state is None:
            # 几何不可用（窗口未布局/最小化/宿主属性缺失）：只等硬超时
            if elapsed >= MAX_WAIT_MS:
                self.reveal(stick_bottom=True)
            return
        if state.get("session_id") != self._session_id:
            # 会话已切走：静默作废，绝不把新会话揭出来
            self.cancel()
            return
        if state.get("away"):
            # 用户自己滚开看历史：立刻揭示，且不要再强行贴底
            self.reveal(stick_bottom=False)
            return

        visible = int(state.get("visible_cards") or 0)
        pending = int(state.get("pending_cards", -1))
        unrendered = int(state.get("unrendered_cards") or 0)
        visible_sig = state.get("visible_geometry")
        layout_height = state.get("layout_height")
        now = time.monotonic()

        visible_stable = visible_sig is not None and visible_sig == self._last_visible_sig
        if layout_height is None or layout_height != self._last_layout_height:
            self._layout_quiet_since = now  # 整屏高度变了 → 重新计静默时长
        self._last_visible_sig = visible_sig
        self._last_layout_height = layout_height
        self._visible_stable_samples = self._visible_stable_samples + 1 if visible_stable else 0

        settled = visible > 0 and bool(state.get("at_bottom")) and pending == 0
        quiet_ms = (now - self._layout_quiet_since) * 1000.0
        hold_ms = LAYOUT_QUIET_HOLD_MS
        if state.get("offscreen_pending"):
            # 屏外 viewer 分批上报，实测两次提交之间可间隔约 300ms；
            # 只延长安静窗口，不等待每张屏外卡就绪（卡片失败仍会揭示）。
            hold_ms = 450
        if settled and visible_stable and quiet_ms >= hold_ms:
            self.reveal(stick_bottom=True)
            return

        stagnant = pending > 0 and unrendered == 0 and self._visible_stable_samples >= _QUIET_TICKS
        if stagnant or elapsed >= MAX_WAIT_MS:
            self.reveal(stick_bottom=True)
            return

        if not self._hint_visible and elapsed >= HINT_TEXT_DELAY_MS:
            self._hint_visible = True
            cover = self._cover
            if cover is not None:
                try:
                    cover.set_hint_visible(True)
                except RuntimeError:
                    pass
