---
name: drifox-dev
description: "DriFox 项目专用开发技能。当你需要在 DriFox 项目中进行任何开发工作时（包括功能开发、Bug 修复、UI 组件开发、架构分析、插件/Skill/Agent 开发、代码审查、重构优化），必须首先加载本技能。本技能提供完整的项目架构参考、编码规范、开发流程指引和关键文件速查，确保所有修改符合项目既定模式和规范。即使看起来简单的改动也应该加载此技能，因为 DriFox 的架构复杂、模块依赖关系多，不了解上下文容易违反项目约定。用户提到任何与 DriFox 代码库相关的任务时（说开发、改代码、加功能、修 bug、重构、写插件等），都触发此技能。"
---

# DriFox 开发技能（drifox-dev，有状态版本）

> **本技能是有状态的**。与一般技能不同，每次加载本技能时，Agent 必须**先读取持久化状态**再继续工作。
> 这能让 Agent 跨会话记住：当前在做什么、最近做了什么决策、用户偏好、已踩过的坑、待澄清的问题、项目最新状态。

---

## 0. 加载流程（必读，开局先做）

```
┌──────────────────────────────────────────────────────────┐
│  Step 1 读取持久化状态                                    │
│  → 运行: python plugins/system/skills/drifox-dev/        │
│          scripts/state_manager.py show --summary          │
│  → 解析输出，构造本轮对话的上下文前缀                      │
├──────────────────────────────────────────────────────────┤
│  Step 2 注入"状态摘要"到上下文                            │
│  → 把 Step 1 的输出放在本 SKILL.md 之前作为系统提示的一部分 │
│  → 这样 Agent 知道自己处于什么上下文（焦点/偏好/坑点/状态） │
├──────────────────────────────────────────────────────────┤
│  Step 3 阅读本 SKILL.md 的静态骨架                        │
│  → 了解 DriFox 的固定规范、目录、模式                      │
├──────────────────────────────────────────────────────────┤
│  Step 4 在任务推进过程中，持续更新状态                     │
│  → 做完有意义的步骤：set_focus / add_pitfall / add_decision│
│  → 关键里程碑后：snapshot_project.py 刷新项目快照          │
└──────────────────────────────────────────────────────────┘
```

> **重要**：本文件本身只提供**静态骨架**。动态信息（关键文件行数、最近 commit、未提交变更等）由
> `state.json` 的 `auto_snapshot` 字段提供——它由 `snapshot_project.py` 自动扫描，永不脱节。

---

## 一、项目总览（固定信息）

| 项目 | 值 |
|------|-----|
| 项目名 | DriFox（飘狐） |
| 类型 | AI 桌面对话助手 |
| 语言 | Python 3.14+ |
| UI 框架 | PyQt5 ≥5.15.0 + PyQt-Fluent-Widgets + PyQtWebEngine |
| 包管理 | uv（也支持 pip） |
| 构建 | PyInstaller 打包 |
| 版本 | 0.2.8 |
| 开发分支 | `dev` |
| 仓库 | github.com/martin98-afk/DriFox |

---

## 二、架构总览（四层）

```
┌──────────────────────────────────────────────┐
│  UI 层 (app/widgets/, app/main_widget.py)    │  PyQt Signals → 前端呈现
├──────────────────────────────────────────────┤
│  引擎层 (app/core/engines/)                  │  BaseEngine 统一接口
│    ├── ui/engine.py      — ChatEngine (UI)   │
│    ├── gateway/engine.py — GatewayEngine     │
│    └── auto_loop/engine.py — 自动循环         │
├──────────────────────────────────────────────┤
│  对话执行层 (app/core/conversation/)         │  ConversationCore + Executor
├──────────────────────────────────────────────┤
│  Worker 层 (app/core/workers/)               │  QThread 流式循环
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│  工具层 (app/tools/)                         │  BuiltinTools 动态派发
└──────────────────────────────────────────────┘
```

**后端核心**: `app/core/backend.py`（`ChatBackend`，~2000 行）统一管理 SessionManager / ChatEngine /
GatewayEngine / ToolExecutor / AgentManager / MemoryManagerCore / HookManager /
SubAgentManager / LspManager / PluginManager / SessionStore / HistoryManager。

**关键信号**（后端 → 前端）:
- `session_created(str)` / `message_received(dict)` / `stream_started()` / `stream_chunk(str)` / `stream_finished(dict)`
- `tool_call_started(id, name, args)` / `tool_result_received(id, name, result, success)`
- `permission_requested(id, name, args)` / `error_occurred(str)` / `context_updated(token_count, limit)`

