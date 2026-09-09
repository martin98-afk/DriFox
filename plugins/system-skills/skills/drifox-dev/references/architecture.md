# DriFox 架构参考（2026-09 实勘版）

> 在 SKILL.md 把任务分派到「架构理解 / 模块定位」时加载。
> 路径以实际仓库为准；行数一律从 `state.json.auto_snapshot.key_files_lines` 读。

---

## 一、分层总览

```
UI 层      app/widgets/  +  app/main_widget.py          PyQt5 组件 · 信号槽 · QWebEngine
模块层      app/widgets/modules/  app/widgets/cards/     主窗口拆分模块 · 卡片·浮动卡
引擎层      app/core/engines/{base,ui/gateway}           双引擎：UI 交互 / 网关机器人
对话层      app/core/conversation/                       ConversationCore + Executor + adapters
Worker 层   app/core/workers/                            QThread 流式循环（chat/subagent）
核心服务    app/core/*.py（38 个平铺模块）                backend / hook / tool / team / usage ...
工具层      app/tools/                                   BuiltinTools + ToolRegistry + 别名映射
插件内核    app/plugins/                                 contracts(Protocol) + registries(单例注册表)
插件资产    plugins/system-*/  +  ~/.drifox/plugins/      系统插件族 + 用户插件
```

## 二、`app/core/`（平铺 38 模块 + 5 子包）

> 注意：`backend / chat_session / hook_manager` **不是目录**，是顶层模块。

| 模块 | 职责 |
|------|------|
| `backend.py` | `ChatBackend` 门面：会话、插件、工具、Hook 的统一入口（~1.4K 行） |
| `chat_session.py` | 单次会话生命周期 |
| `hook_manager.py` | Hook 调度中枢（~3K 行，改动前先读 `known-pitfalls.md`） |
| `tool_executor.py` / `tool_call_parser.py` / `tool_permission_controller.py` / `tool_result_persister.py` | 工具调用链路四件套 |
| `agent.py` | 顶层 agent 编排 |
| `command_manager.py` / `builtin_commands.py` | 斜杠命令注册与内置命令 |
| `history_compactor.py` / `context_builder.py` / `context_usage.py` | 上下文构建与压缩 |
| `team_manager.py` | 多智能体团队（运行目录按 run_id 隔离） |
| `ui_event_bus.py` / `window_registry.py` / `ui_callback_watchdog.py` | UI 横切服务 |
| `gateway_service.py` / `plugin_host_service.py` | 网关与插件宿主服务 |
| `webengine_profile.py` / `crash_handler.py` / `single_instance.py` | 共享 WebEngine profile / 崩溃处理 / 单实例 |
| `mcp_lsp_safety.py` | MCP / LSP 安全确认 |

子包：

| 子包 | 内容 |
|------|------|
| `conversation/` | `core.py` `executor.py` `engine_session.py` `stack_factory.py` `config.py`（PermissionStrategy / HookPolicy）+ `adapters/{base,ui,gateway}.py` |
| `engines/` | `base.py`（BaseEngine）+ `ui/engine.py`（UIEngine + `_PreSendWorker`）+ `gateway/engine.py` |
| `workers/` | `chat_worker.py`（~5K 行）、`subagent_worker.py`、`worker_event_bus.py`、`cache_tracker.py`、`error_handler/` |
| `lsp/` | `lsp_manager.py` `lsp_client.py` `lsp_config.py` `lsp_tools.py` |
| `store/` | `session_store.py` + 各 repository（`session` `memory` `input_history` `file_operation` `key_documents` `subagent_log`）+ `serde.py` |
| `team/` | `template_manager.py` `template_schema.py` |
| `system/` | `lock_screen_remote.py` |

## 三、插件内核 `app/plugins/`

| 路径 | 内容 |
|------|------|
| `contracts/` | Protocol 定义：`model_adapter` `loop_policy` `storage` `message_serializer` `gateway_platform` `hook_policy` `plugin_config` `engine_host` `engine_session` `dialogue_engine` `conversation_stack` `ui_host` `ui_module` `ui_page` `ui_slots` `manifest_schema` |
| `registries/` | 12 个注册表单例：`model_adapter` `loop_policy` `storage` `serializer` `gateway_platform` `hook_policy` `provider` `engine` `plugin_config` `ui_plugin` `coding_plan_fetcher` + `_builtin_fallback.py` |
| `loaders/` | 插件组件加载器 |
| `managers/` | 插件管理器族 |
| `kernel.py` / `deps_loader.py` / `version_gate.py` / `builtin_reloaders.py` / `component_items.py` | 内核：加载 / 依赖隔离 / 版本闸门 / 内置重载 / 组件项 |

