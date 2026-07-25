# Changelog
All notable changes to this project will be documented in this file.

## [v0.4.7] - 2026-07-25

自上一版本以来的变更 | 提交数：16 · 文件变更：24 · +1543/-608 | 贡献者：dingma, mading

> 重点：实现 **Gitee OAuth Token 自动刷新机制**，access_token 过期后自动通过 refresh_token 续期，ConfigSync 全链路同步有效 token，不再因 token 过期导致 401 中断服务。

### ✨ 新功能 (New Features)

- **快捷键绑定同步与新窗口操作**: 重构快捷键绑定同步机制，确保命令管理器中的快捷键与系统注册保持同步；托盘菜单新增「新建窗口」「切换窗口」「新建对话」等窗口管理操作，提升多窗口场景的使用便捷性
- **Gitee OAuth Token 自动刷新**: access_token 过期后自动通过 refresh_token 换取新 token，支持滚动续期（refresh_token 每次刷新同步更新），Token 过期前 60s 自动触发刷新，避免 24h 过期后服务中断
- **ConfigSync Token 全链路同步**: 新增 `_sync_token()` 方法，在 _do_upload / _do_download / _initial_sync / _check_remote_file 四个入口统一同步有效 token，解决 ConfigSync 持有过期 token 导致 401 的问题
- **远程下载 Token 保护**: Settings 重载时保存当前刷新后的 token，防止远端旧配置覆盖本地刚刷新的 token
- **GiteeCard 启动自动同步优化**: 启动时通过 `get_bound_info()` 获取有效 token（含刷新），不再直接读取可能过期的配置值
- **PlainTextViewer 最大高度约束**: 限制消息卡片中纯文本视图的最大高度，提升消息列表可用性
- **子智能体工具结果展示增强**: 改进 subagent_para / subagent_dag 工具结果的展示逻辑
- **配置同步增强**: 在加载过程中从文件到内存同步关键配置段，确保配置一致性

### 🐛 问题修复 (Bug Fixes)

- **子智能体/标题生成模型描述修复**: 内置命令重注册后重新应用模型描述，配置变更时自动刷新参数描述，强制刷新命令卡片详情视图，确保显示与实际配置同步
- **命令重载防抖**: 为命令重载实现防抖机制，避免冗余调用，提升系统响应性

### ♻️ 代码重构 (Refactoring)

- **Gitee OAuth 集成精简**: 移除废弃的 `gitee_oauth.py` 遗留代码，清理 336 行死代码，简化 OAuth 后端注册流程
- **分享卡片默认格式改为 JSON**: 将分享卡片默认导出格式从 Markdown 改为 JSON，提升数据结构完整性与后续处理兼容性

### ⚡ 性能优化 (Performance)

- **定时器与渲染优化**: 调整定时器间隔以减少冗余布局重算，合并渲染调用，动态重建托盘菜单，增强窗口标题同步功能

### 🔧 其他 (Chores & Build)

- **快捷键冲突检测**: ShortcutManagerCard 新增快捷键冲突检测与提示功能

## [v0.4.6] - 2026-07-24

自上一版本以来的变更 | 提交数：40 · 文件变更：38 · +3551/-722 | 贡献者：dingma, mading, martin98-afk

> 重点：包含 **Gitee 账号绑定与 OAuth 集成**、**云配置同步服务**、**Gitee 配置修复与同步增强**、**文件下载 QThread 重构**、**分享记录管理系统**，以及多项重大 bug 修复与 UI 优化。

### ✨ 新功能 (New Features)

- **Gitee 账号绑定与 OAuth 集成**: 新增 Gitee 账号绑定卡片、OAuth 授权流程、仓库可见性选择对话框、自动更新仓库可见性
- **云配置同步服务**: 实现云端配置同步引擎，支持自动启用、远程 SHA 缓存、用户自定义插件备份恢复、文件变更事件抑制
- **分享记录管理系统**: 基于 JSON 存储的分享记录管理，支持下载功能与记录增删改查
- **文件下载 QThread 重构**: 重构文件下载流程为 QThread+信号模式，提升 UI 响应性
- **Gitee 上传增强**: 使用原始文件名上传、支持抑制窗口与 SHA 缓存重试
- **后台导出线程**: 实现项目后台导出线程，防止导出时 UI 冻结；增加 ZIP 归档文件大小限制，防止重复导出
- **上下文压缩与工具块渲染**: 实现自动上下文压缩功能，增强工具块紧凑模式渲染
- **UI 性能优化**: 优化命令注册时机、延迟设置弹窗加载、默认主题从 fallout 改为 lumia
- **GiteeCard 按钮样式增强**: 使用 QPushButton 替换 PrimaryPushButton，增强按钮交互样式
- **插件发现增强**: 确保用户自定义插件具有有效 manifest，能被 PluginManager 发现

### 🐛 问题修复 (Bug Fixes)

