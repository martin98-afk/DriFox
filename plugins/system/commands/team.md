---
description: 多窗口团队协作管理
type: function
argument-hint:
  '[--join=]': '加入团队（如 --join=build 或 --join 弹出选择）'
  '[--leave]': '离开团队恢复独立模式'
  '[--save=]': '保存当前活跃窗口的 agent 列表为命名模板'
  '[--load=]': '加载模板（不指定名称时列出可用模板）；支持 UI 枚举选择'
  '[--delete=]': '删除模板（不指定名称时列出可用模板）'
mutex_groups:
  action: ['--join=', '--leave', '--save=', '--load=', '--delete=']
---
