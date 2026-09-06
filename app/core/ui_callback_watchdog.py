# -*- coding: utf-8 -*-
"""P2-1：UI 回调 watchdog 熔断。

- 单次回调 >5s 或滑动窗口（最近 6 次）累计 >30s → 记一次 degraded + warning
- 连续 3 次 degraded → 自动写入 disabled_plugin_components（复用既有禁用矩阵）停用
  + InfoBar 告知用户（无 Qt 环境时降级为纯日志）
- 修复恢复：组件重新注册（加载成功）即清零 degrade 计数；正常回调同样清零连续计数
- 已停用组件：包装层直接跳过调用（下次跳过语义）

计时经 time.monotonic，测试可 monkeypatch 注入假时钟不真等。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Dict, Optional, Tuple

from loguru import logger

SINGLE_SLOW_SECONDS = 5.0
WINDOW_SECONDS = 30.0
WINDOW_SIZE = 6
DEGRADE_LIMIT = 3

# {(plugin, component): {"consecutive": int, "window": deque[float]}}
_STATES: Dict[Tuple[str, str], dict] = {}


def _state(plugin: str, component: str) -> dict:
    return _STATES.setdefault(
        (plugin, component), {"consecutive": 0, "window": deque(maxlen=WINDOW_SIZE)}
    )


def is_degraded(plugin: str, component: str) -> bool:
    st = _STATES.get((plugin, component))
    return bool(st and st["consecutive"] >= 1)


def is_disabled(plugin: str, component: str) -> bool:
    """组件是否已被 watchdog 熔断写入既有禁用矩阵。"""
    try:
        from app.utils.config import Settings

        disabled = Settings.get_instance().disabled_plugin_components.value or []
        return f"{plugin}:{component}" in set(disabled)
    except Exception:
        return False


def reset_state(plugin: str, component: Optional[str] = None) -> None:
    """修复恢复：组件代码修正后（重新加载成功）清零 degrade 计数。"""
    if component is None:
        for key in [k for k in _STATES if k[0] == plugin]:
            _STATES.pop(key, None)
    else:
        _STATES.pop((plugin, component), None)


def _record_degrade(plugin: str, component: str, cost: float, why: str) -> None:
    st = _state(plugin, component)
    st["consecutive"] += 1
    logger.warning(
        f"[UIWatchdog] {plugin}:{component} degraded #{st['consecutive']} ({why}, {cost:.1f}s)"
    )
    if st["consecutive"] >= DEGRADE_LIMIT:
        _disable_component(plugin, component)


def _disable_component(plugin: str, component: str) -> None:
    key = f"{plugin}:{component}"
    try:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        disabled = list(cfg.disabled_plugin_components.value or [])
        if key not in disabled:
            disabled.append(key)
            cfg.disabled_plugin_components.value = disabled
    except Exception as e:
        logger.warning(f"[UIWatchdog] 写入停用矩阵失败（仅日志降级）: {e}")
    logger.warning(f"[UIWatchdog] {key} 连续 {DEGRADE_LIMIT} 次 degrade，已自动停用")
    # InfoBar 告知用户（无 QApplication 时降级为纯日志，不抛）
    try:
        from qfluentwidgets import InfoBar

        InfoBar.warning(
            title="UI 组件已自动停用",
            content=f"{key} 回调连续超时，已停用；修复插件后重新加载即可恢复",
            duration=8000,
            parent=None,
        )
    except Exception:
        pass


def _observe(plugin: str, component: str, cost: float) -> None:
    """回调计时观测：degrade 判定 + 连续计数管理。"""
    st = _state(plugin, component)
    st["window"].append(cost)
    if cost > SINGLE_SLOW_SECONDS:
        _record_degrade(plugin, component, cost, "单次超时")
    elif sum(st["window"]) > WINDOW_SECONDS:
        _record_degrade(plugin, component, cost, "滑动窗口累计超时")
    else:
        st["consecutive"] = 0  # 正常回调重置连续计数


def wrap_ui_callback(plugin: str, component: str, fn: Callable) -> Callable:
    """UI 回调 watchdog 包装（注册处使用）。已停用组件注册时直接给 no-op。"""
    if is_disabled(plugin, component):
        logger.warning(f"[UIWatchdog] {plugin}:{component} 已停用，回调注册降级为 no-op")
        return lambda *a, **k: None

    # 加载成功（重新注册）即重置 degrade 计数——修复恢复语义
    _state(plugin, component)["consecutive"] = 0

    def wrapped(*args, **kwargs):
        if is_disabled(plugin, component):
            return None
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            _observe(plugin, component, time.monotonic() - t0)

    return wrapped


def timed_ui_callback(plugin: str, component: str, fn: Callable, *args, **kwargs):
    """一次性计时调用（调用点使用，如 fence render 入口），观测口径同 wrap_ui_callback。"""
    if is_disabled(plugin, component):
        return None
    t0 = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        _observe(plugin, component, time.monotonic() - t0)
