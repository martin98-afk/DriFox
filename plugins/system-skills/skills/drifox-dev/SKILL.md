---
name: drifox-dev
description: "DriFox 项目专用开发技能（有状态 · 渐进式披露）。在 DriFox 仓库里做任何开发工作前必须首先加载：功能开发、Bug 修复、UI/卡片/渲染管线改动、流式输出、滚动与卡顿调优、崩溃诊断、插件与 Skill/Agent 开发、代码审查、重构优化、测试与打包。技能把固定规范放在 references/ 子文件按需加载，按任务类型分派（新功能→brainstorming，Bug/崩溃/性能→diagnose，插件→plugin-creator/ui-plugin-creator），并把项目实时状态（git 快照 + GitHub open issues + 已踩坑点库）缓存到 state.json 跨会话复用。即使看起来简单的改动也应该加载本技能——DriFox 架构复杂、模块依赖多，不加载容易违反项目约定。"
---

# DriFox 开发技能（drifox-dev · 有状态 · 渐进式披露）

> 本技能**有状态**。加载时先取 state 摘要，再按任务类型分派到 `references/` 下对应文件。
> 目的：首屏只吃骨架，把 1000+ 行规范按需加载。

---

## 0. 加载流程（开局必做，3 步）

```
Step 1  取状态摘要
        python scripts/state_manager.py show --summary
        → 焦点 / 偏好 / 决策 / 坑点 Top5 / 阻塞问题 / 快照(分支·commit·未提交·GitHub issues)
        ⚠️ auto_snapshot.last_updated 为空或超过 1 天 → 先跑
           python scripts/snapshot_project.py   （--no-network 可跳过 GitHub）

Step 2  按 §1 决策树分派，只读这一步真正需要的 references 文件，不要全读

Step 3  推进中持续回写 state（focus / pitfall / decision / question），任务结束 focus --clear
```

`scripts/` 相对路径：`plugins/system-skills/skills/drifox-dev/scripts/`

---

## 1. 任务分派决策树（**强制**）

先判类型再动手。任务同时含新增 + 修 Bug：先 brainstorming 定边界，再 diagnose 定位。
**就高不就低**：拿不准按更重的流程走。

| 用户说 | 类型 | 第一步 | 必读 references |
|--------|------|--------|-----------------|
| 加功能 / 做新东西 / 设计某某 | 新功能 | `brainstorming` | `scenarios.md` §一 |
| 改架构 / 拆分模块 / 重命名 | 新功能 | `brainstorming` | `scenarios.md` §五 |
| 不工作 / 报错 / 挂了 / 回归 | Bug | `diagnose` | `scenarios.md` §二 |
| **卡 / 慢 / 卡死 / 内存涨 / 白屏** | 性能 | `diagnose` + **先建基线** | `perf-playbook.md` |
| **崩溃 / 闪退 / 黑屏 / 0xC0000005** | 崩溃 | `diagnose` + 先取 dmp | `perf-playbook.md` §四 |
| **渲染 / 流式输出 / 滚动跳变 / 空白卡片 / 骨架屏 / WebView** | 渲染 | 直读 | `rendering-pipeline.md` |
| 加 UI / 卡片 / 主题 / 设置项 | UI | 直读 | `dev-ui.md` |
| 写插件 / 加工具 / 加命令 / 加 provider | 插件 | `plugin-creator` 技能 | `dev-tools-hooks.md` |
| UI 插件（浮动卡 / 渲染器 / 消息元素 / 欢迎 tab） | UI 插件 | `ui-plugin-creator` 技能 | — |
| 改 Agent / 改 Skill | 组件 | 直读 | `dev-agent-skill.md` |
| 跑测试 / 打包 / 发版 / 提 PR | 工程化 | 直读 | `testing-build.md` |
| 定位要改的文件 / 看架构 | 查阅 | 直读 | `architecture.md` |
| 命名 / 格式 / 提交 / 行尾 | 规范 | 直读 | `conventions.md` |
| 多窗口 / 信号槽 / 热更新 / 注册表 | 模式 | 直读 | `patterns.md` |
| 似曾相识的坑 / 想看别人踩过什么 | 查阅 | 直读 | `known-pitfalls.md` |
| 查 state / 改偏好 / 录决策 | 元操作 | 直读 | `state-reference.md` |

> ⚠️ **Bug 不走 brainstorming**。`diagnose` 的第一阶段是构造能跑能验证的反馈循环，没复现前不要碰代码。
> ⚠️ **新功能优先 brainstorm**。即使用户说「很简单加个字段」，也走 1 问 → 2-3 方案 → 拿到批准。
> ⚠️ **改渲染/滚动前先读 `known-pitfalls.md`**：这条链路的坑已踩过 20+ 次，重复踩是纯浪费。

---

