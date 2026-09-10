---
name: ui-plugin-creator
description: "DriFox UI 插件开发技能。用于创建、修改、调试 UI 插件（浮动卡片 / 内容块渲染器 / 消息元素工厂 / 欢迎卡片插件 tab）。非 UI 组件、主程序改动、插件发布不适用本技能。"
license: MIT
compatibility: Requires DriFox UI plugin extension points (register_ui 契约); Python 3.10+
allowed-tools: Read, Glob, Grep, Write, Edit, Bash(python:*), question
---

# ui-plugin-creator —— DriFox UI 插件开发技能

> 快速、规范地把用户意图转化为可工作的 UI 插件代码。本文件是路由面，细节按需加载 `references/`。

## 0. 何时用我 / 何时交回对方

| 需求 | 归属 |
|------|------|
| 插件内 UI 载体（浮动卡/渲染器/工厂/欢迎 tab/输入按钮/工作台页） | **本技能** |
| 完整 UI 插件的骨架、manifest、发布 | plugin-creator（本包只管 UI 载体） |
| 纯工具 / hook / provider / 命令等非 UI 组件 | plugin-creator |
| 改 app/ 主程序内的 widget | drifox-dev |
| 写 skill 本身 | skill-creator |

## 1. 前置链（新建必走，不可跳）

**新建 UI 插件的硬顺序**：

```
brainstorming（需求边界） → frontend-design（视觉稿） → 本技能（代码落地）
```

> 🛑 即使"很简单的卡片"也不跳过——需求理解偏差是返工主因。

**例外**：修改现有插件（调样式/加按钮/换文案）免前置，直接读 `references/modifying.md`。

**frontend-design 未安装降级**：skill 加载失败时告知用户二选一：

a) 安装：本机曾装于 `~/.drifox/plugins/frontend-design/`，若已被移入 `plugins-disabled` 可直接恢复；
b) 跳过设计稿直接实现，但动手前必须用 question 确认三要素：**布局结构、配色方案、交互流程**。禁止无确认直接编码。

## 2. 触发与第一动作（组件类型决策树）

| 用户说 | 组件类型 | 必读 references |
|--------|---------|-----------------|
| "加个卡片""设置界面""统计面板""管理界面" | **浮动卡片** | `templates-cards.md` |
| "加个图表""柱状图""折线图""水平条形图" | **图表控件** | `widgets-charts.md` |
| "统计卡片""数字指标""KPI 卡" | **统计卡片** | `widgets-statcard.md` |
| "读 SQLite""查 14 天数据""字段 fallback" | **SQLite 读取** | `widgets-sqlite.md` |
| "主题色跟着变""深浅色适配" | **主题色映射** | `widgets-theme.md` |
| "聊天里显示 HTML""渲染自定义内容""消息卡片样式" | **内容块渲染器** | `templates-renderers.md` |
| "欢迎卡片加 tab""会话初始卡片" | **欢迎卡片 tab** | `templates-welcome-tab.md` |
| "欢迎卡片加 echarts/统计趋势 tab" | **欢迎 tab + echarts** | `templates-welcome-tab.md`（§8.5） |
| "替换消息气泡""自定义消息控件" | **消息元素工厂** | `templates-renderers.md` |
| "输入框加按钮""截图/快捷发图按钮" | **输入框按钮** | `templates-entries.md`（全屏窗口见 `patterns.md` §10） |
| "标题栏加常驻 tab""顶部 tab 入口" | **标题栏常驻 tab** | `templates-entries.md` |
| "右侧加个页""工作台加 tab""常驻内容页" | **右侧工作台页** | `templates-workbench.md` |
| "做个插件市场""安装/管理插件" | **完整插件** | `templates-plugins.md` + `architecture.md` |
| "插件要 requests/PIL/... 第三方包" | **外部依赖（_vendor/）** | `templates-plugins.md`（§五） |
| "改现有插件""加个按钮""调样式" | **修改现有插件** | `modifying.md` |

> ⚠️ 新插件优先浮动卡片（最常见形态）；图表/统计是卡片内组件，从 `widgets-*.md` 复用。
> ⚠️ 内容渲染器只做"展示"，交互按钮用 data 属性桥接。
> ⚠️ 消息工厂是高级用法——99% 场景用浮动卡片就够（见 §4 硬停止 6）。

## 3. 渐进加载表

| 阶段 | 读取 | 何时使用 |
|------|------|---------|
| 0 需求澄清 | brainstorming | 新建插件前 |
| 0 UI 设计 | frontend-design | 设计稿产出（未装见 §1 降级） |
| 1 组件选型 | 本文件 §2 决策树 | 任务进入时 |
| 1 架构认知 | architecture.md | 扩展点全景 |
| 2 开发流程 | workflow.md | scaffold→迭代→验证 |
| 3 卡片骨架 | templates-cards.md | 浮动卡片 |
| 3 卡片快起 | assets/card_template.py | 单文件骨架直接复制 |
| 3 核心模式 | patterns.md | 上下文/比例高度/异步/热重载/信号链/_vendor/全屏覆盖窗 |
| 3 渲染器 | templates-renderers.md | 内容渲染器/消息工厂 |
| 3 欢迎 tab | templates-welcome-tab.md | 欢迎 tab/echarts |
| 3 入口动作 | templates-entries.md | 输入框按钮/标题栏 tab |
| 3 工作台页 | templates-workbench.md | 右侧工作台页 |
| 3 插件级骨架 | templates-plugins.md | register_ui/plugin.json/_vendor |
| 4 控件选型 | widgets.md | 控件索引与设计原则 |
| 4 控件细节 | widgets-statcard.md 等 5 件按需 | 统计卡/图表/工具函数/SQLite/主题 |
| 4 主题适配 | widgets-theme.md | ctx→QColor |
| 4 数据读取 | widgets-sqlite.md | 路径兜底/N 天窗口/fallback |
| 5 改现有插件 | modifying.md | 小修改免前置 |
| 6 排坑 | pitfalls.md | 编码级踩坑（症状→原因→修法） |
| 7 验证 | checklist.md | 14 大类清单 |
| 7 打包验证 | testing-vendor.md | _vendor PyInstaller |

## 4. 硬停止（触达即停）

1. 无 UI 载体（纯工具/hook/provider/命令）→ 停，转 plugin-creator
2. 新建插件未过 brainstorming → 停，先补前置
3. frontend-design 未安装且用户未选降级 → 停，先走 §1 降级确认
4. 改主程序 app/ 内 widget → 停，转 drifox-dev
5. "加个面板/卡片"载体不明 → 停，question 问清再动
6. 消息工厂（99% 场景用浮动卡片）→ 提示确认后才能用

## 5. 验证

```bash
# 技能包结构自检（本包校验，两包通用脚本）
python ../plugin-creator/scripts/check_skill_package.py .
```

运行时验证（主题色实测 / 性能 / 跨环境 / 打包）按 `references/checklist.md` 执行；静态可判项已由脚本覆盖，人工只做运行时部分。

## 6. 闭环

- 新踩的坑 → 写回 `references/pitfalls.md`（症状→原因→修法）；插件级流程坑 → `modifying.md`
- 技能改进 → writeback / 明确 none-with-reason
- 收尾前跑 §5 自检留证据

## 附：技能衔接

```
ui-plugin-creator（本技能）
├─ 🟡 新建插件 → 先 brainstorming，再 frontend-design，本技能负责代码落地
├─ 遵循 drifox-dev 编码规范 → drifox-dev/references/conventions.md
├─ 复杂功能拆多步 → subagent-driven-development
└─ 调试 bug → diagnose
```