---

## 三、目录速查

### 3.1 app/ 源码目录

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

### 3.2 plugins/ 插件目录

| 路径 | 说明 |
|------|------|
| `plugins/system/` | 系统内置插件（打包在 exe 中） |
| `plugins/system/agents/` | 内置 Agent 定义（build/explore/plan/review 等） |
| `plugins/system/commands/` | 系统命令（/new, /compact, /debug 等） |
| `plugins/system/skills/` | 内置技能（brainstorming, tdd, drifox-dev 等） |
| `plugins/system/themes/` | 主题 |
| `plugins/system/hooks/` | Hook 配置 |
| `.drifox/plugins/` | 用户安装的第三方插件 |

### 3.3 关键文件大小（动态）

> ⚠️ 本节**不要手抄行数**。每次加载技能时，从 `state.json.auto_snapshot.key_files_lines` 读取实时值。
> 需要刷新时运行：`python scripts/snapshot_project.py`

---

## 四、设计模式与约定

### 4.1 关键设计模式

| 模式 | 应用场景 | 说明 |
|------|---------|------|
| **单例模式** | AgentManager, MemoryManagerCore, LspManager, PluginManager, Settings | `_instance = None` + `__new__` 或 classmethod `get_instance()` |
| **动态派发** | BuiltinTools | 通过 `__getattr__` 遍历工具模块，无需手动委托 |
| **观察者模式** | PyQt Signal/Slot | 后端 emit → 前端 slot，自动跨线程（QueuedConnection） |
| **策略模式** | PermissionResolver | allow/ask/deny |
| **适配器模式** | GatewayAdapter | 统一接口适配不同平台 |
| **两阶段停止** | ConversationExecutor | `cancel_worker()` → `finalize_stop()` |
| **事件总线** | WorkerEventBus | 统一分发 Worker 事件 |

### 4.2 多窗口隔离策略

- **窗口级组件**（ChatBackend, ToolExecutor）：每个窗口独立实例
- **全局单例**（AgentManager, MemoryManagerCore, LspManager）：共享只读数据
- **工作目录隔离**：每个 ToolExecutor 有独立 workdir

### 4.3 热更新链路

```
watchfiles 检测变更
  → PluginManager.rescan_plugin()
    → AgentManager.reload_plugin_agents()
    → HookManager.reload_plugin_hooks()
    → 主题/命令刷新
    → UI 重新加载
```

### 4.4 信号/槽机制

- 后端 → 前端通过 **Qt Signal**（继承 QObject）
- 后台线程发射信号 → 主线程槽函数执行（自动 QueuedConnection）
- 跨线程安全：敏感资源用 `_stop_lock` 保护

---

## 五、编码规范

### 5.1 命名约定
- **文档/注释/日志**：使用中文
- **代码符号**：统一英文，语义直白
- **文件名**：小写加下划线

### 5.2 质量标准
- **格式化**：使用 ruff/black
- **设计品味**：优先消除分支与重复，函数单一职责且短小
- **禁止**：顺手重构/大范围改动（除非任务明确要求）
- **Lint**：`ruff check app/`

### 5.3 提交规范
```
feat|fix|docs|chore|refactor|test: scope - summary
```

### 5.4 AGENTS.md 约束
- 禁止修改 `.github/workflows/*.yml`（除非明确要求）
- 禁止修改 `LICENSE`、`CODE_OF_CONDUCT.md`
- 禁止硬编码密钥、Token 或敏感凭证
- **强制同步规则**：任何功能/命令/配置/目录/工作流变化必须同步更新相关文档

---

## 六、核心类速查

### 6.1 智能体系统 (`app/core/agent.py`)

| 类 | 职责 |
|----|------|
| `Agent` | 智能体数据模型（name, prompt, tools, permission, mode） |
| `PermissionResolver` | 工具权限解析（allow/ask/deny，通配符支持） |
| `AgentManager` | 单例，从插件动态加载 Agent，支持增量重载 |

**Agent mode 说明**:
- `primary`/`all` → 主智能体
- `subagent`/`all` → 子智能体
- `mode=None` → 隐藏

### 6.2 Hook 系统 (`app/core/hook_manager.py`)

