# System Plugin

DriFox 系统内置插件，提供开箱即用的默认能力——包括智能体、命令、技能、主题、Hooks 和 MCP 配置。

## 结构

```
system/
├── .drifox-plugin/
│   └── plugin.json            # 插件元数据
├── .mcp.json                  # MCP 服务器配置
├── agents/                    # 系统智能体（11 个）
├── commands/                  # 系统命令（27 个）
├── skills/                    # 系统技能（25 个）
│   └── SKILLS.md              # 技能索引
├── themes/                    # 主题（21 个）
└── hooks/
    └── hooks.json             # Hook 配置
```

## 组件

### Agents (`agents/`)

系统智能体是 DriFox 内置的预配置 AI 工作角色，每个智能体限定特定任务域和权限范围。通过 Markdown 文件定义，包含角色描述、执行步骤、工具权限和环境变量。

| 智能体 | 描述 |
|--------|------|
（auto_loop agent 已随 autoloop 插件化迁至 plugins/autoloop/agents/）
| `build` | 面向编码实现的构建智能体，负责读取代码、修改文件、运行验证并收敛结果 |
| `code-reviewer` | 完成里程碑后审查代码与规范的代码审查智能体 |
| `compaction` | 对话上下文压缩，消除冗余并保留关键信息 |
| `explore` | 快速代码探索分析智能体（只读），用于深入分析代码库、探索项目结构 |
| `leader` | 智能体团队统筹管理 Leader，负责组队、任务拆解、分发、监控进度和汇总结果 |
| `plan` | 制定实施计划、拆解任务的规划智能体 |
| `review` | 审查更改代码的智能体版本 |
| `summary` | 对话摘要管理智能体 |
| `task-executor` | 统一任务执行引擎，严格按照收到的提示词规范执行各类命令任务 |
| `title` | 对话标题生成智能体 |

### Commands (`commands/`)

系统命令是可通过斜杠调用的快捷指令，用于触发常见工作流。

| 命令 | 描述 |
|------|------|
| `branch` | 新建分支窗口 |
| `clear` | 清空当前会话的所有消息，重新显示欢迎页 |
| `compact` | 手动触发上下文压缩，调用子智能体压缩当前对话摘要 |
| `debug` | 系统化调试流程 |
| `finish-branch` | 完成开发工作分支 |
| `lsp-install` | 一站式安装 LSP 服务依赖并自动创建对应插件 |
| `new` | 新建会话 |
| `new-window` | 新建窗口 |
| `plugin` | 管理插件（安装、列出、卸载、启用/禁用、更新、搜索、查看详情、管理市场） |
| `project-note` | 项目笔记初始化、完善 |
| `quota-setting` | 通过 Playwright 辅助抓取 OpenCode Zen/Go 和火山方舟的套餐用量查询配置 |
| `receive-review` | 处理代码审查反馈 |
| `release` | 自动发布新版本：生成更新日志、打 tag、推送触发 CI |
| `remember` | 将输入内容存入长期记忆 |
| `subagent_dag` | 将复杂任务拆解为 DAG 工作流并自动执行 |
| `subagents` | 管理子智能体任务和默认模型 |
| `team` | 多窗口团队协作管理 |
| `theme` | 生成/切换主题颜色 |
| `title-gen` | 切换标题生成使用的默认模型 |
| `todos` | 手动显示/刷新待办事项卡片 |
| `toggle-sidebar` | 收起/展开左侧边栏（默认快捷键 alt+q，可在快捷键管理卡改绑） |
| `toggle-window` | 一键隐藏/显示所有 DriFox 窗口 |
| `toggle-workbench` | 打开/关闭右侧工作台（默认快捷键 alt+e，可在快捷键管理卡改绑） |
| `verify` | 完成前验证工作 |
| `webresearch` | 深度网络研究，支持快速查询与多跳调研 |
| `wordcloud` | 将当前会话内容快速总结为 ECharts 词云 |
| `worktree` | 设置隔离工作区 |

### Skills (`skills/`)

技能是 DriFox 最核心的扩展机制，分为**模型自动触发**和**用户斜杠调用**两种模式。系统提供了 25+ 个内置技能覆盖常见开发场景。

