---
name: drifox-dev
description: "DriFox 项目专用开发技能。当你需要在 DriFox 项目中进行任何开发工作时（包括功能开发、Bug 修复、UI 组件开发、架构分析、插件/Skill/Agent 开发、代码审查、重构优化），必须首先加载本技能。本技能提供完整的项目架构参考、编码规范、开发流程指引和关键文件速查，确保所有修改符合项目既定模式和规范。即使看起来简单的改动也应该加载此技能，因为 DriFox 的架构复杂、模块依赖关系多，不了解上下文容易违反项目约定。用户提到任何与 DriFox 代码库相关的任务时（说开发、改代码、加功能、修 bug、重构、写插件等），都触发此技能。"
---
# DriFox 开发技能（drifox-dev）

本技能为 **DriFox AI 桌面对话助手** 的专用开发指南。加载后，请根据当前任务类型参阅相应的章节。

---

## 一、项目总览

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

## 二、架构总览

### 2.1 分层架构（四层）

```
┌──────────────────────────────────────────────┐
│  UI 层 (app/widgets/, app/main_widget.py)      │  PyQt Signals → 前端呈现
├──────────────────────────────────────────────┤
│  引擎层 (app/core/engines/)                    │  BaseEngine 统一接口
│    ├── ui/engine.py      — ChatEngine (UI)     │
│    ├── gateway/engine.py — GatewayEngine       │
│    └── auto_loop/engine.py — 自动循环           │
├──────────────────────────────────────────────┤
│  对话执行层 (app/core/conversation/)           │  ConversationCore + Executor
│    ├── core.py     — ConversationCore          │
│    └── executor.py — ConversationExecutor     │
├──────────────────────────────────────────────┤
│  Worker 层 (app/core/workers/)                 │  QThread 流式循环
│    ├── chat_worker.py       — 核心对话循环      │
│    ├── subagent_worker.py   — 子智能体         │
│    └── auto_loop_worker.py  — 自动循环         │
├──────────────────────────────────────────────┤
│  工具层 (app/tools/)                           │  BuiltinTools 动态派发
└──────────────────────────────────────────────┘
```

### 2.2 后端核心 (ChatBackend)

**文件**: `app/core/backend.py`（~2000行）

ChatBackend 是**统一后端接口**，创建和管理所有核心组件。它通过 PyQt Signal 与前端通信。

```
ChatBackend 创建并管理:
├── SessionManager         — 会话管理
├── ChatEngine (UI)        — UI 对话引擎
├── GatewayEngine          — 跨平台引擎
├── ToolExecutor           — 工具执行器（窗口级，不共享）
├── AgentManager (单例)    — 智能体管理（跨窗口共享）
├── MemoryManagerCore (单例) — 记忆管理
├── HookManager            — Hook 事件系统
├── SubAgentManager        — 子智能体管理
├── LspManager (单例)      — LSP 管理
├── PluginManager (单例)   — 插件管理
├── SessionStore (单例)    — SQLite 持久化
└── HistoryManager (单例)  — 历史管理
```

**关键信号**（后端 → 前端）:
| 信号 | 说明 |
|------|------|
| `session_created(str)` | 新会话创建 |
| `message_received(dict)` | 新消息到达 |
| `stream_started()` / `stream_chunk(str)` / `stream_finished(dict)` | 流式输出 |
| `tool_call_started(id, name, args)` | 工具调用开始 |
| `tool_result_received(id, name, result, success)` | 工具执行完成 |
| `permission_requested(id, name, args)` | 请求用户授权 |
| `error_occurred(str)` | 错误通知 |
| `context_updated(token_count, limit)` | Token 用量更新 |

---

## 三、目录速查

### 3.1 app/ 源码目录

| 路径 | 说明 | 关键文件 |
|------|------|---------|
| `app/core/` | 核心业务逻辑 | backend.py, agent.py, tool_executor.py, plugin_manager.py |
| `app/core/conversation/` | 对话执行层 | core.py, executor.py |
| `app/core/engines/` | 引擎层（UI/Gateway/AutoLoop） | base.py, ui/engine.py |
| `app/core/workers/` | 工作线程 | chat_worker.py (2988行), subagent_worker.py |
| `app/core/lsp/` | LSP 集成 | lsp_manager.py (719行) |
| `app/core/store/` | 数据持久化 | session_store.py |
| `app/tools/` | 35+ 工具实现 | __init__.py (1208行), file_tools.py |
| `app/widgets/` | UI 组件 | message_card.py (6654行), bottom_input_area.py |
| `app/widgets/cards/` | 卡片组件 | card_container.py, card_manager.py |
| `app/gateway/` | 跨平台消息网关 | manager.py (490行) |
| `app/utils/` | 工具函数 | config.py, theme_manager.py |

### 3.2 plugins/ 插件目录

| 路径 | 说明 |
|------|------|
| `plugins/system/` | 系统内置插件（打包在 exe 中） |
| `plugins/system/agents/` | 10 个内置 Agent 定义（build/explore/plan/review 等） |
| `plugins/system/commands/` | 22 个系统命令（/new, /compact, /debug 等） |
| `plugins/system/skills/` | 25+ 内置技能（brainstorming, tdd, drifox-dev 等） |
| `plugins/system/themes/` | 11 个主题 |
| `plugins/system/hooks/` | Hook 配置 |
| `.drifox/plugins/` | 用户安装的第三方插件（33+ 个） |

