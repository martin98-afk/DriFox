from PyQt5.QtCore import QEvent, QPointF, Qt
from PyQt5.QtGui import QMouseEvent, QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QWidget

from app.widgets.image_lightbox import ImageLightbox


def test_zoom_anchor_pan_and_reset(qapp):
    owner = QWidget()
    owner.resize(600, 450)
    owner.show()
    pix = QPixmap(1400, 900)
    pix.fill(Qt.red)
    view = ImageLightbox(pix, owner)
    view.show()
    qapp.processEvents()
    anchor = QPointF(135, 180)
    before = (anchor - view.origin) / view.scale
    view.zoom(24, anchor)
    assert ((anchor - view.origin) / view.scale - before).manhattanLength() < 1e-6
    assert view.scale == 24 and view.image.size() == pix.size()
    origin = QPointF(view.origin)
    for typ, pos, button, buttons in [(QEvent.MouseButtonPress, anchor, Qt.LeftButton, Qt.LeftButton),
            (QEvent.MouseMove, anchor + QPointF(48, 31), Qt.NoButton, Qt.LeftButton),
            (QEvent.MouseButtonRelease, anchor + QPointF(48, 31), Qt.LeftButton, Qt.NoButton)]:
        qapp.sendEvent(view, QMouseEvent(typ, pos, button, buttons, Qt.NoModifier))
    assert view.origin == origin + QPointF(48, 31)
    QTest.keyClick(view, Qt.Key_0)
    assert view.fitted and view.scale < 1
    QTest.keyClick(view, Qt.Key_Escape)
    assert not view.isVisible() and owner.isVisible()
    owner.close()
