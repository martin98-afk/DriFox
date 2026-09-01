# -*- coding: utf-8 -*-
"""test_sections.py — 分区/浮层组件冒烟测试。"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

_ROOT = Path(__file__).resolve().parents[3]
_UI_DIR = _ROOT / "plugins" / "assistant_hub" / "ui"

_pkg_name = "ui_plugin_assistant_hub"
if _pkg_name not in sys.modules:
    pkg = types.ModuleType(_pkg_name)
    pkg.__path__ = [str(_UI_DIR)]
    sys.modules[_pkg_name] = pkg

# manager 模块先注册（sections import 它）
if "assistant_hub_manager" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "assistant_hub_manager", str(_ROOT / "plugins" / "assistant_hub" / "assistant_manager.py")
    )
    _mgr_mod = importlib.util.module_from_spec(_spec)
    sys.modules["assistant_hub_manager"] = _mgr_mod
    _spec.loader.exec_module(_mgr_mod)


def _load(name: str, file: str):
    full = f"{_pkg_name}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, str(_UI_DIR / file))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load("assistant_avatar", "assistant_avatar.py")
sections = _load("sections", "sections.py")
overlays = _load("overlays", "overlays.py")


def test_profile_section_bind_emit(qtbot=None):
    s = sections.ProfileSection()
    s.bind("小狐", "cfg-1", "cfg-2")
    assert s._name.text() == "小狐"
    got = []
    s.saveRequested.connect(lambda n, c, u: got.append((n, c, u)))
    s._emit_save()
    assert got and got[0][0] == "小狐"


def test_about_section_persona_switch(qtbot=None):
    personas = [
        {"id": "build", "name": "build", "description": "更懂工程的搭档", "tag": "推演"},
        {"id": "hanako", "name": "hanako", "description": "温暖的共鸣者", "tag": "MOOD"},
    ]
    s = sections.AboutSection(personas, "build")
    assert len(s._chip_buttons) == 2
    got = []
    s.personaChangeRequested.connect(got.append)
    s._chip_buttons[1].click()
    assert got == ["hanako"]
    # none 横幅
    s._none_banner.click()
    assert got == ["hanako", "none"]
    # 选中态横幅带 accent 粗边框
    assert "2px solid" in s._none_banner.styleSheet()


def test_memory_section_pins(tmp_path=None, qtbot=None):
    s = sections.MemorySection()
    s.reload_pins(["第一条", "第二条"])
    assert s.pins() == ["第一条", "第二条"]
    got = []
    s.pinsChanged.connect(got.append)
    s._pin_input.setText("第三条")
    s._emit_add_pin()
    assert got[-1] == ["第一条", "第二条", "第三条"]
    # 开关灰置不崩溃
    s.setEnabled_all(False)
    s.setEnabled_all(True)


def test_experience_section_reload(qtbot=None):
    s = sections.ExperienceSection()
    opened = []
    s.viewCategory.connect(opened.append)
    s.reload_categories([{"category": "工作流", "count": 2}, {"category": "工具使用", "count": 1}])
    # 点击第一个分类按钮
    s._list_area.itemAt(0).widget().click()
    assert opened == ["工作流"]


def test_text_view_overlay_readonly(qtbot=None):
    d = overlays.TextViewOverlay("查看", "内容文本", editable=False)
    assert d._edit.toPlainText() == "内容文本"
    assert d._edit.isReadOnly()
