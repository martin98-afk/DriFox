# Changelog
All notable changes to this project will be documented in this file.

## [v0.3.6] - 2026-07-12

自上一版本以来的变更 | 提交数：7 · 文件变更：16 · +620/-154 | 贡献者：dingma

### ✨ 新功能 (New Features)

- **上下文用量统计增强**: 当 API 未返回 usage 时，使用本地上下文计数补全消息 token 用量，确保上下文圆环和消息卡片中的统计保持准确；新增缺失 usage 场景的回归测试
- **历史问题计数提示优化**: 为用户问题数量增加 InfoBadge，并在新建会话和输入用户消息时正确更新可见性；同步优化相关 tooltip 样式与上下文用量展示
- **Hook Token 追踪与上下文用量堆叠图优化**: UIEngine 新增 hook token 追踪，并在上下文用量 Tooltip 的堆叠柱状图末尾显示占比百分比
- **跨组件 Token 显示同步**: UIEngine、ChatWorker 与主窗口三组件的 token 显示统一同步，避免多处 UI 数字不一致并增强上下文处理

### 🐛 问题修复 (Bug Fixes)

- **工具运行折叠框状态修复**: 修复工具调用与工具结果写入不同消息卡片时，运行折叠框偶尔无法转为完成态并持续累积的问题；通过 `tool_call_id` 关联所属卡片，并增加完成态恢复兜底与回归测试

### 🔧 其他 (Chores & Build)

- 更新版本号至 v0.3.6

## [v0.3.5] - 2026-07-11

自上一版本以来的变更 | 提交数：17 · 文件变更：25 · +1199/-744 | 贡献者：dingma, mading

### 🐛 问题修复 (Bug Fixes)

- **网关与聊天工作者修复**: 修复 Stop hook 续命后 full_response 丢失与 llm_config 未定义问题；为 `_prev_stophook_response` 初始化并简化三元运算，code-review 跟进增强稳定性
- **卡片滚轮竞态闪烁修复**: 解决流式输出期间 `_suppressScrollEvent=false` 同步解除但 Chromium scroll 事件异步派发引起的"卡顶部"闪烁问题；在所有 auto-scroll 入口打 `_autoScrollTime = performance.now()` 时间戳，scroll 事件回调增加 50ms 时间窗检查
- **工具流式预览增强**: tool streaming preview 实时显示字符数与文件路径提取；改为自然语言描述提升可读性
- **AGENTS.md 偶发清空修复**: `MemoryManagerCore.save_project_note` 增加空内容/纯空白保护，阻止 UI 编辑器全选删除后 300ms 防抖自动保存意外清空已有内容
- **空 AGENTS.md 恢复处理**: `read_project_notes` hook 增加 0 字节 → 恢复 INITIAL_TEMPLATE 兜底逻辑（纵深防御）
- **上下文构建与历史压缩消息过滤**: 增强 `context_builder` / `history_compactor` 的消息过滤逻辑，修复工具结果显示与消息卡片显示问题
- **终端工具内联脚本转换**: 修复 inline scripts 自动转换的 3 个 bug，提升命令面板与终端工具的可用性
- **UI 响应性优化**: `bottom_input_area` / `command_card` / `message_card` 的响应性与命令卡片参数可见性显著提升
- **CodeGraph 同步与文件监听**: 增强 CodeGraph 文件监听与同步功能，添加冷却保护防止频繁刷新
- **Hook prompt 提示词清理**: 更新 `hooks.json` 的 prompt 文案，使意图更清晰
- **更新检测信号重命名**: `update_checker` 的 `finished` 信号重命名为 `check_finished`，`thread_guard` 添加类型检查防止非 QThread 对象误处理
- **字体与样式微调**: `OpenAIChatToolWindow` 字体族 CSS 与 README 星标历史链接更新

### 🐛 问题修复 (Bug Fixes) — 历史 [Unreleased] 归并

