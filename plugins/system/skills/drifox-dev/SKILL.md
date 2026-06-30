---
name: drifox-dev
description: "DriFox 项目专用开发技能。当你需要在 DriFox 项目中进行任何开发工作时（功能开发、Bug 修复、UI 组件开发、架构分析、插件/Skill/Agent 开发、代码审查、重构优化），必须首先加载本技能。本技能把固定规范放在 references/ 子文件、按需加载，根据任务类型把「新增功能」分派给 brainstorming、把「Bug/回归」分派给 diagnose，并把项目实时状态（git 快照 + GitHub open issues）缓存到 state.json 跨会话复用。即使看起来简单的改动也应该加载此技能——DriFox 架构复杂、模块依赖多，不加载容易违反项目约定。"
---

# DriFox 开发技能（drifox-dev · 有状态 · 渐进式披露）

> **本技能是有状态的**。加载时先读 `state.json` 摘要，再按任务类型分派到 references/ 下的对应文件。
> 目的：减少首屏 token 占用，避免一次性把 480+ 行规范塞进上下文。

---

## 0. 加载流程（开局必做）

```
┌──────────────────────────────────────────────────────────┐
│  Step 1  读 state.json 摘要                              │
│  → python scripts/state_manager.py show --summary        │
│  → 解析输出（焦点 / 偏好 / 坑点 / 决策 / 项目快照 /       │
│             GitHub open issues）                        │
├──────────────────────────────────────────────────────────┤
│  Step 2  按 §1 决策树分派到 references 下的具体文件       │
│  → 只读你这一步真正需要的文件，不要全读                   │
├──────────────────────────────────────────────────────────┤
│  Step 3  模糊项目事实 → 看 state.json.auto_snapshot      │
│  → 想看 GitHub issue → snapshot.github_issues            │
│  → 想看实时行数        → snapshot.key_files_lines         │
├──────────────────────────────────────────────────────────┤
│  Step 4  推进过程中持续更新 state.json（focus / pitfall / │
│          decision / snapshot）                            │
└──────────────────────────────────────────────────────────┘
```

## 1. 任务分派决策树（**强制**，每条任务必须先走这里）

读懂用户意图后，按下表选路径。**默认就高不迭低**：用户任务同时含新增 + 修 Bug，先 brainstorm 再 diagnose。

| 用户说 | 任务类型 | 第一步 | 必读 references |
|--------|---------|-------|-----------------|
| 加功能 / 做新东西 / 设计某某 | **新功能** | `brainstorming` 技能 | `scenarios.md` § 一 |
| 改架构 / 拆分模块 / 重命名 | **新功能** | `brainstorming` 技能 | `scenarios.md` § 五 |
| 不工作 / 报错 / 挂了 / 卡 / 崩溃 / 回归 | **Bug** | `diagnose` 技能 | `scenarios.md` § 二 |
| 性能变慢 / 内存涨 | **Bug** | `diagnose` 技能 | `scenarios.md` § 二 |
| 加 UI / 卡片 / 主题 | UI 改动 | 直读 `dev-ui.md` + `architecture.md` | UI 流程 |
| 加 Agent / 改 Skill / 写插件 | Agent/Skill | 直读 `dev-agent-skill.md` | 组件流程 |
| 加工具 / 调权限 / 改 Hook | 工具层 | 直读 `dev-tools-hooks.md` | 工具流程 |
| 跑测试 / 打包 / 提 PR | 工程化 | 直读 `testing-build.md` | 工程流程 |
| 定位要改的文件 / 看架构 | 查阅 | 直读 `architecture.md` | 全局 |
| 查 state / 改偏好 / 录决策 | 元操作 | 直读 `state-reference.md` | 元操作 |
| 命名 / 格式 / 提交规范 | 编码规范 | 直读 `conventions.md` | 编码规范 |
| 设计模式 / 多窗口 / 信号槽 | 模式 | 直读 `patterns.md` | 模式 |
| 其它（重构 / 优化 / 杂项） | 评估 | 按需加载 references | — |

> ⚠️ **Bug 不走 brainstorming**。`diagnose` 的第一阶段是「构造反馈循环」，脑子里还没想清楚前不要先碰代码。
> ⚠️ **新功能优先 brainstorm**。即使用户说「很简单加个字段」，也走 1 问 → 给 2-3 方案 → 拿到批准这 3 步。

## 2. references/ 文件索引（按需加载）

> **唯一允许全量加载的文件是 SKILL.md 本体**。以下内容仅在决策树指向它们时才读。

| 文件 | 何时读 |
|------|-------|
| `references/architecture.md` | 看架构 / 目录 / 后端信号时 |
| `references/patterns.md` | 设计模式 / 多窗口隔离 / 热更新 / 信号槽时 |
| `references/conventions.md` | 命名 / 风格 / 提交 / AGENTS.md 铁律时 |
| `references/dev-ui.md` | UI 组件开发时 |
| `references/dev-agent-skill.md` | 创建 Agent / 修改 Skill / 新插件时 |
| `references/dev-tools-hooks.md` | 工具 / Hook / 权限系统时 |
| `references/scenarios.md` | 走具体开发场景流程时（强烈推荐先读） |
| `references/testing-build.md` | 测试 / 打包 / 提 PR 时 |
| `references/state-reference.md` | state.json 字段 / CLI 用法时 |

## 3. 核心不变项（固定信息，所有任务都用）

| 项目 | 值 |
|------|-----|
| 项目 | DriFox（飘狐） |
| 语言 / UI / 打包 | Python 3.14+ / PyQt5 + Fluent-Widgets / PyInstaller |
| 包管理 | uv（也支持 pip） |
| 当前分支 | `dev` |
| 仓库 | github.com/martin98-afk/DriFox |
| 工作目录 | `D:/work/DriFox`（项目根用相对路径） |
| 关键文档 | README.md、AGENTS.md |

> 上述信息是固定骨架的一部分；会变化的事实（关键文件行数、最近 commit、未提交变更、GitHub issues）都从 `state.json.auto_snapshot` 读——**绝不手抄，会过期**。

## 4. 与子技能的衔接

```
drifox-dev (本技能)
  ├─ 新功能     → 先 brainstorming
  │                ↓ spec 通过
  │              → writing-plans
  │                ↓ 计划出来
  │              → executing-plans / subagent-driven-development
  │
  ├─ Bug / 回归 → 先 diagnose (6 阶段)
  │                ↓ 定位完成
  │              → tdd (回归测试)
  │                ↓ 修复完成
  │              → code-reviewer
  │
  └─ 元操作     → state-reference.md
```

## 5. 状态保鲜（最后一条）

每一次有意义的步骤后，调用 `scripts/state_manager.py` 更新 state：
- `focus --task "..."` — 明确本轮任务
- `pitfall --module X --symptom "..." --cause "..." --fix "..."` — 踩坑立刻记
- `decision --scope X --decision "..." --rationale "..."` — 架构决策必留痕
- `question [--blocking] --question "..."` — 阻塞当前任务用 `--blocking`，否则默认非阻塞
- 完成时 `focus --clear`

中途需要刷新快照（行数 / commits / GitHub issues）就 `snapshot_project.py`。
