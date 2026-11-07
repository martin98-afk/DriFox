# -*- coding: utf-8 -*-
"""系统深浅色偏好探测（外观模式「自动」的输入）

只做探测，不缓存、不落盘：是否轮询、失败如何回退由调用方
（app/utils/theme_mode.py）决定。任何平台异常都返回 None，本模块不假设
默认视觉，避免把「探测失败」静默固化成浅色。

平台实现：
- Windows：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
  的 AppsUseLightTheme（DWORD，1=浅色）。纯本地注册表调用，可低频轮询。
- macOS：``defaults read -g AppleInterfaceStyle`` 输出 Dark 即深色；
  键不存在（退出码非 0，即系统从未切过深色）按浅色处理。
- Linux：``gsettings get org.gnome.desktop.interface color-scheme``
  （prefer-dark / prefer-light），取不到再退到 GTK 主题名是否含 dark。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional


def _run(args: list) -> Optional[str]:
    """跑一个短命探测命令；失败/超时一律 None（不把异常抛给调用方）"""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or "").strip()


def _windows_prefers_light() -> Optional[bool]:
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(int(value))
    except Exception:
        return None


def _macos_prefers_light() -> Optional[bool]:
    # 键不存在时 defaults 退出码为 1，而「退出码非 0 = 浅色」是本平台的有效
    # 语义（不是失败），所以不能走 _run 的统一失败口径
    try:
        done = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if done.returncode != 0:
        return True
    return (done.stdout or "").strip().lower() != "dark"


def _linux_prefers_light() -> Optional[bool]:
    scheme = _run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
    if scheme:
        low = scheme.lower()
        if "dark" in low:
            return False
        if "light" in low or "default" in low:
            return True
    theme = _run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"])
    if theme:
        return "dark" not in theme.lower()
    return None


def system_prefers_light() -> Optional[bool]:
    """系统当前是否偏好浅色；探测不到返回 None"""
    if sys.platform.startswith("win"):
        return _windows_prefers_light()
    if sys.platform == "darwin":
        return _macos_prefers_light()
    if sys.platform.startswith("linux"):
        return _linux_prefers_light()
    return None


def poll_interval_ms() -> int:
    """轮询间隔：Windows 读注册表几乎零成本；其它平台每次起子进程，放慢"""
    if sys.platform.startswith("win"):
        return 4000
    return 12000