### 3.3 关键文件大小参考

| 文件 | 行数 | 说明 |
|------|------|------|
| `app/main_widget.py` | ~12,265 | 主窗口（注意：大文件，修改需谨慎） |
| `app/core/workers/chat_worker.py` | ~2,988 | 核心对话循环 |
| `app/core/backend.py` | ~1,996 | 统一后端接口 |
| `app/core/hook_manager.py` | ~1,904 | Hook 事件系统 |
| `app/tools/__init__.py` | ~1,208 | 工具注册+动态派发 |
| `app/core/plugin_manager.py` | ~1,035 | 插件管理器 |

---

## 四、设计模式与约定

### 4.1 关键设计模式

| 模式 | 应用场景 | 说明 |
|------|---------|------|
| **单例模式** | AgentManager, MemoryManagerCore, LspManager, PluginManager, Settings | `_instance = None` + `__new__` 或 classmethod `get_instance()` |
| **动态派发** | BuiltinTools | 通过 `__getattr__` 遍历工具模块，无需手动委托 |
| **观察者模式** | PyQt Signal/Slot | 后端 emit → 前端 slot，自动跨线程（QueuedConnection） |
| **策略模式** | PermissionResolver | 三种策略：allow/ask/deny |
| **适配器模式** | GatewayAdapter | 统一接口适配不同平台（钉钉/Telegram等） |
| **两阶段停止** | ConversationExecutor | `cancel_worker()`（非阻塞）→ `finalize_stop()`（阻塞收集） |
| **事件总线** | WorkerEventBus | 统一分发 Worker 事件，替代直接 Signal 连接 |

### 4.2 多窗口隔离策略

- **窗口级组件**（ChatBackend, ToolExecutor）：每个窗口独立实例，不跨窗口共享
- **全局单例**（AgentManager, MemoryManagerCore, LspManager）：共享只读数据
- **工作目录隔离**：每个 ToolExecutor 有独立的 workdir

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

- 所有后端 → 前端的通信通过 **Qt Signal**（继承 QObject）
- 后台线程发射信号 → 主线程槽函数执行（自动 QueuedConnection）
- 跨线程安全：敏感资源用 `_stop_lock` 保护

---

## 五、编码规范

### 5.1 命名约定
- **文档/注释/日志**：使用中文
- **代码符号**：统一英文，语义直白
- **文件名**：小写加下划线（遵循现有风格，如 `chat_session.py`, `hook_manager.py`）

### 5.2 质量标准
- **格式化**：使用项目已有的格式化工具（ruff/black）
- **设计品味**：优先消除分支与重复，函数单一职责且短小
- **禁止**：顺手重构/大范围改动（除非任务明确要求）
- **Lint**：运行 `ruff check` 或 `mypy`（如果项目已配置）

### 5.3 提交规范
```
feat|fix|docs|chore|refactor|test: scope - summary

示例:
- feat: agent - 新增 subagent DAG 编排支持
- fix: message_card - 修复图片渲染缓存问题
- refactor: tool_executor - 提取 _initialize_builtin_tools 方法
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
- `primary`/`all` → 可作为主智能体（显示在 UI 智能体选择器）
- `subagent`/`all` → 可作为子智能体（被 subagent_para/subagent_dag 调度）
- `mode=None` → 隐藏，不在 UI 显示

### 6.2 Hook 系统 (`app/core/hook_manager.py`)

| 事件 | 触发时机 |
|------|---------|
| `SessionStart` | 新会话启动时 |
| `PreUserMessage` | 用户消息发送前 |
| `PostUserMessage` | 用户消息发送后 |
| `PreAssistantMessage` | AI 回复前 |
| `PreToolUse` | 工具执行前（可 BLOCK） |
| `PostToolUse` | 工具执行后 |

**Hook 类型**: command / http / python function
**决策控制**: exit code 2 或 JSON `{"decision": "block"}` 可阻断

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
    "description": "插件描述",
    "version": "1.0.0",
    "components": {
        "commands": true,
        "agents": true,
        "skills": true,
        "themes": true,
        "hooks": true,
        "mcp": true,
        "lsp": true
    }
}
```

---

## 七、开发工作流

### 7.1 标准开发流程

当你收到一个 DriFox 开发任务时，按以下步骤执行：

```
Step 1: 理解需求并确认范围
  ├── 阅读任务描述，明确目标
  ├── 用 grep/glob 定位相关文件
  └── 如有疑问，使用 brainstorming 技能澄清

Step 2: 探索相关代码
  ├── 阅读涉及模块的入口文件和关键类
  ├── 理解现有架构和设计模式
  └── 确认要修改的文件范围

Step 3: 制定修改计划
  ├── 明确改动点（哪些文件、怎么改）
  └── 如涉及架构变更，先写设计文档

Step 4: 执行修改
  ├── 遵循项目现有编码风格
  ├── 只改需要改的，不顺手重构
  ├── 保持信令一致性（信号→槽 连接链）
  └── 确保多窗口隔离策略不被破坏

Step 5: 验证
  ├── 代码可以运行（无 ImportError / SyntaxError）
  ├── 逻辑正确（功能测试）
  ├── 检查 Lint（ruff check）
  └── 更新相关文档

Step 6: 提交
  └── git add -A && git commit -m "feat|fix|docs|chore: scope - summary"
```

