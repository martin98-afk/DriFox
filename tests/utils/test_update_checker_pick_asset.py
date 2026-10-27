# -*- coding: utf-8 -*-
"""更新器安装包变体选择（_pick_asset 回归）。

背景：旧逻辑「Windows 第一个 .exe 直接用」，Release 双变体（pyqt5 无后缀
+ pyside6 带 -pyside6 后缀）在 republish 重传 assets 后顺序不再保证 pyqt5
在前，PyQt5 版更新装成 PySide6 包（_internal 里 Qt5/Qt6 动态库混杂，启动
即崩）。_pick_asset 按 build_variant 过滤，与 assets 顺序无关。
"""
import app.update_checker as uc


def _checker(build_variant: str) -> uc.UpdateChecker:
    """__new__ 绕过 QWidget.__init__，只挂 _pick_asset 依赖的 cfg。"""
    c = uc.UpdateChecker.__new__(uc.UpdateChecker)
    c.cfg = type("Cfg", (), {"build_variant": build_variant})()
    return c


def _asset(name: str) -> dict:
    return {"name": name, "browser_download_url": f"https://x/{name}"}


PYQT5_WIN = "Drifox-Windows-Setup-v0.6.11.exe"
PYSIDE6_WIN = "Drifox-Windows-Setup-v0.6.11-pyside6.exe"


def test_pyqt5_picks_unsuffixed_even_when_pyside6_first():
    """pyside6 包排前也必须选无后缀包（本次事故场景）。"""
    c = _checker("pyqt5")
    got = c._pick_asset(
        [_asset(PYSIDE6_WIN), _asset(PYSIDE6_WIN + ".blockmap"), _asset(PYQT5_WIN)],
        "windows",
    )
    assert got["name"] == PYQT5_WIN


def test_pyside6_picks_marked_asset():
    c = _checker("pyside6")
    got = c._pick_asset([_asset(PYQT5_WIN), _asset(PYSIDE6_WIN)], "windows")
    assert got["name"] == PYSIDE6_WIN


def test_legacy_release_single_pyqt5_asset_fallback_both_variants():
    """历史 Release 只有 pyqt5 包：两变体都回退到它，更新链不断。"""
    assets = [_asset(PYQT5_WIN), _asset("SHA256SUMS")]
    for variant in ("pyqt5", "pyside6"):
        got = _checker(variant)._pick_asset(assets, "windows")
        assert got["name"] == PYQT5_WIN


def test_macos_dmg_variant():
    c = _checker("pyside6")
    got = c._pick_asset(
        [_asset("Drifox-macOS-v0.6.11.dmg"), _asset("Drifox-macOS-v0.6.11-pyside6.dmg")],
        "darwin",
    )
    assert got["name"] == "Drifox-macOS-v0.6.11-pyside6.dmg"


def test_unsupported_system_returns_none():
    assert _checker("pyqt5")._pick_asset([_asset(PYQT5_WIN)], "linux") is None


def test_missing_build_variant_defaults_pyqt5():
    """CI 未注入（sed 失败）时按 pyqt5 兜底，与 release.yml 的 pyqt5-in-front 顺序一致。"""
    c = uc.UpdateChecker.__new__(uc.UpdateChecker)
    c.cfg = type("Cfg", (), {})()  # 无 build_variant 属性
    got = c._pick_asset([_asset(PYSIDE6_WIN), _asset(PYQT5_WIN)], "windows")
    assert got["name"] == PYQT5_WIN