| 事件 | 触发时机 |
|------|---------|
| `SessionStart` | 新会话启动 |
| `PreUserMessage` / `PostUserMessage` | 用户消息前后 |
| `PreAssistantMessage` | AI 回复前 |
| `PreToolUse` / `PostToolUse` | 工具执行前后（前者可 BLOCK） |

### 6.3 工具系统 (`app/tools/__init__.py`)

**BuiltinTools** 通过 `__getattr__` 动态派发到各模块：

| 工具集 | 模块 | 核心工具 |
|--------|------|---------|
| FileTools | file_tools.py | read/write/edit/multi_edit/grep/glob/list |
| TerminalTools | terminal_tools.py | bash/bg_start/bg_stop/bg_logs/bg_list |
| WebTools | web_tools.py | webfetch/websearch |
| TaskTools | task_tools.py | todowrite/todoread/stage_files |
| MCPTools | mcp_tools.py | mcp_list_servers + MCP 动态工具 |
| AutomationTools | automation.py | mouse/keyboard/screenshot |
| DiagnosticsTools | diagnostics_tools.py | get_diagnostics/lsp |

### 6.4 插件系统 (`app/core/plugin_manager.py`)

插件清单文件: `{plugin}/.drifox-plugin/plugin.json`

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

---

## 七、开发工作流

### 7.1 标准开发流程

```
Step 1 理解需求并确认范围
  ├── 阅读任务描述
  ├── 用 grep/glob 定位相关文件
  └── 有疑问时用 brainstorming 技能

Step 2 探索相关代码
  ├── 阅读涉及模块的入口和关键类
  ├── 理解现有架构
  └── 确认修改范围

Step 3 制定修改计划
  ├── 明确改动点
  └── 涉及架构变更先写设计文档

Step 4 执行修改
  ├── 遵循项目现有编码风格
  ├── 只改需要改的
  ├── 保持信号链一致性
  └── 保持多窗口隔离策略

Step 5 验证
  ├── 代码可运行
  ├── 逻辑正确
  ├── ruff check
  └── 更新相关文档

Step 6 提交
  └── git add -A && git commit -m "..."
```

### 7.2 UI 组件开发要点

| 要点 | 说明 |
|------|------|
| 继承 QObject | 需要信号/槽的类必须继承 QObject |
| 信号定义 | 后端定义 → 前端在 `setup_ui` 中连接 |
| 线程安全 | UI 操作必须在主线程；后台用信号通知 UI |
| 卡片系统 | 设置类 UI 用 `widgets/cards/` |
| 设计令牌 | 使用 `app/utils/design_tokens.py` |
| 主题兼容 | 支持亮/暗切换，勿硬编码颜色 |

### 7.3 Agent/Skill 开发要点

**Agent 定义** (`plugins/system/agents/`):
```markdown
# Agent: xxx
你是 [Name]，[Description]。
## 工具
- [工具列表]
## 系统提示词
[详细指令]
```

**Skill 定义** (`plugins/system/skills/{name}/SKILL.md`):
```markdown
---
name: skill-name
description: "触发描述"
---
# 技能标题
[技能指令]
```

### 7.4 测试要点
- 修改工具执行逻辑 → 测试所有受影响的工具调用
- 修改信号链 → 确认前端能正确收到信号
- 修改多窗口组件 → 验证窗口间状态隔离
- 修改 Hook 系统 → 验证事件触发/阻断逻辑

---

## 八、常见开发场景

### 8.1 新增功能
```
1. 明确功能需求
2. 定位要修改/新建的文件
3. 遵循现有模块结构
4. 注册新组件（命令/Agent/Skill）
5. 更新文档
```

### 8.2 修复 Bug
```
1. 复现 Bug
2. grep 关键词定位
3. 理解根本原因
4. 最小化修改
5. 验证 + 防回归
```

### 8.3 新增插件
```
1. 在 .drifox/plugins/ 下创建目录
2. 创建 .drifox-plugin/plugin.json
3. 按需创建 commands/agents/skills/themes/hooks
4. 配置 .mcp.json（如需要）
5. 重启或触发重扫
```

### 8.4 修改现有 Skill
```
1. 定位 skill 目录
2. 修改 SKILL.md
3. 更新 description（如触发条件变了）
4. 测试
5. 更新文档
```

---

## 九、构建与部署

```bash
uv sync                          # 安装核心依赖
uv sync --group dev              # 开发工具
uv sync --all-groups             # 全部
python main.py                   # GUI
python cli.py --version          # CLI
python build.py                  # PyInstaller 打包
ruff check app/                  # Lint
ruff format app/ --check         # 格式
pytest                           # 测试
```

