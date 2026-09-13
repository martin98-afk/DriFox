---
description: 切换标题生成使用的默认模型
type: function
argument-hint:
  "[--model=]": "设置标题生成默认模型（模型名 / 服务商名 / 服务商:模型名）"
  "[--reset]": "清空标题生成默认模型设置，回退到主模型"
mutex_groups:
  mode: ["--model=", "--reset"]
---
