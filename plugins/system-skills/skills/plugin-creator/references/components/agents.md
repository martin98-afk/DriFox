---
description: Agents（@智能体）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Agents（@智能体）组件开发

### 文件位置

```
<plugin>/agents/<name>.md → 注册为 @<name>
```

### 关键点

- 定义 AI 角色：行为模式、知识边界、可用工具
- 支持 `role`、`tools`、`permission`、`description` 等字段
- frontmatter 必含 `description`

### 参考

- 完整规范：[docs/agents.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/agents.md)
- 系统 agent 案例：`plugins/system-agents/agents/`

---
