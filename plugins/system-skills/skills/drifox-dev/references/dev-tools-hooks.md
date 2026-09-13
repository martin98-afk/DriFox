# 工具 / Hook / MCP / 权限

> 加工具、改 Hook、调权限、接 MCP 时加载。
> 写完整插件（含 tools/ 目录、plugin.json）→ 优先用 `plugin-creator` 技能。

---

## 一、工具系统

**应用层**：`app/tools/`

| 模块 | 职责 |
|------|------|
| `__init__.py` | `BuiltinTools(QObject)` 入口 |
| `registry.py` | `ToolRegistration` / `ToolRegistry`（**单一数据源**：schema、图标、分组、danger） |
| `tool_classifier.py` | 工具分类（danger 分级） |
| `tool_name_mapper.py` | `ToolNameMapper` 别名映射（含元类） |
| `result.py` | `ToolResult` |
| `bg_manager.py` | `BackgroundTaskManager` 后台任务 |
| `command_safety.py` | 命令白名单 |
| `process_job.py` / `pty_session.py` | 进程 / 伪终端会话 |
| `mcp_tools.py` | MCP 工具桥 |

**插件层**：`plugins/<name>/tools/<模块>.py` 里实现 `register(registry)`，注册时带 `schema + impl + danger + icon + cn_name + group + description + aliases`。
registry 驱动 LLM schema / 图标 / 分组 / 别名映射，**增删改热生效**，不要在应用层硬编码工具清单。

新增工具三步：
1. 在 `plugins/<name>/tools/<模块>.py` 写实现 + `register(registry)`；系统工具放 `plugins/system-tools/tools/`。
2. 校验 schema 与 danger 分级（走 `tool_classifier`）。
3. 更新 Agent 的 `tools:` 权限（`.drifox/plugins/<p>/agents/*.md` 或 `plugins/system-agents/agents/`）。

## 二、Hook 系统（`app/core/hook_manager.py`）

| 事件 | 时机 | 可阻断 |
|------|------|--------|
| `SessionStart` | 会话启动 | 否 |
| `PreUserMessage` / `PostUserMessage` | 用户消息前后 | 否 |
| `PreAssistantMessage` | AI 回复前 | 否 |
| `PreToolUse` / `PostToolUse` | 工具执行前后 | **前者可 BLOCK** |

类型：`command`（bash/Python）、`prompt`（`__prompt__:` 前缀，直接插入消息列表）、`prompt_command`（动态注入）。
Hook 策略由 `app/plugins/contracts/hook_policy.py` + `registries/hook_policy_registry.py` 注册，`plugins/system-hook-policies/hook_policies/`。

**已知坑（改动前必读 `known-pitfalls.md` §四）**
- 热重载后 hook 顺序变化 → 卸载前记录位置，注册时按原位置 insert。
- 分组映射错位 → pop rule 后遍历所有 skill 修正索引。
- 来源标签错乱 → 以 `rule.skill_name` 为归属真相，全量重建映射。
- 开关幽灵状态 / 双实例覆盖 → 双轨制：插件 hook 写回源文件，系统 hook 保留覆盖层。
- `PostToolUse` matcher 要大小写不敏感匹配工具名；`prompt` 类型 hook 总应进入消息列表。

## 三、MCP

- 配置：`plugins/system-mcp/.mcp.json`，用户级插件各自 `.mcp.json`。
- 安全确认：`app/core/mcp_lsp_safety.py`（用户级插件新服务器需确认，确认 UI 必须真正接线，见 P005）。
- 连接防重：`_busy_names` 与全量连接共享同一套防重；`on_done` 移出锁外。
- 热重载安装了带 `.mcp.json` 的插件 → 广播分支要主动 `refresh_connections()` 补连（幂等）。
- 插件 SDK 一律 vendor 到 `<插件>/deps/`，函数内**延迟导入**，禁止顶层导入第三方 SDK。

## 四、权限（`PermissionResolver` / `tool_permission_controller.py`）

策略 `allow / ask / deny`，支持通配符（`Write*`、`Bash(git:*)`）。
改权限前先 grep 现有规则，确认无冲突；对话侧策略枚举见 `app/core/conversation/config.py`（`PermissionStrategy`）。