- **GiteeCard 连接与同步修复**: 修复不必要的信号发射，优化 GiteeCard 连接处理；增强远程配置检查与错误处理；简化同步逻辑，移除冗余 enable() 调用
- **ConfigSyncService 增强**: 改进设置重载逻辑与批量变更处理，提升配置同步的健壮性；精简 message_card 相关冗余代码
- **ToolPermissionController 同步刷新**: 监听 Settings 变更，使配置同步后自动刷新工具权限列表，无需手动重启
- **配置变更检测修复**: 改进配置变更检测逻辑，更优雅地处理异常；确保旧监视线程正确清理，防止重复触发
- **用户自定义备份路径修复**: 修复用户自定义备份路径触发插件监视的问题，优化提取流程
- **下载处理优化**: 增强下载处理，防止内存泄漏并修复 URL 编码问题
- **QT_PLUGIN_PATH 冲突修复**: 修复 macOS 上与 Drifox.app 的 QT_PLUGIN_PATH 冲突
- **Gitee 配置修复**: 修复 Gitee 配置设置与 UUID 路径生成，优化 LLM 设置卡片提示文本
- **文档显示修复**: 修复 CodeWebViewer 文档高度计算与滚动行为，增强图表工具提示字体族支持

### ♻️ 代码重构 (Refactoring)

- **GiteeCard 同步逻辑精简**: 简化 GiteeCard 同步逻辑，移除冗余 enable() 调用

### 🔧 其他 (Chores & Build)

- 版本号更新至 v0.4.6

## [v0.4.5] - 2026-07-22

自上一版本以来的变更 | 提交数：55 · 文件变更：79 · +7210/-1211 | 贡献者：dingma, mading

> 重点：包含 **AnimatedStopButton 呼吸按钮组件**、**ShareHistory 分享历史功能**、自定义 Tooltip 系统、SubAgentCompact 高度重构、以及大量性能优化与修复。

### ✨ 新功能 (New Features)

- **AnimatedStopButton 呼吸按钮**: 集成到 BottomInputArea，支持呼吸缩放动画效果
- **ShareHistory 分享历史系统**: 新增分享历史插件、UI 组件与数据库集成，支持直接写入 sessions.db
- **ShareHistoryCard 增强**: 主题色管理、图标样式更新、InfoBar 复制确认提示，支持字体族
- **自定义 Tooltip 系统**: 全面替换原生 QToolTip 为自定义悬停 Tooltip，修复深色模式 Chromium 黑色提示框问题
- **流式渲染稳定性优化**: 流式内容防抖高度调整，减少 viewer 尺寸抖动
- **工具与思维块区域自动折叠**: 流式结束后自动折叠工具/思维区域，优化 UI 体验
- **思维块动态图标渲染**: 根据主题动态渲染思维块图标，改善视觉一致性
- **历史记录分页**: 历史卡片添加分页功能，提升性能
- **主题化图标标签**: 新增 ThemedIconLabel 支持自适应颜色，更新工具控制卡片图标
- **Overflow-anchor 属性支持**: 流式 dock 与代码 viewer 添加 overflow-anchor，提升布局稳定性
- **滚动条样式统一**: 模型选择器与项目选择器卡片刷新滚动条样式，适配主题
- **SendableTextEdit 高度优化**: 增加最大高度并调整内边距

### 🐛 问题修复 (Bug Fixes)

- **SubAgentCompact 高度重构**: 修复高度计算错误导致行被裁剪的问题，提取 `_calculate_total_height()`、优化布局失效链，增强动画处理
- **深色模式 Tooltip 修复**: 使用 CSS 自绘 Tooltip 替代 HTML title，避免 Chromium 黑色提示框
- **SendStopButton 颜色修复**: 禁用状态使用 50% 透明度而非 QIcon.Disabled；统一使用纯黑/白图标；修复 QPainter 兼容性
- **禁用状态视觉修复**: 保持金色渐变背景仅调暗图标、使用 CAPSULE_BG 替代 TOOLBAR_BG、统一 40% 整体透明度
- **呼吸方块尺寸调整**: 基础尺寸 20→17，振幅 0.12→0.10
- **工具分隔符提示**: 默认启用浅色模式并添加 Tooltip
- **QPushButton 兼容性**: Tooltip 过滤器添加 HoverEnter/HoverLeave 事件处理

### ♻️ 代码重构 (Refactoring)

- 提取 SubAgentCompact 高度计算方法，提升常量定义
- 消除发送按钮 resize 事件防抖定时器
- 统一 SendStopButton 替代双按钮方案
- 重构子智能体组件与图标结构
- 分离分享历史记录处理逻辑，直接写入 sessions.db

### ⚡ 性能优化 (Performance)

- **增量内存优化**: 增量式会话保存，指纹去重，减少冗余消息拷贝
- **差异渲染**: 仅在自然边界执行完整 HTML 渲染
- **MCP 超时缩短**: 工具调用超时从 120s 降至 60s
- **清理死代码**: 移除 `_accumulated_content` 属性（内容已由 MessageCard 追踪）
- **图标与字号缓存**: 缓存图标前缀与字号，减少重复计算

### 📚 文档 (Documentation)

