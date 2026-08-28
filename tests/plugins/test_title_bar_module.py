# -*- coding: utf-8 -*-
"""TitleBarModule：属性契约 + compose 集成

契约属性集（源 main_widget.setup_ui L2878-L3013 全量 self.* → host.setattr）：
- _project_branch_container _project_avatar _pb_separator _branch_widget
- _project_label title_edit _history_questions_btn _share_btn diff_btn
（共 9 项；其余 balance_display / coding_plan_ring / _coding_plan_hidden /
context_usage_ring / _history_questions_badge 亦由 build 设置，此处聚焦会话栏契约）
"""

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

pytest.importorskip("PySide6.QtWidgets")

_CONTRACT_ATTRS = (
    "_project_branch_container",
    "_project_avatar",
    "_pb_separator",
    "_branch_widget",
    "_project_label",
    "title_edit",
    "_history_questions_btn",
    "_share_btn",
    "diff_btn",
)


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_module_id():
    from app.widgets.modules.title_bar_module import TitleBarModule

    assert TitleBarModule.module_id == "title_bar"


def test_compose_builds_title_bar(fresh_registry, qapp):
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from app.widgets.modules.title_bar_module import TitleBarModule
    from app.widgets.ui_composition import compose

    fresh_registry.register_ui_module("title_bar", TitleBarModule, plugin_name="system")

    class _Host(QWidget):
        pass

    host = _Host()

    def _ensure_layout(h):
        if h.layout() is None:
            QVBoxLayout(h)

    report = compose(host, ["title_bar"], root_layout_factory=_ensure_layout)

    assert report["title_bar"] == "system"
    for attr in _CONTRACT_ATTRS:
        assert hasattr(host, attr), f"missing host attribute: {attr}"
