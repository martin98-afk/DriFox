# -*- coding: utf-8 -*-
"""InputCardModule：输入卡模块——属性契约 + compose 端到端

契约集提取命令（搬运基线 main_widget.setup_ui 1-indexed L3274-3415）：
    python -X utf8 -c "import re; lines=open('app/main_widget.py',encoding='utf-8').read().split(chr(10)); pat=re.compile(r'self\\.([\\w]+)\\s*[:=]'); attrs=[m.group(1) for l in lines[3273:3415] if (m:=pat.match(l.strip()))]; print(chr(10).join(attrs))"
"""

import pytest
from unittest.mock import MagicMock

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.ui_composition import compose


# 源段 self.X = 赋值全集（含类型注解）：host 上契约属性
_CONTRACT_ATTRS = (
    "_bottom_input_container",
    "_bottom_input_layout",
    "_input_card",
    "_input_card_wrapper",
    "_attach_container",
    "_attach_layout",
    "_attachments",
    "_history_working_attachments",
    "input_area",
    "_command_card",
    "_file_mention_card",
    "_undo_delete_card",
    "_undo_delete_cache",
    "_truncation_sentinel",
    "_pending_send_after_truncation",
    "_pending_send_user_text",
)


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_module_id():
    from app.widgets.modules.input_card_module import InputCardModule

    assert InputCardModule.module_id == "input_card"


def test_compose_builds_input_card(fresh_registry, qapp):
    from PyQt5.QtWidgets import QWidget

    from app.widgets.modules.input_card_module import InputCardModule

    fresh_registry.register_ui_module("input_card", InputCardModule, plugin_name="system")

    # host 桩：提供 build 依赖的宿主属性/方法（真实 QWidget 以支持父子控件关系）
    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self._window_id = "test-window"
            self._card_manager = MagicMock()
            self._bottom_card_container = MagicMock()
            for _m in (
                "_load_input_history",
                "_update_subagents_param_description",
                "_update_title_gen_param_description",
                "_ensure_file_mention_cache",
                "_on_send_clicked",
                "_on_stop_clicked",
                "_on_clear_shortcut",
                "_on_agent_changed",
                "_on_slash_triggered",
                "_on_slash_dismissed",
                "_on_slash_show_hint",
                "_on_at_triggered",
                "_on_at_dismissed",
                "_on_files_dropped",
                "_on_entering_history_mode",
                "_on_history_attachments_restored",
                "_on_history_mode_exited",
                "_on_attachments_removed_from_text",
                "_on_pet_typing",
                "_on_file_mention_selected",
                "_restore_deleted_message",
                "_on_undo_delete_dismissed",
                "_on_subagent_model_config_changed",
                "_on_title_gen_model_config_changed",
            ):
                setattr(self, _m, lambda *a, **k: None)

    host = _Host()
    report = compose(host, ["input_card"])
    assert report["input_card"] == "system"
    for attr in _CONTRACT_ATTRS:
        assert hasattr(host, attr), f"missing host attribute: {attr}"