- 停止按钮呼吸效果设计说明 Spec

### 🔧 其他 (Chores & Build)

- 版本号更新至 v0.4.5
- 集成完整性验证
- 新增并更新 SVG 图标资源

## [v0.4.4] - 2026-07-21

自上一版本以来的变更 | 提交数：36 · 文件变更：20 · +2278/-307 | 贡献者：dingma (8), mading (28)

> 重点：包含 **codegraph-py v1.4.0 升级**、项目导入导出能力，以及消息卡片流式渲染与思维块处理优化。

### ✨ 新功能 (New Features)

- **CodeGraph 能力升级**: 同步 codegraph-py v1.3.5、v1.3.7 与 v1.4.0，支持批量探索、子串匹配、可见性过滤、大小写敏感搜索与风险排序，并增强文件摘要处理
- **项目导入/导出系统**: ZIP 归档打包导出、异步上传到 Gitee、拖放支持 .drifox_project 文件、导入/导出对话框与背景下载功能
- **项目根目录恢复处理**: 文件恢复时根目录路径处理优化
- **历史记录限制提升至 500**: 与 SQLite 懒加载保持一致性，优化会话处理
- **紧凑模式思维块渲染优化**: 思维块以纯文本行渲染，消除闪烁问题
- **工具内容自动滚动与渲染修复**: 历史会话工具内容渲染与自动滚动行为实现
- **工具内容管理优化**: 清理过期 think-blocks、保证时间顺序
- **SubAgent 任务清理**: 新增 `cancel_all` 方法，在窗口关闭时清理运行中的任务
- **SubAgentCompactFloatingWidget 增强**: 布局计算与滚动行为适配动态内容高度

### 🐛 问题修复 (Bug Fixes)

- **流式工具块自动展开**: 添加新流式工具时自动展开工具区域
- **工具区域 UI 调整**: 简化标题移除动态计数、描述清晰化、紧凑模式增强
- **None 类型与 Markdown 渲染修复**: kind 解析处理 None 类型、紧凑模式适配
- **Skill 解析与命令缓存修复**: 改进技能名称解析与命令处理缓存失效逻辑
- **CodeGraph 同步稳定性**: 将同步冷却时间提升至 30 秒，并修复升级后的文件摘要处理
- **Body 滚动修复**: 添加 overflow-y:auto、移除 max-height 滚动定位问题
- **流式工具块超时移除**: 移除 30s 自动超时限制
- **思维块闪烁与流式处理修复**: 延迟 _thinking_finalized 重置、block-key 一致性、闭合标签管理与内容吞咽修复
- **消息卡片工具调用修复**: 异常处理语法统一并改进工具调用处理
- **消息卡片思维状态修复**: 修复 `<think>` 标签模型思考状态闪烁与工具块在简洁模式下残留问题

### ⚡ 性能优化 (Performance)

- **消息卡片增量渲染**: 工具结果缓存增量 Markdown 构建，减少全量重绘
- **流式文本高度报告优化**: `_append_text_incremental` 改用防抖高度报告，减少布局抖动

### 🔧 其他 (Chores & Build)

- 版本号更新至 v0.4.4

## [v0.4.3] - 2026-07-20

自上一版本以来的变更 | 提交数：54 · 文件变更：73 · +6764/-3362 | 贡献者：dingma, mading

> 重点：包含 **21 个新功能**、**15 个问题修复**、**4 次性能优化**、**4 项样式改进**、**3 项测试增强**、**2 项重构**。

### ✨ 新功能 (New Features)

- **插件市场全量重构**: 集成 Claude Code 插件市场协议，支持多源聚合；新增筛选标签（全部/已安装/未安装/待更新）、刷新按钮（跳过缓存）、加载更多（每次 30 个）、全局搜索；安装/更新/失败时推送 InfoBar 通知；自动识别 GitHub URL 并转换为 github 源；支持相对路径源通过仓库克隆；市场名自动取路径末段，附带链接跳转按钮；防止重复条目
- **文件树组件 (FileTree)**: 异步目录扫描、拖拽式文件树小部件、目录变更监听器，侧边栏文件管理全链路
- **通用对话框统一 (MaskDialogBase)**: 为 PluginManagerCard、Dialogs 等组件提供主题感知的统一弹出对话框，支持确认/信息/自定义场景
- **消息卡片增强 (MessageCard)**: 思考块 UI 增加图标与状态指示，提升可视性
- **Shortcut Manager 增强**: 命令文件处理支持 Windows 兼容路径，系统级快捷键注册与内存释放功能
- **分享卡片 (ShareCardContent)**: 保存文件后支持打开所在文件夹并选中文件
- **插件管理器卡片 (PluginManagerCard)**: 实现主题感知的确认对话框
- **FileTreeWidget**: 新增主题色解析，统一对话框样式集成
- **Agent 身份注入增强**: 钩子中优化身份信息输出格式
- **缓存管理组件**: 新增 UI 组件支持缓存管理与对话样式设置
- **插件市场搜索增强 (Marketplace Search)**: 搜索框接入 300ms 防抖与缓存机制，避免每敲一个字就全量重建；打开卡片时自动清理旧搜索状态与防抖定时器；`_compute_tags` 升级为支持 `categories` / `category` / `keywords` 三字段去重合并，补齐单数 `category` 字段
- **插件行尺寸与滚动布局改进 (Plugin Row Layout)**: `_PluginRow` 设置最小宽度为 0 + `QSizePolicy.Ignored` 横向策略，让插件行在搜索与刷新时能更合理地参与流式布局与滚动区收缩

