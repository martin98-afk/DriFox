# -*- coding: utf-8 -*-
"""「打开方式」候选枚举 — 列出系统里可以为某个文件打开的应用程序

Windows 走注册表（与资源管理器「打开方式」菜单同源）：
- ``HKCU\\...\\FileExts\\.ext\\UserChoice``      当前默认程序
- ``HKCU\\...\\FileExts\\.ext\\OpenWithList``   最近用过的程序（MRUList 定序）
- ``...\\FileExts\\.ext\\OpenWithProgids`` /
  ``HKCR\\.ext\\OpenWithProgids``               其他推荐程序
每个 progid / exe 名再回查 ``Applications\\<exe>`` 拿命令行与友好名。

非 Windows 平台返回空列表，调用方退化为「系统默认程序 + 显示文件位置」。
"""

import os
import subprocess
import sys
from typing import Dict, List, NamedTuple, Optional, Tuple

DEFAULT_SUFFIX = "（默认）"

# ── 数据结构 ──────────────────────────────────────────────


class Opener(NamedTuple):
    """一个可用的外部程序"""

    label: str  # 展示名（如 "PyCharm 2025.1.1.1"）
    exe: str  # 可执行文件路径（用于取图标）
    command: str = ""  # 注册表命令行模板（含 %1 之类的占位符）
    is_default: bool = False


# 进程内缓存：按扩展名 / 可执行文件缓存，避免每次点菜单都读注册表
_OPENER_CACHE: Dict[str, List[Opener]] = {}
_ICON_CACHE: Dict[str, object] = {}
_LABEL_CACHE: Dict[str, str] = {}


# ── 注册表读取（Windows） ─────────────────────────────────


def _reg_read(root: int, path: str, name: str = "") -> str:
    import winreg

    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            value, _type = winreg.QueryValueEx(key, name)
            return str(value or "")
    except OSError:
        return ""


def _reg_names(root: int, path: str) -> List[str]:
    """列出键下的值名（不含默认值）"""
    import winreg

    names: List[str] = []
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, _value, _type = winreg.EnumValue(key, index)
                except OSError:
                    break
                if name:
                    names.append(name)
                index += 1
    except OSError:
        pass
    return names


def _reg_subkeys(root: int, path: str) -> List[str]:
    """列出键下的子键名（shell 动词都是子键，``_reg_names`` 只返回值名）"""
    import winreg

    names: List[str] = []
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        pass
    return names

def _file_exts_key(ext: str) -> str:
    return rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}"


def _default_progid(ext: str) -> str:
    import winreg

    return _reg_read(winreg.HKEY_CURRENT_USER, _file_exts_key(ext) + r"\UserChoice", "ProgId")


def _open_with_exes(ext: str) -> List[str]:
    """``OpenWithList`` 里的程序名，按 MRUList 排序（最近用过的在前）"""
    import winreg

    path = _file_exts_key(ext) + r"\OpenWithList"
    values = {}
    for name in _reg_names(winreg.HKEY_CURRENT_USER, path):
        if name.lower() == "mrulist":
            continue
        exe_name = _reg_read(winreg.HKEY_CURRENT_USER, path, name)
        if exe_name:
            values[name] = os.path.basename(exe_name)
    if not values:
        return []
    mru = _reg_read(winreg.HKEY_CURRENT_USER, path, "MRUList")
    ordered = [values[ch] for ch in mru if ch in values]
    for name, exe_name in values.items():  # MRUList 缺项时补在后面
        if exe_name not in ordered:
            ordered.append(exe_name)
    return ordered


def _open_with_progids(ext: str) -> List[str]:
    """``OpenWithProgids`` 里的 progid（HKCU 优先于 HKCR）"""
    import winreg

    result: List[str] = []
    for root, path in (
        (winreg.HKEY_CURRENT_USER, _file_exts_key(ext) + r"\OpenWithProgids"),
        (winreg.HKEY_CLASSES_ROOT, ext + r"\OpenWithProgids"),
    ):
        for name in _reg_names(root, path):
            if name and name not in result:
                result.append(name)
    return result


