# Agent / Skill / 插件开发

> SKILL.md 在任务涉及「创建 Agent / 修改 Skill / 新增插件」时分派到这里。

---

## 一、Agent 定义

**位置**：`plugins/system/agents/<name>.md`

**最小骨架**：

```markdown
---
name: <agent-name>
description: "<触发描述>"
mode: primary|all|subagent|all
tools: [file.read, file.write, ...]
permission: default|strict
---

# Agent: <名字>
你是 [Name]，[Description]。

## 工具
- [工具列表]

## 系统提示词
[详细指令]
```

| `mode` 取值 | 含义 |
|------------|------|
| `primary` | 主智能体 |
| `subagent` | 子智能体 |
| `all` | 同时主 + 子可见 |
| `None` | 隐藏 |

## 二、Skill 定义

**位置**：`plugins/system/skills/<name>/SKILL.md`

**最小骨架**：

```markdown
---
name: skill-name
description: "<触发描述 — 写到能被 LLM 通过关键词自动匹配的程度>"
---

# 技能标题

[技能指令]

## 加载时机
[何时该加载]

## 工作流
[步骤]
```

修改 Skill 后：
1. 更新 `description`（如果触发条件变化）
2. 同步 README.md（如有）
3. 测试: 实际触发一次确认加载

## 三、插件开发

**位置**：`.drifox/plugins/<plugin-name>/`

**清单文件**：`<plugin>/.drifox-plugin/plugin.json`

```json
{
    "name": "my-plugin",
    "version": "1.0.0",
    "components": {
        "commands": true, "agents": true, "skills": true,
        "themes": true, "hooks": true, "mcp": true, "lsp": true
    }
}
```

**可注册组件子目录**：
- `commands/` — 斜杠命令
- `agents/` — Agent
- `skills/<name>/` — Skill
- `themes/` — 主题
- `hooks/hooks.json` — Hook 配置
- `.mcp.json` — MCP 服务器

**开发完成**：
1. 重启 / 触发重扫（`PluginManager.rescan_plugin()`）
2. 在测试里引用 / 在主进程加载验证
3. 更新 `.drifox/plugins/README.md` 或文档