### 🐛 问题修复 (Bug Fixes)

- **Backend 插件发现修复**: `_try_identify_new_plugins` 现在返回所有新插件而非仅第一个
- **UI 插件边缘启动器**: 打开菜单前刷新插件列表，确保数据显示最新
- **Marketplace 多处修复**: 刷新按钮跳过全部缓存；恢复按钮样式、用 QFont 替代 QSS；字号调整至 13px；交换刷新/关闭按钮顺序；移除 ThreadPoolExecutor，改用延迟刷新 + processEvents 防止 UI 阻塞；将 tab 栏移至标题下方、改用 Pivot 替代 SegmentedToggleToolWidget；修复重复市场条目问题；静默验证错误并在 UI 显示添加反馈
- **MainWidget**: MCP 编辑卡片高度模式改为 content
- **主窗口（main）**: 属性装饰器格式调整；`ui_plugin_edge_launcher` 启用半透明背景并记录卡片信息
- **测试体系重构**: conftest.py 修复 Qt 静默初始化失败（`QT_VERSION` → `QT_VERSION_STR`）；UIPluginRegistry 重置兼容性；QMessageBox → ConfirmDialog AST 适配；main_widget 冒烟测试

### ⚡ 性能优化 (Performance)

- **Marketplace 批量渲染**: 每次渲染 30 个插件、移除 hover QSS，并行拉取所有源 + 缓存复用
- **CodeWebViewer**: 增量文本渲染优化，减少 JS bridge 开销
- **消息卡片**: 工具结果缓存增量 Markdown 构建，减少全量重绘

### 🎨 样式改进 (Style)

- **Marketplace 按钮**: 统一 install/update 按钮上下文字号，13px 基准
- **Marketplace 行字号**: 市场行标题升至 18px、副标题 14px

### ♻️ 代码重构 (Refactoring)

- **UIPluginRegistry.reset() 隔离**: 重置后重新 `get_instance()` 保证测试隔离性

### 🔧 其他 (Chores & Build)

- **测试归档**: 已删除的 Cron 子系统测试模块以 `pytest.skip` 归档保留，备未来复用
- **陈旧断言修复**: `test_default_opencode_provider` 中模型列表字段断言改为存在性检测
- **Agent 冒烟测试**: 为 Agent 数据类、PermissionResolver、AgentManager 单例添加 93 个冒烟用例

## [v0.4.2] - 2026-07-18

自上一版本以来的变更 | 提交数：49 · 文件变更：82 · +6362/-1046 | 贡献者：dingma (30), mading (20), DriFox Dev (2)

> 重点：包含 **19 个问题修复**、**23 个新功能**、**4 次重构**、**2 篇设计文档**、**1 项清理**。

### ✨ 新功能 (New Features)

- **插件图标体系**: 为系统插件添加 SVG 图标，新增 `PluginIconWidget`，统一插件市场 / 插件管理器 / 边缘启动器菜单展示
- **Shortcut Manager 插件**: 系统级快捷键管理 UI，支持命令与技能管理，含图标与卡片 UI
- **UI 插件 — 左侧边缘启动器**: 独立窗口模式、位置追踪与可见性管理优化
- **工具流预览动画增强**: 渐变背景动画 + 旋转提示文字 + 左→右渐变扫描
- **Buddy 多模态生成客户端**: 支持视频 / 图片 / 3D 模型
- **OpenCode Zen 免费模型**: 跨多家 provider 异步拉取与自动刷新
- **通用对话框统一**: 用 `ConfirmDialog` / `InfoDialog` 替换原生 QMessageBox
- **Plugin Creator 文档集**: components / manifest / publishing / testing / troubleshooting / workflow 全套文档
- **Shortcut Manager 命令参数保留**: 在快捷键执行时保留原命令参数

### 🐛 问题修复 (Bug Fixes)

- **流式思考 / 工具执行 UI**: 修复 spinner SVG 误清除、移除黄色背景、保留工具块 DOM 位置、精确化 think tip 选择器、新增旋转提示
- **历史会话延迟加载**: 优化卡片渲染性能
- **托盘热键**: `toggle-window` 改用 `RegisterHotKey` 并自动回退 keyboard
- **主题管理**: 插件主题提前加载、重启后主题保持、防止未初始化前主题重载、刷新逻辑优化
- **边缘启动器**: 位置追踪与可见性管理、菜单间距 / 边距 / 图标尺寸调整、移除无效 `QMenu.setIconSize`、禁用左边缘 resize
- **技能检索**: 支持文件夹名匹配，提升插件发现准确率
- **插件管理器图标**: 重设计为 2x2 模块网格风格
- **InfoBar**: 位置改为 `TOP_RIGHT`，错误降级为 `Warning`
- **CodeWebViewer 缓存清理**: 修复缓存清理后消息内容获取失败
- **README 图片格式**: 修正支持图片格式，更新版本一致性文档

