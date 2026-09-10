---
name: plugin-creator
description: "DriFox 插件全生命周期开发技能。涵盖全部 11 类组件（commands/agents/skills/hooks/mcp/lsp/themes/ui/tools/providers/team_templates），从脚手架生成 → 本地开发/调试 → 验证 → 发布到 drifox-plugins 官方市场的完整流程。UI 组件开发桥接 ui-plugin-creator 技能。主程序改动、一次性脚本、UI 卡片载体开发不适用本技能。"
license: MIT
compatibility: Requires DriFox plugin system (目录即插件, register(registry) 契约); Python 3.10+
allowed-tools: Read, Glob, Grep, Write, Edit, Bash(python:*), question
---

# plugin-creator — DriFox 插件开发技能

> 从需求到发布，构建可安装的 DriFox 插件。本文件是路由面：按需加载 `references/`，不在本文件找实现细节。

## 0. 何时用我 / 何时交回对方

| 需求 | 归属 |
|------|------|
| 可安装插件（11 类组件 / manifest / 发布） | **本技能** |
| 插件内 UI 载体（浮动卡/渲染器/欢迎 tab） | ui-plugin-creator |
| 完整 UI 插件（如插件市场） | **本技能主导**骨架+manifest+发布，UI 载体桥接 ui-plugin-creator |
| 改 app/ 主程序（含 plugins/system-* 内置） | drifox-dev |
| 主程序 bug/崩溃/性能 | drifox-dev + diagnose |
| 学习单组件概念，不产出安装物 | plugin-dev 分项技能 |
| 写 skill 本身 | skill-creator |

> plugin-dev 让位条款：用户在学习单个组件概念 → plugin-dev；一旦要产出可安装插件 → 本技能。

## 1. 触发与第一动作

**触发词表**（详解决策见 references/components.md 对应组件章节）：

| 你说 | 任务类型 | 去向 |
|------|---------|------|
| "做个新插件""创建插件" | 新建 | 第一动作=新建 → references/workflow.md Scaffold |
| "加个 /xx 命令" | Commands | references/components.md §Commands |
| "做个 @xx 智能体" | Agents | references/components.md §Agents |
| "做个技能""写 SKILL.md" | Skills | references/components.md §Skills |
| "加个钩子""事件驱动" | Hooks | references/components.md §Hooks |
| "配置 MCP 服务器" | MCP | references/components.md §MCP |
| "配置 LSP 语言服务器" | LSP | references/components.md §LSP |
| "做个主题""改配色" | Themes | references/components.md §Themes |
| "做 UI 卡片""浮动卡" | UI | → 调用 ui-plugin-creator 技能 |
| "加个工具""做个 AI 工具" | Tools | references/components.md §Tools |
| "加个服务商""接新模型厂商" | Providers | references/components.md §Providers |
| "做团队模板""预设 @角色组合" | Team Templates | references/components.md §Team Templates |
| "改 plugin.json""设置页配置" | Manifest | references/manifest.md |
| "验证""跑测试" | 验证 | references/testing.md |
| "发布到市场""提 PR" | 发布 | references/publishing.md |
| "不工作""报错""不加载" | 除错 | references/troubleshooting.md |
| "改现有插件" | 修改 | 跳过 Scaffold，直改组件 + 更新 version |

**第一动作三问分流**：新建？修改现有？发布？→ 新建走 workflow.md Scaffold；修改定位目标插件后直改；发布走 publishing.md。

## 2. 渐进加载表

| 阶段 | 读取 | 何时使用 |
|------|------|---------|
| 路由决策 | 本 SKILL.md（只读这一个） | 任务进入时 |
| 脚手架与流程 | references/workflow.md | 新建插件、迭代循环、版本策略（SemVer） |
| 组件实现 | references/components.md | 开发任一组件（模板+约束+真实案例） |
| manifest 与配置 | references/manifest.md | 新建/修改 plugin.json、config_schema/E1 契约 |
| 测试验证 | references/testing.md | 热更新测试、validate_plugins.py、除错 |
| 发布 | references/publishing.md | Fork→PR 上架官方市场 |
| 排障 | references/troubleshooting.md | 报错、不加载、CI 失败 |
| 参考成品 | examples/ 目录 | 工具插件、config_schema、浮动卡、欢迎 tab 最小骨架 |

## 3. 插件解剖速览

插件位于 `~/.drifox/plugins/<name>/`，manifest 固定在 `<name>/.drifox-plugin/plugin.json`：

```
your-plugin/
├── .drifox-plugin/plugin.json   ← manifest（必需，插件身份证）
├── commands/*.md                ← 斜杠命令
├── agents/*.md                  ← @智能体
├── skills/<name>/SKILL.md       ← AI 技能
├── hooks/hooks.json + *.py      ← 事件钩子
├── themes/<name>/*.yaml         ← 配色
├── ui/__init__.py + *.py        ← UI 组件（register_ui）
├── tools/*.py + icons/          ← 工具（register(registry)）
├── providers/*.py + icons/      ← 服务商（register(registry)）
├── team_templates/*.yaml        ← 团队模板
├── .mcp.json / .lsp.json        ← MCP / LSP（插件根）
└── README.md / __init__.py      ← 说明 / 包标记（可选）
```

11 类组件速查（字段细节、代码模板 → references/components.md）：

| 组件 | manifest flag | 触发方式 |
|------|--------------|---------|
| Commands | `commands: true` | 用户输入 `/xxx` |
| Agents | `agents: true` | 用户输入 `@xxx` |
| Skills | `skills: true` | AI 自动匹配 description |
| Hooks | `hooks: true` | DriFox 事件触发 |
| MCP | `mcp: true` | DriFox 启动注入 |
| LSP | `lsp: true` | DriFox 启动注入 |
| Themes | `themes: true` | 用户 `/theme xx` |
| UI | `ui: true` | 启动加载 + `/<card_id>` 命令 |
| Tools | `tools: true` | AI 工具调用 |
| Providers | `providers: true` | 用户选择模型/服务商 |
| Team Templates | `team_templates: true` | `/team --load=<name>` |

## 4. 硬停止（触达即停，不得绕行）

1. 要改 `app/` 主程序 → 停，转交 drifox-dev
2. 一次性任务、无插件形态 → 停，not-a-skill，直接实现
3. UI 载体开发（卡片/渲染器内部实现）→ 停，路由 ui-plugin-creator
4. drifox-dev 未加载 → 停，先加载再继续
5. 新建/修改对象不明 → 停，用 question 问清再动手
6. `validate_plugins.py` 未通过 → 禁止提 PR

## 5. 验证

```bash
# 本包自检（结构/引用/evals 完整性）
python scripts/check_skill_package.py .

# 插件完整验证（在 drifox-plugins clone 中，发布前必做）
python tools/validate_plugins.py && python tools/generate_marketplace.py
```

细节（热更新延迟表、检查项清单、除错流程）→ references/testing.md。

## 6. 闭环

- 新踩的坑 → 写回 `references/troubleshooting.md`（症状→原因→修法）
- 技能改进 → writeback / 明确 none-with-reason
- 收尾前跑 `python scripts/check_skill_package.py .` 留证据

## 附：技能衔接

```
plugin-creator（本技能）
├─ 🟡 需求不明确 → brainstorming
├─ 🟡 UI 插件     → ui-plugin-creator
├─ 🟡 编码规范     → drifox-dev/references/conventions.md
├─ 🟡 修 Bug      → diagnose
└─ 🟡 复杂任务     → subagent-driven-development
```
