# -*- coding: utf-8 -*-
"""示例 zero 组件：演示宿主事件订阅与状态存取。

本文件属于 zero-bridge 插件的 zero 组件（registry 登记名
``zero-bridge.hello``）。修改保存 → DriFox watcher 识别 → zero 回滚重装；
删除本文件 → 组件自动卸载、全部副作用回滚。
"""

NAME = "hello"


def apply(ctx):
    counts = {"theme_changed": 0}

    def _on_theme(payload):
        counts["theme_changed"] += 1
        ctx.set("hello.theme_switches", counts["theme_changed"])

    ctx.on("host.theme_changed", _on_theme)
    ctx.add_disposer(lambda: print("[hello] 已卸载，副作用回滚"))
