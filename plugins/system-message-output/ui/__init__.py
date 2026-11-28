from app.plugins.sdk import caps_for

from .compact import CSS, grouped_runs
from .settings import OutputSettingsCard
from .summary import describe

NAME = 'system-message-output'


def _cap(name):
    return caps_for(name, plugin=NAME, surface='ui')


def register_ui(registry):
    state = {'id': None}

    def current():
        if state['id'] is None:
            read = _cap('plugin.config.read')
            value = read('mode_id') if read else None
            if value:
                state['id'] = value
            else:
                legacy = _cap('config.read')
                value = 'original' if legacy and not legacy('ui_compact_tool_area', True) else 'compact'
                choose(value)
                return value
        return state['id']

    def choose(mode_id):
        write = _cap('plugin.config.write')
        if write and write('mode_id', mode_id):
            state['id'] = mode_id
            return True
        return False

    class SettingsCard(OutputSettingsCard):
        def __init__(self, parent=None):
            super().__init__(registry.output_mode_choices, current, choose, parent)

    registry.register_output_mode('compact', '简洁', '按正文分组折叠思考与工具，单独标出失败次数。',
                                  groups=grouped_runs, css=CSS, summary=describe, plugin_name=NAME)
    registry.register_ui_module('message_output_selection', current, plugin_name=NAME)
    registry.register_settings_card(NAME, 'stream_output_modes', '流式输出方式', SettingsCard,
                                    section='plugins', priority=20,
                                    metadata={'description': '选择下一条消息使用的输出方式'})
