---
description: Skills（AI 技能）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Skills（AI 技能）组件开发

### 文件位置

```
<plugin>/skills/<name>/SKILL.md → AI 可检索技能
```

### 最小模板

```markdown
---
name: <skill-name>
description: 一句话描述技能用途，AI 会匹配此字段
---

# <skill-name> — 技能标题

> 技能说明

## 行为

1. 步骤一
2. 步骤二
```

### 关键点

- `name` 在 frontmatter 中定义，与目录名一致
- `description` 是 AI 匹配的唯一依据——**精准描述**
- 结构自由，建议用 ## 分章节

### 参考

- 完整规范：[docs/skills.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/skills.md)
- 系统案例：`plugins/system-skills/skills/`（25+ 技能）

---
