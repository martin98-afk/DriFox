---
description: Team Templates（团队模板）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Team Templates（团队模板）组件开发

> 团队模板让插件预置一组 @角色组合（如「统筹 + 构建 + 审查 + 计划」），
> 用户通过 `/team --load=<name>` 一键拉起多智能体团队。

### 文件位置

```
<plugin>/
├── team_templates/
│   └── my-team.yaml        # 一个文件即一个模板（可多个）
└── .drifox-plugin/
    └── plugin.json         # components 可声明 "team_templates": true（物理自动检测，可选）
```

### 最小模板

```yaml
# team_templates/my-team.yaml
# 用法：/team --load=my-team
schema_version: 1
template_name: my-team
description: 一句话描述这个团队组合
agents:
  - agent_name: leader
    description: 团队统筹 Leader，负责组队与任务分发
  - agent_name: build
    description: 构建智能体，负责读写代码与验证
```

### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `schema_version` | ✅ | 当前固定为 `1`（非 1 会抛 TemplateError） |
| `template_name` | ✅ | 模板名（建议与文件名 stem 一致） |
| `description` | 可选 | 一句话描述（列出时展示） |
| `agents` | ✅ | 非空列表，按顺序对应窗口 1..N |
| `agents[].agent_name` | ✅ | 引用已存在的 @角色名（加载时语义校验，缺失报 TemplateError） |
| `agents[].description` | 可选 | 角色描述，注入团队上下文时附加 |

### 关键约束

- 文件名：长度 1-64，禁止 `.`/`/`/反斜杠/`..`（防路径穿越）
- `agents` 至少 1 个；同一模板内 `agent_name` 必须唯一

### 来源优先级与覆盖

同名时高优先级覆盖低优先级：

1. **user-custom** — `.drifox/plugins/user-custom/team_templates/`（可写、可删）
2. **plugin** — 各启用插件声明的 `team_templates/`（只读，按插件优先级排序）
3. **system** — `plugins/system-team-templates/team_templates/`（只读，内置 default-team）

### 使用方式

| 命令 | 说明 |
|------|------|
| `/team --load=<name>` | 载入指定模板 |
| `/team` | 列出所有可用模板（标示 用户/插件/系统 来源） |
| `/team --save=<name>` | 将当前团队另存为用户模板 |
| `/team --delete=<name>` | 删除用户模板（仅 user-custom 可删） |

### 热插拔

`team_templates/*.yaml` 新增/修改 → 懒加载无缓存，下次 `/team` 即生效。

### 参考

- 模板结构与校验：`app/core/team/template_schema.py`
- 系统模板案例：`plugins/system-team-templates/team_templates/default-team.yaml`
- 测试：`python -m pytest tests/core/test_team_template.py -v`
