# -*- coding: utf-8 -*-
"""守卫测试：遮罩对话框隐藏/恢复后外层滚动位置回设。

根因：MaskDialog 出现 → CodeWebViewer._hide_for_dialog 调 hide() →
MessageCard 自身高度未被 fixed 钉住，卡片 sizeHint 随 viewer 隐藏塌缩 →
chat_scroll_area 内容总高骤减 → 滚动条 value 被 Qt 自动 clamp；
对话框关闭 → viewer.show() 高度恢复，但无代码回设滚动值 → 位置丢失。

修复：首个 viewer 隐藏前记录外层滚动条 value，恢复显示后
QTimer.singleShot(0)（等 posted LayoutRequest 处理完）回设。

测试用轻量 QWidget 替身绑定 CodeWebViewer 的方法，不拉起 WebEngine。
"""

import sys

from PyQt5.QtCore import QCoreApplication, Qt
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# 必须在 QtWebEngine（经 app.widgets.message_card 间接导入）之前设置
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets.message_card import CodeWebViewer  # noqa: E402


class _FakeViewer(QWidget):
    """绑定 CodeWebViewer 遮罩对话框方法的替身（逻辑一致，无 WebEngine）。"""

    _find_chat_scroll_area = CodeWebViewer._find_chat_scroll_area
    _hide_for_dialog = CodeWebViewer._hide_for_dialog
    _restore_from_dialog = CodeWebViewer._restore_from_dialog
    _restore_chat_scroll_pos = CodeWebViewer._restore_chat_scroll_pos

    def __init__(self):
        super().__init__()
        self._hidden_dialogs = set()
        self._saved_dialog_scroll_pos = -1
        self.setFixedHeight(800)


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_host(viewer):
    """假宿主链：host(带 chat_scroll_area) → 容器(含 viewer，撑出滚动范围)。"""
    host = QWidget()
    host.resize(600, 400)
    area = QScrollArea(host)
    area.resize(600, 400)
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.addWidget(QLabel("顶部内容"))
    layout.addWidget(viewer)
    layout.addWidget(QLabel("底部内容"))
    area.setWidget(container)
    area.setWidgetResizable(True)
    host.chat_scroll_area = area
    host.show()
    return host, area


def test_hide_records_scroll_pos(qapp):
    viewer = _FakeViewer()
    host, area = _make_host(viewer)
    bar = area.verticalScrollBar()
    bar.setValue(300)
    dialog = QDialog(host)

    viewer._hide_for_dialog(dialog)

    assert viewer._saved_dialog_scroll_pos == 300, "隐藏前应记录外层滚动值"
    assert viewer.isHidden(), "遮罩对话框显示时 viewer 应隐藏"
    assert dialog in viewer._hidden_dialogs


def test_repeated_show_no_recount(qapp):
    viewer = _FakeViewer()
    host, area = _make_host(viewer)
    bar = area.verticalScrollBar()
    bar.setValue(120)
    dialog = QDialog(host)

    viewer._hide_for_dialog(dialog)
    bar.setValue(50)  # 模拟 clamp 后位置已变
    viewer._hide_for_dialog(dialog)  # 同一对话框重复 Show 不覆盖记录

    assert viewer._saved_dialog_scroll_pos == 120, "重复 Show 不应覆盖首次记录值"


def test_restore_sets_scroll_back(qapp):
    viewer = _FakeViewer()
    host, area = _make_host(viewer)
    bar = area.verticalScrollBar()
    bar.setValue(300)
    dialog = QDialog(host)

    viewer._hide_for_dialog(dialog)
    bar.setValue(0)  # 模拟 hide 期间布局塌缩导致的 value clamp
    dialog.finished.emit(0)  # 对话框关闭 → finished 信号触发恢复

    assert not viewer.isHidden(), "对话框关闭后 viewer 应恢复显示"
    assert viewer._saved_dialog_scroll_pos == -1, "恢复后应清空记录"
    # singleShot(0) 等布局重排完成后回设
    QApplication.processEvents()
    QApplication.processEvents()
    assert bar.value() == 300, f"滚动位置应回设到 300，实际 {bar.value()}"


def test_restore_via_destroy_fallback(qapp):
    """eventFilter 兜底路径（Hide/Close/Destroy）同样回设滚动位置。"""
    viewer = _FakeViewer()
    host, area = _make_host(viewer)
    bar = area.verticalScrollBar()
    bar.setValue(200)
    dialog = QDialog(host)

    viewer._hide_for_dialog(dialog)
    bar.setValue(0)
    # 模拟 _DialogEventFilter._dispatch 的兜底分支：discard 后 show + 回设
    hidden = viewer._hidden_dialogs
    if dialog in hidden:
        hidden.discard(dialog)
    if not hidden:
        viewer.show()
        viewer._restore_chat_scroll_pos()

    QApplication.processEvents()
    QApplication.processEvents()
    assert not viewer.isHidden()
    assert bar.value() == 200, f"兜底路径滚动位置应回设到 200，实际 {bar.value()}"


if __name__ == "__main__":
    _ensure_qapp()
    test_hide_records_scroll_pos(_ensure_qapp())
    test_repeated_show_no_recount(_ensure_qapp())
    test_restore_sets_scroll_back(_ensure_qapp())
    test_restore_via_destroy_fallback(_ensure_qapp())
    print("4 项守卫测试全部通过")
