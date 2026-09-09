# drifox-dev（有状态技能 · 渐进式披露）

DriFox 项目开发技能。

- **有状态**：跨会话记住焦点 / 偏好 / 决策 / 坑点 / 项目快照 / GitHub open issues。
- **渐进式披露**：`SKILL.md` 只有骨架 + 任务分派决策树，详细规范按需从 `references/` 加载。
- **坑点驱动**：`known-pitfalls.md` 沉淀真实踩过的坑，按根因模式归类，开工前先扫一眼。

## 目录结构

```
drifox-dev/
├── SKILL.md                       # 入口：加载流程 + 决策树 + 六条硬规则 + 索引
├── README.md                      # 本文件
├── references/
│   ├── architecture.md            # 分层架构 + 目录速查 + 插件内核（2026-09 实勘）
│   ├── rendering-pipeline.md      # 流式渲染 / WebView 池 / 骨架屏 / 高度与滚动锚定
│   ├── perf-playbook.md           # 卡顿定位 / 崩溃诊断 / 线程与 Qt 生命周期
│   ├── known-pitfalls.md          # 已踩坑点提炼（渲染·性能·热重载·打包·状态同步）
│   ├── dev-ui.md                  # 通用 UI / 卡片 / 布局坑 / 主题
│   ├── dev-tools-hooks.md         # 工具注册 / Hook / MCP / 权限
│   ├── dev-agent-skill.md         # Agent / Skill / 插件清单
│   ├── testing-build.md           # 测试 / Lint / 打包 / 发版 / 提 PR
│   ├── conventions.md             # 命名 / 风格 / 提交 / 行尾纪律 / 范围铁律
│   ├── patterns.md                # 注册表 / 多窗口 / 热更新 / 信号槽
│   ├── scenarios.md               # 场景流程（新功能 / 修 Bug / 渲染 / 性能 / 发版）
│   └── state-reference.md         # state.json 字段与 CLI
├── state/
│   └── state.json                 # 持久化状态（运行时生成，坑点上限 50）
└── scripts/
    ├── state_manager.py           # 状态读写（CLI + API）
    └── snapshot_project.py        # 项目快照 + GitHub issues
```

## 任务分派决策树（节选）

| 用户说 | 第一步 | 必读 |
|--------|-------|------|
| 加功能 / 改架构 | `brainstorming` | `scenarios.md` §一 |
| 报错 / 回归 | `diagnose` | `scenarios.md` §二 |
| 卡 / 慢 / 内存涨 | `diagnose` + 建基线 | `perf-playbook.md` |
| 崩溃 / 闪退 / 0xC0000005 | `diagnose` + 取 dmp | `perf-playbook.md` §四 |
| 渲染 / 流式 / 滚动 / 空白卡 | 直读 | `rendering-pipeline.md` |
| 加 UI / 卡片 / 主题 | 直读 | `dev-ui.md` |
| 写插件 / 加工具 | `plugin-creator` | `dev-tools-hooks.md` |
| UI 插件 | `ui-plugin-creator` | — |
| 测试 / 打包 / 发版 | 直读 | `testing-build.md` |
| 似曾相识的坑 | 直读 | `known-pitfalls.md` |

## 快速开始

```bash
cd D:/work/DriFox
S=plugins/system-skills/skills/drifox-dev/scripts

python $S/state_manager.py init                 # 首次
python $S/state_manager.py show --summary       # 加载技能时先看这个
python $S/snapshot_project.py                   # 刷新快照（--no-network 跳过 GitHub）

python $S/state_manager.py focus --task "..." --module <module>
python $S/state_manager.py pitfall --module X --symptom "..." --cause "..." --fix "..."
python $S/state_manager.py decision --scope X --decision "..." --rationale "..."
python $S/state_manager.py question --question "..." [--blocking]
python $S/state_manager.py focus --clear        # 任务结束
```

## 维护约定

- **坑点库上限 50 条**。满了先归档：把已沉淀进 `known-pitfalls.md` 的老条目删掉再写新的。
- **写坑点要写根因**，只写现象等于没写。
- **快照字段（行数 / commit / issues / 版本）一律不手抄**，从 `auto_snapshot` 读；超过一天就重跑 `snapshot_project.py`。
- **references 与代码同步**：架构 / 常量 / 命令变了，顺手改对应 reference，否则技能会骗人。
