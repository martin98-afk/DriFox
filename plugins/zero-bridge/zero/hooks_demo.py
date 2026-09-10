# -*- coding: utf-8 -*-
"""示例 zero 组件：python hook 直接声明（不经 hooks.json）。

与 hooks.json 的关系：
- hooks.json：声明式清单，适合静态 prompt/命令注入（继续用，不变）
- zero 组件：python 里直接构造 hook 规则并注册，可读可算可拆，
  卸载时 ctx 托管自动反注册

skill_name 用 "<插件名>.zero" 子域：与同插件 hooks.json 的注册互不干扰，
回滚只清 zero 注册的部分。
"""

NAME = "hooks_demo"


def apply(ctx):
    hm = ctx.require("hook_manager")
    skill = "zero-bridge.zero"

    rules = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "prompt",
                            "prompt": "（zero 示例）会话结束提醒：检查未提交改动。",
                            "statusMessage": "zero hook:",
                            "id": "zero_demo_stop",
                        }
                    ]
                }
            ]
        }
    }

    # 成对 API：register_hooks_from_json ↔ unregister_skill_hooks（已存在，零改动）
    hm.register_hooks_from_json(skill, "", rules)
    ctx.add_disposer(lambda: hm.unregister_skill_hooks(skill))
