# -*- coding: utf-8 -*-
"""ui-driver 插件三重安全闸测试（S3c，秒级，无 QApplication 依赖）。

覆盖：
- 闸 1：enabled=false（默认）→ 零工具注册
- 闸 2：DRIFOX_UI_DRIVER != 1 → 零工具注册
- 闸 0：import 失败降级（mock）→ warning 不炸、零注册
- 正向：隔离数据目录 + 显式开启 → 5 工具注册
"""

from __future__ import annotations

import json

import pytest


class _FakeRegistry:
    def __init__(self):
        self.registered: list[str] = []

    def register(self, name, schema, **kwargs):
        self.registered.append(name)


@pytest.fixture()
def ui_tools_module():
    import importlib

    mod = importlib.import_module("plugins.ui-driver.tools.ui_tools")
    importlib.reload(mod)
    return mod


def _enable_plugin(monkeypatch, tmp_path):
    """隔离数据目录 + 写启用配置 + 设环境变量（闸 1/2 全开）。"""
    monkeypatch.setenv("DRIFOX_DATA_DIR", str(tmp_path))
    cfg_dir = tmp_path / "plugin_data" / "ui-driver"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    monkeypatch.setenv("DRIFOX_UI_DRIVER", "1")


def test_gate1_disabled_by_default(ui_tools_module, monkeypatch):
    monkeypatch.setenv("DRIFOX_UI_DRIVER", "1")
    monkeypatch.delenv("DRIFOX_DATA_DIR", raising=False)
    reg = _FakeRegistry()
    ui_tools_module.register(reg)
    assert reg.registered == [], "enabled=false（默认）必须零注册"


def test_gate2_env_var_required(ui_tools_module, monkeypatch, tmp_path):
    _enable_plugin(monkeypatch, tmp_path)
    monkeypatch.delenv("DRIFOX_UI_DRIVER", raising=False)
    reg = _FakeRegistry()
    ui_tools_module.register(reg)
    assert reg.registered == [], "缺 DRIFOX_UI_DRIVER=1 必须零注册"


def test_gate0_import_failure_degrades(ui_tools_module, monkeypatch, tmp_path):
    _enable_plugin(monkeypatch, tmp_path)
    # 模拟打包态 import 失败
    monkeypatch.setattr(ui_tools_module, "_DRIVER_IMPORT_ERROR", "ImportError: mocked")
    reg = _FakeRegistry()
    ui_tools_module.register(reg)  # 不应抛错
    assert reg.registered == [], "import 失败必须零注册（降级不炸）"


def test_positive_registers_five_tools(ui_tools_module, monkeypatch, tmp_path):
    _enable_plugin(monkeypatch, tmp_path)
    reg = _FakeRegistry()
    ui_tools_module.register(reg)
    assert reg.registered == [
        "ui_inspect",
        "ui_state",
        "ui_memory",
        "ui_screenshot",
        "ui_click",
    ]


def test_auto_arm_requires_both_flags(ui_tools_module, monkeypatch, tmp_path):
    """auto_arm 单开不应自动 ARM（需与 auto_confirm 双开）。"""
    from tools.ui_driver import is_armed, setArmed

    setArmed(False)
    _enable_plugin(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "plugin_data" / "ui-driver"
    (cfg_dir / "config.json").write_text(json.dumps({"enabled": True, "auto_arm": True}), encoding="utf-8")
    reg = _FakeRegistry()
    ui_tools_module.register(reg)
    assert len(reg.registered) == 5
    assert is_armed() is False
    setArmed(False)
