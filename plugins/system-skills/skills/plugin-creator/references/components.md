---
description: 18 类组件文档索引——按组件加载对应文件
---

# 组件文档索引

> **渐进加载**：处理某类组件任务时，只读对应组件文件，不要全量加载。
> 每个文件含：文件位置、最小模板、关键约束、组件排障、真实样例路径。

| 组件 | 文件 |
|------|------|
| [Commands（斜杠命令）](components/commands.md) | `commands.md` |
| [Agents（@智能体）](components/agents.md) | `agents.md` |
| [Skills（AI 技能）](components/skills.md) | `skills.md` |
| [Hooks（事件钩子）](components/hooks.md) | `hooks.md` |
| [MCP（服务器配置）](components/mcp.md) | `mcp.md` |
| [LSP（语言服务器）](components/lsp.md) | `lsp.md` |
| [Themes（主题配色）](components/themes.md) | `themes.md` |
| [UI（界面组件）](components/ui.md) | `ui.md` |
| [Tools（工具插件化）](components/tools.md) | `tools.md` |
| [Providers（服务商插件化）](components/providers.md) | `providers.md` |
| [Team Templates（团队模板）](components/team-templates.md) | `team-templates.md` |
| [Hook Policies（hook 触发策略）](components/hook-policies.md) | `hook-policies.md` |
| [Loop Policies（循环策略）](components/loop-policies.md) | `loop-policies.md` |
| [Engines（对话引擎）](components/engines.md) | `engines.md` |
| [Storages（会话存储引擎）](components/storages.md) | `storages.md` |
| [Serializers（消息序列化器）](components/serializers.md) | `serializers.md` |
| [Gateways（消息网关平台适配器）](components/gateways.md) | `gateways.md` |
| [Model Adapters（模型协议适配器）](components/model-adapters.md) | `model-adapters.md` |

## 通用流程（跨组件）

| 主题 | 文件 |
|------|------|
| 脚手架与迭代流程 | [workflow.md](workflow.md) |
| manifest 与 config_schema | [manifest.md](manifest.md) |
| 测试与热更新验证 | [testing.md](testing.md) |
| 发布与市场 PR | [publishing.md](publishing.md) |
| 通用排障（manifest/热重载/发布） | [troubleshooting.md](troubleshooting.md) |

## 触发点速记（策略/引擎类）

- 引擎会话：`services["create_engine_session"](name, hook_policy_id=…, loop_policy_id=…)`
- worker 侧事件（工具级/Stop/消息级）：ChatWorker 按 hook policy 过滤
- 引擎侧事件（SessionStart/BuildSystemPrompt/PreUserMessage）：EngineSession 按 hook policy 过滤
