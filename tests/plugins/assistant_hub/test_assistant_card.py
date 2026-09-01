# -*- coding: utf-8 -*-
"""test_assistant_card.py — 单列主页面冒烟测试。"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("qfluentwidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

_ROOT = Path(__file__).resolve().parents[3]
_UI_DIR = _ROOT / "plugins" / "assistant_hub" / "ui"

_pkg_name = "ui_plugin_assistant_hub"
if _pkg_name not in sys.modules:
    pkg = types.ModuleType(_pkg_name)
    pkg.__path__ = [str(_UI_DIR)]
    sys.modules[_pkg_name] = pkg

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


for _f in ("assistant_avatar", "arc_stack", "overlays", "sections", "rename_dialog"):
    _load(_f, f"{_f}.py")
card_mod = _load("assistant_card", "assistant_card.py")


def test_card_construct_and_bind(tmp_path, monkeypatch):
    """构造主页面：创建助手 → 绑定编辑器 → 切换助手。"""
    mgr_mod = sys.modules["assistant_hub_manager"]
    mgr_mod.AssistantManager.reset_instance()
    mgr = mgr_mod.AssistantManager.get_instance(root_dir=str(tmp_path / "hub"))
    a = mgr.create("小狐")

    card = card_mod.AssistantCardWidget()
    assert len(card._stack._cards) == 1
    assert card._active_aid == a.id
    assert card._name_label.text() == "小狐"

    # 新建第二个 → 切换
    b = mgr.create("二号")
    card._reload_all(select_aid=b.id)
    assert card._active_aid == b.id
    assert card._name_label.text() == "二号"

    # 删除保护：至少保留一个助手 → 删 a 后自动绑定 b
    mgr.delete(a.id)
    card._reload_all()
    assert card._active_aid == b.id
    assert mgr.delete(b.id) is False  # 最后一个不可删


def test_card_persona_change(tmp_path):
    mgr_mod = sys.modules["assistant_hub_manager"]
    mgr_mod.AssistantManager.reset_instance()
    mgr = mgr_mod.AssistantManager.get_instance(root_dir=str(tmp_path / "hub2"))
    a = mgr.create("人格测试")
    card = card_mod.AssistantCardWidget()
    card._active_aid = a.id
    card._on_persona_change("hanako")
    assert mgr.get(a.id).yuan == "hanako"
    card._on_persona_change("none")
    assert mgr.get(a.id).yuan == "none"