### 7.2 UI 组件开发要点

| 要点 | 说明 |
|------|------|
| 继承 QObject | 所有需要信号/槽的类必须继承 QObject |
| 信号定义 | 后端定义信号 → 前端在 `setup_ui` 中连接 |
| 线程安全 | UI 操作必须在主线程；后台用信号通知 UI |
| 卡片系统 | 设置类 UI 使用 `widgets/cards/` 下的卡片容器 |
| 设计令牌 | 使用 `app/utils/design_tokens.py` 定义的颜色/字体 |
| 主题兼容 | 支持亮/暗主题切换，勿硬编码颜色值 |

### 7.3 Agent/Skill 开发要点

**Agent 定义格式**（在 `plugins/system/agents/` 下）:
```markdown
# Agent: xxx

你是 [Name]，[Description]。

## 工具
- [工具列表]

## 系统提示词
[详细指令]
```

**Skill 定义格式**（在 `plugins/system/skills/{skill-name}/SKILL.md` 下）:
```markdown
---
name: skill-name
description: "触发描述，需包含触发条件和行为说明"
---
# 技能标题

[技能指令内容]
```

**插件组件注册**：在 `.drifox-plugin/plugin.json` 的 `components` 中声明。

### 7.4 测试要点

- 修改工具执行逻辑后，测试所有受影响的工具调用
- 修改信号链后，确认前端槽函数能正确收到信号
- 修改多窗口组件后，验证窗口间状态是否隔离
- 修改 Hook 系统后，验证事件触发/阻断逻辑

---

## 八、常见开发场景

### 8.1 新增一个功能

```
1. 明确功能需求
2. 定位要修改/新建的文件
3. 遵循现有模块结构：
   - 如果涉及后端逻辑 → app/core/ 下新增或修改
   - 如果涉及 UI → app/widgets/ 下新增或修改
   - 如果涉及工具 → app/tools/ 下新增或修改
   - 如果是命令 → plugins/system/commands/ 下新增
   - 如果是 Agent/Skill → plugins/system/agents/ 或 skills/ 下新增
4. 注册新组件（如需要）：
   - 命令 → plugins/system/commands/ 目录下创建 .md 文件
   - 智能体 → plugins/system/agents/ 目录下创建 .md 文件
   - 技能 → plugins/system/skills/{name}/SKILL.md
5. 更新文档（README.md / AGENTS.md 等）
```

### 8.2 修复 Bug

```
1. 复现 Bug（理解触发条件）
2. grep 相关关键词定位代码
3. 理解根本原因（不要只修表面）
4. 最小化修改（只改必要的行）
5. 验证修复 + 确认不引入回归
```

### 8.3 新增插件

```
1. 在 `.drifox/plugins/` 下创建插件目录
2. 创建 `.drifox-plugin/plugin.json` 清单文件
3. 按需创建 commands/agents/skills/themes/hooks 子目录
4. 配置 .mcp.json（如需要 MCP Server）
5. 重启或触发插件重扫
```

### 8.4 修改现有 Skill

```
1. 定位 skill 目录: plugins/system/skills/{name}/
2. 修改 SKILL.md
3. 如果变更影响触发条件，更新 description 字段
4. 测试技能是否按预期工作
5. 更新相关文档
```

---

## 九、构建与部署

```bash
# 开发环境
uv sync                          # 安装核心依赖
uv sync --group dev              # 安装开发工具
uv sync --all-groups             # 安装全部

# 运行
python main.py                   # GUI 模式
python cli.py --version          # CLI 模式

# 打包
python build.py                  # PyInstaller 打包

# Lint
ruff check app/                  # 代码检查
ruff format app/ --check         # 格式检查

# 测试（如有）
pytest                           # 运行测试
```

---

## 十、路径使用建议

- **项目根目录**: `D:/work/DriFoxx/`
- **根目录内**：使用相对路径（如 `app/core/backend.py`），节省 Token
- **根目录外**：使用绝对路径
- **关键文档**：`README.md`（项目主页）、`AGENTS.md`（开发规范）

---

## 注意事项

1. **信号链完整性**：修改后端信号时，确保前端有对应的槽函数连接
2. **单例访问**：全局单例（AgentManager, PluginManager 等）通过 get_instance() 获取
3. **线程安全**：后台线程中不要直接操作 UI，使用信号通知主线程
4. **文件备份**：edit/multi_edit 工具在修改前会自动备份到 .drifox/backups/
5. **文档同步**：任何功能/配置变更必须更新相关文档（AGENTS.md 强制要求）
6. **窗口隔离**：不要假设跨窗口共享状态（ToolExecutor 等窗口级组件不共享）
