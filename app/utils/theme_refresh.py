# -*- coding: utf-8 -*-
"""
主题刷新协调器 - 多窗口/多 Tab 场景下的高性能主题刷新

核心优化：
  1. ThemeVersion：全局版本号，同一主题幂等跳过
  2. JSCache：按主题版本缓存 MessageCard CSS 变量 JS 字符串
  3. BGCache：按 (image_path, opacity) 缓存背景 QPixmap，LRU 淘汰
  4. 计时日志：debug 级别统计各阶段耗时（DRIFOX_DEBUG_THEME_PERF 环境变量控制）
"""

import logging
import os
import threading
import time

from PyQt5.QtGui import QPixmap

logger = logging.getLogger(__name__)

# ── 常量 ──
_VERSION_MAX: int = 2**30  # 版本号上界（≈10亿，wrap 保护）


def _perf_enabled() -> bool:
    """是否启用性能计时日志（环境变量 DRIFOX_DEBUG_THEME_PERF=1）"""
    return os.environ.get("DRIFOX_DEBUG_THEME_PERF") == "1"


_logger_level_ready = False


def _perf_log(msg: str):
    """性能打点输出（DRIFOX_DEBUG_THEME_PERF=1 才生效）

    ⚠️ 本模块用 stdlib logging，而 logging_setup 把 root 锁在 WARNING
    （避免第三方库 DEBUG 噪音淹没 all.log）。因此 logger.debug 默认**全部被丢**，
    历史上这个开关等于死的。这里在首次打点时把本 logger 单独提到 DEBUG，
    不动 root（只开这一个模块）。日志仍走 stdlib→loguru 桥进 all.log。
    """
    global _logger_level_ready
    if not _perf_enabled():
        return
    if not _logger_level_ready:
        logger.setLevel(logging.DEBUG)
        _logger_level_ready = True
    logger.debug(msg)


