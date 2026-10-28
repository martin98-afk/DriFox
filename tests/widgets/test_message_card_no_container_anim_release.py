# -*- coding: utf-8 -*-
"""回归：``noContainerAnimation`` 标志必须能被复位（PyQt5 三参 singleShot 崩溃）。

## 现象

实机日志在每个卡片的高度更新链路上刷屏:

    TypeError: arguments did not match any overloaded call:
      singleShot(msec: int, slot: PYQT_SLOT): too many arguments
      singleShot(msec: int, timerType: Qt.TimerType, slot: PYQT_SLOT):
      argument 2 has unexpected type 'MessageCard'

## 根因

``QTimer.singleShot(ms, self, fn)``（context + functor）是 Qt6 / PySide6 的重载；
PyQt5 只有 ``singleShot(ms, slot)`` 与 ``singleShot(ms, Qt.TimerType, slot)``，
传 self 会走 TimerType 重载并抛 TypeError。

后果不止是报错：标志位永远停在 ``True``，该卡片之后所有高度变化都退化为
无动画 snap（容器 200ms 过渡被永久跳过）。

## 修复

改为 parent=self 的一次性 ``QTimer``（同 ``_schedule_stat_refresh`` /
``card_render_core._defer_emit``），卡片销毁时定时器一并析构。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.widgets.message_card as mc  # noqa: E402

_SRC = Path(mc.__file__)


def _pump(ms: int = 120) -> None:
    """跑一段事件循环，让排队的定时器到期。"""
    QApplication.processEvents()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.start(ms)
    while deadline.isActive():
        QApplication.processEvents()


# ── 源码守卫：锁死「PyQt5 不支持的三参 singleShot」这一 bug 形态 ──────────
def test_no_context_overload_singleshot_in_source():
    """全文件不得出现 ``QTimer.singleShot(ms, <非 TimerType>, ...)``。

    PyQt5 的 TimerType 重载第二参必须是 ``Qt.XXX`` 属性；传 QObject 会 TypeError。
    Qt6/PySide6 才有 context+functor 重载，移植/抄代码时最容易踩这条。
    """
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "singleShot"):
            continue
        if len(node.args) < 3:
            continue
        second = node.args[1]
        is_timer_type = isinstance(second, ast.Attribute) and isinstance(second.value, ast.Name) and second.value.id == "Qt"
        if not is_timer_type:
            bad.append(node.lineno)
    assert not bad, f"第 {bad} 行使用了 PyQt5 不支持的 singleShot 三参重载（context + functor）"


# ── 行为：标志位确实被复位 ──────────────────────────────────────────
def test_release_resets_flag(qapp):
    """延迟到期后 noContainerAnimation 必须回到 False。"""
    card = mc.MessageCard(role="assistant")
    card.setProperty("noContainerAnimation", True)

    card._release_no_container_animation(0)
    _pump()

    assert card.property("noContainerAnimation") is False


def test_release_timer_bound_to_card_lifetime(qapp):
    """定时器 parent 必须是卡片本身，且同卡复用同一个实例（不堆积）。"""
    card = mc.MessageCard(role="assistant")

    card._release_no_container_animation(10_000)
    timer = card._no_anim_release_timer
    assert timer.parent() is card, "定时器必须绑定卡片生命周期"
    assert timer.isActive()

    card._release_no_container_animation(20_000)
    assert card._no_anim_release_timer is timer, "重复调用应复用同一个定时器"
    assert timer.remainingTime() > 10_000


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