**插件化硬约束**：主程序里**不写平台 if**——平台能力由插件注册到 registry，主程序只查注册表。

## 四、`app/tools/`

```
app/tools/
├── __init__.py          # BuiltinTools(QObject) —— 工具入口（动态派发 + 注册表双通道）
├── registry.py          # ToolRegistration / ToolRegistry
├── result.py            # ToolResult
├── tool_classifier.py   # 工具分类器（danger 分级）
├── tool_name_mapper.py  # ToolNameMapper + 元类（别名映射）
├── bg_manager.py        # BackgroundTask / BackgroundTaskManager
├── command_safety.py    # 命令执行白名单
├── process_job.py / pty_session.py   # 进程与伪终端会话
├── task_state.py        # 任务状态
└── mcp_tools.py         # MCP 工具桥
```

> 新增工具详见 `dev-tools-hooks.md`。分类 / 别名 / 权限全部走 registry，不要在应用层硬编码。

## 五、`app/widgets/`（UI 层）

| 文件 | 职责 |
|------|------|
| `message_card.py` | 消息卡片 + `CodeWebViewer`（骨架屏 / 流式注入 / 打字机 / FLIP / 图表），**最热文件** |
| `main_widget.py` | 主窗口（21K+ 行）：发送、滚动锚定、卡片配额回收、事件总线接线 |
| `tab_manager_window.py` | 多窗口 / 多标签：`_ContentStack` 叠放、`ResizeOrchestrator`、工作台 |
| `webview_pool.py` | `WebViewPool` WebEngine 实例池 |
| `height_commit_batch.py` | `HeightCommitBatch` 批量高度提交 + 锚点保持 |
| `resize_orchestrator.py` | resize 期协调，避免抖动 |
| `workbench_panel.py` | 右侧工作台（插件页槽位） |
| `bottom_input_area.py` | 输入区 |
| `cards/` `modules/` | 卡片组件 / 主窗口拆分模块 |

## 六、`plugins/` 插件族（24 个）

| 插件 | 提供组件 |
|------|---------|
| `system-tools` | `tools/` |
| `system-ui` | `ui/`（UI 扩展点） |
| `system-hooks` | `hooks/` |
| `system-hook-policies` | `hook_policies/` |
| `system-commands` | `commands/` |
| `system-agents` | `agents/` |
| `system-skills` | `skills/` |
| `system-themes` | `themes/` |
| `system-model-adapters` | `model_adapters/` |
| `system-loop-policies` | `loop_policies/` |
| `system-serializers` | `serializers/` |
| `system-storages` | `storages/` |
| `system-providers` | `providers/` |
| `system-team-templates` | `team_templates/` |
| `system-mcp` | `.mcp.json` |
| `system-cleaner` | `ui/`（缓存清理） |
| `assistant_hub` | 人格 / 记忆 / Dream 编译（独立大插件） |
| `agent_trace` `context-usage-stats` `file-tree` `plugin-marketplace` `share-history` `shortcut-manager` `welcome_changelog` | 功能插件 |

用户级插件：`~/.drifox/plugins/`（watchfiles 热扫描，用户根覆盖 system 根）。

## 七、关键信号链（后端 → 前端）

- 会话：`session_created(str)` / `message_received(dict)`
- 流式：`stream_started()` / `stream_chunk(str)` / `stream_finished(dict)`
- 工具：`tool_call_started(...)` / `tool_result_received(...)` / `permission_requested(...)`
- 其它：`context_updated(token_count, limit)` / `error_occurred(str)`

> 改后端信号必须确认前端有对应槽连接；UI 侧横切事件优先走 `app/core/ui_event_bus.py`。

## 八、不要做的三件事

1. 不要在 `main_widget.py` / `chat_worker.py` / `hook_manager.py` 继续堆逻辑——先看 `modules/` 或 `core/` 有没有更合适的落点。
2. 不要在主程序写平台 / 模型 / 存储类型的 if——走 registry。
3. 不要手抄行数、版本号、最近的 commit——读 `state.json.auto_snapshot`。