## 2. 六条硬规则（所有任务都适用，违反必返工）

1. **只改任务范围文件**。不顺手重构相邻代码、不顺手改格式、不顺手删死代码。每行变更要能追溯到请求。
2. **写文件保行尾**。Windows 下裸 `open(p,"w")` 会把 `\n` 转成 `\r\n`，一次误写 = 整文件行尾翻转 = 真实 diff 被淹没。用 `tools/eol_guard.py` 的 `write_text_keep_eol`；提交前 `python tools/eol_guard.py check`。
3. **UI 只在主线程动**。后台线程一律走 Qt signal → 主线程 slot；不要在工作线程直接 `widget.xxx()`。
4. **QObject 生命周期**：跨线程析构会 0xC0000005。线程退出用协作式取消 + `quit()` + `wait()` 真正结束后再丢引用，`deleteLater` 挂在 `finished` 上。
5. **中文输出**：注释 / 文档 / 日志 / 提交 summary 用中文；代码符号英文。
6. **文档同步**：功能 / 配置 / 目录 / 命令变化必须同步 README / CHANGELOG / 相关 docs，否则视为不完整提交。

---

## 3. references/ 索引（按需加载，唯一允许全量读的是本文件）

| 文件 | 何时读 |
|------|--------|
| `architecture.md` | 看架构 / 目录 / 插件化契约 / 定位文件 |
| `rendering-pipeline.md` | 消息渲染、流式、滚动、WebView、骨架屏、图表 |
| `perf-playbook.md` | 卡顿 / 内存 / 崩溃 / 线程与 Qt 生命周期 / 性能基线 |
| `known-pitfalls.md` | 开工前扫一眼同类坑；收工后往里加 |
| `dev-ui.md` | 通用 UI / 卡片 / 主题 / 设置项 |
| `dev-tools-hooks.md` | 工具注册 / Hook / 权限 / MCP |
| `dev-agent-skill.md` | Agent / Skill / 插件清单 |
| `testing-build.md` | 测试 / Lint / 打包 / 发版 / 提 PR |
| `conventions.md` | 命名 / 风格 / 提交 / 行尾 |
| `patterns.md` | 注册表 / 多窗口 / 热更新 / 信号槽 |
| `scenarios.md` | 具体场景流程（新功能 / 修 Bug / 重构 / 发版） |
| `state-reference.md` | state.json 字段与 CLI |

---

## 4. 核心不变项

| 项目 | 值 |
|------|-----|
| 项目 | DriFox（飘狐） · github.com/martin98-afk/DriFox |
| 技术栈 | Python 3.14+ / PyQt5 + PyQt-Fluent-Widgets / QWebEngine |
| 包管理 | uv（`uv sync --all-groups`）；也有 pyproject + pip 兜底 |
| 入口 | `main.py`（GUI）/ `cli.py` |
| 主分支 | `dev`（发布分支 `main`） |
| 工作目录 | `D:/work/DriFox`（根目录内一律相对路径） |
| 关键文档 | `AGENTS.md`、`README.md`、`CHANGELOG.md`、`CONTRIBUTING.md` |

> 会变的事实（关键文件行数、最近 commit、未提交变更、GitHub issues、版本号）**一律从 `state.json.auto_snapshot` 读**，绝不手抄。

**巨型文件预警**（改动前先想清楚，别再往上堆）：
`app/main_widget.py` 21000+ 行、`app/core/workers/chat_worker.py` 5000+ 行、`app/core/hook_manager.py` 3000+ 行、`app/widgets/message_card.py`。往这些文件加东西前先确认没有更合适的落点（`app/widgets/modules/`、`app/core/` 子模块）。

---

## 5. 与子技能衔接

```
drifox-dev（本技能 = 项目约定 + 状态 + 分派）
  ├─ 新功能      → brainstorming → writing-plans → 实现 → code-reviewer
  ├─ Bug/崩溃/性能 → diagnose（6 阶段）→ tdd（回归测试）→ code-reviewer
  ├─ 插件        → plugin-creator（11 类组件）/ ui-plugin-creator（UI 扩展点）
  └─ 大改收尾    → code-reviewer（对照原计划 + 本技能规范）
```

---

## 6. 状态保鲜（每次有意义的步骤后）

```bash
python scripts/state_manager.py focus --task "..." --module <module>
python scripts/state_manager.py pitfall --module X --symptom "..." --cause "..." --fix "..."
python scripts/state_manager.py decision --scope X --decision "..." --rationale "..."
python scripts/state_manager.py question --question "..." [--blocking]
python scripts/state_manager.py focus --clear            # 任务结束
python scripts/snapshot_project.py                       # 刷新快照
```

坑点库 50 条上限，**满了先归档**：把最老的、已沉淀进 `known-pitfalls.md` 的条目删掉再写新的。
