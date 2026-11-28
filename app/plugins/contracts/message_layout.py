"""只读消息展示契约。每张消息卡只解析一次方式，注册和卸载由 UI 注册表管理。"""
from dataclasses import dataclass
from typing import Callable

from loguru import logger

PREFIX = 'message_output:'
SELECTOR = 'message_output_selection'


@dataclass(frozen=True)
class OutputMode:
    id: str
    name: str
    description: str
    groups: Callable | None = None
    css: str = ''

    def layout(self, kinds, *, streaming):
        if self.groups is None:
            return ()
        try:
            groups = tuple(self.groups(kinds, streaming=streaming))
            end = 0
            for start, stop, opened in groups:
                if (type(start) is not int or type(stop) is not int or type(opened) is not bool
                        or not end <= start < stop <= len(kinds)):
                    raise ValueError('排版区间越界、重叠或类型错误')
                end = stop
            return groups
        except Exception:
            logger.exception('消息排版失败：{}，保留全部消息块', self.id)
            return ()


ORIGINAL = OutputMode('original', '原始', '按原始消息顺序显示正文、思考和工具详情。')


def register_output_mode(registry, mode, *, plugin_name):
    if not mode.id or mode.id == ORIGINAL.id or PREFIX + mode.id in registry.list_ui_module_ids():
        raise ValueError(f'输出方式 ID 为空、保留或已注册：{mode.id}')
    registry.register_ui_module(PREFIX + mode.id, lambda: mode, plugin_name=plugin_name)


def output_modes(registry):
    return [registry.get_ui_module(key) for key in registry.list_ui_module_ids() if key.startswith(PREFIX)] + [ORIGINAL]


def selected_mode():
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    registry = UIPluginRegistry.get_instance()
    mode_id = registry.get_ui_module(SELECTOR)
    if not mode_id or mode_id == ORIGINAL.id:
        return ORIGINAL
    return registry.get_ui_module(PREFIX + mode_id) or ORIGINAL
