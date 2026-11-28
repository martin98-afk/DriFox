from pathlib import Path

import pytest


@pytest.fixture
def compact_output_plugin(monkeypatch, tmp_path, qapp):
    """独立窗口回放不启动主窗口；走真实插件加载器补齐生产扩展点。"""
    from app.plugins.managers.plugin_config_store import PluginConfigStore
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, 'get_instance', lambda: reg)
    monkeypatch.setattr(PluginConfigStore, '_path', lambda self, name: tmp_path / name / 'config.json')
    name = 'system-message-output'
    assert PluginConfigStore().set_values(name, {'mode_id': 'compact'})
    assert reg.load_plugin(name, Path(__file__).resolve().parents[2] / 'plugins' / name)
    yield reg
    reg.unload_plugin(name)
