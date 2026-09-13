# -*- coding: utf-8 -*-
"""UpdateChecker._pick_asset 选包逻辑测试。

同一 GitHub Release 现在同时挂 PyQt5 与 PySide6 两套安装包，选错包会让
_internal 下 Qt5 / Qt6 动态库混杂，覆盖安装后启动即崩。这里锁住三件事：
按变体精确匹配、旧 Release 单包兜底、Linux 不参与自动更新。
"""

from types import SimpleNamespace

from PySide6.QtWidgets import QWidget

from app.update_checker import UpdateChecker


def _checker(build_variant: object) -> UpdateChecker:
    """只造壳不跑 __init__（Shiboken 拒 object.__new__，且建 QWidget 需要 QApplication）。"""
    obj = QWidget.__new__(UpdateChecker)
    obj.cfg = SimpleNamespace(build_variant=build_variant)  # type: ignore[attr-defined]
    return obj


def _assets(*names: str) -> list[dict]:
    return [{"name": n, "browser_download_url": f"https://d/{n}"} for n in names]


# 双包共存时各取所需
_WIN_BOTH = _assets(
    "Drifox-Windows-Setup-v0.6.0.exe",
    "Drifox-Windows-Setup-v0.6.0-pyside6.exe",
)
_MAC_BOTH = _assets(
    "Drifox-macOS-v0.6.0.dmg",
    "Drifox-macOS-v0.6.0-pyside6.dmg",
)


def test_windows_pyqt5_picks_unsuffixed():
    assert _checker("pyqt5")._pick_asset(_WIN_BOTH, "windows")["name"] == "Drifox-Windows-Setup-v0.6.0.exe"


def test_windows_pyside6_picks_suffixed():
    assert _checker("pyside6")._pick_asset(_WIN_BOTH, "windows")["name"] == "Drifox-Windows-Setup-v0.6.0-pyside6.exe"


def test_macos_variant_match():
    assert _checker("pyside6")._pick_asset(_MAC_BOTH, "darwin")["name"] == "Drifox-macOS-v0.6.0-pyside6.dmg"
    assert _checker("pyqt5")._pick_asset(_MAC_BOTH, "darwin")["name"] == "Drifox-macOS-v0.6.0.dmg"


def test_asset_order_does_not_matter():
    """变体过滤不依赖 asset 数组顺序。"""
    reversed_assets = list(reversed(_WIN_BOTH))
    picked = _checker("pyside6")._pick_asset(reversed_assets, "windows")
    assert picked["name"] == "Drifox-Windows-Setup-v0.6.0-pyside6.exe"


# 历史 Release 只有无后缀的 PyQt5 包
def test_legacy_release_falls_back_for_pyside6():
    """PySide6 版在旧 Release 上找不到自己变体的包时退回首包，不让更新链断掉。"""
    legacy = _assets("Drifox-Windows-Setup-v0.5.11.exe")
    assert _checker("pyside6")._pick_asset(legacy, "windows")["name"] == "Drifox-Windows-Setup-v0.5.11.exe"


def test_missing_build_variant_defaults_to_pyqt5():
    """旧 config.py 无 build_variant 字段（getattr 兜底）时按 pyqt5 选包。"""
    obj = QWidget.__new__(UpdateChecker)
    obj.cfg = SimpleNamespace()  # type: ignore[attr-defined]
    assert obj._pick_asset(_WIN_BOTH, "windows")["name"] == "Drifox-Windows-Setup-v0.6.0.exe"


# 平台隔离
def test_linux_not_supported():
    assert _checker("pyqt5")._pick_asset(_assets("Drifox-Linux-v0.6.0.tar.gz"), "linux") is None


def test_no_platform_asset_returns_none():
    assert _checker("pyqt5")._pick_asset(_assets("SHA256SUMS"), "windows") is None
    assert _checker("pyqt5")._pick_asset([], "darwin") is None
