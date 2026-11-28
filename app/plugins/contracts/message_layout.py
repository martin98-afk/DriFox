"""只读消息展示契约。每张消息卡只解析一次方式，注册和卸载由 UI 注册表管理。"""
from dataclasses import dataclass
from typing import Callable, NamedTuple

from loguru import logger

PREFIX = 'message_output:'
SELECTOR = 'message_output_selection'


class SummaryItem(NamedTuple):
    kind: str
    name: str
    state: str
    target: int | None


def summary_items(kinds, blocks):
    """目标只供本批去重，绝不向排版插件传原始参数、路径、预览或结果。"""
    targets = {}
    items = []
    for kind, block in zip(kinds, blocks):
        if kind != 'tool':
            items.append(SummaryItem(kind, '', '', None))
            continue
        args = block.get('arguments')
        path = (args.get('path') or args.get('file_path')) if isinstance(args, dict) else None
        target = targets.setdefault(path, len(targets)) if isinstance(path, str) and path else None
        state = ('running' if block.get('type') == 'tool_streaming' else
                 'failed' if block.get('success') is False else 'done')
        items.append(SummaryItem(kind, str(block.get('name') or ''), state, target))
    return tuple(items)


@dataclass(frozen=True)
class OutputMode:
    id: str
    name: str
    description: str
    groups: Callable | None = None
    css: str = ''
    summary: Callable | None = None

    def describe(self, items):
        if self.summary is not None:
            try:
                label = self.summary(items)
                if isinstance(label, str) and label.strip():
                    return label.strip()
            except Exception:
                logger.exception('消息摘要失败：{}，回退分类计数', self.id)
        counts = [(name, sum(i.kind == kind for i in items)) for name, kind in (('思考', 'reasoning'), ('工具', 'tool'))]
        return ' · '.join(f'{name} {n} 次' for name, n in counts if n) or '消息详情'

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
