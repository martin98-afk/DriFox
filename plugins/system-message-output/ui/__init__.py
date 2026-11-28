from app.plugins.contracts.message_layout import SELECTOR, register_output_mode
from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.utils.config import Settings
from .compact import COMPACT
from .settings import OutputSettingsCard

NAME = 'system-message-output'


def register_ui(registry):
    store = PluginConfigStore()
    selection = store.get(NAME, 'mode_id')
    if not selection:
        selection = 'compact' if Settings.get_instance().ui_compact_tool_area.value else 'original'
        store.set_values(NAME, {'mode_id': selection})
    state = {'id': selection}

    def choose(mode_id):
        if store.set_values(NAME, {'mode_id': mode_id}):
            state['id'] = mode_id
            return True
        return False

    class SettingsCard(OutputSettingsCard):
        def __init__(self, parent=None):
            super().__init__(registry, lambda: state['id'], choose, parent)

    register_output_mode(registry, COMPACT, plugin_name=NAME)
    registry.register_ui_module(SELECTOR, lambda: state['id'], plugin_name=NAME)
    registry.register_settings_card(NAME, 'stream_output_modes', '流式输出方式', SettingsCard,
                                    section='plugins', priority=20,
                                    metadata={'description': '选择下一条消息使用的输出方式'})