- **AGENTS.md 偶尔被清空修复**: `MemoryManagerCore.save_project_note` 增加空内容/纯空白保护，阻止 UI 编辑器被全选删除后 300ms 防抖自动保存、或其他上游调用方传入空 content 时意外清空已有 AGENTS.md。**API 行为变化**：修复后 `save_project_note("proj", "")` / `save_project_note("proj", "   \n")` / `save_project_note("proj", None)` 一律返回 `False`（而非写入空文件），调用方需注意处理返回值。新增回归测试 `tests/core/test_memory_manager_empty_protection.py`（7 项用例覆盖空字符串/纯空白/None/边界换行/有效内容/无 workdir 场景）。

### ♻️ 代码重构 (Refactoring)

- **代码结构可读性提升**: 优化代码结构以提升可读性与可维护性
- **ui_helpers 格式化与组织**: 统一字符串引号为双引号、清理多余空行、改进函数调用清晰度、增强注释

### 🔧 其他 (Chores & Build)

- **依赖升级**: `pyproject.toml` 升级 `codegraph-py` 依赖至 1.3.4（提升代码语义探索能力）
- 更新版本号至 v0.3.5

## [v0.3.4] - 2026-07-09

自上一版本以来的变更 | 提交数：30 · 文件变更：32 · +2914/-627 | 贡献者：dingma, mading

### ✨ 新功能 (New Features)

- **CodeGraph 代码智能引擎**: 集成语义化代码探索引擎，支持 search/explore/callers/callees/impact 五种探索模式；移除旧 CodeGraph 插件，统一通过系统 hooks 调用并添加代码探索提示
- **Diff 视图全面升级**: 新增单栏/双栏视图模式，支持语法高亮、语言扩展映射；改进 inline diff 语法高亮处理与单词级高亮
- **工作目录管理优化**: 改进工作目录处理逻辑，支持临时工作目录创建；CodeGraph 实例在工作目录变更时自动重初始化
- **命令卡片性能优化**: 跳过容器动画减少弹出延迟；为菜单项添加 tooltip 描述并改进布局
- **团队协作与子智能体增强**: 在 SessionStart hook 中增强团队协作功能；新增子智能体任务停止功能并更新 UI 元素
- **Session 处理增强**: 引入 pending session hook 标记防止消息拦截；改进 stop hook 消息注入逻辑，防止无限循环
- **Hook 管理增强**: 添加 stop reason 匹配支持事件处理；增强会话状态选项
- **工具执行器增强**: 增加 explore/search 模式的默认 max_files/limit 参数；增强工具参数自然语言描述
- **窗口托盘管理**: 实现所选窗口的循环排列模式
- **Hook 设置 UI 增强**: 添加 matcher 标签显示，改进命令文本处理
- **格式化内存上下文增强**: 增强自动 Git 初始化安全检查与日志记录

### 🐛 问题修复 (Bug Fixes)

- **消息卡片折叠动画闪烁修复**: 优化高度过渡处理，防止 collapsible 动画期间布局闪烁
- **Diff 统计信息恢复**: 在可折叠摘要中恢复 diff 统计信息(+N/-M)
- **Diff 渲染布局优化**: 移除 tool-diff-inline 顶部间距，消除折叠头与 diff 内容之间的间隙；切换到先删后增布局
- **单词级高亮恢复**: 在单栏 diff 视图中恢复单词级高亮
- **异常处理统一**: 整合消息卡片和渲染辅助函数中的异常处理
- **CodeGraph 根路径校验**: 改进项目根路径验证，确保工作目录变更时正确重初始化

### ♻️ 代码重构 (Refactoring)

- **Diff 内联渲染精简**: 简化 diff 内联渲染为 bash 风格，移除冗余按钮

### 🔧 其他 (Chores & Build)

- 更新版本号至 v0.3.4

## [v0.3.3] - 2026-07-07

自上一版本以来的变更 | 提交数：15 · 文件变更：27 · +2979/-136 | 贡献者：dingma, mading

### ✨ 新功能 (New Features)

