# -*- coding: utf-8 -*-
"""浮动卡片示例 — ui/__init__.py

参照自系统插件 file-tree（D:/work/DriFox/plugins/file-tree/ui/__init__.py）。

要点：
- 入口函数固定为 register_ui(registry)，由 UIPluginRegistry 加载钩子调用
- register_floating_card 注册浮动卡后会自动注册同名命令（/floating-card-example）
- 热重载惯用法：先清理 sys.modules 中本插件残留的子模块缓存，
  否则 Python 用旧 __pycache__ 缓存，改代码后可能 NameError / 行为不更新
"""
import sys


def register_ui(registry):
    """注册浮动卡片

    container 可选值（file-tree 用 "left"）：
      - "left"   停靠 Tab 窗口左侧停靠区，宽度可拖拽
      - 其他容器见 ui-plugin-creator SKILL.md §1 决策树
    """
    # 热重载兼容：清掉旧子模块缓存（[改名] 换成你的 ui_plugin_xxx 前缀）
    prefix = "ui_plugin_floating_card_example."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .example_card import ExampleCard

    registry.register_floating_card(
        plugin_name="floating-card-example",  # [改名] 与 plugin.json name 一致
        card_id="floating-card-example",      # [改名] 卡片唯一 id
        widget_class=ExampleCard,             # 卡片类（见 example_card.py）
        container="left",
        title="示例卡片",
        default_visible=False,                # 默认不自动弹出
    )