---

## 十、路径使用建议

- **项目根目录**: `D:/work/DriFoxx/`
- 根目录内：用相对路径
- 根目录外：用绝对路径
- 关键文档：`README.md`、`AGENTS.md`

---

## 十一、状态管理（**本技能特有**）

### 11.1 state.json 结构

完整定义见 `state/state.template.json`，运行字段：

| 字段 | 用途 | 写入方式 |
|------|------|---------|
| `version` | schema 版本 | 自动 |
| `current_focus` | 当前正在处理的任务/模块/分支 | Agent 手动（`focus`） |
| `recent_decisions` | 最近的架构/技术决策 | Agent 手动（`decision`） |
| `user_preferences` | 用户编码风格偏好 | 用户/Agent（`preference`） |
| `known_pitfalls` | 已踩过的坑 | Agent 手动（`pitfall`） |
| `open_questions` | 待澄清的开放问题 | Agent 手动（`question`） |
| `auto_snapshot` | 项目实时快照（行数/git） | **自动**（`snapshot_project.py`） |

### 11.2 state_manager.py CLI

```bash
# 初始化（首次使用）
python scripts/state_manager.py init

# 查看完整状态（JSON）
python scripts/state_manager.py show

# 查看给 AI 看的摘要（技能加载时调这个）
python scripts/state_manager.py show --summary

# 设置当前焦点
python scripts/state_manager.py focus --task "重构 tool_control_card" --module tool_control_card --branch dev

# 记录一条决策
python scripts/state_manager.py decision \
    --scope "agent" \
    --decision "把 drifox-dev 升级为有状态技能" \
    --rationale "跨会话需要记住项目状态"

# 记录一条坑点
python scripts/state_manager.py pitfall \
    --module tool_control_card \
    --symptom "用户开关不更新" \
    --cause "_on_active_toggles_changed 缺 rebuild 链路" \
    --fix "完整实现 + 直接调 rebuild()"

# 记录/解决开放问题
python scripts/state_manager.py question --question "..." --context "..."
python scripts/state_manager.py question --resolve Q001

# 修改用户偏好
python scripts/state_manager.py preference --key no_unrelated_refactor --value true
```

### 11.3 snapshot_project.py 自动化

```bash
# 采集并写入 state.json
python scripts/snapshot_project.py

# 只预览不写入
python scripts/snapshot_project.py --json
```

> **何时刷新快照**：
> - 任务开始时（建立基线）
> - 重要 commit 后
> - 关键文件大改后（如重构一个核心模块）
> - 任何时候觉得 state.json 的行数对不上时

### 11.4 Agent 使用规约

| 场景 | 操作 |
|------|------|
| **加载本技能时** | `state_manager.py show --summary` → 注入到上下文 |
| **明确本轮任务时** | `state_manager.py focus --task "..."` |
| **做出架构决策时** | `state_manager.py decision ...` |
| **发现/修复一个坑时** | `state_manager.py pitfall ...` |
| **遇到不确定但暂不深究的问题** | `state_manager.py question ...` |
| **完成任务/切换任务** | `state_manager.py focus --clear` |
| **上下文长期任务** | 中途调用 `snapshot_project.py` 刷新行数 |

### 11.5 设计原则（参考 eliteai.tools / Anthropic 实践）

1. **原子写入**：`tempfile + os.replace` 防止崩溃时损坏
2. **跨平台文件锁**：Windows 用 `msvcrt`，Linux/Mac 用 `fcntl`
3. **版本迁移**：schema 升级时 `migrate_state()` 自动迁移
4. **去重与限额**：坑点/决策各保留最近 50 条
5. **不冗余原则**：动态事实（行数/commit）由脚本采集，AI 不手抄
6. **优雅降级**：模板/锁/迁移任何环节失败都不阻塞主流程

---

## 注意事项

1. **信号链完整性**：修改后端信号时，确保前端有对应的槽函数连接
2. **单例访问**：通过 `get_instance()` 获取
3. **线程安全**：后台线程不要直接操作 UI，用信号通知主线程
4. **文件备份**：edit/multi_edit 在修改前会自动备份到 `.drifox/backups/`
5. **文档同步**：任何功能/配置变更必须更新相关文档
6. **窗口隔离**：不要假设跨窗口共享状态
7. **状态保鲜**：本技能的有状态能力依赖 state.json 及时更新，AI 应主动维护