class ThemeRefreshCoordinator:
    """全局主题刷新协调器（类级别单例状态，线程安全）

    由 _execute_batched_theme_refresh 和 _apply_runtime_ui_settings 调用。
    """

    _lock = threading.Lock()

    _current_theme_id: str | None = None
    _version: int = 0

    # ── JS 缓存 ──
    _js_cache: dict[int, str] = {}

    # ── 背景图片缓存 ──
    _bg_cache: dict[str, QPixmap] = {}

    # ── 计时 ──
    _phase_starts: dict[str, float] = {}
    # 最近一轮各段耗时（dump_slow 汇总用；每轮 batched 刷新结束取一次快照并清空）
    _phase_ms: dict[str, float] = {}

    # ── 刷新周期令牌 ──
    # 一次主题刷新周期（= 一次 _apply_runtime_ui_settings 进入）内，同一张卡
    # 可能被多条刷新链命中：main_widget 5a 显式列表 + TabManagerWindow 的
    # GlobalCardController.refresh_theme_styles（设置弹窗两处都在刷，实测
    # 单卡 260ms 级被付两次）。卡被刷过就打上本周期号，后到的链路直接跳过。
    _refresh_cycle: int = 0

    @classmethod
    def begin_refresh_cycle(cls) -> int:
        """开启新一轮刷新周期（每个 _apply_runtime_ui_settings 入口调一次）

        注意：**不**在此推进外观纪元。本方法每个窗口调一次（多窗口批处理里会
        连进 N 次），推进会让先刷过的可见页面立刻又变「落后」，显示时白刷第二
        遍。纪元由 ``ThemeManager.on_theme_changed`` 在每次外观变更时推进一次。
        """
        with cls._lock:
            cls._refresh_cycle += 1
            return cls._refresh_cycle

    @classmethod
    def get_refresh_cycle(cls) -> int:
        with cls._lock:
            return cls._refresh_cycle

    # ══════════════════════════════════════════════════════════════
    #  版本管理
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def should_skip(cls, theme_id: str) -> bool:
        """检查主题是否与上次刷新相同。

        Returns:
            True:  主题未变，应跳过本次刷新
            False: 主题已变，已自动推进版本号
        """
        with cls._lock:
            if cls._current_theme_id == theme_id:
                return True
        cls.on_theme_changed(theme_id)
        return False

    @classmethod
    def on_theme_changed(cls, new_theme_id: str):
        """主题确实变更时调用：推进版本 + 清缓存"""
        with cls._lock:
            cls._current_theme_id = new_theme_id
            cls._version = (cls._version + 1) % _VERSION_MAX
            cls._js_cache.clear()
        _perf_log(f"[ThemeRefreshCoordinator] 主题变更 → v{cls._version}: {new_theme_id}")

    @classmethod
    def get_version(cls) -> int:
        with cls._lock:
            return cls._version

    # ══════════════════════════════════════════════════════════════
    #  外观纪元（主题/字体/字号统一计数）
    # ══════════════════════════════════════════════════════════════

    _appearance_epoch: int = 0

    @classmethod
    def bump_appearance_epoch(cls) -> int:
        """推进外观纪元（每次主题/字体/字号批量刷新 +1）

        与 ``_version`` 的分工：version 只在**主题 id 变化**时推进（消息卡 HTML
        缓存分桶用）；epoch 走**每一次外观变更**——字体家族/字号变更走同一条
        batched 链但不改主题 id，若复用 version，脏页面会永远等不到补刷。
        """
        with cls._lock:
            cls._appearance_epoch = (cls._appearance_epoch + 1) % _VERSION_MAX
            return cls._appearance_epoch

    @classmethod
    def get_appearance_epoch(cls) -> int:
        with cls._lock:
            return cls._appearance_epoch

    # ══════════════════════════════════════════════════════════════
    #  JS 缓存
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def get_or_build_js(cls, theme: dict, is_light: bool) -> str:
        """获取或构建 MessageCard CSS 变量注入 JS。

        同一主题版本内只构建一次，后续调用直接返回缓存。
        """
        with cls._lock:
            v = cls._version
            if v in cls._js_cache:
                return cls._js_cache[v]

        js = _build_css_var_js(theme, is_light)
        with cls._lock:
            cls._js_cache[v] = js
        _perf_log(f"[ThemeRefreshCoordinator] JS 缓存构建 v{v} ({len(js)} 字节)")
        return js

    # ══════════════════════════════════════════════════════════════
    #  背景缓存（LRU 淘汰，防止内存泄漏）
    # ══════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════
    #  计时工具
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def timer_start(cls, phase: str):
        """开始计时阶段（**始终计时**）

        常驻计时的理由：主题刷新慢的时候，正是最需要分段数字的时候，而那时用户
        不会去记得开 DRIFOX_DEBUG_THEME_PERF。所以计时永远跑（perf_counter 两次
        调用，开销可忽略），是否输出由阈值（dump_slow）与环境变量决定。
        """
        with cls._lock:
            cls._phase_starts[phase] = time.perf_counter()

    @classmethod
    def timer_end(cls, phase: str) -> float | None:
        """结束计时阶段并记录耗时。

        Returns:
            耗时毫秒数（阶段未开始过时为 None）。是否打日志由 _perf_enabled 决定。
        """
        with cls._lock:
            start = cls._phase_starts.pop(phase, None)
            if start is None:
                return None
            elapsed_ms = (time.perf_counter() - start) * 1000
            cls._phase_ms[phase] = elapsed_ms
        _perf_log(f"[ThemeRefresh] {phase}: {elapsed_ms:.1f}ms")
        return elapsed_ms

    @classmethod
    def dump_slow(cls, threshold_ms: float = 500.0) -> None:
        """总耗时超阈值时打一条 INFO 摘要（不依赖环境变量）

        只在慢路径输出，正常切换静默。日志落在 all.log，事后可直接看到
        哪一段吃掉了时间，不用复现、不用开 env。
        """
        with cls._lock:
            snapshot = dict(cls._phase_ms)
            cls._phase_ms.clear()
        total = snapshot.get("batched_total")
        if total is None or total < threshold_ms:
            return
        parts = [f"{k}={v:.0f}ms" for k, v in sorted(snapshot.items(), key=lambda kv: -kv[1])]
        # 必须 warning：logging_setup 把 root 锁在 WARNING，INFO/DEBUG 一律不落盘
        logger.warning("[ThemeRefresh-SLOW] 主题刷新 %.0fms | %s", total, " ".join(parts))