- **团队协作系统**: 新增完整的团队协作功能，包括任务邮件系统（task email system）、Leader 智能体（任务拆解/分发/监控/汇总）、团队成员列表与当前用户标识，支持 Agent 切换时同步团队工具上下文
- **团队模板管理**: 新增 `/team` 命令支持模板的保存/加载/列表/删除操作，支持用户自定义模板，加载时保留邮箱配置，添加确认对话框防止误操作，修复保存计数
- **团队窗口管理优化**: 增加新窗口待排列计数，优化自动排列逻辑，提升多窗口协作体验
- **重复窗口打开性能优化**: 跳过冗余初始化步骤，直接复制配置，显著提升重复窗口的打开速度与响应体验
- **Agent 命令权限处理增强**: 增强 OpenAIChatToolWindow 中 agent 命令的权限处理，改进 SystemCardFrame 初始化可见性
- **占位提示与日期解析增强**: 优化输入框占位提示文案，改进上下文用量统计中的日期解析逻辑

### 🐛 问题修复 (Bug Fixes)

- **参数值选择逻辑修复**: 增强参数值选择逻辑，防止在特定条件下提前退出导致流程中断
- **Leader 权限配置修复**: 更新 Leader 智能体的权限配置与任务拆分配置说明

### ♻️ 代码重构 (Refactoring)

- **团队命令选项重构**: 移除废弃的 send 命令，增强 load/delete 功能，更新相关文档
- **移除废弃 --status 命令**: 从团队管理文档中移除不再使用的 --status 命令

## [v0.3.2] - 2026-07-06

自上一版本以来的变更 | 提交数：23 · 文件变更：58 · +3267/-12012 | 贡献者：dingma, mading

### ✨ 新功能 (New Features)

- **系统 Hook 持久化内容覆盖**: 为系统 Hook 实现持久化内容覆盖机制，支持在运行中动态覆写 Hook 配置
- **多模块导入与缓存优化**: 全局优化模块导入语句并增强缓存机制，显著提升启动速度与运行性能
- **工具执行共享线程池**: 引入共享线程池重构工具执行流程，并缓存模型名称减少重复查询
- **会话持久化优化**: 优化会话持久化与多级缓存机制，减少冗余磁盘写入
- **UI 滚动条统一样式**: 统一滚动条样式并优化下拉框视觉，提升整体 UI 一致性
- **子智能体会话对话框**: 新增子智能体会话对话框，实时展示任务日志与执行摘要
- **子智能体会话对话框拖拽支持**: 为 SubAgentSessionDialog 增加可拖拽的头部与底部区域，提升交互灵活度
- **来源项目追踪**: 实现来源项目追踪机制，防止项目切换时产生会话误分配
- **PostToolUse Hook 内容替换**: 实现工具调用后的内容替换与队列管理 Hook

### 🐛 问题修复 (Bug Fixes)

- **Hook 注入与消息过滤增强**: 改进 Hook 处理和消息过滤逻辑，防止重复注入导致消息翻倍
- **message_card 滚轮 race condition**: 修复流式输出时卡片内部滚轮事件与程序 auto-scroll 之间的竞态条件导致的"偶尔卡顶部"问题——通过 auto-scroll 时间戳 + 50ms 时间窗识别程序触发事件，避免误标记用户主动滚动
- **auto-scroll 用户状态重置**: 改进自动滚动逻辑，在内容更新后正确重置用户滚动状态
- **project_card 分支标签刷新**: 修复绑定根目录后项目卡片分支标签未及时刷新的问题
- **project_card 工作目录缓存同步**: 修复绑定根目录后实例工作目录缓存未同步更新的问题

### ♻️ 代码重构 (Refactoring)

- **Stop Hook 改用 prompt 类型**: 更新 Stop Hook 使用 prompt 类型，移除废弃的 stop_cleanup.py 模块
- **移除 Hook 预设管理**: 从 OpenAIChatToolWindow 中移除 Hook 预设管理功能（命令和 UI 元素），简化架构
- **启动管理器重构**: 重构启动管理器逻辑并改进自启动功能的可靠性
- **插件管理命令路径**: 更新插件管理与标题生成相关命令的名称与路径
- **代码结构可读性提升**: 重构多处代码结构，提升整体可读性与可维护性
- **_on_provider_changed 优化**: 重构 `_on_provider_changed` 处理逻辑，优化配置更新流程

### 🔧 其他 (Chores & Build)