# ── 命令行 / 友好名解析 ───────────────────────────────────


def _exe_from_command(command: str) -> str:
    """命令行首段即程序路径（支持带引号与裸路径）"""
    command = (command or "").strip()
    if not command:
        return ""
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 1 else ""
    return command.split(" ", 1)[0]


def _resolve_exe(exe: str, name_hint: str = "") -> str:
    """把裸 exe 名解析成完整路径：存在即用，否则查 App Paths"""
    import winreg

    candidates = [exe, name_hint]
    for candidate in candidates:
        if candidate and os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
    for candidate in candidates:
        base = os.path.basename(candidate or "")
        if not base:
            continue
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            path = _reg_read(
                root, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{base}"
            )
            if path:
                return path.strip('"')
    return exe


def _app_default_label(exe_name: str) -> str:
    """``Applications\\<exe>`` 默认值有时就是资源管理器里显示的程序名"""
    import winreg

    for path in (exe_name, exe_name + r"\shell\open"):
        label = _reg_read(winreg.HKEY_CLASSES_ROOT, rf"Applications\{path}").strip()
        if label:
            return label
    return ""


def _mui_label(exe: str) -> str:
    """MuiCache 里存着资源管理器实际显示的程序名（常带版本号，如 "PyCharm 2025.1.1.1"）"""
    import winreg

    cache = r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"
    return _reg_read(winreg.HKEY_CURRENT_USER, cache, os.path.abspath(exe) + ".FriendlyAppName").strip()


