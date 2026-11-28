from pathlib import Path

import pytest

from app.plugins.contracts.message_layout import ORIGINAL, OutputMode, register_output_mode, selected_mode
from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

NAME = 'system-message-output'
PATH = Path(__file__).resolve().parents[2] / 'plugins' / NAME


@pytest.mark.parametrize('legacy,expected', [(True, 'compact'), (False, 'original')])
def test_load_choose_reload_unload(qapp, qtbot, monkeypatch, tmp_path, legacy, expected):
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    monkeypatch.setattr(cfg.ui_compact_tool_area, 'value', legacy)
    monkeypatch.setattr(PluginConfigStore, '_path', lambda self, name: tmp_path / name / 'config.json')
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, 'get_instance', lambda: reg)
    assert reg.load_plugin(NAME, PATH)
    assert selected_mode().id == expected
    compact = reg.get_ui_module('message_output:compact')
    kinds = ('reasoning', 'blank', 'tool', 'body', 'tool')
    assert compact.layout(kinds, streaming=True) == ((0, 3, False), (4, 5, True))
    assert compact.layout(kinds, streaming=False)[-1] == (4, 5, False)
    extra = OutputMode('example', '示例', '逐项显示', lambda kinds, *, streaming: ())
    register_output_mode(reg, extra, plugin_name='example')
    assert selected_mode().id == expected
    card = reg.get_settings_cards()[0].widget_class()
    qtbot.addWidget(card)
    card._choices['example'].click()
    assert selected_mode() is extra
    assert reg.unload_plugin(NAME)
    assert selected_mode() is ORIGINAL
    assert reg.load_plugin(NAME, PATH)
    assert selected_mode() is extra
    reg.unload_plugin(NAME)


def test_policy_failure_preserves_blocks():
    mode = OutputMode('broken', '测试', '', lambda kinds, *, streaming: [(0, 3, True)])
    assert mode.layout(('body',), streaming=True) == ()