### ♻️ 代码重构 (Refactoring)

- **model_capabilities / models_dev_sync**: 移除写死的免费模型 capability，改为基于 models.dev 动态继承
- **ShortcutManagerCard**: UI 代码清理与一致性优化（双 commit 合并）
- **导入与延迟加载**: 减少模块加载开销

### 🎨 样式改进 (Style)

- **message_card**: 调整 think block 字体大小，提升可读性

### 📚 文档 (Documentation)

- **插件图标支持设计文档**
- **UI 插件边缘启动器设计文档**

### 🔧 其他 (Chores & Build)

- 删除已废弃的 `buddy-cloud.py` 脚本
- 删除过时的 UI 插件边缘启动器设计文档

## [v0.4.1] - 2026-07-16

自上一版本以来的变更 | 提交数：13 · 文件变更：7 · +428/-208 | 贡献者：dingma, mading

### ✨ 新功能 (New Features)

- **历史会话记录按索引安全检索**: 实现通过索引安全获取历史会话记录的功能，增强数据访问的便捷性与健壮性
- **截图工具结果添加视觉提示**: 为截图工具结果添加可视化提示，防止重复截图循环（后经重构改为 hook 注入模式）

### 🐛 问题修复 (Bug Fixes)

- **视觉内容注入机制重构**: 将截图工具结果提示重构为通过 hook 模式注入视觉内容，避免无限工具循环；添加 `_hook_event` 标记防止 UI 渲染注入消息；同步 `_current_session_messages` 以匹配 hook 持久化模式；为工具结果添加非视觉声明，防止模型对图像读取/截图产生幻觉
- **Hook 消息确认规则增强**: 补充系统提醒确认规则，防止 hook 消息被误判为用户输入，确保 agent 等待用户真实响应
- **会话切换 sentinel 保护**: 实现会话切换哨兵机制，防止会话间消息内容互相覆盖
- **视觉注入文本包裹优化**: 将视觉注入文本包裹在 system-reminder 标签中，保持上下文一致性
- **图片体积自动压缩防止 API 400**: 为 `screenshot`/`read` 工具结果注入和用户图片附件添加 >5MB 自动压缩（基于 PyQt5 QImage 等比缩小 + JPEG quality=85），避免 4K 截图因 base64 过大导致 API "media exceeds size limit" 错误，解决 PyInstaller 打包后因 PIL 缺失 PNG 略大而更容易触发的问题

### ♻️ 代码重构 (Refactoring)

- **移除截图工具结果视觉提示**: 清理旧的截图工具结果视觉提示代码，为后续 hook 注入模式做准备

### 🎨 样式改进 (Style)

- **视觉注入文本标签包装**: 将视觉注入文本用 system-reminder 标签包裹，提升系统消息的可辨识性

## [v0.4.0] - 2026-07-16

自上一版本以来的变更 | 提交数：19 · 文件变更：48 · +1492/-435 | 贡献者：mading

### ✨ 新功能 (New Features)

- **models.dev 同步与动态模型能力集成**: 实现 models.dev 平台同步机制，支持动态模型能力（capabilities）的自动获取与集成，增强多平台模型兼容性
- **OpenCode Go 增强与代码格式化**: 增强 provider 对 OpenCode Go 的支持，同时改进代码格式化输出效果
- **默认 OpenCode 免费 Provider 自动注入**: 新增默认 OpenCode 免费 provider，对所有用户自动注入，并限制仅开放 4 个免费模型
- **模型参数按 Provider 隔离**: 实现模型参数按 provider 独立管理，增强多 provider 场景下的配置隔离性与灵活性
- **模型能力处理增强与 thinking 字段一致性**: 增强模型 capabilities 处理逻辑，确保 thinking 相关字段在不同模型间的一致表现
- **模型选择器信息增强**: 为模型项添加能力 emoji 图标、模型描述信息和增强 tooltip，显著提升选模型时的信息密度与直观性

### 🐛 问题修复 (Bug Fixes)

- **单实例检查修复**: 修复主应用流程中单实例检查的逻辑问题，确保多开窗口按预期运行
- **工具 Schema 5s TTL 缓存**: 为 `get_builtin_tools_schema` 添加 5 秒 TTL 缓存，减少重复构建开销；将 MCP 注入日志降级为 debug 级别，减少日志噪声
- **CardManager 撤销操作缓存清空修复**: 修复撤销操作时 CardManager 状态处理不当导致缓存被意外清空的问题
- **默认 OpenCode Provider 用户删除后重建**: 修复用户删除默认 OpenCode 免费 provider 后未正确重建，再次创建时出现竞态条件的问题
- **LLM 默认字体族优化**: 将默认 LLM 字体族依次更新为微软雅黑和楷体，优化中文显示效果

