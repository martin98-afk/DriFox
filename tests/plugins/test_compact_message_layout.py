from pathlib import Path

import pytest

from app.plugins.contracts.message_layout import ORIGINAL, OutputMode, register_output_mode, selected_mode
from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

NAME = 'system-message-output'
PATH = Path(__file__).resolve().parents[2] / 'plugins' / NAME


@pytest.mark.parametrize('legacy,expected', [(True, 'compact'), (False, 'original')])
def test_load_choose_reload_unload(qapp, qtbot, monkeypatch, tmp_path, output_config_schema, legacy, expected):
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


def test_plugin_host_imports_only_capability_entry():
    import ast

    for file in (PATH / 'ui').glob('*.py'):
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith('app.') for alias in node.names), file
            elif isinstance(node, ast.ImportFrom) and (node.module or '').startswith('app.'):
                assert node.module == 'app.plugins.sdk' and [n.name for n in node.names] == ['caps_for'], file


def test_dynamic_summaries(qapp, monkeypatch):
    from app.tools.registry import ToolRegistry
    import importlib.util
    sandbox = ToolRegistry()
    monkeypatch.setattr(ToolRegistry, 'get_instance', lambda: sandbox)
    spec = importlib.util.spec_from_file_location('summary_test_files', PATH.parent / 'system-core/tools/file_tools.py')
    files = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(files)
    from app.plugins.loaders.plugin_tool_loader import _PluginRegistryProxy
    files.register(_PluginRegistryProxy(sandbox, 'system-core'))
    from app.plugins.contracts.message_layout import summary_items

    reg = UIPluginRegistry()
    assert reg.load_plugin(NAME, PATH)
    try:
        mode = reg.get_ui_module('message_output:compact')
        def items(names, paths, running=False):
            blocks = [dict(type='tool_streaming' if running else 'tool_result', name=n,
                           arguments={'path': p, 'token': 'secret'}, result='private result') for n, p in zip(names, paths)]
            return summary_items(['tool'] * len(blocks), blocks)
        mixed = items(['grep', 'read', 'read', 'list', 'read', 'glob'], ['dir', 'a', 'b', 'dir', 'c', 'dir'])
        from app.tools.registry import ToolRegistry
        tools = ToolRegistry.get_instance()
        tools.register('dfx_test_archive', {}, cn_name='归档', source='plugin:test-summary', danger='safe')
        try:
            assert mode.describe(items(['dfx_test_archive'], [None])) == '归档了 1 次'
        finally:
            tools.unregister('dfx_test_archive')
        assert mode.describe(mixed) == '读取了 3 个文件，搜索了 1 次，另有 2 次操作'
        assert mode.describe(items(['read'] * 3, ['a'] * 3)) == '分 3 次读取了 1 个文件'
        assert mode.describe(items(['read'], [None], running=True)) == '正在读取 1 次'
        assert mode.describe(items(['my_tool'], [None])) == '调用了 my_tool 1 次'
        assert mode.describe(summary_items(['reasoning'] * 2, [{}, {}])) == '思考 2 次'
        assert mixed[0]._fields == ('kind', 'name', 'state', 'target') and mixed[0].target == 0
        assert ORIGINAL.describe(mixed) == '工具 6 次'
        def fail(_):
            raise ValueError('模拟摘要失败')
        assert OutputMode('bad', '测试', '', summary=fail).describe(mixed) == '工具 6 次'
    finally:
        reg.unload_plugin(NAME)


def test_policy_failure_preserves_blocks():
    mode = OutputMode('broken', '测试', '', lambda kinds, *, streaming: [(0, 3, True)])
    assert mode.layout(('body',), streaming=True) == ()
