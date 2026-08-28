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

from PySide6.QtGui import QPixmap

logger = logging.getLogger(__name__)

# ── 常量 ──
_VERSION_MAX: int = 2**30  # 版本号上界（≈10亿，wrap 保护）
_MAX_BG_CACHE: int = 10  # 背景缓存最大条目数
_BG_OPACITY_PRECISION: int = 3  # 缓存 key 中 opacity 的小数精度


def _perf_enabled() -> bool:
    """是否启用性能计时日志（环境变量 DRIFOX_DEBUG_THEME_PERF=1）"""
    return os.environ.get("DRIFOX_DEBUG_THEME_PERF") == "1"


def _perf_log(msg: str):
    if _perf_enabled():
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

    @classmethod
    def get_bg_cache_key(cls, image_path: str | None, opacity: float) -> str:
        """生成背景图片缓存键。

        Args:
            image_path: 图片路径，None 表示无背景
            opacity:   透明度值 (0.0 ~ 1.0)

        Returns:
            格式为 "path:opacity" 的缓存键字符串
        """
        return f"{image_path or '__none__'}:{opacity:.{_BG_OPACITY_PRECISION}f}"

    @classmethod
    def get_cached_bg_pixmap(cls, cache_key: str) -> QPixmap | None:
        """从缓存获取背景 QPixmap。

        Args:
            cache_key: 由 get_bg_cache_key() 生成的缓存键

        Returns:
            缓存的 QPixmap，不存在则返回 None
        """
        with cls._lock:
            return cls._bg_cache.get(cache_key)

    @classmethod
    def set_cached_bg_pixmap(cls, cache_key: str, pixmap: QPixmap) -> None:
        """将背景 QPixmap 存入缓存。

        Args:
            cache_key: 由 get_bg_cache_key() 生成的缓存键
            pixmap:   要缓存的 QPixmap
        """
        with cls._lock:
            if cache_key in cls._bg_cache:
                return  # 已缓存，跳过
            if len(cls._bg_cache) >= _MAX_BG_CACHE:
                # LRU：删除最早插入的条目
                first_key = next(iter(cls._bg_cache))
                old = cls._bg_cache.pop(first_key, None)
                if old is not None:
                    old.detach()  # 释放 Qt 底层资源
            cls._bg_cache[cache_key] = pixmap

    # ══════════════════════════════════════════════════════════════
    #  计时工具
    # ══════════════════════════════════════════════════════════════

    @classmethod
    def timer_start(cls, phase: str):
        """开始计时阶段"""
        if _perf_enabled():
            with cls._lock:
                cls._phase_starts[phase] = time.perf_counter()

    @classmethod
    def timer_end(cls, phase: str) -> float | None:
        """结束计时阶段并记录耗时。

        Returns:
            耗时毫秒数，性能日志未启用时返回 None
        """
        if not _perf_enabled():
            return None
        with cls._lock:
            start = cls._phase_starts.pop(phase, None)
        if start is None:
            return None
        elapsed_ms = (time.perf_counter() - start) * 1000
        _perf_log(f"[ThemeRefresh] {phase}: {elapsed_ms:.1f}ms")
        return elapsed_ms


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
        "})();"
    )
