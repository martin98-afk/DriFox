"""Windows 流式完成通知，在第一次显示时才加载 WinRT。"""

from collections.abc import Callable
from pathlib import Path

from windows_toasts import (
    InteractableWindowsToaster,
    Toast,
    ToastActivatedEventArgs,
    ToastAudio,
    ToastButton,
    ToastInputTextBox,
)
from windows_toasts.register_hkey_aumid import register_hkey


class WindowsReplyToaster:
    def __init__(self, activated: Callable[[str, str, str | None], None]) -> None:
        app_id = "DriFox.Chat"
        icon = Path(__file__).resolve().parents[2] / "icons" / "drifox.ico"
        register_hkey(app_id, "DriFox", icon if icon.is_file() else None, activator=True)
        self._toaster = InteractableWindowsToaster("DriFox", app_id)
        self._activated = activated
        self._toaster.on_activated = self._on_activated

    def show(self, title: str, message: str, window_id: str, session_id: str) -> None:
        field = ToastInputTextBox("message", placeholder="回复 DriFox")
        toast = Toast(
            [title, message],
            group=window_id,
            tag=session_id,
            inputs=[field],
            actions=[ToastButton("回复", "reply", relatedInput=field, background=True)],
            audio=ToastAudio(silent=True),  # 提示音由主窗口按用户设置播放
            expires_on_reboot=True,
        )
        self._toaster.show_toast(toast)

    def _on_activated(self, event: ToastActivatedEventArgs) -> None:
        if not event.group or not event.tag:
            return
        if event.arguments == "reply":
            text = (event.inputs or {}).get("message", "").strip()
            if text:
                self._activated(event.group, event.tag, text)
        elif event.arguments == event.tag:
            self._activated(event.group, event.tag, None)

    def close(self) -> None:
        self._toaster.on_activated = None
        self._toaster.clear_toasts()
