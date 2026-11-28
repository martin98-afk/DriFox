from pathlib import Path

import pytest
from PyQt5.QtCore import QMimeData, QPointF, Qt, QTimer
from PyQt5.QtGui import QImage
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QToolButton

from app.widgets.bottom_input_area import AttachmentChip
from app.widgets.image_lightbox import ImageLightbox


@pytest.mark.ui
def test_pasted_screenshot_chip_to_annotated_attachment(ui_app, qtbot, monkeypatch):
    window = ui_app["chat_window"]
    qtbot.waitUntil(lambda: not window._initialization_in_progress, timeout=15000)
    image = QImage(320, 200, QImage.Format_ARGB32)
    image.fill(Qt.white)
    mime = QMimeData()
    mime.setImageData(image)
    edit = window.input_area
    edit.insertPlainText("请看这里 ")
    edit.insertFromMimeData(mime)
    (source,) = window._attachments
    exec_dialog = ImageLightbox.exec_

    def edit_dialog(dialog):
        def mark_and_apply():
            dialog.annotations.toggle.click()
            arrow = next(b for b in dialog.findChildren(QToolButton) if b.text() == "箭头")
            arrow.click()
            start = (dialog.origin + QPointF(60, 60) * dialog.scale).toPoint()
            end = (dialog.origin + QPointF(140, 60) * dialog.scale).toPoint()
            QTest.mousePress(dialog, Qt.LeftButton, pos=start)
            QTest.mouseRelease(dialog, Qt.LeftButton, pos=end)
            apply = next(b for b in dialog.findChildren(QToolButton) if b.text() == "应用到输入框")
            apply.click()

        QTimer.singleShot(0, mark_and_apply)
        QTimer.singleShot(3000, dialog.reject)
        return exec_dialog(dialog)

    monkeypatch.setattr(ImageLightbox, "exec_", edit_dialog)
    chip = window._attach_container.findChild(AttachmentChip)
    QTest.mouseDClick(chip, Qt.LeftButton)
    qtbot.waitUntil(lambda: window._attachments != [source], timeout=5000)
    (edited,) = window._attachments
    assert window._attach_container.findChild(AttachmentChip).filepath == edited
    assert edit.toPlainText() == f"请看这里 [[{Path(edited).name}]] "
    assert window._build_user_text_with_attachments(edit.toPlainText()) == f"请看这里 {edited}"
    assert QImage(edited).pixelColor(100, 60).name() == "#ff453a"
    assert QImage(source).pixelColor(100, 60).name() == "#ffffff"
    window._clear_input_area()
    window._clear_attachments()