- **移除废弃插件文件**: 移除已废弃的插件市场卡片与命令提示注入 Hook 相关文件

## [v0.3.1] - 2026-07-02

自上一版本以来的变更 | 提交数：22 · 文件变更：57 · +16292/-260 | 贡献者：mading, dingma

### ✨ 新功能 (New Features)

- **动态主题系统**: UI 组件实现字体与颜色动态主题化，支持实时主题切换，插件 UI 与主界面风格保持一致
- **标题生成命令**: 新增 `/title_gen` 命令，支持切换标题生成的默认模型，提升对话管理灵活性
- **会话追踪支持**: 为 ChatWorker 和 SubAgentExecutor 添加 session_id 支持，实现跨会话的精确追踪与状态隔离
- **UI 插件文档增强**: 扩充 UI 插件创建器参考文档，新增字体注入、按钮样式、工具函数等实用指南
- **file-tree 拖拽移动与删除**: 新增节点拖拽移动与 Delete 键删除功能，操作体验大幅提升
- **插件清单文件**: 为 file-tree 与 system-cleaner 插件新增 `plugin.json` 元数据描述
- **SquircleAvatar 插件**: 新增 SquircleAvatar 组件用于插件图标展示
- **QThread 全局安全守卫**: 新增全局 QThread 安全守卫并优化插件加载逻辑，提升多线程稳定性
- **通用工具函数库**: 新增尺寸格式化与颜色调整等实用工具函数
- **话题摘要追踪**: OpenAIChatToolWindow 实现话题摘要生成追踪，避免重复生成

### 🐛 问题修复 (Bug Fixes)

- **子智能体缓存失效修复**: 修复 SubAgentFinished 事件中消息角色从 'system' 误设为 'assistant' 导致缓存异常的严重 bug，确保子智能体任务完成时消息队列状态一致
- **技能缓存机制优化**: 重构 `/skill` 命令执行时的技能缓存策略，显著提升技能加载性能，减少重复缓存失效
- **file-tree 对话框重构**: 用自定义 QDialog 替代 QMessageBox，背景样式通过 `#id` 选择器覆盖全局样式，按钮通过 `findChildren` 直接设置样式
- **file-tree 拖拽支持**: 为树节点添加 `ItemIsDragEnabled` / `ItemIsDropEnabled` flags，新增拖拽 hover 高亮与暗色主题对话框适配
- **QPushButton 导入修复**: 修复因缺少 `QPushButton` import 导致的 NameError
- **多插件综合修复**: 修复多处跨插件 bug 与 UI 改进

### ♻️ 代码重构 (Refactoring)

- **代码结构优化**: 重构代码结构提升可读性与可维护性

## [v0.3.0] - 2026-07-02

自上一版本以来的变更 | 提交数：15 · 文件变更：34 · +6977/-350 | 贡献者：dingma, mading

---

> 🧩 **里程碑：UI 插件系统发布** — 插件从此不仅能扩展 AI 能力，还能直接在 DriFox 界面中渲染自定义 UI 组件。三种组件类型（浮动卡片 / 内容渲染器 / 消息工厂）覆盖从卡片面板到消息渲染的全场景，极大丰富了插件可实现的内容形态。

### ✨ 新功能 (New Features)

#### 🧩 UI 插件系统（核心亮点）

