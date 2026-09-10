# tool-plugin-example — 最小工具插件示例

## 这是什么

可运行的**最小工具插件**结构：一个 plugin.json + 一个 tools/*.py，注册一个执行系统命令的工具。参照自真实插件 `win-powershell`（实现细节已精简，注册结构与惯用法注释保留）。

目录结构（即本目录）：

```
tool-plugin/
├── plugin.json            # 清单：components.tools = true 是关键
├── icon.svg / icon_dark.svg   # 可选：明暗两套图标
└── tools/
    └── example_tool.py    # 实现 + _SCHEMA + register(registry) 三要素
```

## 何时参照

- 从零写一个新的**纯工具插件**（只有 tools/，无 UI / 无配置）
- 忘记 `register(registry)` 的参数签名（danger / preview / metadata 等）
- 不确定 `ToolResult` 的正确返回方式

带配置项的工具插件 → 参照 [`config-schema-plugin/`](../config-schema-plugin/)。
带界面的插件 → 走 ui-plugin-creator 技能包。

## 对应 SKILL.md 章节

- §2.1 一个插件长这样 / §2.2 11 类组件速查
- §5.9 Tools（工具插件化）— register 参数逐项说明
- §6 测试与验证 — 改完名后照此验证
- §8 常见陷阱 — 「工具没声明 danger」「components flag 开了但没文件」

## 如何使用

1. 复制本目录到 `~/.drifox/plugins/tool-plugin-example/`
2. 全局搜索 `[改名]` / `[必改]` 标记，改工具名、danger 等级、icon
3. 重启或等 watchfiles 热更新（1-3 秒），用 `/plugin-marketplace` 确认加载状态
4. 改成你自己的实现逻辑

> Qt 绑定说明：本示例无 Qt 依赖。工具层若需 Qt（如图标处理），DriFox 当前使用 **PyQt5**（2026-09 实测仓库现状，以动手时 `grep "from PyQt5" app/` 结果为准）。