def _file_description(exe: str) -> str:
    """读 exe 版本资源里的 FileDescription（没有 MuiCache 条目时的主力来源）"""
    if os.name != "nt" or not exe or not os.path.exists(exe):
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(ctypes.c_wchar_p(exe), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(ctypes.c_wchar_p(exe), 0, size, buffer):
            return ""
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        translations = []
        if (
            version.VerQueryValueW(
                buffer,
                ctypes.c_wchar_p("\\VarFileInfo\\Translation"),
                ctypes.byref(value),
                ctypes.byref(length),
            )
            and length.value >= 4
        ):
            raw = ctypes.string_at(value, length.value)
            for i in range(0, len(raw) - 3, 4):
                lang = int.from_bytes(raw[i : i + 2], "little")
                codepage = int.from_bytes(raw[i + 2 : i + 4], "little")
                translations.append((lang, codepage))
        if not translations:
            translations = [(0x0409, 0x04B0)]
        for lang, codepage in translations[:6]:
            query = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
            ok = version.VerQueryValueW(
                buffer, ctypes.c_wchar_p(query), ctypes.byref(value), ctypes.byref(length)
            )
            if ok and length.value:
                text = ctypes.wstring_at(value, length.value).split("\x00")[0].strip()
                if text:
                    return text
    except Exception:
        return ""
    return ""


def label_for_exe(exe: str) -> str:
    """程序展示名：MuiCache → 版本资源 → Applications 默认值 → 文件名"""
    if not exe:
        return ""
    key = exe.lower()
    cached = _LABEL_CACHE.get(key)
    if cached is not None:
        return cached
    base = os.path.basename(exe)
    label = (_mui_label(exe) or _file_description(exe) or _app_default_label(base)).strip()
    if not label:
        label = os.path.splitext(base)[0]
        if label:
            label = label[0].upper() + label[1:]
    _LABEL_CACHE[key] = label
    return label


def _progid_command(progid: str) -> str:
    """progid → 命令行模板"""
    import winreg

    command = _reg_read(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command")
    if not command:
        command = _reg_read(
            winreg.HKEY_CLASSES_ROOT, rf"Applications\{progid}\shell\open\command"
        )
    return command


def _app_command(exe_name: str) -> str:
    """``Applications\\<exe>`` → 命令行模板"""
    import winreg

    return _reg_read(winreg.HKEY_CLASSES_ROOT, rf"Applications\{exe_name}\shell\open\command")


# ── 对外接口 ──────────────────────────────────────────────


def list_openers(path: str) -> List[Opener]:
    """列出能打开该文件的程序（默认程序在首位并带 ``is_default``）

    结果按「默认 → 最近使用 → 其他推荐」排序，同一程序只保留一次。
    """
    if os.name != "nt":
        return []
    ext = os.path.splitext(path or "")[1].lower()
    if not ext:
        return []
    cached = _OPENER_CACHE.get(ext)
    if cached is not None:
        return list(cached)

    default_progid = _default_progid(ext)
    candidates: List[Tuple[str, str, bool]] = []  # (kind, value, is_default)

    if default_progid:
        candidates.append(("progid", default_progid, True))
    for exe_name in _open_with_exes(ext):
        candidates.append(("app", exe_name, False))
    for progid in _open_with_progids(ext):
        candidates.append(("progid", progid, False))

    openers: List[Opener] = []
    seen = set()
    for kind, value, is_default in candidates:
        try:
            command = _progid_command(value) if kind == "progid" else _app_command(value)
        except Exception:  # 注册表异常不影响其它候选
            continue
        if not command:
            continue
        exe = _resolve_exe(_exe_from_command(command), os.path.basename(value))
        label = label_for_exe(exe)
        key = (os.path.basename(exe) or label).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        openers.append(
            Opener(label=label or key, exe=exe, command=command, is_default=is_default)
        )
    _OPENER_CACHE[ext] = list(openers)
    return openers


# ── 目录「打开方式」（资源管理器右键同源） ────────────────

def _explorer_exe() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")

def _shell_verbs(key_path: str) -> List[Tuple[str, str]]:
    """枚举 ``<key_path>\\*`` 的 shell 动词 → [(显示名, 命令行)]

    显示名依次取 ``MUIVerb`` / 键默认值；``@dll,-id`` 形式的资源引用取不到
    本地化名（会把原始引用串直接显示出来），一律跳过。
    """
    import winreg

    verbs: List[Tuple[str, str]] = []
    for verb in _reg_subkeys(winreg.HKEY_CLASSES_ROOT, key_path):
        base = key_path + "\\" + verb
        label = (
            _reg_read(winreg.HKEY_CLASSES_ROOT, base, "MUIVerb")
            or _reg_read(winreg.HKEY_CLASSES_ROOT, base)
        ).strip()
        if not label or label.startswith("@"):
            continue
        command = _reg_read(winreg.HKEY_CLASSES_ROOT, base + r"\command") or _reg_read(
            winreg.HKEY_CLASSES_ROOT, base + r"\shell\open\command"
        )
        if command:
            verbs.append((label.replace("&", ""), command))
    return verbs

_FOLDER_OPENERS: Optional[List[Opener]] = None

def _explorer_opener() -> Opener:
    return Opener(
        label="文件资源管理器",
        exe=_explorer_exe(),
        command='"explorer.exe" "%1"',
        is_default=True,
    )

# ── macOS：目录候选（LaunchServices 声明的文件夹支持） ────

# 声明这些 UTI 的应用才能“打开文件夹”（Finder 右键的「用 xx 打开」同源）
_MAC_FOLDER_TYPES = ("public.folder", "public.directory")

def _mac_app_dirs() -> List[str]:
    return [
        "/Applications",
        "/Applications/Utilities",
        os.path.join(os.path.expanduser("~"), "Applications"),
    ]

def _mac_bundle_label(bundle: str, info: dict) -> str:
    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return os.path.splitext(os.path.basename(bundle))[0]

def _mac_folder_apps() -> List[Opener]:
    """扫描 .app 的 Info.plist，挑出声明支持文件夹的应用（IDE / 终端等）

    mac 没有注册表动词可枚举，权限信息在 bundle 的 ``CFBundleDocumentTypes``
    里；只认 ``public.folder`` / ``public.directory``，避免把只能开文件的
    应用也列进来。启动用 ``open -a <bundle> <path>``，复用通用命令行切分。
    """
    import plistlib

    found: List[Opener] = []
    seen = set()
    for folder in _mac_app_dirs():
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".app"):
                continue
            bundle = os.path.join(folder, name)
            try:
                with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                    info = plistlib.load(fh)
            except Exception:  # 损坏/无权限的 bundle 跳过，不影响其它候选
                continue
            if not isinstance(info, dict):
                continue
            types: List[str] = []
            for doc in info.get("CFBundleDocumentTypes") or []:
                if isinstance(doc, dict):
                    types.extend(doc.get("LSItemContentTypes") or [])
            if not any(t in types for t in _MAC_FOLDER_TYPES):
                continue
            real = os.path.realpath(bundle)
            if real in seen:
                continue
            seen.add(real)
            found.append(
                Opener(
                    label=_mac_bundle_label(bundle, info),
                    exe=bundle,
                    command=f'open -a "{bundle}" "%1"',
                )
            )
    found.sort(key=lambda o: o.label.lower())
    return found

def _mac_finder_opener() -> Opener:
    return Opener(
        label="访达",
        exe="/System/Library/CoreServices/Finder.app",
        command='open "%1"',
        is_default=True,
    )

def list_folder_openers(path: str = "") -> List[Opener]:
    """目录的「打开方式」候选（首项 = 系统文件管理器）

    - Windows：枚举 ``HKCR\\Directory\\shell`` 与 ``Directory\\Background\\shell``
      下注册的动词（与资源管理器右键菜单同源）
    - macOS：访达 + 扫描 ``/Applications`` 各 bundle 的 ``CFBundleDocumentTypes``
    - 其它平台：空列表（调用方退化为单一项）

    结果按进程缓存；按 exe / bundle 去重。
    """
    global _FOLDER_OPENERS
    if _FOLDER_OPENERS is not None:
        return list(_FOLDER_OPENERS)
    openers: List[Opener] = []
    if os.name == "nt":
        openers.append(_explorer_opener())
        seen = {"explorer.exe"}
        for key in (r"Directory\shell", r"Directory\Background\shell"):
            for label, command in _shell_verbs(key):
                exe = _resolve_exe(_exe_from_command(command))
                key_name = (os.path.basename(exe) or label).lower()
                if not key_name or key_name in seen:
                    continue
                seen.add(key_name)
                openers.append(Opener(label=label, exe=exe, command=command))
    elif sys.platform == "darwin":
        openers.append(_mac_finder_opener())
        openers.extend(_mac_folder_apps())
    _FOLDER_OPENERS = list(openers)
    return openers

def _split_command(command: str) -> List[str]:
    """按 Windows 引号规则切分命令行（只处理双引号，足够覆盖 open 命令）"""
    parts: List[str] = []
    buf: List[str] = []
    in_quotes = False
    for ch in command:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == " " and not in_quotes:
            if buf:
                parts.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def build_args(opener: Opener, path: str) -> List[str]:
    """把注册表命令行模板实例化成本次启动的参数表

    ★ 先切分再替换：模板里的 ``%1`` 通常自带引号（``"exe" "%1"``），
    先替换成带引号路径再切分会把含空格的路径拆成几个参数。
    """
    command = (opener.command or "").strip()
    if not command:
        return [opener.exe, path] if opener.exe else []
    placeholders = ("%1", "%l", "%L", "%*")
    args: List[str] = []
    replaced = False
    for part in _split_command(command):
        if any(ph in part for ph in placeholders):
            replaced = True
            for ph in placeholders:
                part = part.replace(ph, path)
        args.append(part)
    if not replaced:
        args.append(path)
    return args


def launch(opener: Opener, path: str) -> bool:
    """用指定程序打开文件；启动失败返回 ``False``（不抛）"""
    args = build_args(opener, path)
    if not args:
        return False
    try:
        kwargs = {}
        if os.name == "nt":
            # 不弹控制台窗口（启动 CLI 类程序时不闪黑框）
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(args, cwd=os.path.dirname(path) or None, **kwargs)
        return True
    except Exception:
        return False


# ── 图标 ──────────────────────────────────────────────────

def _mac_icon_path(bundle: str) -> str:
    """定位 .app 的 .icns 文件（读 Info.plist，不碰 LaunchServices）

    ``CFBundleIconFile`` 可能带也可能不带 ``.icns`` 扩展；缺省时依次试
    ``AppIcon.icns`` 与 Resources 下首个 icns（部分 app 不声明这个键）。
    """
    import plistlib

    resources = os.path.join(bundle, "Contents", "Resources")
    name = ""
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
            info = plistlib.load(fh)
        if isinstance(info, dict):
            name = str(info.get("CFBundleIconFile") or "").strip()
    except Exception:
        name = ""
    candidates: List[str] = []
    if name:
        candidates.append(name if name.lower().endswith(".icns") else name + ".icns")
    candidates.append("AppIcon.icns")
    for candidate in candidates:
        path = os.path.join(resources, candidate)
        if os.path.isfile(path):
            return path
    try:
        for entry in sorted(os.listdir(resources)):
            if entry.lower().endswith(".icns"):
                return os.path.join(resources, entry)
    except OSError:
        pass
    return ""

def _mac_bundle_icon(bundle: str):
    """读 bundle 内 icns 得到图标（Qt 自带 icns 读支持，无额外依赖）

    不用 ``QFileIconProvider``：它走 LaunchServices / 图标缓存，与 Windows
    上 SHGetFileInfo 同一类风险（未知耗时，UI 线程调用可能冻窗）。
    """
    from PyQt5.QtGui import QIcon, QPixmap

    path = _mac_icon_path(bundle)
    if not path:
        return QIcon()
    pixmap = QPixmap(path)
    return QIcon(pixmap) if not pixmap.isNull() else QIcon()

_ICON_API_CACHE = None


def _icon_api():
    """一次性准备好 ExtractIconExW / DestroyIcon 的签名（模块级缓存）"""
    global _ICON_API_CACHE
    if _ICON_API_CACHE is not None:
        return _ICON_API_CACHE
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ExtractIconExW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HICON),
        ctypes.POINTER(wintypes.HICON),
        wintypes.UINT,
    ]
    shell32.ExtractIconExW.restype = wintypes.UINT
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    _ICON_API_CACHE = (ctypes, wintypes, shell32, user32)
    return _ICON_API_CACHE


