from pathlib import Path

import pytest
from PyQt5.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt5.QtGui import QColor, QImage, QTextCursor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QWidget

from app.main_widget import OpenAIChatToolWindow
from app.widgets.bottom_input_area import AttachmentChip, SendableTextEdit
from app.widgets.image_lightbox import ImageLightbox


def test_double_click_image_and_keep_normal_text_selection(qapp, tmp_path):
    path = str(tmp_path / "截图.png")
    edit = SendableTextEdit()
    edit.resize(480, 120)
    edit.insertPlainText("before ")
    edit.insert_file_mention(path)
    edit.show()
    qapp.processEvents()
    requests = []
    edit.imagePreviewRequested.connect(requests.append)
    cursor = edit.document().find("\ufffc")
    left = QTextCursor(cursor)
    left.setPosition(cursor.selectionStart())
    start, end = edit.cursorRect(left), edit.cursorRect(cursor)
    for x in (start.x() + 3, end.x() - 3):
        QTest.mouseDClick(edit.viewport(), Qt.LeftButton, pos=QPoint(x, end.center().y()))
    assert requests == [path, path]
    QTest.mouseDClick(edit.viewport(), Qt.LeftButton, pos=edit.cursorRect(QTextCursor(edit.document())).center())
    assert edit.textCursor().selectedText() == "before"
    assert len(requests) == 2
    chip = AttachmentChip(path)
    chip.imagePreviewRequested.connect(requests.append)
    QTest.mouseDClick(chip, Qt.LeftButton)
    assert requests == [path] * 3
    edit.close()


@pytest.mark.parametrize("apply", [True, False])
def test_apply_or_cancel_annotation_in_draft(qapp, monkeypatch, tmp_path, apply):
    source = tmp_path / "截图.png"
    image = QImage(1600, 900, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    assert image.save(str(source))
    original = source.read_bytes()
    other = tmp_path / "另一个" / source.name
    other.parent.mkdir()
    assert image.save(str(other))
    paths = [QUrl.fromLocalFile(str(p)).toLocalFile() for p in (other, source)]
    host = QWidget()
    host._attachments = paths.copy()
    host.input_area = SendableTextEdit(host)
    host._rebuild_attachment_chips = lambda: None
    host.input_area.imageReferenceReplaced.connect(
        lambda old, new: OpenAIChatToolWindow._replace_image_attachment(host, old, new)
    )
    host.input_area.attachmentsRemoved.connect(lambda names: pytest.fail(f"误删附件 {names}"))
    host.input_area.insertPlainText("看这两张 ")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    host.input_area.insertFromMimeData(mime)
    host.input_area.insertPlainText(" 标出这里")
    before = host.input_area.toPlainText()
    monkeypatch.setattr("app.main_widget.get_app_data_dir", lambda: tmp_path)

    def interact(dialog):
        dialog.show()
        qapp.processEvents()
        assert dialog.owner is host and dialog.geometry().size() == host.size()
        assert dialog.windowFlags() & Qt.FramelessWindowHint
        editor = dialog.annotations
        assert not editor.tool and editor.toggle.isVisible()
        dialog.zoom(dialog.scale * 1.2, QPointF(dialog.rect().center()))
        start = (dialog.origin + QPointF(400, 300) * dialog.scale).toPoint()
        end = (dialog.origin + QPointF(800, 500) * dialog.scale).toPoint()
        editor.toggle.click()
        for tool in ("arrow", "rect", "pen"):
            editor.select_tool(tool)
            QTest.mousePress(dialog, Qt.LeftButton, pos=start)
            QTest.mouseRelease(dialog, Qt.LeftButton, pos=end)
        editor.select_tool("text")
        QTest.mouseClick(dialog, Qt.LeftButton, pos=start)
        assert editor.entry.isVisible()
        editor.entry.setText("中文注释")
        QTest.keyClick(editor.entry, Qt.Key_Return)
        assert len(editor.marks) == 4
        assert not editor.marks[-1].path.isEmpty()
        editor.undo()
        assert len(editor.marks) == 3
        if apply:
            editor.apply_button.click()
        else:
            QTest.keyClick(dialog, Qt.Key_Escape)
        return dialog.result()

    monkeypatch.setattr(ImageLightbox, "exec_", interact)
    OpenAIChatToolWindow._preview_input_image(host, paths[1])
    assert source.read_bytes() == original
    if apply:
        first, edited = host._attachments
        assert first == paths[0]
        annotated = QImage(edited)
        assert annotated.size() == image.size()
        assert annotated.pixelColor(600, 400) == QColor("#ff453a")
        assert annotated.pixelColor(40, 40).alpha() == 0
        text = host.input_area.toPlainText()
        assert text == f"看这两张 [[{source.name}]] [[{Path(edited).name}]]  标出这里"
        sent = OpenAIChatToolWindow._build_user_text_with_attachments(host, text)
        assert edited in sent and first in sent and paths[1] not in sent
        host.input_area.undo()
        assert host._attachments == paths
        assert host.input_area.toPlainText() == before
        host.input_area.redo()
        assert host._attachments == [first, edited]
        assert host.input_area.toPlainText() == text
    else:
        assert host._attachments == paths
        assert host.input_area.toPlainText() == before
        assert not (tmp_path / "screenshots").exists()
    host.close()
