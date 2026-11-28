from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent, QWheelEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QScrollArea, QWidget

from app.utils import app_state
from app.widgets.question_edge_navigator import QuestionEdgeNavigator, QuestionPreview, question_previews


def test_navigation_keeps_chat_geometry_and_browses_all_rows(qapp):
    area = QScrollArea()
    body = QWidget()
    body.resize(580, 2000)
    area.setWidget(body)
    area.resize(640, 480)
    area.show()
    qapp.processEvents()
    viewport = area.viewport()
    bounds = viewport.geometry(), body.geometry()
    edge = QuestionEdgeNavigator(viewport)
    selected = []
    edge.questionClicked.connect(selected.append)
    edge.set_questions([QuestionPreview(str(i), str(i)) for i in range(10000)], "a")
    rail = edge.surface.geometry()
    origin = viewport.mapToGlobal(QPoint())
    assert 0 <= rail.left() - origin.x() < 24
    assert abs(rail.center().y() - origin.y() - viewport.height() // 2) <= 1
    QTest.keyClick(edge, Qt.Key_Return)
    QTest.qWait(400)
    assert edge.preview.isVisible() and edge.preview.geometry().left() > rail.right()
    assert edge.surface.geometry().left() == rail.left()
    assert (viewport.geometry(), body.geometry()) == bounds
    QTest.keyClick(edge, Qt.Key_End)
    QTest.keyClick(edge, Qt.Key_Return)
    assert selected == [9999]
    QTest.qWait(400)
    assert not edge.preview.isVisible()
    assert not edge.enter_timer.isActive() and not edge.leave_timer.isActive()
    area.close()
    assert not edge.surface.isVisible()


def test_session_switch_cancels_slide_and_empty_hides_rail(qapp):
    viewport = QWidget()
    viewport.resize(640, 480)
    viewport.show()
    edge = QuestionEdgeNavigator(viewport)
    edge.set_questions([QuestionPreview("旧会话", "old")], "a")
    QTest.keyClick(edge, Qt.Key_Return)
    edge.anim.setCurrentTime(60)
    assert 0 < edge.progress < 1
    edge.set_questions([QuestionPreview("新会话", "new")], "b")
    assert not edge.opened and not edge.preview.isVisible()
    edge.expand(keyboard=True)
    QTest.keyClick(edge, Qt.Key_Escape)
    QTest.qWait(200)
    assert not edge.opened
    edge.set_questions([], "c")
    assert not edge.surface.isVisible() and not edge.preview.isVisible()
    viewport.close()


def test_hover_tracks_row_keeps_reading_and_persists_bookmark(qapp, monkeypatch):
    store = {}
    monkeypatch.setattr(app_state, "get", store.get)
    monkeypatch.setattr(app_state, "set", store.__setitem__)
    viewport = QWidget()
    viewport.resize(620, 480)
    viewport.show()
    edge = QuestionEdgeNavigator(viewport)
    rows = [QuestionPreview(f"问题 {i}", str(i), [f"这是第 {i} 条回复。" * 30]) for i in range(70)]
    selected = []
    edge.questionClicked.connect(selected.append)
    edge.set_questions(rows, "s")
    edge.set_reading(48)
    start, end = edge.slot_bounds(edge.slot_for_row(48))
    assert start <= 48 < end
    rail = edge.surface
    image = rail.grab().toImage()
    assert max(image.pixelColor(x, y).alpha() for x in range(image.width()) for y in range(image.height())) > 100
    pos = QPoint(8, 24)
    idx = rail.row_at(pos)
    qapp.sendEvent(rail, QMouseEvent(QEvent.MouseMove, QPointF(pos), Qt.NoButton, Qt.NoButton, Qt.NoModifier))
    QTest.qWait(275)
    assert edge.current == idx and edge.preview.title == rows[idx].title
    assert edge.preview.body.endswith("…")
    edge.set_reading(49)
    assert (edge.current, edge.reading) == (idx, 49)
    qapp.sendEvent(rail, QEvent(QEvent.Leave))
    qapp.sendEvent(edge.preview, QEvent(QEvent.Enter))
    QTest.qWait(200)
    assert edge.opened
    QTest.mouseClick(edge.preview.bookmark, Qt.LeftButton)
    assert edge.preview.bookmark.isChecked() and rows[idx].key in edge.bookmarks
    QTest.mouseClick(edge.preview, Qt.LeftButton, pos=QPoint(30, 27))
    assert selected == [idx]
    edge.set_questions(rows[:2], "other")
    assert not edge.bookmarks
    edge.set_questions(rows, "s")
    assert edge.bookmarks == {rows[idx].key}
    viewport.close()


def test_wheel_keeps_preview_under_pointer_when_viewport_is_narrow(qapp):
    viewport = QWidget()
    viewport.resize(210, 125)
    viewport.show()
    edge = QuestionEdgeNavigator(viewport)
    rows = [QuestionPreview(f"条目 {i}", str(i), ["长摘要" * 60]) for i in range(40)]
    edge.set_questions(rows, "wheel")
    rail = edge.surface
    pos = QPoint(9, 25)
    event = QWheelEvent(
        QPointF(pos),
        QPointF(rail.mapToGlobal(pos)),
        QPoint(),
        QPoint(0, -120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    before = rail.row_at(pos)
    qapp.sendEvent(rail, event)
    assert edge.current == before + 1 == rail.row_at(pos)
    edge.expand()
    edge.anim.setCurrentTime(edge.anim.duration())
    origin = viewport.mapToGlobal(QPoint())
    assert viewport.rect().contains(edge.preview.geometry().translated(-origin))
    selected = []
    edge.questionClicked.connect(selected.append)
    expected = edge.current
    QTest.mouseClick(rail, Qt.LeftButton, pos=pos)
    assert selected == [expected]
    viewport.close()


def test_preview_pairs_consecutive_questions_and_skips_internal_content():
    rows = question_previews(
        [
            {"role": "user", "content": "相同的问题"},
            {"role": "user", "content": "相同的问题"},
            {"role": "assistant", "content": "正在查询"},
            {"role": "user", "content": "内部事件", "_hook_event": "SessionStart"},
            {"role": "tool", "content": "内部工具结果"},
            {"role": "assistant", "content": [{"type": "text", "text": "<think>过程</think>**答复**"}]},
            {"role": "assistant", "content": [{"type": "reasoning", "content": "内部推理"}]},
        ]
    )
    assert [r.excerpt for r in rows] == ["", "答复"]
    assert rows[0].key != rows[1].key


def test_scroll_does_not_reassign_long_history_ticks(qapp):
    panel = QWidget()
    panel.setObjectName("chatWrapper")
    panel.resize(1100, 550)
    area = QScrollArea(panel)
    area.setGeometry(200, 30, 700, 410)
    panel.show()
    edge = QuestionEdgeNavigator(area.viewport(), area)
    edge.set_questions([QuestionPreview(str(i), str(i)) for i in range(10000)], "long")
    rail = edge.surface
    first = QPoint(2, 5)
    last = QPoint(2, rail.height() - 6)
    points = [first, rail.rect().center(), last]
    mapping = [rail.row_at(p) for p in points]
    assert mapping[0] == 0 and mapping[-1] == 9999
    rect = rail.geometry()
    assert rect.left() == panel.mapToGlobal(QPoint()).x()
    for row in (4500, 5001, 5300, 9999, 1):
        edge.set_reading(row)
        assert mapping == [rail.row_at(p) for p in points]
        assert rail.geometry() == rect
    area.setGeometry(0, 30, 1100, 410)
    qapp.processEvents()
    narrow_bounds = area.viewport().geometry()
    assert rail.width() == 12
    edge.expand(keyboard=True)
    edge.anim.setCurrentTime(edge.anim.duration())
    assert edge.preview.isVisible() and area.viewport().geometry() == narrow_bounds
    edge.set_questions([], "empty")
    assert area.viewport().geometry() == narrow_bounds
    panel.close()
