---
name: network-graph
display_name: 网络节点图
description: 使用 ECharts 渲染力导向节点图网络，展示技术架构或知识图谱
prompt: |
  当用户想要可视化节点关系网络（如技术架构、知识图谱、组织结构图等），
  使用 network-graph skill 生成基于 ECharts 的力导向图。

  用法: 打开 index.html 即可展示节点图网络。
scripts:
  - index.html
---

# Network Graph Skill

使用 ECharts Graph（力导向布局）展示节点关系网络。

## 功能

- 力导向布局，节点自动排布
- 拖拽节点、滚轮缩放
- 悬停高亮关联节点与边
- 图例分类
- 深色主题

## 自定义数据

编辑 `index.html` 中的 `nodes` 和 `edges` 数组即可替换为自有数据。
