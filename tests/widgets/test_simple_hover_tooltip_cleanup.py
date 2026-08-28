# -*- coding: utf-8 -*-
"""Phase 1/2 of diagnose — aggressive reproducer attempts.

Trying many trigger sequences to find the one that reliably produces
the 'wrapped C/C++ object deleted' RuntimeError.
"""

import pytest

from app.widgets import simple_hover_tooltip as sht_mod


def _flush_events(ms=100):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _make_app_widget_tree():
    """Build a small tree with tooltip-installed widgets."""
    from PySide6.QtWidgets import QWidget

    from app.widgets.simple_hover_tooltip import install_hover_tooltip

    root = QWidget()
    root.show()
    _flush_events(20)

    # Add 10 children with tooltips
    children = []
    for i in range(10):
        c = QWidget(root)
        c.setToolTip(f"tip {i}")
        install_hover_tooltip(c)
        children.append(c)

    return root, children


def test_scenario_a_simple_destroy(_qt_app):
    """Just destroy all children, no events."""
    root, children = _make_app_widget_tree()
    _flush_events(20)

    for c in children:
        c.deleteLater()
    _flush_events(500)


def test_scenario_b_destroy_with_shown_tooltip(_qt_app):
    """Show tooltips on some children, then destroy."""
    from PySide6.QtWidgets import QWidget

    root, children = _make_app_widget_tree()
    _flush_events(20)

    # Show tooltips on some
    for c in children[:5]:
        f = sht_mod._filters.get(id(c))
        if f:
            tt = f._get_tooltip()
            tt.set_text("shown")
            tt.show_above(c)
    _flush_events(100)

    # Destroy all
    for c in children:
        c.deleteLater()
    _flush_events(500)


def test_scenario_c_hide_then_destroy(_qt_app):
    """Show tooltips, manually hide some, then destroy."""
    root, children = _make_app_widget_tree()
    _flush_events(20)

    for c in children[:3]:
        f = sht_mod._filters.get(id(c))
        if f:
            tt = f._get_tooltip()
            tt.set_text("shown")
            tt.show_above(c)
    _flush_events(50)

    # Hide some tooltips manually
    for c in children[:3]:
        f = sht_mod._filters.get(id(c))
        if f and f._tooltip:
            f._tooltip.hide()
    _flush_events(50)

    # Destroy
    for c in children:
        c.deleteLater()
    _flush_events(500)


def test_scenario_d_deleteLater_then_immediate_access(_qt_app):
    """deleteLater on parent, then try to access tooltip."""
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    parent.show()
    _flush_events(20)

    child = QWidget(parent)
    child.setToolTip("test")
    f = sht_mod._filters.get(id(child))

    tt = f._get_tooltip()
    tt.set_text("hello")
    tt.show_above(child)
    _flush_events(50)

    # Trigger leave event to hide tooltip
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QHoverEvent

    leave = QHoverEvent(QEvent.HoverLeave, QPoint(0, 0), QPoint(0, 0))
    # Don't actually send — just destroy
    child.deleteLater()
    _flush_events(500)


def test_scenario_e_close_main_window(_qt_app):
    """Close the main widget tree like app shutdown would."""
    root, children = _make_app_widget_tree()
    _flush_events(20)

    for c in children:
        f = sht_mod._filters.get(id(c))
        if f:
            tt = f._get_tooltip()
            tt.set_text("shown")
            tt.show_above(c)
    _flush_events(50)

    # Simulate app shutdown: close root
    root.close()
    _flush_events(50)

    root.deleteLater()
    _flush_events(500)


def test_scenario_f_quit_app_subprocess(_qt_app):
    """Destroy all top-level windows like QApplication.quit() would."""
    from PySide6.QtWidgets import QWidget

    roots = []
    for _ in range(3):
        r = QWidget()
        r.show()
        roots.append(r)
        c = QWidget(r)
        c.setToolTip("hi")
        sht_mod._filters.get(id(c))
    _flush_events(50)

    # Close all top-level windows
    for r in roots:
        r.close()
        r.deleteLater()
    _flush_events(500)


def test_scenario_g_timer_fires_during_destruction(_qt_app):
    """Tooltip timer fires after parent starts destroying."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    from app.widgets.simple_hover_tooltip import install_hover_tooltip

    parent = QWidget()
    parent.show()
    _flush_events(20)

    child = QWidget(parent)
    child.setToolTip("test")
    f = install_hover_tooltip(child)

    # Don't show tooltip yet — let timer fire AFTER deleteLater
    # First, queue deleteLater with 0 delay
    QTimer.singleShot(0, child.deleteLater)
    QTimer.singleShot(50, lambda: f._timer.start())
    _flush_events(500)