def _icon_from_exe(exe: str):
    """直接读 exe 的图标资源，返回 QPixmap（失败返回 None）

    ★ 绝不能用 QFileIconProvider / SHGetFileInfo：它们走 Shell 图标缓存，
    某些 exe 会命中 Qt 的 5 秒超时（实测 pycharm64.exe、Typora.exe 各卡 5s，
    qt_messages 日志留下 "SHGetFileInfo() timed out"）。在 UI 线程调用等于整窗冻住。
    ExtractIconExW 只解析 PE 资源，实测 1-2ms。
    """
    if os.name != "nt" or not exe or not os.path.exists(exe):
        return None
    try:
        ctypes, wintypes, shell32, user32 = _icon_api()
        from PyQt5 import QtWinExtras

        large = wintypes.HICON()
        small = wintypes.HICON()
        count = shell32.ExtractIconExW(exe, 0, ctypes.byref(large), ctypes.byref(small), 1)
        if not count:
            return None
        hicon = large.value or small.value
        if not hicon:
            return None
        try:
            pixmap = QtWinExtras.QtWin.fromHICON(hicon)
        finally:
            user32.DestroyIcon(hicon)
        if pixmap is None or pixmap.isNull():
            return None
        return pixmap
    except Exception:
        return None


def icon_for_exe(exe: str):
    """取程序自身图标；取不到返回空 QIcon（调用方退通用图标）"""
    from PyQt5.QtGui import QIcon

    if not exe or not os.path.exists(exe):
        return QIcon()
    key = exe.lower()
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    if os.name == "nt":
        pixmap = _icon_from_exe(exe)
        icon = QIcon(pixmap) if pixmap is not None else QIcon()
    elif sys.platform == "darwin":
        icon = _mac_bundle_icon(exe)
    else:
        icon = QIcon()
    _ICON_CACHE[key] = icon
    return icon
