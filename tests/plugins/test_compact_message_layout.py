"""简洁排版的边界、插件契约与回退。"""
from app.plugins.contracts.message_layout import SLOT, grouped_runs, resolve_groups
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


def test_group_boundaries():
    kinds = ('reasoning', 'blank', 'tool', 'body', 'body', 'tool', 'reasoning')
    assert list(grouped_runs(kinds, streaming=True)) == [(0, 3, False), (5, 7, True)]
    assert list(grouped_runs(kinds, streaming=False)) == [(0, 3, False), (5, 7, False)]


def test_plugin_and_failure_fallback(monkeypatch):
    registry = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, 'get_instance', lambda: registry)
    kinds = ('reasoning', 'body')
    def register(policy):
        registry.register_ui_module(SLOT, lambda: policy, plugin_name='test-layout', priority=100)
    register(lambda kinds, *, streaming: ())
    assert resolve_groups(kinds, streaming=True) == ()
    register(lambda kinds, *, streaming: [(0, 3, True)])
    assert resolve_groups(kinds, streaming=True) == ((0, 1, False),)
