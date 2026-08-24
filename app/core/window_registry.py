"""进程级 / 窗口级单例状态外提（全量减负 E）。

原承载于 ``OpenAIChatToolWindow`` 的类级字段统一收口到本模块，
消除页面类充当容器级状态中心的职责：

- ``window_instances``：窗口实例登记表（原 ``_instances``）
- ``last_hot_reload_fingerprint``：插件热重载指纹（原 ``_last_hot_reload_fingerprint``）
- ``subagent_log_cleanup_timer``：子智能体日志全局清理 timer（原 ``_class_subagent_log_cleanup_timer``）

读写 API：
- ``register_window(win)`` / ``unregister_window(win)``：登记 / 注销窗口实例
- 直接访问模块级容器 ``window_instances`` / ``last_hot_reload_fingerprint`` /
  ``subagent_log_cleanup_timer`` 保持与原类级字段等价的就地读写语义。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from app.main_widget import OpenAIChatToolWindow

# ── 窗口实例登记表（原 OpenAIChatToolWindow._instances）──
window_instances: List["OpenAIChatToolWindow"] = []

# ── 插件热重载指纹（原 OpenAIChatToolWindow._last_hot_reload_fingerprint）──
last_hot_reload_fingerprint: Optional[str] = None

# ── 子智能体日志全局清理 timer（原 OpenAIChatToolWindow._class_subagent_log_cleanup_timer）──
subagent_log_cleanup_timer = None


def register_window(win: "OpenAIChatToolWindow") -> None:
    """登记窗口实例（原 ``_instances.append``）。"""
    window_instances.append(win)


def unregister_window(win: "OpenAIChatToolWindow") -> None:
    """注销窗口实例（原 ``_instances.remove``），忽略未登记情况。"""
    try:
        window_instances.remove(win)
    except ValueError:
        pass
