# -*- coding: utf-8 -*-
"""SystemCardsModule：属性契约 + compose 集成

契约属性集提取命令（搬运自 main_widget.setup_ui 3122-3255）：
    python -X utf8 -c "import re; lines=open('app/main_widget.py',encoding='utf-8').read().split(chr(10)); attrs=[re.match(r'self\\.([\\w]+) *=',l.strip()).group(1) for l in lines[2978:3112] if re.match(r'self\\.([\\w]+) *=', l.strip())]; print(chr(10).join(attrs))"
"""

import pytest
from PySide6.QtWidgets import QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.ui_composition import compose

pytest.importorskip("PySide6.QtWidgets")

# 契约属性集（grep `self.[a-z_]+ *=` over 3122-3255）：None 占位 + 懒创建卡片 + 标题栏按钮
_CONTRACT_ATTRS = (
    "_history_card",
    "_history_popup_card",
    "_share_card",
    "_share_card_content",
    "_history_questions_card",
    "_history_questions_card_content",
    "_memory_card",
    "_memory_card_popup",
    "_model_config_card",
    "_model_config_popup",
    "_model_selector_card",
    "_model_selector_card_content",
    "_tool_control_card",
    "_project_selector_card",
    "_project_selector_card_content",
    "_project_new_edit",
    "_project_new_btn",
    "_project_open_folder_btn",
    "_project_import_btn",
    "_question_floating_widget",
)


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class _StubCardManager:
    def hide_card(self, *a, **k):
        pass

    def add_card(self, *a, **k):
        pass

    def on_card_shown(self, *a, **k):
        pass

    def on_card_hidden(self, *a, **k):
        pass


class _StubContainer:
    def add_card(self, *a, **k):
        pass


class _Host(QWidget):
    """最小宿主 stub：提供 SystemCardsModule.build 所需的 host 属性/回调。"""

    def __init__(self):
        super().__init__()
        self._window_id = "test_window"
        self._system_card_ids = []
        self._bottom_card_container = _StubContainer()
        self._card_manager = _StubCardManager()
        self._tool_permission_controller = None

    def _register_cards_to_manager(self):
        pass

    def _restore_after_system_close(self):
        pass

    def _refresh_tool_toggle_btn(self):
        pass

    def _init_builtin_commands(self, *a, **k):
        pass

    def _on_project_selected(self, *a, **k):
        pass

    def _on_new_project_created(self, *a, **k):
        pass

    def _on_archive_project(self, *a, **k):
        pass

    def _on_export_project(self, *a, **k):
        pass

    def _on_import_project(self, *a, **k):
        pass

    def _on_project_file_dropped(self, *a, **k):
        pass

    def _on_open_project_folder(self, *a, **k):
        pass

    def _on_project_folder_dropped(self, *a, **k):
        pass

    def _on_question_answered(self, *a, **k):
        pass

    def _on_question_cancelled(self, *a, **k):
        pass

    def _on_question_preview_requested(self, *a, **k):
        pass

    def _on_header_new_project(self, *a, **k):
        pass

    def _on_project_filter_changed(self, *a, **k):
        pass

    def _on_project_open_folder_btn(self, *a, **k):
        pass

    def _on_system_card_opened(self, *a, **k):
        pass

    def _on_system_card_closed(self, *a, **k):
        pass


def test_module_id():
    from app.widgets.modules.system_cards_module import SystemCardsModule

    assert SystemCardsModule.module_id == "system_cards"


def test_compose_builds_system_cards(fresh_registry, qapp):
    from app.widgets.modules.system_cards_module import SystemCardsModule

    fresh_registry.register_ui_module("system_cards", SystemCardsModule, plugin_name="system")

    host = _Host()

    report = compose(host, ["system_cards"])
    assert report["system_cards"] == "system"
    for attr in _CONTRACT_ATTRS:
        assert hasattr(host, attr), f"missing host attribute: {attr}"
