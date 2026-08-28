# -*- coding: utf-8 -*-
"""Phase 1/2: Investigate Qt destruction order behavior.

We need to understand when _cleanup is actually called in PySide6.
"""

import pytest


def _flush_events(ms=100):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_destroyed_signal_after_child_destruction(_qt_app):
    """Verify: when parent is destroyed, does the destroyed slot run
    even though the filter (a Qt child) is also destroyed?"""
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QWidget

    cleanup_called = []
    filter_ref = []

    class Filter(QObject):
        def __init__(self, parent):
            super().__init__(parent)
            self._standalone = QWidget()  # no Qt parent — independent
            parent.destroyed.connect(self._cleanup)
            filter_ref.append(self)

        def _cleanup(self):
            cleanup_called.append(True)
            try:
                self._standalone.hide()
            except RuntimeError as e:
                cleanup_called.append(("error", str(e)))

    parent = QWidget()
    parent.show()
    _flush_events(20)

    f = Filter(parent)
    f._standalone.show()
    _flush_events(50)

    # Destroy parent
    parent.deleteLater()
    _flush_events(200)

    print(f"\ncleanup_called = {cleanup_called}")
    print(f"filter C++ alive: {f.__bool__() if hasattr(f, '__bool__') else 'n/a'}")
    # We expect: cleanup_called should NOT contain anything,
    # because filter is destroyed before parent's destroyed fires.


def test_destroyed_with_external_widget_deleteLater(_qt_app):
    """Test: when parent is destroyed AND the standalone widget is also
    deleteLater'd externally before, does cleanup get called and fail?"""
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtWidgets import QWidget

    cleanup_called = []

    class Filter(QObject):
        def __init__(self, parent):
            super().__init__(parent)
            self._standalone = QWidget()
            self._standalone.show()
            parent.destroyed.connect(self._cleanup)

        def _cleanup(self):
            cleanup_called.append("start")
            try:
                self._standalone.hide()
            except RuntimeError as e:
                cleanup_called.append(("error", str(e)))
            cleanup_called.append("end")

    parent = QWidget()
    parent.show()
    _flush_events(20)

    f = Filter(parent)
    f._standalone.show()
    _flush_events(50)

    # Delete standalone BEFORE destroying parent
    f._standalone.deleteLater()
    _flush_events(50)

    # Now destroy parent
    parent.deleteLater()
    _flush_events(200)

    print(f"\ncleanup_called = {cleanup_called}")