- **UI 插件注册表（UIPluginRegistry）**: 新增单例注册表，标准化管理三种 UI 组件类型——浮动卡片（Floating Card）、内容渲染器（Content Renderer）、消息元素工厂（Message Factory）。插件通过 `ui/__init__.py` 中的 `register_ui(registry)` 函数注册，热插拔即时生效
- **浮动卡片（Floating Card）**: 插件可在聊天区域上方/下方注册独立 QWidget 面板，支持 toggle 显示/隐藏、自动注册 `/命令`、多窗口隔离缓存、上下文注入（项目根目录/会话ID/主题色）—— 实现即装即用的桌面级 UI 体验
- **内容渲染器（Content Renderer）**: 支持自定义消息 `custom` 类型内容块渲染，插件定义 `custom_type` 和渲染函数，消息渲染时自动匹配调用，实现消息级别的个性化展示
- **消息元素工厂（Message Factory）**: 根据消息条件判断返回自定义 QWidget，替代默认卡片渲染，为特殊消息格式提供专属 UI
- **拉模型上下文注入**: 浮动卡片通过 `set_context_provider()` 按需拉取最新上下文，主题色自动映射为图表 QColor 配色，告别硬编码 fallback，支持实时主题切换
- **4 个 UI 浮动卡片插件**: 上下文用量统计（Token 趋势/消息量/会话活跃度图表）、项目文件树（树形浏览/搜索过滤/实时监听）、Git 仪表盘（分支切换/变更清单/提交历史）、_vendor/ 依赖演示（内置打包第三方包）—— 即装即用，立体展示 UI 插件能力
- **UI 插件开发指南**: 新增 `SKILL.md` 和 `architecture.md`，提供三步开发流程（创建 `ui/__init__.py` → 实现 QWidget → 注册浮动卡片）、组件设计模式、构建序列说明，降低开发门槛

#### 🔧 基础能力增强

- **图表悬停交互**: 折线图和柱状图实现鼠标悬停高亮 + Tooltip 实时数据点显示
- **插件热重载增量机制**: 仅重载变更插件而非全量扫描，目录变更过滤减少不必要重载
- **卡片自适应尺寸**: 最小高度归零，支持内容自适应动态扩展
- **插件市场版本检查**: 支持版本比较与更新提示，完善安装流程
- **输入区恢复兜底**: 卡片关闭时强制恢复输入区，防止回调链断裂导致永久隐藏

### 🐛 问题修复 (Bug Fixes)

- 移除 `context-usage-stats` Token 消耗显示中重复的标签文本
- 修复关闭第三方插件后 `command_card` 固定高度未重置导致卡片高度异常
- 改进文件系统目录变更过滤逻辑，防止无关事件触发跨插件重载

### ♻️ 代码重构 (Refactoring)

- 优化工具调用结果顺序修复逻辑，移除已废弃的 Hook 块渲染功能

### 📚 文档 (Documentation)

- 新增 UI 插件完整说明到 README：三种组件类型详解、生命周期、现有 UI 插件一览表、开发指南与图片展示

### 🔧 其他 (Chores & Build)

- 版本号更新至 v0.3.0，同步更新 pyproject.toml / config.py / installer.iss 三处版本文件

## [v0.2.14] - 2026-07-01

自上一版本以来的变更 | 提交数：60 · 文件变更：59 · +16715/-6109 | 贡献者：dingma, mading

---

### ✨ 新功能 (New Features)

- **对话上下文用量统计插件**: 新增 Context Usage Stats 插件，基于 `.drifox/sessions.db` 实时展示最近对话的 Token 用量、消息数量趋势、会话活跃度图表
- **会话管理与上下文跟踪增强**: 优化会话管理逻辑，增强上下文使用追踪与数据持久化能力
- **UI 插件注册系统**: 新增完整的 UI 插件注册表（UIPluginRegistry），支持插件加载/卸载/重载生命周期管理、浮动卡片自动命令注册、消息工厂与内容渲染器注册 API
- **插件管理与市场增强**: 新增插件管理和插件市场功能，完善插件浏览、安装与管理流程
- **插件市场系统插件**: 新增 Plugin Marketplace 系统插件，集成市场插件浏览与管理能力
- **插件管理器 UI 集成**: 插件管理器自动检测 `ui/` 目录注册 UI 组件；启用/禁用插件自动加载/卸载 UI；重新扫描自动加载
- **主窗口插件集成**: 主窗口注入 UI 插件注册表并集成消息工厂 Hook，支持插件自定义消息内容块
- **卡片管理器外部注册**: 支持从 UI 插件外部注册卡片到主卡片管理器
- **Hook 预设系统**: 实现多窗口隔离的 Hook 预设管理与一键切换功能（HookPresetManager + UI）
- **自动上下文压缩**: 在 PostToolUse Hook 中实现自动上下文压缩，优化长对话场景的 Token 消耗，并调整触发策略与 Hook 映射
- **Agent 身份注入 Hook**: 新增 `inject_agent_identity` Hook，支持在系统提示中动态注入 Agent 身份配置
- **Token 用量与模型展示**: SubAgent 任务行增加 Token 用量和模型名称实时显示
- **流式子代理结果注入优化**: 优化流式场景中的子代理结果注入策略，提升用户体验
- **单任务完成状态提示**: 单任务完成时添加 `subagent_status` 提示
- **UI 样式与布局增强**: AgentTaskRow 和详情面板样式布局优化；命令和文件提及卡片查询渲染优化；LLM 设置卡片新增手风琴效果与样式刷新
- **图标自适应缩放**: 图标尺寸随系统字体大小自动缩放适配
- **编码规范与开发文档**: 新增编码规范、开发流程、状态管理和构建测试文档

