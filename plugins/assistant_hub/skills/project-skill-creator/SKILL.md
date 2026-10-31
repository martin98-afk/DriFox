---
name: project-skill-creator
description: 在当前项目创建「项目内技能」（project skill）。当用户说「给当前项目加个技能」「创建项目技能」「把这个流程固化成项目技能」「在 .drifox/skills 里建技能」时使用。项目技能只对当前项目生效，放进目录即被发现并自动启用。
---

# project-skill-creator — 项目技能创建

项目技能是放在 `<项目根>/.drifox/skills/<技能名>/SKILL.md` 的技能，随项目走、入 git、
只对当前项目生效。放进目录即被发现：自动进系统提示「偏好技能」段、`/技能名` 可加载、
`manage_skill(action="list")` 可见（来源标 project）。

## 创建流程

1. 确认目录：`<项目根>/.drifox/skills/`（不存在则创建）
2. 新建技能目录（小写字母/数字/中划线，如 `release-checklist`）
3. 写 `SKILL.md`：

---
name: <技能名>
description: <一句话触发描述，必须覆盖用户会说的触发词；这是模型路由依据>
---

# <技能名>

（技能正文：工作流步骤、规则约束、示例。详细资料可拆 references/ 子文件按需加载）

4. 验证：`manage_skill(action="list")` 应出现该技能（来源 project）；
   输入 `/技能名` 应能加载正文。

## 规范

- `name` 必须与目录名一致（小写中划线）
- `description` 决定自动触发倾向，写「何时用」，不写「是什么」
- frontmatter 后空一行再写正文；正文不要重复 frontmatter
- 禁用某个项目技能：编辑 `<项目根>/.drifox/skills/.disabled.json`：
  `{"disabled": ["技能名"]}`（本地图件，建议加入 .gitignore）
- 删除技能：直接删技能目录
- 技能名不要带 `-skill`/`-cmd`/`-prompt`/`-agent` 后缀（这些是同名消歧保留后缀）

## 与全局技能的区别

| | 项目技能 | 全局技能 |
|---|---|---|
| 位置 | `<项目根>/.drifox/skills/` | 用户技能目录（manage_skill create 默认） |
| 生效范围 | 仅当前项目 | 所有项目 |
| 启用方式 | 自动（可用 .disabled.json 禁用） | 设置卡手动启用 |
| 版本管理 | 随项目 git | 不入项目库 |
