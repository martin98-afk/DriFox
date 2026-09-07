# DriFox 架构参考

> 仅在 SKILL.md 把任务分派到「涉及架构理解/模块定位」时按需加载。

---

## 一、四层架构总览

```
┌──────────────────────────────────────────────┐
│  UI 层 (app/widgets/, app/main_widget.py)    │  PyQt Signals → 前端呈现
├──────────────────────────────────────────────┤
│  引擎层 (app/core/engines/)                  │  BaseEngine 统一接口
│    ├── ui/engine.py      — ChatEngine (UI)   │
│    └── gateway/engine.py — GatewayEngine     │
│    （AutoLoop 引擎已插件化 → plugins/autoloop/，│
│      经 ui context services 驱动自建栈）      │
├──────────────────────────────────────────────┤
│  对话执行层 (app/core/conversation/)         │  ConversationCore + Executor
├──────────────────────────────────────────────┤
│  Worker 层 (app/core/workers/)               │  QThread 流式循环
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│  工具层 (app/tools/)                         │  BuiltinTools 动态派发
└──────────────────────────────────────────────┘
```

**后端核心**：`app/core/backend.py`（`ChatBackend`，~2000 行）统一管理：
- SessionManager / ChatEngine / GatewayEngine
- ToolExecutor / AgentManager / MemoryManagerCore
- HookManager / SubAgentManager / LspManager
- PluginManager / SessionStore / HistoryManager

## 二、目录速查

### app/ 源码目录

| 路径 | 说明 | 关键文件 |
|------|------|---------|
| `app/core/` | 核心业务逻辑 | backend.py, agent.py, tool_executor.py, plugin_manager.py |
| `app/core/conversation/` | 对话执行层 | core.py, executor.py |
| `app/core/engines/` | 引擎层 | base.py, ui/engine.py |
| `app/core/workers/` | 工作线程 | chat_worker.py, subagent_worker.py |
| `app/core/lsp/` | LSP 集成 | lsp_manager.py |
| `app/core/store/` | 数据持久化 | session_store.py |
| `app/tools/` | 35+ 工具实现 | __init__.py, file_tools.py |
| `app/widgets/` | UI 组件 | message_card.py, bottom_input_area.py |
| `app/widgets/cards/` | 卡片组件 | card_container.py, card_manager.py |
| `app/gateway/` | 跨平台消息网关 | manager.py |
| `app/utils/` | 工具函数 | config.py, theme_manager.py |

### plugins/ 插件目录

| 路径 | 说明 |
|------|------|
| `plugins/system/` | 系统内置插件（打包在 exe 中） |
| `plugins/system/agents/` | 内置 Agent 定义 |
| `plugins/system/commands/` | 系统命令（/new, /compact, /debug 等） |
| `plugins/system/skills/` | 内置技能 |
| `plugins/system/themes/` | 主题 |
| `plugins/system/hooks/` | Hook 配置 |
| `.drifox/plugins/` | 用户安装的第三方插件 |

## 三、关键文件行数（动态）

> ⚠️ **不要手抄行数**。每次加载技能时，从 `state.json.auto_snapshot.key_files_lines` 读取实时值。
> 需要刷新时：`python scripts/snapshot_project.py`

## 四、后端 → 前端关键信号

- `session_created(str)` / `message_received(dict)`
- `stream_started()` / `stream_chunk(str)` / `stream_finished(dict)`
- `tool_call_started(id, name, args)` / `tool_result_received(id, name, result, success)`
- `permission_requested(id, name, args)` / `error_occurred(str)`
- `context_updated(token_count, limit)`
