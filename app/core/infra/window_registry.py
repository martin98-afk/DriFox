# -*- coding: utf-8 -*-
"""进程级 / 窗口级单例状态外提（全量减负 E）。

原承载于 ``OpenAIChatToolWindow`` 的类级字段统一收口到本模块，
消除页面类充当容器级状态中心的职责。

内存泄漏修复（P0-B）：``window_instances`` 由强引用列表改为
``List[weakref.ReferenceType]``。原实现中本模块对 ``OpenAIChatToolWindow``
强持有，窗口 ``close()`` 仅隐藏不销毁（无 WA_DeleteOnClose），Python wrapper
被本模块长期持有无法回收，反复开关 tab 时窗口对象树堆积。改为弱引用后，
窗口销毁即自动从表中脱落，``alive_window_instances()`` 返回当前存活实例。

保留不动的模块变量：
- ``last_hot_reload_fingerprint``：插件热重载指纹（原 ``_last_hot_reload_fingerprint``）
- ``subagent_log_cleanup_timer``：子智能体日志全局清理 timer（原 ``_class_subagent_log_cleanup_timer``）

读写 API：
- ``register_window(win)`` / ``unregister_window(win)``：登记 / 注销窗口实例
- ``alive_window_instances()``：返回当前存活窗口实例列表（自动过滤已回收弱引用）
- 原强引用列表 ``window_instances`` 的全部外部消费点（见 #11 第二节清单，
  含 main_widget.py 23 处 + 其余 5 处）已统一改为调用 ``alive_window_instances()``。
"""
from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from app.main_widget import OpenAIChatToolWindow

# ── 窗口实例弱引用登记表（原 OpenAIChatToolWindow._instances，强引用 → 弱引用）──
window_instances: List[weakref.ReferenceType] = []

# ── 插件热重载指纹（原 OpenAIChatToolWindow._last_hot_reload_fingerprint，保留不动）──
last_hot_reload_fingerprint: Optional[str] = None

# ── 子智能体日志全局清理 timer（原 OpenAIChatToolWindow._class_subagent_log_cleanup_timer，保留不动）──
subagent_log_cleanup_timer = None


def register_window(win: "OpenAIChatToolWindow") -> None:
    """登记窗口实例（存入弱引用，避免强持有导致关窗后泄漏）。"""
    window_instances.append(weakref.ref(win))


def unregister_window(win: "OpenAIChatToolWindow") -> None:
    """注销窗口实例（按 id 移除对应弱引用，忽略未登记 / 已回收情况）。"""
    target_id = id(win)
    window_instances[:] = [
        r for r in window_instances
        if r() is not None and id(r()) != target_id
    ]


def alive_window_instances() -> List["OpenAIChatToolWindow"]:
    """返回当前存活窗口实例列表（剔除已回收的弱引用）。"""
    return [r() for r in window_instances if r() is not None]
