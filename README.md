<!-- README.md -->
<p align="center">
  <img width="16%" align="center" src="images/drifoxlogo.png" alt="logo">
</p>
<p align="center">
  <img width="40%" align="center" src="images/drifoxtext.png" alt="logo">
</p>

<div align="center">

![Python](https://img.shields.io/badge/Python-3.14%2B-blue)
![License](https://img.shields.io/github/license/martin98-afk/DriFox)
![Stars](https://img.shields.io/github/stars/martin98-afk/DriFox)
![Downloads](https://img.shields.io/github/downloads/martin98-afk/DriFox/total)
![Last Commit](https://img.shields.io/github/last-commit/martin98-afk/DriFox)
![Version](https://img.shields.io/badge/version-0.4.2-brightgreen)

</div>

<h1 align="center">DriFox 飘狐 v0.4.2 — 轻量化 AI 桌面对话助手</h1>

<p align="center">
  <b>不做大而全的 IDE。</b> 只是一个对话框——随时调出，随意提问，随性分支。
</p>

![软件介绍](images/软件介绍.png)

---

使用中遇到问题欢迎加入技术支持群进行反馈！

![技术支持](images/技术支持.jpg)

---

## 核心特性一览

| 特性 | 说明 |
|------|------|
| 🎯 **极简悬浮界面** | 置顶对话框，随开随用，支持穿透/透明度/锁定 |
| 🔀 **分支会话** | 问题分叉，多窗口并行，待办追踪 |
| 🌲 **Git Worktree** | UI 直接管理 worktree，AI 感知当前分支 |
| 🧠 **长记忆系统** | SQLite 持久化，置信度评分，自动学习偏好 |
| 📊 **上下文压缩** | Token 预算控制，长对话自动摘要，环形图可视化 |
| 📈 **交互式图表** | ````echarts` 代码块渲染 ECharts 图表 |
| ☁️ **词云总结** | `/wordcloud` 一键生成 ECharts 词云 |
| 🧩 **UI 插件系统** | 浮动卡片/内容渲染器/消息工厂，热插拔 |
| 🤖 **团队协作** | Leader 智能体，任务邮件，模板管理 |
| 🧠 **CodeGraph 引擎** | 语义化代码探索（search/explore/callers/callees/impact）|
| 🎨 **动态主题系统** | 21 套主题，浅色/深色，组件级感知 |
| 🤖 **多智能体并行** | 20+ 子智能体，DAG 工作流编排 |
| 🧩 **插件系统** | 33+ 即装即用插件，命令/Agent/Skill/主题/Hook/MCP |
| 🛠️ **40+ 内置工具** | 文件/执行/网络/代码/桌面/团队/MCP |
| 🔌 **多模型** | OpenAI / Claude / DeepSeek / MiniMax / 通义 / Gemini / Groq / OpenCode Zen / OpenCode Go / SiliconFlow / Ollama / 火山方舟 / 百度千帆 / 智谱AI |
| 🌐 **MCP 系统** | Model Context Protocol，扩展工具能力 |
| 🔌 **Hook 系统** | 6 种事件钩子，PreToolUse 可 BLOCK |
| 🧩 **Skill 系统** | 25+ 即用技能，可自行扩展 |
| 🖼️ **图片富媒体** | Markdown/HTML 图片原生渲染 |
| 🐾 **桌宠系统** | PixelPet 桌面宠物，状态动画 |
| 🚀 **自动更新** | 自动检查新版本 |
| 🌐 **models.dev 动态同步** | 启动时从 models.dev 拉取最新模型元数据，24h 缓存，增量合并 |
| 🧩 **模型参数服务商级隔离** | 同名模型在不同服务商下的参数配置互不干扰 |

---

## 快速开始

### 环境要求
- Python 3.14+
- PyQt5 >= 5.15.0

### 安装

```bash
git clone https://github.com/martin98-afk/DriFox.git
cd DriFox
pip install uv
uv sync
# 可选组件组
uv sync --group gateway     # 通讯平台（钉钉/Telegram/Discord/飞书/Slack）
uv sync --group dev         # 开发工具（pytest/ruff/mypy）
uv sync --group build       # 跨平台打包
uv sync --all-groups        # 全部安装
```

### 运行

```bash
python main.py
```

---

## 架构概要

```
┌──────────────────────────────────────────────────────────────┐
│                    DriFox v0.4.2 架构                       │
├──────────────────────────────────────────────────────────────┤
│  UI 层      悬浮窗口 / 消息卡片 / 差异视图 / 输入区         │
│             浮动卡片 / 桌宠 / 系统托盘 / 设置面板            │
│  渲染层     Markdown→HTML / ECharts / 图片 / 代码高亮       │
│  引擎层     对话引擎 / 工具执行 / 插件管理 / DAG 编排        │
│             Agent管理 / Hook管理 / 上下文压缩 / Gateway      │
│             UI引擎 / 团队系统 / CodeGraph / Cron定时任务     │
│  存储层     会话管理 / 记忆系统 / 日志持久化 / SQLite        │
└──────────────────────────────────────────────────────────────┘
```

---

## 功能速览

### 核心交互
- **会话并行** — 多窗口并行，分支会话独立探索，内置待办追踪
- **Git Worktree** — 树状展示所有 worktree，一键切换/新建/删除
- **代码差异对比** — 单栏/双栏视图，语法高亮，单词级高亮，统计摘要
- **上下文压缩** — Token 预算环形图，LLM 摘要，`/compact` 命令
- **穿透模式** — 悬浮窗口可穿透点击，透明度 0-100% 可调
- **托盘管理** — 系统托盘常驻，窗口循环排列模式

### 图表与可视化
- **ECharts 图表** — 折线/柱状/饼图/散点/关系图/雷达图/词云，暗色/浅色主题
- **DAG 工作流图** — 子智能体执行时自动生成力导向节点图，状态颜色编码
- **词云总结** — `/wordcloud [焦点]` 一键提取 15-30 个关键术语
- **上下文用量环形图** — 实时显示 Token 占用比例，堆叠柱状图详情

### UI 插件系统 (v0.3.0+)
插件可渲染三种 UI 组件：**浮动卡片**（独立面板，自动注册 `/命令`）、**内容渲染器**（自定义消息块）、**消息工厂**（自定义 Widget）。支持热插拔、多窗口隔离、上下文注入。内置 4 个 UI 插件：上下文用量统计、项目文件树、Git 仪表盘、_vendor/ 依赖演示。

### 团队协作 (v0.3.3+)
- **Leader 智能体** — 任务拆解、分发、监控、汇总
- **任务邮件系统** — Agent 之间异步收发任务
- **团队模板管理** — `/team` 命令保存/加载/列表/删除模板

### CodeGraph 代码智能引擎 (v0.3.4+)
语义化代码探索工具，5 种模式：`search`（搜索符号）、`explore`（综合探索+调用上下文）、`callers`（谁调用了）、`callees`（调用了谁）、`impact`（变更影响分析）。工作目录变更时自动重初始化。

### 动态主题系统 (v0.3.1/0.3.7+)
21 套主题（含浅色系列：daylight, cloud, pearl 等），组件级主题感知，Pyhments 语法高亮多主题适配，实时切换无需重启。

---

## 智能体系统

三层设计：**Primary / Subagent / Hidden**，Markdown+YAML frontmatter 定义。

| 类型 | 典型 Agent |
|------|-----------|
| **Primary** | plan（规划）、build（编码）、code-reviewer（审查）|
| **Subagent** | explore、leader（团队领导）、architecture-critic、security-auditor、test-engineer、legacy-analyst、code-simplifier、deep-research 等 15+ |
| **Hidden** | summary、compaction、title、auto_loop |

支持 DAG 工作流编排、并行分发（subagent_para）、级联执行（subagent_dag）。

---

## 插件系统

```
plugins/system/          # 系统内置插件（打包在 exe 中）
.drifox/plugins/         # 用户安装的第三方插件
```

**插件组件类型**：commands/（命令）、agents/（智能体）、skills/（技能）、themes/（主题）、hooks/（Hook）、.mcp.json（MCP 配置）、ui/（UI 组件）

**21 主题**：amber, azure, bordeaux, cloud, crema, daylight, fallout, forest, graphite, jade, laven, lumia, meadow, midnight, minta, obsidian, ocean, pearl, rosee, sakura, slate

**Skills (25+)**：brainstorming, tdd, diagnose, caveman, ui-plugin-creator, agent-canvas-designer, github-ops, session-summary, subagent-driven-development, grill-me, writing-plans, git-commit, find-skills, triage, zoom-out 等

**新命令 (since v0.2.6)**：`/team`、`/theme`、`/title-gen`、`/toggle-window`、`/clear`、`/subagents`、`/webresearch`、`/remember`、`/todos`、`/lsp-install`、`/verify`、`/release`、`/receive-review`、`/worktree`

---

## 工具系统（40+）

| 类别 | 工具 |
|------|------|
| **文件** | read, write, edit, multi_edit, grep, glob, list, upload_file |
| **执行** | bash, bg_start/stop/logs/list |
| **网络** | webfetch, websearch |
| **代码** | get_diagnostics, lsp, codegraph_explore |
| **桌面** | screenshot, mouse, keyboard |
| **子智能体** | subagent_para, subagent_dag, subagent_status |
| **团队** | team_list_members, team_send_message |
| **MCP** | `mcp__server__tool`（连接 MCP Server 后自动出现）|
| **记忆** | memory_save/search/list |
| **其他** | scan_repo, stage_files, question, todowrite/read |

---

## Hotkeys & Commands

| 快捷键 | 功能 |
|--------|------|
| `ALT+Z` | 全局热键唤起/隐藏主窗口 |
| 命令 `/command` | 所有系统/插件命令统一通过 `/` 触发 |

---

## 更新日志 (v0.3.x 亮点)

| 版本 | 亮点 |
|------|------|
| **v0.3.11** | models.dev 动态模型同步, OpenCode Go 独立服务商, 模型参数服务商级隔离 |
| **v0.3.10** | 搜索引引擎修复 (Tavily+TinyFish), toggle-window 修复 |
| **v0.3.9** | 消息指纹, toggle-window/clear 命令, 浮动tooltip独立窗口, 平滑动画 |
| **v0.3.8** | 性能优化, 灾难性回溯修复, JSON 序列化兼容性 |
| **v0.3.7** | 浅色主题, 21套主题全面升级, 组件级主题感知 |
| **v0.3.6** | 上下文用量统计增强, Hook Token追踪, 跨组件Token同步 |
| **v0.3.5** | 大量bug修复, 流式预览增强, CodeGraph同步优化 |
| **v0.3.4** | **CodeGraph 代码智能引擎**, Diff双栏视图, 团队窗口管理 |
| **v0.3.3** | **团队协作系统**, Leader智能体, 任务邮件, `/team` 模板管理 |
| **v0.3.2** | 共享线程池, 子智能体会话对话框, Hook持久化覆盖, 来源项目追踪 |
| **v0.3.1** | 动态主题系统, `/title-gen` 命令, file-tree拖拽移动 |
| **v0.3.0** | **🎉 UI 插件系统** — 浮动卡片/内容渲染器/消息工厂, 4个内置UI插件 |

---

## 技术栈

| 技术 | 用途 |
|------|------|
| PyQt5 + PyQt-Fluent-Widgets | GUI 框架 |
| PyQtWebEngine | HTML/Markdown/ECharts/图片渲染 |
| ECharts 5 | 交互式图表 |
| SQLite | 持久化存储 |
| OpenAI Python Client | LLM API 调用 |
| loguru | 日志 |
| mcp | Model Context Protocol |
| uv | 包管理/构建 |
| codegraph-py | 代码智能引擎 |
| fastapi + uvicorn | 本地 HTTP 服务 |

---

## 许可证

MIT License © 2025~2026 Martin98-afk

---

## Star History

<a href="https://www.star-history.com/?repos=martin98-afk%2FDriFox&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=martin98-afk/DriFox&type=date&theme=dark&legend=top-left&sealed_token=Se21DjuSbRR0D9a6EvXrplFATV6QBNk-YU2uGpRZaW_Qf5xJfOsN0A8lprq00EqRCMkNc9hSX83_zJuusGdtXYHRJIUhgR1NNAC157hyXBLe5vpQyMemSCNjB1Dorw7xAWq6374BlNcUVotaj0ItMABro5wictN5Bp8GA2d5oK0o_gzmDUXumfjQfnsp" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=martin98-afk/DriFox&type=date&legend=top-left&sealed_token=Se21DjuSbRR0D9a6EvXrplFATV6QBNk-YU2uGpRZaW_Qf5xJfOsN0A8lprq00EqRCMkNc9hSX83_zJuusGdtXYHRJIUhgR1NNAC157hyXBLe5vpQyMemSCNjB1Dorw7xAWq6374BlNcUVotaj0ItMABro5wictN5Bp8GA2d5oK0o_gzmDUXumfjQfnsp" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=martin98-afk/DriFox&type=date&legend=top-left&sealed_token=Se21DjuSbRR0D9a6EvXrplFATV6QBNk-YU2uGpRZaW_Qf5xJfOsN0A8lprq00EqRCMkNc9hSX83_zJuusGdtXYHRJIUhgR1NNAC157hyXBLe5vpQyMemSCNjB1Dorw7xAWq6374BlNcUVotaj0ItMABro5wictN5Bp8GA2d5oK0o_gzmDUXumfjQfnsp" />
 </picture>
</a>