### ⚡ 性能优化 (Performance)

- **非关键插件与 WebEngine 延迟初始化**: 将非关键插件初始化、共享 WebEngine Profile 设置延迟至主窗口显示后，显著提升 UI 启动响应速度
- **get_tool_counts 延迟导入**: 延迟导入 `get_tool_counts` 函数调用，减少模块加载阶段的初始化开销

### ♻️ 代码重构 (Refactoring)

- **多处代码结构可读性优化**: 重构多处代码结构，提升整体可读性与可维护性

### 🔧 其他 (Chores & Build)

- 版本号更新至 v0.4.0，同步更新 pyproject.toml、配置文件和安装脚本

## [v0.3.11] - 2026-07-15

自上一版本以来的变更 | 提交数：23 · 文件变更：31 · +2805/-2472 | 贡献者：dingma, mading

### ✨ 新功能 (New Features)

- **后台线程加载余额与 URL 导入**: 将余额获取与 URL 导入操作移至后台线程，避免阻塞 UI 主线程，显著提升界面响应流畅度
- **Gemini 模型 thought_signature 支持**: 增强 Gemini 模型消息处理，兼容 thought_signature 字段，提升多模型兼容性
- **会话脏标记机制**: 引入会话脏标记（dirty flag），仅在内容变更时持久化消息，避免冗余保存操作
- **重型卡片内容延迟加载**: 对重型卡片内容实施延迟加载策略，优化初始窗口显示速度
- **UI 插件延迟加载与浮动组件回流优化**: 实现 UI 插件懒加载，并优化 SubAgentCompactFloatingWidget 回流处理，提升布局准确性
- **性能日志与 Token 跟踪增强**: UI 消息处理及上下文构建增加性能日志；SubAgentExecutor 及相关组件增强 Token 跟踪与上下文用量显示
- **实时日志流与任务统计增强**: SubAgentSessionDialog 增加实时日志流（logs_provider），并补充任务统计信息与日志渲染优化

### 🐛 问题修复 (Bug Fixes)

- **日志过滤增强**: 优化 SubAgentSessionDialog 日志过滤逻辑，跳过空的 ai_response/thinking 条目，减少无效展示
- **字体兼容性修复**: 更新 SubAgentSessionDialog 字体处理方式，提升跨平台兼容性
- **会话摘要背景色修正**: 更新会话摘要背景色，统一使用实时标签样式
- **SubAgentSessionDialog 布局重构**: 重构对话框布局，新增侧边栏导航与内容分区，提升使用体验
- **移除过期记忆文件并修复命令崩溃**: 删除过期 memory 文件，修复 toggle-window 命令偶发崩溃与内容差异问题
- **托盘 toggle-window 快捷键修复**: 修复托盘 toggle-window 快捷键间歇性失效问题，增加热键健康检查与 QShortcut 回退机制
- **搜索引擎切换 Tavily + TinyFish**: 因 DuckDuckGo 搜索引擎失效，全面切换至 Tavily（主要）+ TinyFish（备用）双搜索引擎，重写 web_tools 搜索逻辑，更新配置项与使用文档

### ♻️ 代码重构 (Refactoring)

- **移除 tooltip 阴影效果**: 移除 QGraphicsDropShadowEffect，防止分层窗口上的渲染异常
- **移除冗余计时代码**: 清理 context builder、UI engine 和 main widget 中已不再使用的计时逻辑

### 🔧 其他 (Chores & Build)

- **清理废弃文件**: 移除过期的 app_line_counts.txt 和工作记忆文件
- **代码结构优化**: 重构多处代码结构，提升可读性与可维护性

### 📚 文档 (Docs)

- **性能优化实施方案**: 新增完整的性能优化实施计划文档，涵盖 P0/P1/P2 共 10 项优化任务

### 🔧 其他 (Chores & Build)

- 版本号更新至 v0.3.11，同步更新 pyproject.toml、配置文件和安装脚本

## [v0.3.10] - 2026-07-15

> 本版本为 v0.3.9 重大 bug 修复版。补入 v0.3.9 标签之后遗漏的 3 个修复 commit：导入功能重构、PyInstaller 路径处理优化、命令卡片 tooltip 短文本裁切修复。

自上一版本以来的变更 | 提交数：3 · 文件变更：11 · +456/-135 | 贡献者：mading

### 🐛 问题修复 (Bug Fixes)

- **PyInstaller 路径处理与 Git 超时配置优化**: 改进 `PyInstaller` 打包路径处理逻辑，修正打包后资源定位问题；同步调整 Git 相关超时设置，避免长时间无响应阻塞
- **导入功能增强（专用对话框与 tooltip 支持）**: 引入专用导入对话框替换原有简易流程，配合 tooltip 提示提升用户操作引导；同步支持更完整的导入预览与确认步骤
- **command_card tooltip 短文本底部 1/3 裁切修复**: 修复 `command_card` 中 tooltip 在显示短文本时底部 1/3 区域被裁切的问题，改用 `math.ceil` 计算并增加 4px 安全边距，确保不同长度文本均能完整显示

