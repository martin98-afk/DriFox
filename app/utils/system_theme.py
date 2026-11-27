# -*- coding: utf-8 -*-
"""系统深浅色偏好探测（外观模式「自动」的输入）

只做探测，不缓存、不落盘：是否轮询、失败如何回退由调用方
（app/utils/theme_mode.py）决定。任何平台异常都返回 None，本模块不假设
默认视觉，避免把「探测失败」静默固化成浅色。

平台实现：
- Windows：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
  的 AppsUseLightTheme（DWORD，1=浅色）。纯本地注册表调用，可低频轮询。
- macOS：进程内 CFPreferences 读 ``AppleInterfaceStyle``（ctypes 调
  CoreFoundation，等价 ``defaults read -g``），值为 Dark 即深色；键不存在
  （系统从未切过深色）按浅色处理。原实现 fork ``defaults`` 子进程，单次 ~15ms，
  且启动 1 次 + 轮询每 12s 1 次；进程内读 ~0.003ms，且 cfprefsd 会主动推送
  外部变更，轮询语义不变（实测另一进程改 -g 键，1~2s 内本进程可读到新值）。
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


_CF_BUNDLE = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CF_UTF8 = 0x08000100

def _macos_read_interface_style() -> Optional[str]:
    """进程内读全局域 AppleInterfaceStyle；键不存在返回 None。

    走 CoreFoundation 的 CFPreferences（``defaults`` 命令自身的实现层）：
    值由 cfprefsd 统一缓存并在外部变更时主动推送，所以不需要每次同步。
    返回值语义与 ``defaults read -g`` 一致（Dark / 其它字符串 / 无键）。
    任何异常返回 None（探测失败，交给调用方按「未知」处理）。
    """
    import ctypes

    cf = ctypes.CDLL(_CF_BUNDLE)
    cf.CFStringCreateWithBytes.restype = ctypes.c_void_p
    cf.CFStringCreateWithBytes.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32, ctypes.c_bool
    ]
    cf.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
    cf.CFPreferencesCopyAppValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetTypeID.argtypes = []
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    any_app = ctypes.c_void_p.in_dll(cf, "kCFPreferencesAnyApplication")

    key_bytes = b"AppleInterfaceStyle"
    key = cf.CFStringCreateWithBytes(
        None, key_bytes, len(key_bytes), _CF_UTF8, False
    )
    if not key:
        return None
    try:
        value = cf.CFPreferencesCopyAppValue(key, any_app)
        if not value:
            return None
        try:
            # 类型校验：键存在但不是字符串时按「读不到」处理，避免误读
            if cf.CFGetTypeID(value) != cf.CFStringGetTypeID():
                return None
            buf = ctypes.create_string_buffer(256)
            if not cf.CFStringGetCString(value, buf, 256, _CF_UTF8):
                return None
            return buf.value.decode("utf-8", "replace")
        finally:
            cf.CFRelease(value)
    finally:
        cf.CFRelease(key)

def _macos_prefers_light() -> Optional[bool]:
    # 键不存在 = 系统从未切过深色 = 浅色（这是有效语义，不是探测失败），
    # 所以不能把 None 直接当未知返回。
    try:
        style = _macos_read_interface_style()
    except Exception:
        return None
    if style is None:
        return True
    return style.strip().lower() != "dark"


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