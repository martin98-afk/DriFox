# -*- coding: utf-8 -*-
"""P1-6：frozen 感知插件根解析 + 分发目录只读守卫（源码侧完整用例）。

打包形态：onedir（Drifox.spec COLLECT，datas 把 plugins/ 放进 _internal）。
项目惯例：resource_path() 用 hasattr(sys, "_MEIPASS") 判定分发目录；
onedir 下 _MEIPASS == <安装目录>/_internal（非临时解压），系统插件根即
_MEIPASS/plugins。只读守卫：打包形态下任何写入分发目录的目标拒绝。
"""
import sys
from pathlib import Path

import pytest

from app.plugins.managers.plugin_manager import (
    _assert_writable_plugin_target,
    _resolve_system_plugin_dir,
)


@pytest.fixture()
def fake_meipass(monkeypatch, tmp_path):
    """模拟打包形态：sys._MEIPASS 指向临时分发目录。"""
    meipass = tmp_path / "_internal"
    meipass.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    return meipass


def test_frozen_plugin_root_resolves_to_meipass(fake_meipass):
    """打包形态：插件根解析到 _MEIPASS/plugins（onedir 分发布局）。"""
    assert _resolve_system_plugin_dir() == fake_meipass / "plugins"


def test_readonly_guard_blocks_internal_writes(fake_meipass):
    """只读守卫：分发目录（_internal）下任何写入目标 → PermissionError。"""
    with pytest.raises(PermissionError):
        _assert_writable_plugin_target(fake_meipass / "plugins" / "system" / ".mcp.json")
    with pytest.raises(PermissionError):
        _assert_writable_plugin_target(fake_meipass / "app" / "resources" / "x.svg")


def test_dev_mode_resolves_project_plugins(monkeypatch):
    """dev 回归锚点：无 _MEIPASS → 解析回项目根 plugins/。"""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    expected = Path(__file__).resolve().parent.parent / "plugins"
    assert _resolve_system_plugin_dir() == expected


def test_dev_mode_guard_always_allows(tmp_path, monkeypatch):
    """dev 模式守卫恒过（无 _MEIPASS 即无分发目录语义）。"""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    _assert_writable_plugin_target(tmp_path / "anything")  # 不抛即过
