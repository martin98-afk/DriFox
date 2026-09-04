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

from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

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
    host = QWidget()
    s = sections.ProfileSection(host)
    s.bind("小狐", "cfg-2")  # 两参签名：对话模型跟随系统配置，不再单独设置
    assert s._name.text() == "小狐"
    got = []
    s.saveRequested.connect(lambda n, u: got.append((n, u)))
    s._emit_save()
    assert got and got[0][0] == "小狐"


def test_about_section_persona_switch(qtbot=None):
    personas = [
        {"id": "build", "name": "build", "description": "更懂工程的搭档", "tag": "推演"},
        {"id": "hanako", "name": "hanako", "description": "温暖的共鸣者", "tag": "MOOD"},
        {"id": "none", "name": "纯净助手", "description": "不附加人格底座", "tag": ""},
    ]
    host = QWidget()
    s = sections.AboutSection(personas, "build", parent=host)
    # 「无」= 纯净助手作为普通 chip 参与选择（不再单独横幅）
    assert len(s._chips) == 3
    got = []
    s.personaChangeRequested.connect(got.append)
    s._chips[1].mousePressEvent(None)
    assert got == ["hanako"]
    s._chips[2].mousePressEvent(None)
    assert got == ["hanako", "none"]


def test_pinned_section(tmp_path=None, qtbot=None):
    # 人工提示独立分区（原置顶记忆，从 MemorySection 拆出）
    s = sections.PinnedSection()
    s.reload_pins([("pin-1", "第一条"), ("pin-2", "第二条")])
    got = []
    s.pinAddRequested.connect(got.append)
    s._pin_input.setText("第三条")
    s._emit_add_pin()
    assert got == ["第三条"]


def test_memory_section_toggle_hides_body(tmp_path=None, qtbot=None):
    s = sections.MemorySection()
    # 记忆开关灰置只作用于记忆内容；人工提示已不在本分区
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


def test_experience_section_toggle_hides_body(qtbot=None):
    s = sections.ExperienceSection()
    # 经验关闭 → 内容区全隐藏（与记忆一致），开启恢复；isHidden 不受顶层未 show 影响
    s.set_enabled(False)
    assert s._body_wrap.isHidden()
    s.set_enabled(True)
    assert not s._body_wrap.isHidden()


def test_text_view_overlay_readonly(qtbot=None):
    host = QWidget()
    host.resize(800, 600)
    d = overlays.TextViewOverlay("查看", "内容文本", editable=False, parent=host)
    assert d._edit.toPlainText() == "内容文本"
    assert d._edit.isReadOnly()