## [v0.3.9] - 2026-07-15

> 本版本为重新发布，**补入 v0.3.9 标签之后遗漏的 6 个修复 commit**（命令处理增强、命令安全加固、Windows PATHEXT 回退、diff 回退机制、provider 配置优化、变量使用修正）及后续 1 个模型标签点击修复。

自上一版本以来的变更 | 提交数：30 · 文件变更：29 · +1996/-1560 | 贡献者：dingma

### ✨ 新功能 (New Features)

- **消息处理用户文本指纹识别**: 增强消息处理流程，基于用户文本生成指纹用于 worker 身份识别，便于会话回溯与调试定位
- **toggle-window 与 clear 命令及快捷键**: 新增 `toggle-window`（隐藏/显示主窗口）与 `clear`（清空当前会话）两条内置命令及其快捷键支持，同步更新对应的命令说明文档
- **命令处理与快捷键管理增强**: 新增用户自定义 function 命令的 Python 处理器检查逻辑（`_has_command_handler`），无处理器时回退到在输入框插入 `/command` 文本，与命令卡片选中行为一致；改进快捷键读取机制，优先使用 `CommandManager` 中注册的快捷键，回退到系统命令文件；统一多模块异常处理风格

### 🐛 问题修复 (Bug Fixes)

- **卡片宽度同步循环依赖优化**: 重构 `main_widget` 与 `message_card` 在窗口 resize 时的宽度同步逻辑，避免信号相互触发导致的循环依赖问题，提升大窗口拖动时的稳定性
- **toggle-window 命令启动崩溃与全局热键支持**: 解决注册 `toggle-window` 命令后应用启动时偶发的崩溃问题，并实现全局热键注册以支持系统级快捷键唤起主窗口
- **MessageCard 用户头像与标题本地化**: 将 `MessageCard` 中硬编码的用户头像与标题替换为本地化文本，匹配界面整体语言切换
- **check_update 静默模式与用户反馈**: 重构 `update_checker` 的 `check_update` 方法以支持静默模式，优化检查过程中的用户反馈提示；同步精简 `CardManager` 中的冗余调用
- **布局边距与命令卡片对齐优化**: 调整 `OpenAIChatToolWindow`、`CardContainer`、`bottom_input_area` 的布局边距以改进整体间距；将命令卡片标签对齐到顶部避免不均匀的内边距；新增 `CardManager` 方法支持跨容器隐藏非系统卡片
- **command_card tooltip 首次显示延迟**: 修复 `command_card` 中 tooltip 在首次显示时的位置延迟问题，提升交互即时感
- **Windows 命令找不到自动回退 cmd /c**: `command_safety.run_safe` 在 Windows 上找不到可执行文件时自动回退到 `cmd /c` 包装，支持 PATHEXT 扩展名解析（如 `pip → pip.exe`、`tsc → tsc.cmd`），解决 `shell=False` 模式下的 PATH 查找问题
- **Windows Shell 元字符正则增强**: 修正 `WINDOWS_SHELL_META` 正则匹配逻辑，正确处理 Windows 路径分隔符 `\\` 与字面 `^`，避免路径误判
- **command_safety 字符串风格与内置命令列表统一**: `command_safety` 模块统一使用双引号字符串风格（替换单引号）；扩展 Windows Shell 内置命令列表，覆盖 cmd.exe 内置命令全集
- **diff 生成会话消息回退机制**: 实现从会话消息生成 diff 的回退路径，当工具调用结果不可用时仍能生成可读 diff；同步新增 `app.tools` 模块相关 diff 生成入口
- **provider 配置处理与消息卡片交互优化**: 增强 `main_widget` 中 provider 配置的处理逻辑；改进 `tool_popup`、`terminal_tools`、`message_card` 等模块的交互流程，提升多 provider 切换与命令触发场景下的稳定性
- **BackgroundTaskManager 命令编码变量修正**: 修正 `BackgroundTaskManager` 中命令编码相关的变量使用，避免编码错误引发的隐性 bug
- **模型标签点击使用 config_id 而非 provider_name**: 修复 `message_card` 中模型标签点击事件使用 `provider_name` 而非 `config_id` 的问题，确保在多配置场景下正确切换模型

### ♻️ 代码重构 (Refactoring)

- **浮动 tooltip 独立窗口化**: 将命令卡片中的浮动 tooltip 重构为独立窗口实现，统一布局与可见性管理逻辑，便于跨组件复用
- **ToolPopupDialog 平滑淡入动画**: 在 `ToolPopupDialog` 中引入 `QGraphicsOpacityEffect` 实现更平滑的渐隐淡入动画效果，替换原有硬切换过渡

## [v0.3.8] - 2026-07-13

自上一版本以来的变更 | 提交数：6 · 文件变更：18 · +239/-147 | 贡献者：dingma

