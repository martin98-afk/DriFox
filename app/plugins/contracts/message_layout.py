"""消息排版插件契约：只传块类别，返回要折叠的连续区间（左闭右开）。"""
from loguru import logger

SLOT = "compact_message_layout"


def grouped_runs(kinds, *, streaming):
    start = None
    for i, kind in enumerate(kinds):
        if kind in ("reasoning", "tool"):
            if start is None:
                start = i
        elif kind != "blank" and start is not None:
            yield start, i, False
            start = None
    if start is not None:
        yield start, len(kinds), streaming


def resolve_groups(kinds, *, streaming):
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    try:
        policy = UIPluginRegistry.get_instance().get_ui_module(SLOT) or grouped_runs
        groups = tuple(policy(kinds, streaming=streaming))
        end = 0
        for start, stop, opened in groups:
            if (type(start) is not int or type(stop) is not int or type(opened) is not bool
                    or not end <= start < stop <= len(kinds)):
                raise ValueError("排版区间越界、重叠或类型错误")
            end = stop
        return groups
    except Exception:
        logger.exception("简洁模式排版策略失败，使用默认分组")
        return tuple(grouped_runs(kinds, streaming=streaming))