### 🐛 问题修复 (Bug Fixes)

- **UI 插件注册表修复**: 支持带连字符的插件名称，优化命令命名空间处理逻辑
- **Hook 预设系统修复**: 编辑/新增 hook 后自动保存预设 agent_identity；预设切换同步 agent 到 hook 覆写；默认预设不可删除；弹窗主题和布局修正
- **合并冲突修复**: engine.py 改用 `approx_messages` 解决工具/模型 Token 计数冲突
- **冗余信号移除**: 移除 subagent 流注入中多余的 `_hook_messages_updated` 信号发射
- **Debug 打印清理**: 移除 OpenAIChatWorker 类中残留的 debug print 语句
- **角色标识更新**: 更新角色标识并增强 UI 插件的命令过滤
- **强制关窗运行时持久化修复**: 强制关闭窗口时正确计算并保存累计运行时长（`elapsed`），避免重启后看不到运行时长
- **会话关闭消息丢失修复**: 处理会话关闭时被中断的消息，防止数据丢失
- **子代理组件 F541 lint 修复**: 移除 `sub_agent_compact_widget` 中多余的 f-string 前缀
- **工具 schema 描述精简**: 精简工具 schema 描述文案，提升清晰度与简洁性

### ♻️ 代码重构 (Refactoring)

- **上下文处理与数据库交互重构**: 重构上下文处理流程和数据库交互层，提升数据访问效率与稳定性
- **UI 插件注册表重构**: 重构 UI 插件注册表与市场插件的架构设计
- **render_helpers 可读性改进**: 规范字符串格式化，提升代码可读性
- **Token 跟踪重构**: 重构 OpenAIChatToolWindow 的 Token 用量跟踪与压缩状态处理

## [v0.2.13] - 2026-06-29

自上一版本以来的变更 | 提交数：24 · 文件变更：57 · +4001/-2094 | 贡献者：dingma, martin98-afk

---

### ✨ 新功能 (New Features)

- **Hook 系统全面完善**: 新增 `project_intel`（项目智能）与 `safety_guard`（安全守卫）两个系统级 Hook，提供项目上下文智能理解、安全隐患检测与防护能力
- **Git 状态集成到 Memory Context Hook**: 在 PreUserMessage 内存上下文 Hook 中注入 Git 状态信息，包括文件变更统计、储藏计数、相对时间和临时文件提示
- **Git 子进程 Windows 弹窗修复**: 在 Windows 平台执行 Git 子进程时隐藏命令行窗口，避免黑窗闪烁
- **自动生成 .gitignore 规则**: 根据项目上下文自动检测并附加 .gitignore 规则，提升项目配置体验
- **BuildSystemPrompt Hook 实现与重构**: 新增 BuildSystemPrompt Hook 统一处理系统提示生成与上下文注入；重构项目笔记管理，移除 `project_notes_manager` 模块，改为直接从 `AGENTS.md` 读取项目笔记
- **Hook 系统增强**: 增强 hook 管理，支持系统插件（system plugin）与上下文注入；matcher 占位符根据当前事件类型动态更新
- **实时上下文用量显示**: 新增 `context_updated` 信号，实时推送上下文使用量，前端可即时展示上下文消耗
- **多平台 LSP 诊断与命令执行**: 增强 LSP 诊断信息处理能力，改进跨平台命令执行逻辑
- **项目管理增强**: 项目选择卡片新增"打开项目根目录"快捷功能

