# -*- coding: utf-8 -*-
"""回归：GlobalCardController 的 diff 卡 closed 槽必须真的接上（PySide6 UniqueConnection 陷阱）

背景：_show_file_undo_diff 旧写法是

    self._diff_viewer_card.closed.connect(self._return_to_file_undo, type=Qt.UniqueConnection)

而 GlobalCardController 是普通类（不是 QObject 子类）。PySide6 对「非 QObject 成员函数 +
Qt.UniqueConnection」的连接会直接拒绝，且只在 stderr 打一行 qt.core.qobject.connect 警告、
不抛 Python 异常 —— 于是这个槽从来没接上过，关掉 diff 查看器不会回到文件撤销视图，
日志里也查不到任何痕迹。

测试要点：必须用**真 Qt 信号**（QObject + Signal）和**真 bound method**（不替换
_return_to_file_undo）。若把槽换成 lambda、或自造 fake signal 桩，都会绕过这个断点。
"""

import pytest
from PySide6.QtCore import QObject, Signal


class _FakeDiffCard(QObject):
    """替身 diff 卡：只提供 closed 信号，但它是真 QObject + 真 Signal"""

    closed = Signal()


class _FakeCardManager:
    """替身 CardManager：记录 hide/show，供断言 _return_to_file_undo 真的跑过"""

    def __init__(self):
        self.actions = []

    def hide_card(self, card_id, window_id):
        self.actions.append(("hide", card_id))

    def show_card(self, card_id, window_id):
        self.actions.append(("show", card_id))


def _make_controller():
    """绕开重量级 __init__ 构造 controller，只喂被测路径需要的依赖"""
    from app.widgets.cards.global_card_controller import GlobalCardController

    ctrl = GlobalCardController.__new__(GlobalCardController)
    ctrl._diff_viewer_card = _FakeDiffCard()
    ctrl._card_manager = _FakeCardManager()
    ctrl._file_undo_card = object()  # 非 None：命中「回到文件撤销视图」分支
    ctrl.show_diff_viewer = lambda html, title="": None  # 隔离真实卡片构建
    return ctrl


def test_diff_viewer_closed_returns_to_file_undo(qapp):
    """点关闭 diff 卡 → 必须回到文件撤销视图（旧写法下槽从未接上，这里必挂）"""
    ctrl = _make_controller()
    ctrl._show_file_undo_diff("<html></html>", "标题")

    ctrl._diff_viewer_card.closed.emit()

    actions = ctrl._card_manager.actions
    assert ("hide", "diff_viewer") in actions, (
        "关闭 diff 查看器应触发 _return_to_file_undo；"
        "为空说明 closed → 槽 的连接被 PySide6 静默拒绝（UniqueConnection 接非 QObject 方法）"
    )
    assert ("show", "file_undo") in actions, "应把文件撤销视图重新显示出来"


def test_diff_viewer_closed_wired_only_once(qapp):
    """反复打开 diff 不得重复连接（去重不能依赖 UniqueConnection）"""
    ctrl = _make_controller()
    for _ in range(3):
        ctrl._show_file_undo_diff("<html></html>", "标题")

    ctrl._diff_viewer_card.closed.emit()

    hides = [a for a in ctrl._card_manager.actions if a[0] == "hide"]
    assert len(hides) == 1, f"closed 只应连一次，实际触发 {len(hides)} 次 hide：{ctrl._card_manager.actions}"


def test_diff_viewer_flag_resets_on_card_rebuild(qapp):
    """diff 卡被重建时接线标志应随实例复位，不能因旧标志漏接新卡"""
    ctrl = _make_controller()
    ctrl._show_file_undo_diff("<html></html>", "标题")
    assert getattr(ctrl._diff_viewer_card, "_file_undo_wired", False) is True

    ctrl._diff_viewer_card = _FakeDiffCard()  # 模拟卡片重建
    ctrl._show_file_undo_diff("<html></html>", "标题")
    ctrl._diff_viewer_card.closed.emit()

    assert ("hide", "diff_viewer") in ctrl._card_manager.actions, "重建后的新卡也必须接上关闭槽"
