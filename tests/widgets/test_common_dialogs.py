# -*- coding: utf-8 -*-
"""通用弹框（ConfirmDialog 等）长文本不裁切回归测试

背景：弹框内容自适应原先只靠 `adjustSize()`，而含 wordWrap 文本的 widget
其 `sizeHint` 高度按「理想宽度」换行估算；理想宽度超过 `maximumSize` 被
截短后，同一段文本实际需要更高的高度，layout 把内容 label 压缩后文字垂直
溢出被裁（「加载团队模板」确认框首行/末行各露半截字）。

修复：`_fit_dialog_widget` 在 `adjustSize()` 后按实际宽度用 `heightForWidth`
重求高度。本文件锁定该行为：内容 label 高度必须 ≥ 文本按当前宽度换行的
真实需求高度；短文本时高度仍按 DEFAULT_HEIGHT 保底不膨胀。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QLabel


def _ensure_qapp():
    """确保 QApplication 可用"""
    return QApplication.instance() or QApplication(sys.argv)


def _host():
    """MaskDialogBase 要求非空 parent（按其尺寸铺遮罩），给个固定尺寸容器"""
    from PyQt5.QtWidgets import QWidget

    host = getattr(_host, "_instance", None)
    if host is None:
        host = QWidget()
        host.resize(1400, 800)
        host.show()
        QApplication.instance().processEvents()
        _host._instance = host
    return host


def _needed_text_height(label: QLabel) -> int:
    """文本按 label 当前宽度换行后的真实需求高度"""
    fm = label.fontMetrics()
    return fm.boundingRect(
        0, 0, label.width(), 100000, Qt.TextWordWrap, label.text()
    ).height()


def _content_labels(widget):
    """弹框内所有开启自动换行的 BodyLabel"""
    return [lbl for lbl in widget.findChildren(QLabel) if lbl.wordWrap()]


LONG_CONTENT = (
    "确定要加载模板「default-team」吗？\n"
    "将新建 4 个全新窗口并加入团队，已有标签页不会被修改或移动。\n\n"
    "将加载角色：leader、plan、build、review"
)


class TestConfirmDialogFit:
    def test_long_content_not_clipped(self):
        """长文本（wordWrap + 宽度被 maximumSize 截短）：内容 label 不被裁切"""
        _ensure_qapp()
        from app.widgets.common_dialogs import ConfirmDialog

        dlg = ConfirmDialog(title="加载团队模板", content=LONG_CONTENT, parent=_host())
        dlg.show()
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        clipped = [
            (lbl.text()[:16], _needed_text_height(lbl), lbl.height())
            for lbl in _content_labels(dlg.widget)
            if _needed_text_height(lbl) > lbl.height()
        ]
        assert not clipped, f"内容被裁切: {clipped}"

    def test_long_content_height_grows_beyond_default(self):
        """长文本时 widget 高度按 heightForWidth 撑开，超过 DEFAULT_HEIGHT"""
        _ensure_qapp()
        from app.widgets.common_dialogs import ConfirmDialog

        dlg = ConfirmDialog(title="加载团队模板", content=LONG_CONTENT, parent=_host())
        dlg.show()
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        assert dlg.widget.height() > ConfirmDialog.DEFAULT_HEIGHT, (
            f"长文本高度未撑开: {dlg.widget.height()} <= {ConfirmDialog.DEFAULT_HEIGHT}"
        )

    def test_short_content_keeps_min_height(self):
        """短文本：高度按 DEFAULT_HEIGHT 保底，不因 helper 改动而膨胀"""
        _ensure_qapp()
        from app.widgets.common_dialogs import ConfirmDialog

        dlg = ConfirmDialog(title="确认", content="确定吗？", parent=_host())
        dlg.show()
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        assert ConfirmDialog.DEFAULT_HEIGHT <= dlg.widget.height() <= ConfirmDialog.DEFAULT_HEIGHT + 30

    def test_info_dialog_long_content_not_clipped(self):
        """InfoDialog 长文本同样不裁切（共用 _fit_dialog_widget）"""
        _ensure_qapp()
        from app.widgets.common_dialogs import InfoDialog

        dlg = InfoDialog(title="提示", content=LONG_CONTENT, parent=_host())
        dlg.show()
        app = QApplication.instance()
        for _ in range(3):
            app.processEvents()
        clipped = [
            (lbl.text()[:16], _needed_text_height(lbl), lbl.height())
            for lbl in _content_labels(dlg.widget)
            if _needed_text_height(lbl) > lbl.height()
        ]
        assert not clipped, f"内容被裁切: {clipped}"
