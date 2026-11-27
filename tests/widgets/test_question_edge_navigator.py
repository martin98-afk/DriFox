from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QScrollArea, QWidget

from app.widgets.question_edge_navigator import QuestionEdgeNavigator


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
    edge.set_questions(['提问'] * 10000, 'a')
    surface = edge.surface
    right = surface.geometry().right()
    QTest.keyClick(edge, Qt.Key_Return)
    QTest.qWait(175)
    assert edge.surface is surface and surface.isWindow()
    assert surface.geometry().right() == right
    assert (viewport.geometry(), body.geometry()) == bounds
    QTest.keyClick(edge, Qt.Key_End)
    QTest.keyClick(edge, Qt.Key_Return)
    assert selected == [9999]
    QTest.qWait(180)
    assert surface.width() == 22
    assert not edge.enter_timer.isActive() and not edge.leave_timer.isActive()
    area.close()
    assert not surface.isVisible()


def test_session_switch_cancels_slide_and_empty_hides_rail(qapp):
    viewport = QWidget()
    viewport.resize(640, 480)
    viewport.show()
    edge = QuestionEdgeNavigator(viewport)
    edge.set_questions(['旧会话'], 'a')
    QTest.keyClick(edge, Qt.Key_Return)
    edge.set_questions(['新会话'], 'b')
    assert not edge.opened and edge.surface.width() == 22
    assert edge.rows == ['新会话']
    edge.expand(keyboard=True)
    QTest.keyClick(edge, Qt.Key_Escape)
    QTest.qWait(180)
    assert not edge.opened
    edge.set_questions([], 'c')
    assert not edge.surface.isVisible()
    viewport.close()


def test_idle_rail_and_reading_position(qapp):
    from PyQt5.QtCore import QEvent
    from app.utils.utils import get_unified_font

    viewport = QWidget()
    viewport.resize(600, 480)
    viewport.show()
    edge = QuestionEdgeNavigator(viewport)
    edge.set_questions(['测试'] * 70, 'a')
    edge.set_reading(48)
    assert edge.offset <= 48 < edge.offset + edge.count_visible()
    configured = get_unified_font(11)
    assert (edge.font().family(), edge.font().pointSizeF()) == (configured.family(), configured.pointSizeF())
    image = edge.surface.grab().toImage()
    assert max(image.pixelColor(x, y).alpha() for x in range(image.width()) for y in range(image.height())) <= 1
    qapp.sendEvent(edge.surface, QEvent(QEvent.Enter))
    hover = edge.surface.grab().toImage()
    assert max(hover.pixelColor(x, y).alpha() for x in range(hover.width()) for y in range(hover.height())) > 150
    edge.expand()
    edge.current = 45
    edge.set_reading(49)
    assert (edge.current, edge.reading) == (45, 49)
    viewport.close()


def test_approaching_card_footprint_only_reveals_ticks(qapp):
    from PyQt5.QtCore import QEvent, QPoint
    from PyQt5.QtGui import QHoverEvent

    viewport = QWidget()
    viewport.resize(600, 480)
    viewport.show()
    edge = QuestionEdgeNavigator(viewport)
    edge.set_questions(['提问'] * 20, 's')
    for point, inside in [(QPoint(270, 85), True), (QPoint(500, 380), True), (QPoint(200, 260), False)]:
        qapp.sendEvent(viewport, QHoverEvent(QEvent.HoverMove, point, QPoint()))
        assert edge.hovered is inside
        assert edge.surface.width() == 22 and not edge.enter_timer.isActive() and not edge.opened
    viewport.close()
