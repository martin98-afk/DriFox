from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.infra import window_registry
from app.tray_manager import TrayManager


def _chat(window_id, session_id):
    return SimpleNamespace(
        _window_id=window_id,
        session_manager=SimpleNamespace(get_current_session=lambda: SimpleNamespace(session_id=session_id)),
        _on_send_clicked=Mock(),
    )


def test_reply_targets_original_chat_and_preserves_composer(monkeypatch):
    first, second = _chat("win_01", "a"), _chat("win_02", "b")
    monkeypatch.setattr(window_registry, "alive_window_instances", lambda: [second, first])
    tray = Mock()
    TrayManager._on_chat_notification(tray, "win_01", "a", "  继续  ")
    first._on_send_clicked.assert_called_once_with(user_text="继续", preserve_input=True)
    second._on_send_clicked.assert_not_called()
    tray._show_window.assert_not_called()


@pytest.mark.parametrize("windows", [[], [_chat("win_01", "new-session")]])
def test_stale_notification_reports_unsent_reply(monkeypatch, windows):
    monkeypatch.setattr(window_registry, "alive_window_instances", lambda: windows)
    tray = Mock()
    TrayManager._on_chat_notification(tray, "win_01", "old-session", "继续")
    tray.notify.assert_called_once()
    for chat in windows:
        chat._on_send_clicked.assert_not_called()


def test_body_click_uses_current_tab_index(monkeypatch):
    chat = _chat("win_01", "a")
    monkeypatch.setattr(window_registry, "alive_window_instances", lambda: [chat])
    tray = Mock()
    tray._tab_manager_window._window_to_index = {id(chat): 3}
    TrayManager._on_chat_notification(tray, "win_01", "a", None)
    tray._show_window.assert_called_once_with(chat)
    tray._tab_manager_window._tab_panel.set_active_index.assert_called_once_with(3)
    chat._on_send_clicked.assert_not_called()


def test_native_notification_actions(monkeypatch):
    pytest.importorskip("windows_toasts")
    from app.utils import windows_reply_toast as native

    shell = Mock()
    activated = Mock()
    monkeypatch.setattr(native, "register_hkey", Mock())
    monkeypatch.setattr(native, "InteractableWindowsToaster", lambda *args: shell)
    toaster = native.WindowsReplyToaster(activated)
    toaster.show("会话 A", "完成", "win_01", "session-a")
    toast = shell.show_toast.call_args.args[0]
    button, field = toast.actions[0], toast.inputs[0]
    assert button.relatedInput == field and button.background

    def click(action, text):
        shell.on_activated(native.ToastActivatedEventArgs(action, {field.input_id: text}, toast.tag, toast.group))

    click(button.arguments, "   ")
    activated.assert_not_called()
    click(button.arguments, "收到")
    activated.assert_called_once_with("win_01", "session-a", "收到")
    click(toast.tag, "")
    activated.assert_called_with("win_01", "session-a", None)