def _build_css_var_js(theme: dict, is_light: bool) -> str:
    """构建更新 :root CSS 变量的 JS 代码

    对缺失的 theme key 记录 warning 并使用空字符串回退。
    """
    _REQUIRED_KEYS = [
        "card_bg_solid",
        "content_bg",
        "border",
        "border_accent",
        "text_primary",
        "text_secondary",
        "text_muted",
        "accent",
        "accent_warm",
    ]
    missing = [k for k in _REQUIRED_KEYS if k not in theme]
    if missing:
        logger.warning(f"[ThemeRefresh] 主题缺少 CSS 变量键: {missing}")

    def _js_str(s: str | None) -> str:
        if s is None:
            return ""
        return s.replace("\\", "\\\\").replace("'", "\\'")

    def _accent_rgba(v: str | None, alpha: float) -> str:
        """#rrggbb → rgba(...)，与 card_render_core._accent_rgba / 骨架同语义"""
        h = (v or "").strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            try:
                return "rgba(%d, %d, %s)" % (int(h[0:2], 16), int(h[2:4], 16), alpha)
            except ValueError:
                pass
        return f"rgba(100, 198, 255, {alpha})"  # 解析失败兜底：原 midnight 色

    return (
        "(function(){"
        "var r=document.documentElement;"
        "if(!r)return;"
        f"r.style.setProperty('--bg','transparent');"
        f"r.style.setProperty('--panel','{_js_str(theme.get('card_bg_solid'))}');"
        f"r.style.setProperty('--panel-elevated','{_js_str(theme.get('card_bg_solid'))}');"
        f"r.style.setProperty('--panel-soft','{_js_str(theme.get('content_bg'))}');"
        f"r.style.setProperty('--border','{_js_str(theme.get('border'))}');"
        f"r.style.setProperty('--border-strong','{_js_str(theme.get('border_accent'))}');"
        f"r.style.setProperty('--text','{_js_str(theme.get('text_primary'))}');"
        f"r.style.setProperty('--text-secondary','{_js_str(theme.get('text_secondary'))}');"
        f"r.style.setProperty('--text-muted','{_js_str(theme.get('text_muted'))}');"
        f"r.style.setProperty('--accent','{_js_str(theme.get('accent'))}');"
        f"r.style.setProperty('--accent-warm','{_js_str(theme.get('accent_warm'))}');"
        f"r.style.setProperty('--code-bg','{'var(--panel-soft)' if is_light else 'transparent'}');"
        f"r.style.setProperty('--code-toolbar','{'rgba(0,0,0,0.03)' if is_light else 'rgba(255,255,255,0.03)'}');"
        f"r.style.setProperty('--code-border','{'var(--border)' if is_light else '#2a3447'}');"
        # 语义派生层：骨架 :root 有这批变量（字面量烘进骨架），运行时注入必须
        # 同步覆盖，否则主题切换后已存在卡片/池化复用卡片的会话卡底色、
        # hover、表头色停留旧主题（欢迎卡片「会话导览」直接踩中）。
        # 与 chart_viewer_card._inject_theme_vars 的全量表保持同源。
        f"r.style.setProperty('--accent-text','{_js_str(theme.get('accent'))}');"
        f"r.style.setProperty('--accent-soft','{_js_str(theme.get('hover_bg'))}');"
        f"r.style.setProperty('--accent-soft-strong','{_js_str(theme.get('selected_bg'))}');"
        f"r.style.setProperty('--accent-border-weak','{_accent_rgba(theme.get('accent'), 0.22)}');"
        f"r.style.setProperty('--accent-glow','{_accent_rgba(theme.get('accent'), 0.10)}');"
        f"r.style.setProperty('--row-alt','{'rgba(15, 23, 42, 0.03)' if is_light else 'rgba(255, 255, 255, 0.02)'}');"
        f"r.style.setProperty('--row-hover','{'rgba(15, 23, 42, 0.05)' if is_light else 'rgba(255, 255, 255, 0.05)'}');"
        f"r.style.setProperty('--row-header','{'rgba(15, 23, 42, 0.06)' if is_light else 'rgba(255, 255, 255, 0.04)'}');"
        "})();"
    )
