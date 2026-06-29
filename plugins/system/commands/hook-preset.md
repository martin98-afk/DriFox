---
description: 快速切换 Hook 预设（无参数=切换到下一个预设，参数=切换到指定预设）
type: function
shortcut: Ctrl+Shift+H
argument-hint:
  "[<preset-name>]": "预设名称（可选，留空则切换到下一个预设）"
---
# /hook-preset 命令 — Hook 预设切换

快速切换 Hook 配置预设。Hook 预设存储了不同场景下的 Hook 开关组合和智能体身份（agent_identity）。

## 使用方式

- `/hook-preset` — 切换到下一个预设
- `/hook-preset coding` — 切换到名为 "coding" 的预设

## 效果

- 自动切换目标预设的所有 Hook 开关状态
- 自动切换智能体身份（agent_identity）
- 通过 Infobar 显示切换结果
- 自动保存当前预设选择