> 本版本为重新发布，修复了上一版 v0.3.8 之后发现的问题并补入性能与重构改进。

### 🐛 问题修复 (Bug Fixes)

- **message_card 正则匹配灾难性回溯修复**: 修复 `message_card` 中部分正则模式因使用 `.*+DOTALL` 而吞掉整行文本的问题，避免当用户消息包含 `<system-reminder>` 时出现卡片解析灾难性损坏
- **JSON 序列化兼容性与可读性修复**: 切换 JSON 序列化选项为 `OPT_SORT_KEYS` 并解码为 UTF-8，提升跨平台序列化稳定性与可读性

### ⚡ 性能优化 (Performance)

- **Token 估算缓存性能优化**: 优化 `token_estimator` 的 token 缓存实现，减少重复计算开销并提升统计清晰度，同步调整 `main.py` 调用入口

### ♻️ 代码重构 (Refactoring)

- **核心代码结构可读性优化**: 重构 `message_content`、`token_estimator`、`chat_worker` 等核心模块代码结构，提升可读性与可维护性
- **锁屏远程与本地服务 API 入口整理**: 简化 `lock_screen_remote` 与 `local_service/api_server` 入口逻辑，统一调用风格
- **ProviderIconWidget 绘制逻辑简化**: 精简 `ProviderIconWidget` 的 `paintEvent` 实现，统一多个 SVG 图标尺寸规格，提升跨图标视觉一致性

### 🔧 其他 (Chores & Build)

- 更新版本号至 v0.3.8

## [v0.3.7] - 2026-07-13

自上一版本以来的变更 | 提交数：13 · 文件变更：106 · +6653/-758 | 贡献者：dingma, mading

### ✨ 新功能 (New Features)

- **浅色主题与图标资源**: 新增浅色主题（light theme）及对应图标资源，统一多个主题配色提升整体观感一致性，并同步刷新主题相关配置
- **主题管理与样式刷新机制全面优化**: 细化主题管理 UI 刷新逻辑、增加变更类型检测；优化组件树遍历与主题处理性能，增强样式刷新对主题切换的响应；统一 tooltip 字号管理并在主题变更时同步刷新
- **主题感知组件样式与代码高亮增强**: 多种组件（卡片、设置、tooltip 等）实现主题感知样式；增强 Pygments 语法高亮在多主题下的视觉一致性
- **会话上下文使用追踪增强**: 为 `ChatSession` 新增 `last_api_message_count` 与 `last_api_prompt_tokens` 字段，并实现模型 token 比率计算，强化上下文用量与 API 使用情况的追踪能力

### 🐛 问题修复 (Bug Fixes)

- **message_card 主题适配修复**: 修复 `message_card` 背景色透明度未跟随系统透明度变化、代码框按钮图标在浅色主题下显示异常的问题

### 🔧 其他 (Chores & Build)

- 更新版本号至 v0.3.7

## [v0.3.6] - 2026-07-12

自上一版本以来的变更 | 提交数：11 · 文件变更：18 · +669/-188 | 贡献者：dingma

### ✨ 新功能 (New Features)

- **上下文用量统计增强**: 当 API 未返回 usage 时，使用本地上下文计数补全消息 token 用量，确保上下文圆环和消息卡片中的统计保持准确；新增缺失 usage 场景的回归测试
- **历史问题计数提示优化**: 为用户问题数量增加 InfoBadge，并在新建会话和输入用户消息时正确更新可见性；同步优化相关 tooltip 样式与上下文用量展示
- **Hook Token 追踪与上下文用量堆叠图优化**: UIEngine 新增 hook token 追踪，并在上下文用量 Tooltip 的堆叠柱状图末尾显示占比百分比
- **跨组件 Token 显示同步**: UIEngine、ChatWorker 与主窗口三组件的 token 显示统一同步，避免多处 UI 数字不一致并增强上下文处理

### 🐛 问题修复 (Bug Fixes)

- **工具运行折叠框状态修复**: 修复工具调用与工具结果写入不同消息卡片时，运行折叠框偶尔无法转为完成态并持续累积的问题；通过 `tool_call_id` 关联所属卡片，并增加完成态恢复兜底与回归测试
- **上下文圆环 token 防闪现修复**: 修复刷新上下文圆环时，因 `session.messages` 还未包含本轮新增消息（陈旧）而闪现异常小值的问题；以 worker 实时 token 量为下限兜底
- **重载卡片 token 与上下文圆环同步**: 重载历史会话时，卡片底部 token 直接复用上下文圆环快照的 `used_tokens`（同一来源），避免基于 worker 估算导致显示远小于真实占用

### ♻️ 代码重构 (Refactoring)

- **Lock Screen Remote 移除命令注册**: 移除 `register_command_handler` 与 `/lock-remote` 命令注册路径，统一由设置 UI 控制开关；同步清理 `app/core/system/__init__.py` 的导出与 `main_widget.py` 的调用点
- **代码注释精简化**: 锁屏远程相关注释与单例/命令注册章节标题更新，可读性提升

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
