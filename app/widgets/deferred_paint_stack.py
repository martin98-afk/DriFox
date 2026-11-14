# -*- coding: utf-8 -*-
"""切页首绘合并化的 QStackedWidget 派生类（#25）。

背景（plan #24 实测）：会话切换顿挫的 88% 成分是切页后整页首次 paint
（propagate 8.4ms / paint 61.8ms），show 传播本身占比很小。本类把首绘
推迟到事件循环下一拍：切页期间冻结 updates，下一拍恢复时 Qt 语义保证
schedule 一次完整重绘——传播与首绘不再同拍串联，多次切页的首绘自然合并。

env 开关 ``DRIFOX_TAB_SWITCH_DEFERRED_PAINT``：默认开；``0``/``false``/``no``
走原生路径（可回退）。import 期读取固化，进程内不热变，对齐
webview_pool._load_max_idle_per_bucket 先例。
"""

from __future__ import annotations

import os

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QStackedWidget


def _load_deferred_paint_enabled() -> bool:
    """实验开关读取：缺失/空/未知值一律默认开，显式 0/false/no 关闭。"""
    v = os.environ.get("DRIFOX_TAB_SWITCH_DEFERRED_PAINT", "").strip().lower()
    return v not in ("0", "false", "no")


# perf: (#25) import 期固化开关，进程内不热变（实验按进程设 env 启动）
DEFERRED_PAINT_ENABLED = _load_deferred_paint_enabled()


class DeferredPaintStack(QStackedWidget):
    """setCurrent* 首绘推迟到下一拍合并（#25）。

    切页动作包裹 ``setUpdatesEnabled(False)`` → 原生切换 → 下一拍恢复。
    恢复（True）即 Qt 调度一次整页重绘；半途再次切页时 False 幂等（Qt 的
    updates 标志非计数），恢复任务由下一次 singleShot 覆盖，无泄漏。
    """

    def setCurrentWidget(self, widget) -> None:  # noqa: N802 (Qt 命名)
        if not DEFERRED_PAINT_ENABLED:
            super().setCurrentWidget(widget)
            return
        # perf: (#25) 切页冻结 updates，下一拍合并整页首绘（paint 61.8ms 不再
        # 与 show 传播同拍串联）
        self.setUpdatesEnabled(False)
        try:
            super().setCurrentWidget(widget)
        finally:
            QTimer.singleShot(0, self._flush_deferred_paint)

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 (Qt 命名)
        if not DEFERRED_PAINT_ENABLED:
            super().setCurrentIndex(index)
            return
        # perf: (#25) 同 setCurrentWidget：冻结 + 下一拍合并首绘
        self.setUpdatesEnabled(False)
        try:
            super().setCurrentIndex(index)
        finally:
            QTimer.singleShot(0, self._flush_deferred_paint)

    def _flush_deferred_paint(self) -> None:
        """下一拍恢复 updates（恢复即触发一次合并重绘）；幂等。"""
        if not self.updatesEnabled():
            self.setUpdatesEnabled(True)