| 技能 | 类型 | 描述 |
|------|------|------|
| `brainstorming` | 模型触发 | 创意探索和需求设计，在执行创造性工作前使用 |
| `caveman` | 模型触发 | 调试智能体，查找 bug 和问题根因 |
| `diagnose` | 模型触发 | 诊断分析智能体 |
| `tdd` | 模型触发 | 测试驱动开发——先写测试，让测试失败，再实现代码通过测试 |
| `triage` | 模型触发 | 问题分类与分流 |
| `skill-creator` | 模型触发 | 根据参考技能和用户需求自动生成新 Agent 技能 |
| `find-skills` | 用户触发 | 发现和安装 Agent 技能的入口 |
| `executing-plans` | 模型触发 | 执行预定义计划的智能体 |
| `writing-plans` | 模型触发 | 编写开发计划的智能体 |
| `dispatching-parallel-agents` | 模型触发 | 并行分发子智能体任务 |
| `subagent-driven-development` | 模型触发 | 子智能体驱动的开发模式 |
| `agent-canvas-designer` | 模型触发 | Agent Canvas 设计器 |
| `github-ops` | 模型触发 | GitHub 操作集成 |
| `grill-me` | 模型触发 | 提问拷问模式 |
| `grill-with-docs` | 模型触发 | 基于文档的拷问模式 |
| `improve-codebase-architecture` | 模型触发 | 代码架构改进建议 |
| `minimax-image-understanding` | 模型触发 | MiniMax 图片理解能力 |
| `session-summary` | 模型触发 | 会话摘要生成 |
| `setup-matt-pocock-skills` | 模型触发 | 设置特定技能集 |
| `to-issues` | 模型触发 | 内容转为 Issues |
| `to-prd` | 模型触发 | 内容转为 PRD 文档 |
| `using-superpowers` | 模型触发 | 超能力使用指南 |
| `zoom-out` | 模型触发 | 全局视角分析 |

> 详细列表见 [`skills/SKILLS.md`](./skills/SKILLS.md)

### Themes (`themes/`)

系统主题提供配色方案定制能力，共 21 个内置主题：

| 主题 | 风格 |
|------|------|
| `amber` | 琥珀色暖调 |
| `azure` | 天蓝清新 |
| `bordeaux` | 波尔多酒红 |
| `cloud` | 云白浅色 |
| `crema` | 奶油浅色 |
| `daylight` | 日光浅色 |
| `fallout` | 废土风格 |
| `forest` | 森林绿调 |
| `graphite` | 石墨灰调 |
| `jade` | 翡翠绿调 |
| `laven` | 薰衣草紫 |
| `lumia` | 流光溢彩 |
| `meadow` | 草地绿调 |
| `midnight` | 深蓝午夜 |
| `minta` | 薄荷清新 |
| `obsidian` | 黑曜石暗色 |
| `ocean` | 海洋蓝调 |
| `pearl` | 珍珠浅色 |
| `rosee` | 玫瑰粉调 |
| `sakura` | 樱花粉调 |
| `slate` | 板岩灰调 |

通过 `/theme` 命令生成新主题。

主题 YAML 中 `colors` 段还支持 `input_glow_preset` 字段（`subtle` / `breath` / `platinum` / `ember`），用于一键切换输入框聚焦发光的整体风格。详细 token 与参数见 [`commands/theme.md`](./commands/theme.md) 第 4.5 节。

### Hooks (`hooks/`)

Hooks 是事件驱动的扩展点，系统在特定生命周期触发时执行对应逻辑。当前系统内置 Hooks 配置为空，供插件扩展使用。

### MCP (`mcp.json`)

配置外部工具集成。当前系统内置 MCP 配置为空，供插件或用户扩展。

## 使用

- **智能体**：在对话中通过 `@智能体名` 引用，例如 `@build`
- **命令**：通过 `/命令` 斜杠调用，例如 `/init`、`/theme`
- **技能**：模型根据上下文自动触发，或通过 `/技能名` 调用
- **主题**：使用 `/theme <主题名>` 切换，例如 `/theme ocean`

## UI 插件

UI 插件（`ui/components`）通过浮动卡片（floating card）将自定义面板注入主窗口，
例如文件树、插件市场、上下文用量统计等。每个 UI 插件自动注册一个斜杠命令，
用户可通过以下两种方式打开其浮动卡片：

- **左侧边缘入口**：主窗口左侧默认显示 4px 细线，鼠标靠近展开为胶囊，
  点击胶囊弹出菜单，菜单项即所有可用 UI 插件；
- **斜杠命令**：每个 UI 插件自动注册对应命令（卡片 ID 即命令名），
  例如 `/file-tree`、`/plugin-marketplace`、`/context-usage-stats`、
  `/system-cleaner`。


## 扩展

系统插件设计为可扩展的基底。如需添加自定义能力：

1. **自定义命令**：在 `commands/` 下添加 Markdown 文件
2. **自定义技能**：在 `skills/` 下创建子目录并编写 `SKILL.md`
3. **自定义主题**：在 `themes/` 下添加主题配置
4. **自定义 Agent**：在 `agents/` 下添加 Markdown 定义
5. **Hooks/MCP**：编辑 `hooks/hooks.json` 和 `.mcp.json`

## 许可

MIT - 见 [LICENSE](../../LICENSE)