### 🐛 问题修复 (Bug Fixes)

- **Hook 注入空输出处理**: 处理 hook 注入时的空输出场景，避免重复触发
- **Skill 工具描述优化**: 更新 Skill 工具描述文案，提升清晰度与简洁性
- **卡片展开布局修复**: 卡片展开时正确触发布局失效与父布局激活，避免视觉残留与层级错乱
- **MCP 编辑卡片响应式**: 调整 MCP 编辑卡片高度与模式，提升响应式表现
- **PixelPet emoji 跨平台字体**: emoji 字体加载逻辑支持多平台，修复部分系统下图标显示问题
- **调试日志清理**: 移除 `OpenAIChatWorker` 与编辑器中残留的 debug print 语句，避免日志噪声

### ♻️ 代码重构 (Refactoring)

- **项目笔记管理重构**: 移除 `project_notes_manager` 模块，统一通过 `AGENTS.md` 读取项目笔记，简化调用链
- **操作约束与项目指南精简**: 精简项目指南文档与运营约束条款，提升可读性与可执行性

## [v0.2.12] - 2026-06-27

自上一版本以来的变更 | 提交数：35 · 文件变更：53 · +5987/-5440 | 贡献者：dingma, mading

---

### ✨ 新功能 (New Features)

- **Hook 系统全面升级**: 架构重构与功能增强
  - 迁移至 Claude Code 兼容 XML 格式，支持 Stop Hook 强制继续
  - 同步触发与输出注入、全局重载与跨窗口同步
  - 线程安全消息队列，PreToolUse/PostToolUse 独立队列确保消息顺序正确
  - 插件路径变量支持、字段级覆盖与持久化、Windows 命令支持、状态消息
  - PreUserMessage 钩子机制：命令提示注入、技能注入、会话元数据处理
  - 会话上下文增强：session_id 传递到 hooks 与工具执行、内存上下文与项目笔记格式化

- **消息与工具调用增强**
  - 客户端侧重复工具调用检测，自动识别并中断工具调用循环，预防 Qwen 服务端 HTTP 400 错误
  - Qwen/DashScope 流式工具调用处理增强 — 修复 `id` 字段在后续 chunk 中清空导致的聚合 key 丢失问题，新增 `_tool_calls_index_to_id` 映射
  - 自然语言工具调用预览（Inline Cards + Collapsible Headers）
  - CommandCard 和 QuestionFloatingWidget 功能增强

- **插件管理增强**: 新增更新、搜索、查看详情命令

- **桌宠系统增强**: 新增"阅读"行为替代"播放"，更新动画/时长/状态，新增阅读图标

### 🐛 问题修复 (Bug Fixes)

- **Qwen/DashScope 流式工具调用永远卡在"接收参数中"**
  - 根因：`ChatWorker._process_response` 用 `tc.id` 作为流式 tool_calls 聚合 key，但 Qwen/DashScope
    OpenAI 兼容协议在 chunk 2+ 会把 `tc.id` 清空为空字符串（OpenAI 官方 SDK 用 `tc.index` 作为聚合 key）。
    旧逻辑会为每个 `id=""` 的 chunk 创建孤立 buffer（`""`、`"index_N"` 等），无法清理，导致
    `tool_args_pending` 永远 True、主循环 `while tool_calls_found and tool_args_pending: continue`
    死循环，工具永远不执行
  - 修复：新增 `_tool_calls_index_to_id: Dict[int, str]` 映射，在 `tc.id` 缺失时通过 `tc.index`
    找回真实 id；只有 chunk 含 `name` 时才允许创建新 buffer（避免孤立）；`arguments=None`
    跳过避免 TypeError；流结束后清理 `index_to_id` 映射
  - 影响：所有 Qwen 系（qwen3-max、qwen-plus、qwen-turbo、qwen2.5/3、SiliconFlow 上的 qwen 等）
    通过 DashScope 兼容模式调用的工具调用都能正常执行
  - 文件：`app/core/workers/chat_worker.py`、`app/core/workers/chat_worker_state.py`

