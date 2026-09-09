# Agent / Skill / 插件清单

> 创建 Agent / 修改 Skill / 写插件清单时加载。
> **完整插件开发走 `plugin-creator` 技能（11 类组件）**，本文件只给 DriFox 专属约定。

---

## 一、Agent 定义

位置：`plugins/system-agents/agents/<name>.md`（用户级：`.drifox/plugins/<p>/agents/`）

```markdown
---
name: <agent-name>
description: "<触发描述>"
mode: primary|subagent|all
tools: [file.read, file.write, ...]
permission: default|strict
---

# Agent: <名字>
你是 [Name]，[Description]。

## 工具
## 系统提示词
```

`mode`：`primary` 主智能体 / `subagent` 子智能体 / `all` 两者可见 / 缺省隐藏。

## 二、Skill 定义

位置：`plugins/system-skills/skills/<name>/SKILL.md`

```markdown
---
name: skill-name
description: "<触发描述 — 写到能被 LLM 通过关键词自动匹配的程度>"
---
```

改完 Skill：更新 `description`（触发条件变了必须改）+ README，并**实际触发一次**验证加载。
渐进式披露的写法参考本技能自身：SKILL.md 只放骨架，细节放 `references/`。

## 三、插件清单（`plugin.json`）

```json
{
    "name": "my-plugin",
    "version": "1.0.0",
    "config_schema": {
        "title": "插件设置",
        "fields": [
            { "key": "api_key", "label": "API Key", "type": "password",
              "env": "MY_PLUGIN_API_KEY", "placeholder": "...", "description": "..." }
        ]
    },
    "module_prefixes": ["my_plugin_core."]
}
```

- **E1 配置契约**：`config_schema` 的 `type` 取 `text / password / bool / select / number / textarea`；主程序自动渲染设置卡，存储到 `<app_data_dir>/plugins/<plugin>/config.json`；取值链：环境变量 → 存储 → 默认。
- `module_prefixes`（可选）：热重载时按前缀 purge `sys.modules`，解决改了代码不生效（P038）。

## 四、组件目录（插件根下）

| 目录 | 组件 | 注册方式 |
|------|------|---------|
| `tools/` | 工具 | `register(registry)`，见 `dev-tools-hooks.md` |
| `model_adapters/` | 模型适配器 | `register(registry)` |
| `loop_policies/` | 循环策略 | `register(registry)` + `set_active(id)` |
| `storages/` | 存储引擎 | `register(registry)` |
| `serializers/` | 消息序列化 | `register(registry)` |
| `hook_policies/` | Hook 策略 | `register(registry)` |
| `providers/` | Provider | `register(registry)` |
| `commands/` | 斜杠命令 | 命令文件 |
| `agents/` | Agent | markdown |
| `skills/<name>/` | Skill | SKILL.md |
| `themes/` | 主题 | 主题定义 |
| `hooks/` | Hook 配置 | hooks.json |
| `ui/` | UI 扩展点 | `register_ui` 导出 |
| `team_templates/` | 团队模板 | 模板定义 |
| `.mcp.json` | MCP 服务器 | 配置 |
| `deps/` | vendor 的第三方 SDK | 运行时 `sys.path` 优先 |

系统插件族：`plugins/system-{tools,ui,hooks,hook-policies,commands,agents,skills,themes,model-adapters,loop-policies,serializers,storages,providers,team-templates,mcp,cleaner}`。

## 五、开发完成检查

1. 触发重扫 / 热重载验证；**新增组件类型要确认差异补载生效**（P034），否则需重启。
2. 第三方 SDK 一律 `deps/` + 函数内延迟导入（顶层导入会拖垮宿主包加载）。
3. UI 插件另见 `ui-plugin-creator` 技能；图标提供深色 + 浅色两套。
4. 更新插件 README；`marketplace.json` 由 CI 自动重生成。
