# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]

## [v0.3.0] - 2026-07-02

自上一版本以来的变更 | 提交数：15 · 文件变更：34 · +6977/-350 | 贡献者：dingma, mading

---

### ✨ 新功能 (New Features)

- **UI 插件上下文注入体系**: 实现拉模型（Pull Model）上下文注入，浮动卡片通过 `set_context_provider()` 按需获取最新项目根目录、会话 ID、主题色；上下文主题色自动映射为图表配色（QColor），告别硬编码 fallback
- **图表悬停交互增强**: 折线图（`_LineChartWidget`）和柱状图（`_BarChartWidget`）实现鼠标悬停高亮 + Tooltip 数据点实时显示，支持跨系列邻接高亮
- **插件热重载增量机制**: 支持增量重载受影响的相邻插件，仅重载变更插件而非全量扫描；目录变更过滤改进，过滤无关事件减少不必要重载触发
- **卡片自适应尺寸**: 卡片最小高度归零，支持内容自适应动态扩展，避免固定高度导致内容截断
- **插件市场版本检查**: 市场插件支持版本比较与更新提示，`plugin-marketplace` 新增版本号校验和更新下载功能
- **输入区恢复兜底逻辑**: 卡片关闭时强制恢复输入区，防止因回调链断裂导致输入区永久隐藏
- **UI 插件开发指南文档**: 新增 `SKILL.md` 和 `architecture.md`，提供详细的 UI 插件开发指引、组件设计模式、构建流程说明

### 🐛 问题修复 (Bug Fixes)

- 移除 `context-usage-stats` Token 消耗显示中重复的标签文本
- 修复关闭第三方插件后 `command_card` 固定高度未重置导致卡片高度异常
- 改进文件系统目录变更过滤逻辑，防止无关事件触发跨插件重载

### ♻️ 代码重构 (Refactoring)

- 优化工具调用结果顺序修复逻辑，移除已废弃的 Hook 块渲染功能

### 📚 文档 (Documentation)

- 新增 UI 插件完整说明到 README：浮动卡片、内容渲染器、消息工厂三种组件类型、生命周期、现有插件一览表、开发指南

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