- **Qwen/DashScope `Repetitive tool calls detected` 错误（HTTP 400）**
  - 根因：Qwen 服务端会拒绝"连续多轮相同 (name, arguments) 的工具调用"，错误码
    `InternalError.Algo.InvalidParameter`。同样的请求序列重试仍会被拒，必须客户端主动中断
  - 修复（三层防护）：
    1. **客户端主动循环检测**：`ChatWorker._detect_repetitive_tool_loop` 在每次 API 调用
       前扫描最近 3 轮 assistant 消息的 tool_calls 签名（按 `name + arguments` 计算，忽略
       `tool_call_id` 和 JSON 空白差异），连续 3 轮完全一致就主动终止并发友好错误提示
    2. **`ErrorClassifier` 新增 `tool_loop` 分类**：识别服务端 400 + Repetitive tool calls
       模式（`REPETITIVE_TOOL_CALL_PATTERNS`），标 `retryable=False`，避免后续自动重试
    3. **`_handle_error` 中文友好提示**：万一客户端没拦住，给出根因分析和解决建议
  - 判断标准（与 qwen 服务端语义对齐）：
    - 比较**内容**：`tool_name + arguments`（不是 tool_call_id，id 每轮新生成）
    - 比较**轮次**：连续 N 轮 assistant 消息签名完全相同 → 循环
    - 中间插入不同 tool_call → 重置计数（只有连续才算）
  - 文件：`app/core/workers/chat_worker.py`、`app/core/workers/error_handler/error_classifier.py`

- **切换项目/创建会话时立即清除聊天区**: 防止旧消息残留导致视觉混乱
- **消息卡片差异渲染视觉一致性**: 添加 meta class 改进差异对比视觉效果
- **CodeWebViewer 增强**: 调整懒加载批处理大小，改进 Chromium 进程处理
- **聊天工作线程工具调用循环死锁修复**: 解决会话死锁问题，改进 HookItem/HookEditCard UI 内边距
- **输入框占位提示优化**: 改进清晰度与随机性
- **渲染间隔调整**: 优化 CodeWebViewer 立即渲染逻辑
- **PixelPet 动画时序调整**: 改进动画流畅度
- **`backend.py` logger.debug 多余 f-string 前缀**: 移除多余的 `f` 前缀避免日志格式异常

### ✅ 测试 (Tests)

- 新增 `tests/test_chat_worker_qwen_streaming.py`（5 个测试用例）
  - 核心回归：qwen 流式 tool_calls `id` 消失场景
  - 多 tool_call 并行场景
  - OpenAI 兼容模式（每个 chunk 都有 id）正常
  - `arguments` 全 `None` 不崩溃
  - 孤立 chunk（无 name 无 buffer）正确跳过
- 新增 `tests/test_chat_worker_tool_loop.py`（13 个测试用例）
  - 签名稳定性 / `tool_call_id` 不影响签名 / 空白规范化
  - 3 轮完全相同 → 检测到循环
  - args 不同 / 中间插入不同调用 / 不到阈值 → 不算循环
  - 并行 tool_calls 全相同 → 循环 / 不一致 → 不算循环
  - 错误消息包含工具名 + 参数 + 建议
  - 纯文本 assistant 消息不影响判断

### ♻️ 代码重构 (Refactoring)

- **PixelPetWidget 行为重构**: 'playing' → 'reading'，更新动画/时长/状态，新增阅读图标
- **代码结构优化**: 提高可读性和可维护性

### 📚 文档 (Docs)

- 修复 `current_datetime.py` 和 `format_memory_context.py` 中的注释错误

### 🔧 其他 (Chores & Build)

- 版本升级至 v0.2.12（config / installer）
- **CI: Release 日志自动提取修复**: 按 tag 名匹配 CHANGELOG 区块而非固定匹配第一个 `##` 章节，避免多版本共存时提取错误
- **构建修复: 保留 QtPositioning.framework**: `to_remove_macos` 列表误删 `QtPositioning.framework`，导致 QtWebEngineWidgets 加载失败、macOS 启动闪退；CI 新增静态检查（check job）和产物依赖完整性验证（macOS verify 步骤）防止复发
