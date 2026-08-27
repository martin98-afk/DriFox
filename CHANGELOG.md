# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]
### 🐛 问题修复 (Bug Fixes)
- **macOS 窗口最小化按钮无响应** (`app/widgets/tab_manager_window.py` + `app/widgets/cards/settings/gitee_card.py`): 根因是「窗口置顶」给主窗口加 `WindowStaysOnTopHint`，Qt 在 macOS 上将窗口提到 NSStatusWindowLevel(8)，WindowServer 对非 normal 层级窗口丢弃标题栏最小化点击（系统层限制）。修复：macOS 改软置顶（不加 hint，应用激活时抬升），并主动摘除历史残留 hint 恢复最小化能力；Windows/Linux 保持原生置顶并补「关闭时摘除」；置顶开关统一收敛到 `_apply_window_topmost` 单一入口。同时移除 `AA_DontUseNativeMenuBar` 误用（应用级属性传给 `QWidget.setAttribute` 导致 TabManagerWindow 创建即 TypeError 崩溃）与 `WA_MacAlwaysShowToolWindow` 无效修复及 changeEvent 死代码双保险。

## [v0.5.4] - 2026-08-23

自上一版本以来的变更 | 提交数：83 · 文件变更：160 · +14963/-1976 | 贡献者：dingma

### ✨ 新功能 (New Features)

- **事件驱动插件管理 HookPolicy 与 EngineSession** (`app/core/engine/` + `app/core/hook_manager/` + `plugins/system/hooks/`): 引入 `HookPolicy`（三档规范化）与 `EngineSession` 通用驱动原语；新增 `PluginChanged`（工具变更）钩子；实现 team member hook policy 并增强 registry 激活；触发条件扩展至 `SessionStartEvent`。按 run_id 粒度实现团队项目管理。
- **UI 组件化与插槽体系** (`app/widgets/` + `app/plugins/registries/ui_plugin_registry.py`): 新增 `UIModule` 契约与模块插槽注册表、`compose()` 驱动组装；注册 5 个系统 `UIModule` 插槽（plugin 可整体覆盖 region）；将 `setup_ui` 拆解为 `title_bar` / `bottom_toolbar` / `input_card` / `system_cards` / `chat_area` 模块（行为等价）；集成 `ChatAreaModule` 并支持 plugin override。
- **UI 事件总线与通用插槽模型** (`app/plugins/registries/ui_plugin_registry.py`): 新增 `UIEventBus`（插件作用域自动取消订阅）并埋点 theme / tab / card-visibility 发布；新增 `Region` / `SlotEntry` 通用挂载模型，将 4 类 Phase-D 插槽统一迁移到单一 region storage（API 兼容）。
- **UI 扩展点补全** (`app/widgets/`): 新增 `IWindowHost` 协议（兼容旧 duck-typing）；settings-card section 路由；context-menu 开放 input_area 区域；input-button 支持 start/before/after/end 位置锚定；sidebar 优先级排序优于标题排序；tab_manager 将 UI 上下文构建委派给活动聊天窗。
- **卡片多卡堆叠与 workspace 页** (`app/widgets/cards/` + `app/widgets/workspace/`): 新增 `card-manager` 堆叠模型、`CardStackContainer`（Pivot + 堆叠）widget、`card-dock` 多卡堆叠联动；workspace-page 懒加载宿主接入 tab manager 内容区，新增页面级扩展槽与有序注册。
- **子代理循环策略与工具加载安全** (`app/core/loop_policies/` + `app/tools/`): 实现 subagent loop policy（轮次上限 + 最终总结提示）；`plugin_tool_loader` 增强工具入口模块校验，防止 `sys.modules` 污染并确保有效 `register` 函数。
- **性能基建与可观测性** (`tests/` + `scripts/`): 新增 perf baseline 测试（内存/动画/上传/初始化）、`baseline_extra.py` 扩展指标、`measure_perf.py` 回归 harness；`singleton-signals` 迁移 `UsageService.coding_plan_ready` 至 `_reg_sig`；`image-attach` 抽取 `_image_path_to_data_uri`（三档尺寸）；`thread-guard` 卡死 QThread 看门狗（>60s）；`cleanup` 延迟 worker 清理看门狗。
- **tab 面板增强** (`app/widgets/tab_panel.py`): splitter 拖拽防抖 + 内容区渲染优化；自定义插件卡新增可旋转箭头与样式。
- **任务列表功能增强** (`app/widgets/`): 增强任务列表并修复 UI 抖动。

### 🐛 问题修复 (Bug Fixes)

- **chat-worker / hook-policies** (`app/core/workers/chat_worker.py` + `app/core/hook_manager/`): 预计算当前消息文本避免 `UnboundLocalError`；hook 触发条件纳入 `SessionStartEvent`。
- **welcome 稳定性** (`app/widgets/`): 补全渲染回调链路 DEBUG 日志 + `_show_initial_welcome` 异常兜底；修复插件热重载后欢迎卡片消失 / 新建会话失败的契约属性防御。
- **dock / card 渲染** (`app/widgets/streaming_dock.py` + `app/widgets/cards/`): 修复 streaming-dock 水平滚动条与用户滚动时自动滚动打断；修复隐藏控件 resize 死循环；card-dock wrapper 初始折叠态立即同步且可见性联动 splitter 尺寸。
- **streaming-dock 自动滚动误判** (`app/widgets/streaming_dock.py`): DOM 更新期间忽略程序化滚动事件，避免被误判为用户滚动而打断自动滚动（补 `46993d3f` 热修）。
- **card-dock splitter 记忆** (`app/widgets/cards/card_container.py` + `app/widgets/tab_manager_window.py`): 修复左右栏 splitter 按插件记忆失效与切换双跳回归（补 `26df65f2` 热修）。
- **message-card 滚动位置** (`app/widgets/message_card.py`): 工具更新不再影响内容滚动位置（补 `6693093d` 热修）。
- **agents / OpenAIChatToolWindow** (`plugins/system/agents/` + `app/widgets/`): 更新 build / explore 步骤数并移除 summary agent；改进 `ToolResult` 与 dict 格式的内容处理。
- **message_card 滚动与缓存** (`app/widgets/message_card.py`): 移除 CodeWebViewer 进度文本前导 emoji；更新 skeleton 缓存版本并改进 in-progress / streaming 滚动行为。
- **tab_panel 滚动态** (`app/widgets/tab_panel.py`): 刷新时保留自定义插件滚动状态（展开 / 折叠模式）。
- **团队邮件丢失根因 G1** (`app/core/`): 流结束时重排队被回滚的 pending 邮件，修复团队邮件丢失。
- **信号与生命周期** (`app/widgets/` + `app/main_widget.py`): QObject 初始化后安全连接 `destroyed` 信号；应用退出时停止全局 subagent 日志清理定时器；`tool_permission` 信号注册避免悬挂引用；baseline 子进程调用编码与错误处理；`slow_hooks` 阻塞 sleep 钩子函数签名一致性；`profile_create_session` 扩展模式选项并改进钩子注册日志。

### ♻️ 代码重构 (Refactoring)

- **ui-module 抽取（Phase 2）** (`app/widgets/`): 收敛系统模块注册顺序并补 e2e override 测试；将 title_bar / bottom_toolbar / input_card / system_cards / chat_area 从 `setup_ui` 抽取为独立模块（行为等价）。
- **ui-slots 收敛** (`app/plugins/registries/ui_plugin_registry.py`): 将 4 类 Phase-D 插槽迁移至单一 region storage（API 兼容）。
- **tray-manager** (`app/widgets/`): 提取托盘切换防抖为命名常量。

### ⚡ 性能优化 (Performance)

- **缓存与重绘控制** (`app/`): 模块级 icon / render 缓存以 OrderedDict LRU（上限 256）；main-widget resize 防抖 32ms→80ms（命名常量）；pixel-pet 动画重绘率上限（最小帧间隔）；tray 切换防抖抽取常量。

### 📚 文档 (Documentation)

- **UI 模块化文档** (`docs/`): 补 UIModule 契约、系统模块与 override 指南、UIEventBus / Region / IWindowHost / slot 增强、dock 堆叠与 workspace page 指南。
- **插件架构文档** (`docs/plugin-architecture.md`): HookPolicy 三档规范化与 EngineSession 通用驱动原语（含 PluginChanged 遗漏段落）。
- **其他** (`AGENTS.md` + `docs/reports/`): 更新 AGENTS.md 结构清晰度；归档 #4 perf 环境 / 可观测性 / 蓝图交付物；补 ui-events / workspace-page 测试与验收 fixture。

### 🔧 其他 (Chores & Build)

- **单例信号 / 测试修正**: `pyqtBoundSignal` 无 `.receivers()`，改用方法存在性 + 源字符串匹配验证结构性配对；修正测试假设（`__init__` 已注册单例 connect，断言 `copy_state_from` 不再向 dst 追加 src 条目）；为 3 个缺失文件补 `_reg_sig()`。

### 🔄 Hotfix 重新发布 (Re-release · 2026-08-24)

基于 `v0.5.4` 标签的增量变更 | 提交数：5 · 文件变更：11 · +1430/-160 | 贡献者：mading

#### ✨ 新功能 (New Features)

- **ReplaceTabBar 全容器卡片 tab 管理** (`app/widgets/`): 实现 `ReplaceTabBar` 用于管理全容器卡片 tab，与 tab-manager 顶层结构衔接。
- **replace tab 窗口级状态隔离** (`app/widgets/tab_manager_window.py`): 增强 replace tab 管理，按活动窗口隔离状态，避免多窗口串扰。

#### 🐛 问题修复 (Bug Fixes)

- **card-container / tab-manager 居中与 margin** (`app/widgets/`): 调整叠加层居中行为与 margin 同步。
- **对话按钮保留开启的 tab 并抑制关闭事件** (`app/widgets/replace_tab_bar.py` + `app/widgets/tab_manager_window.py`): 增强对话按钮行为，保留已开启的 tab 并抑制关闭事件，避免误关。
- **replace tab 状态按对话隔离** (`app/widgets/tab_manager_window.py` + `plugins/plugin-marketplace/ui/cards.py`): 隔离 replace tab 状态按对话单元存储，防止跨会话串扰，并扩展 `test_replace_tab_bar` 用例覆盖。

### 🔄 Hotfix 重新发布 (Re-release · 2026-08-25)

基于 `v0.5.4` 标签的增量变更 | 提交数：9 · 文件变更：48 · +2280/-550 | 贡献者：dingma, mading

#### ✨ 新功能 (New Features)

- **字号步进器与 legacy key 迁移** (`app/widgets/cards/settings/llm_settings_card.py` + `app/widgets/cards/settings/plugin_config_card.py` + `app/utils/design_tokens.py` + `app/utils/config.py` + `app/main_widget.py` + `app/widgets/tab_manager_window.py`): 字号 stepper 用 delta 映射替代离散档位；旧字号键名平滑迁移；设置卡片字号面板与插件配置卡片扩展；新增 `tests/widgets/test_font_size_stepper.py` 与 `test_replace_tab_bar` 回归测试。
- **多区主题基建 scene/decoration layer** (`app/widgets/scene_layer.py` + `app/widgets/decoration_layer.py` + `app/utils/theme_manager.py` + `app/widgets/modules/chat_area_module.py` + `app/main_widget.py` + `app/widgets/cards/settings/llm_settings_card.py` + `app/widgets/tab_manager_window.py`): 引入 scene layer（场景切换与刷新钩子）与 decoration layer（装饰叠加层）；主题管理器增强；chat_area 模块接入；新增 `test_font_size_stepper` 用例。
- **欢迎卡片 tab 字号适配与 replace tab bar 样式** (`app/main_widget.py` + `app/widgets/message_card.py` + `app/widgets/replace_tab_bar.py`): 增强欢迎卡片 tab 字号自适应能力；replace tab bar 样式优化。
- **scene layer 落地与欢迎卡片软刷新** (`app/widgets/scene_layer.py` + `app/widgets/message_card.py` + `app/utils/theme_manager.py` + `app/widgets/modules/chat_area_module.py` + `app/widgets/tab_manager_window.py`): 落地 scene layer；欢迎卡片软刷新行为改进（避免重建抖动）；新增 `tests/widgets/test_scene_layer_mount.py` 与 `test_welcome_card_enter_anim.py`。

#### 🐛 问题修复 (Bug Fixes)

- **字号/字族切换即时重建左侧导航按钮样式** (`app/widgets/cards/settings/llm_settings_card.py`): 字号/字族切换即时重建左侧导航按钮样式（不等切 tab），并使刻度条大字号标签高度自适应；新增 `test_font_size_stepper` 用例。
- **CodeWebViewer 水平滚动条抑制** (`app/utils/design_tokens.py` + `app/widgets/message_card.py`): 显式设置 `overflow-x: hidden`，防止 CodeWebViewer 水平滚动条出现。
- **默认字号 key 与 superlarge 配置对齐** (`app/utils/design_tokens.py`): 默认字号 key 更新以匹配新 superlarge 配置。

#### ♻️ 代码重构 (Refactoring)

- **window_registry 收敛窗口实例管理** (`app/core/window_registry.py` + `app/main_widget.py` + `app/widgets/tab_manager_window.py` + `app/widgets/pixel_pet.py` + `app/widgets/cards/global_card_controller.py` + `app/widgets/cards/settings/gitee_card.py` + `app/core/builtin_commands.py` + `app/core/team_manager.py`): 新增 `window_registry` 模块统一管理窗口实例，主窗口/团队管理/内置命令/像素宠物迁移至注册表；新增/更新 `test_main_widget_smoke` / `test_team_add_member` / `test_input_button_hot_reload` 测试。
- **scene/decoration layer 代码结构优化** (`app/widgets/scene_layer.py` + `app/widgets/decoration_layer.py` + `app/widgets/tab_manager_window.py` + `tests/widgets/test_decoration_layer.py`): 提升可读性与可维护性；scene/decoration layer 内部结构清理；新增 decoration_layer 测试覆盖。

### 🔄 Hotfix 重新发布 (Re-release · 2026-08-25 · Round 2)

基于 `v0.5.4` 标签的增量变更 | 提交数：23 · 文件变更：83 · +5753/-553 | 贡献者：dingma

#### ✨ 新功能 (New Features)

- **动态 value provider 注册** (`app/widgets/cards/floating/command_card.py`): 引入动态 value provider 注册机制，统一 value 选项获取路径，便于插件扩展设置面板选项。
- **插件变更钩子广播到所有活跃 backend** (`app/core/backend.py` + `app/core/hook_manager.py` + `app/widgets/cards/global_card_controller.py` + `app/widgets/tab_manager_window.py`): `PluginChanged` 钩子事件广播到所有活跃 backend（含多 tab 场景）；改善 `GlobalCardController` 卡片关闭逻辑。
- **本地 token 估算比例解析** (`app/core/context_usage.py` + `app/core/engines/ui/engine.py` + `app/core/provider_profile.py` + `app/core/token_estimator.py` + `app/core/workers/chat_worker.py` + `plugins/system/providers/{baidu,dashscope,deepseek}.py`): 实现 token ratio 解析机制，统一各 provider 本地估算比例来源。
- **LLMSettingsCard 动态字族与标签渲染** (`app/widgets/cards/settings/llm_settings_card.py`): 字体族动态切换与标签渲染优化。
- **百炼（Bailian）控制台用量查询增强** (`plugins/system/providers/dashscope.py`): 用量查询新增额外配额字段（基于既有 fetcher 框架扩展）。

#### 🐛 问题修复 (Bug Fixes)

- **初始显示高度抖动修复** (`app/widgets/cards/floating/question_floating_widget.py`): 修正首次显示时高度计算，避免内容抖动（用户反馈）。
- **tooltip 父对象弱引用 + 定位改用父控件位置** (`app/widgets/simple_hover_tooltip.py`): `_HoverTooltipFilter` 父对象改弱引用，断开 filter↔父 引用环（避免 per-tab 泄漏）；定位改父控件位置，跨 DPI/主题对齐更稳；cleanup 兜底置 None。
- **禁用后台模型拉取避免窗口销毁崩溃** (`main.py`): 禁用后台 models.dev 拉取，避免后台线程在窗口销毁后回调到已删除 C++ 对象触发原生崩溃（`STATUS_STACK_BUFFER_OVERRUN`）。
- **资源管理与多 backend 清理** (`app/main_widget.py` + `app/widgets/cards/settings/history_card.py` + `app/widgets/message_card.py` + `app/widgets/simple_hover_tooltip.py`): 改进各组件资源管理与清理流程（懒加载 + 安全关闭），避免悬挂引用与延迟初始化空指针。
- **window_registry 弱引用重构** (`app/core/builtin_commands.py` + `app/core/window_registry.py` + `app/main_widget.py` + `app/widgets/cards/card_manager.py` + `app/widgets/cards/global_card_controller.py` + `app/widgets/cards/settings/gitee_card.py` + `app/widgets/tab_manager_window.py`): `window_registry` 改弱引用管理窗口实例，防止多 tab 切换/关闭时内存泄漏。
- **token ratio 估算统一为 1.0** (`app/core/token_estimator.py` + `plugins/system/providers/{baidu,dashscope,deepseek,gemini,minimax,siliconflow,volcengine}.py`): 基于 cl100k_base 验证将各 provider 的 token 比例统一为 1.0，避免本地估算与官方 API 偏差。
- **默认字号 key 与 superlarge 配置对齐** (`app/utils/design_tokens.py`): 默认字号 key 更新以匹配新 superlarge 配置。

#### ♻️ 代码重构 (Refactoring)

- **`vDockSplitter` → `chatVsplitter` 重命名** (`app/widgets/tab_manager_window.py`): 命名更清晰，与聊天垂直分割条语义一致；同步更新所有引用点。
- **`window_registry` 收敛窗口实例管理** (`app/core/window_registry.py` + `app/core/builtin_commands.py` + `app/core/team_manager.py` + `app/main_widget.py` + `app/widgets/cards/global_card_controller.py` + `app/widgets/cards/settings/gitee_card.py` + `app/widgets/pixel_pet.py` + `app/widgets/tab_manager_window.py`): 提取 `window_registry` 模块统一管理窗口实例；补 `test_main_widget_smoke` / `test_team_add_member` / `test_input_button_hot_reload` 测试。

#### 📚 文档 (Documentation)

- **Midnight Aurora 主题实验与归档** (`plugins/system/themes/midnight_aurora/` + `scripts/generate_midnight_aurora_sidebar.py` + `docs/superpowers/{plans,specs}/2026-08-25-midnight-aurora-theme*.md`): 实验性深色极光侧栏主题（YAML/PNG 资产 + 生成脚本 + 设计文档）；后续评估后整体移除（回滚实现、设计与测试），保留生成脚本与文档做未来参考。

#### ✅ 测试 (Tests)

- **Aurora PNG 颜色模式校验** (`tests/utils/test_midnight_aurora_sidebar.py`): 校验生成 aurora 渐变 PNG 的颜色模式与基础形状（与移除同步归档）。
- **插件变更钩子集成 + tab-manager 卡片关闭** (`tests/core/test_plugin_changed_hook_integration.py` + `tests/widgets/test_tab_manager_window.py`): 覆盖 `PluginChanged` 钩子在多 backend/多 tab 场景的广播；补 tab-manager 卡片关闭用例。

### 🔄 Hotfix 重新发布 (Re-release · 2026-08-25 · Round 3)

基于 `v0.5.4` Round 2 的增量变更 | 提交数：2 · 文件变更：24 · +3791/-26 | 贡献者：dingma

#### 🐛 问题修复 (Bug Fixes)

- **shimmer gradient 缓存导致 stop 累积脏色** (`app/widgets/message_card.py`): 移除未使用的 `_grad_shimmer` 模板缓存（`setColorAt` 持续追加 stop 会残留脏色，每帧 paint 仍新建 gradient，缓存不仅无效还可能造成误用）；补注释说明 stop 位置随相位连续变化必须每帧新建 gradient，修复 shimmer 流光拖影/脏色残留。

#### ⚡ 性能优化 (Performance)

- **shimmer 渐变按帧创建避免 stop 累积** (`app/widgets/message_card.py`): 每帧根据 `shimmer_pos` 连续变化新建 `QLinearGradient`，移除冗余缓存读取分支，减少 paintEvent 内对象引用与冗余路径。

#### ✅ 测试 (Tests)

- **长跑内存泄漏压测场景** (`tests/perf/long_run/` + `app/core/{backend,hook_manager,lsp/lsp_manager}.py` + `app/core/workers/subagent_worker.py` + `app/gateway/manager.py` + `app/main_widget.py` + `app/utils/drag_stall_profiler.py` + `app/widgets/{message_card,pixel_pet}.py`): 新增运行时采样器（RSS / QObject 计数 / tracemalloc 快照），三个压测场景（消息流压测 / 会话切换压测 / 插件热重载压测）+ pytest 阈值断言 + markdown 报告生成；`.gitignore` 忽略压测输出。
- **最小化复现脚本** (`tests/debug/memleak_repro/`): 消息流 / 信号定时器 / 综合 repro / 中途 verify 四个最小化复现脚本，便于内存泄漏根因定位。


### 🔄 Hotfix 重新发布 (Re-release · 2026-08-27 · Round 4)

基于 `v0.5.4` Round 3 的增量变更 | 提交数：9 · 文件变更：11 · +2281/-17 | 贡献者：dingma

#### ✨ 新功能 (New Features)

- **Qt 渲染器接入消息卡片（带回退）** (`app/widgets/message_card.py`): 引入 Qt 原生 `MarkdownBlockViewer` 渲染消息内容（性能优于 WebEngine），WebEngine 作为回退路径。新增 `markdown_block_viewer.py`（1767 行 Qt 块级渲染实现）。
- **`PlainTextViewer` 大文本高度计算优化** (`app/widgets/`): 大文本内容的高度计算优化，避免长消息初次渲染耗时过高。
- **`ToolCardWidget` 工具图标与参数预览** (`app/widgets/cards/`): 工具卡片新增工具图标加载与参数预览，让 LLM 调用结果更直观可读。
- **MessageCard 流式渲染节流性能回归测试** (`tests/widgets/test_message_card_streaming_resize_throttle.py`): 新增流式渲染 resize 节流的性能回归用例，避免回归导致卡顿。

#### 🐛 问题修复 (Bug Fixes)

- **ChatBackend 父路径解析** (`app/core/backend.py` + `app/utils/config.py`): resolve parent path correctly，避免插件注册时因路径拼接错误导致加载失败（`e730fdff`）。
- **Tooltip 隐藏守卫** (`app/widgets/simple_hover_tooltip.py`): 增加 cursor leave / app deactivate 时的 tooltip 隐藏守卫，避免关闭应用后还会触发 tooltip 显示导致崩溃或残留。
- **`LLMSettingsCard` Qt 渲染器描述冗余清理** (`app/widgets/cards/settings/llm_settings_card.py`): 移除 Qt renderer 描述中的冗余 text，简化文案。
- **插件热重载 Python hook 缓存** (`app/core/hook_manager.py`): 插件热重载时清除 Python hook 函数缓存，避免旧 hook 残留导致重复触发或错误响应。
- **插件 watcher 新装插件识别** (`app/plugins/contracts/plugin_config.py` + `app/widgets/cards/settings/plugin_config_card.py`): watcher 识别新装插件——未注册 manifest 在监控根下时交给 rescan 注入，修复新装插件立即可见但 manifest 未在监控列表内的情况。


### 🔄 Hotfix 重新发布 (Re-release · 2026-08-27 · Round 5)

基于 `v0.5.4` Round 4 的增量变更 | 提交数：1 · 文件变更：1 · +4/-3 | 贡献者：dingma

#### 🐛 问题修复 (Bug Fixes)

- **`ToolControlCard` 描述标签对齐** (`app/widgets/cards/settings/tool_control_card.py`): 移除冗余 `QSizePolicy` 设置，将描述标签的 stretch 改为 1，让 `_ElidedLabel` 吃掉 source/name 之后的全部剩余水平空间，把开关按钮推到行尾靠右对齐（原 SizePolicy 设置失效导致开关位置不固定）。

## [v0.5.3] - 2026-08-22

自上一版本以来的变更 | 提交数：175 · 文件变更：303 · +28058/-15698 | 贡献者：dingma, mading, drifox-bot, builder

### ✨ 新功能 (New Features)

- **models.dev 数据优先从 DriFox 镜像仓库拉取** (`app/core/models_dev_sync.py`): 优先从 DriFox 镜像仓库拉取 models.dev 数据，官方源作为保底；镜像不可达时自动降级官方源，提升服务商模型列表加载的可靠性。

- **UI 扩展点补全（Phase D）** (`app/plugins/registries/ui_plugin_registry.py` + `app/widgets/tab_panel.py` + `app/main_widget.py` + `app/widgets/message_card.py` + `app/widgets/cards/settings/llm_settings_card.py`): 新增四类 UI 扩展点——侧边栏项（`SidebarItemInfo`，与 floating card 解耦，存量兼容映射）/ 输入区按钮（`InputButtonInfo`，per-window 实例化 + 热重载经 `_on_plugin_hot_reload` 重建）/ 右键菜单项（`ContextMenuActionInfo`，统一聚合器注入 message_card/tab 菜单，enabled_func 置灰 + 返回 False 关菜单语义）/ 设置卡片（`SettingsCardInfo`，LLMSettingsCard 末尾插件分区滚动区，初始隐藏，打开时重建）。`unregister_plugin` 清理四类新注册（幂等）。UI 插件生态从「卡片 + 渲染」升级为「全区域可插拔」（E2E：临时插件文件注册四类 → 卸载全清）。
- **序列化单入口（Phase C）** (`app/plugins/contracts/message_serializer.py` + `app/core/workers/chat_worker.py` + `subagent_worker.py`): 新增 `SerializeResult`（messages/input_items/instructions）+ `MessageSerializer.serialize` 单入口（内部按 `ctx.flags.use_responses_api` 路由 chat/responses 形态）；worker 从「按协议形态调 3 个函数」收敛为 1 个入口，`ProtocolFlags.serializer_id` 真正被消费（adapter 可指定专属序列化器，默认 openai）；薄壳函数内部转发单入口（导出与调用形态不变）。
- **协议家族适配器拆分（Phase C）** (`plugins/system/model_adapters/`): 单适配器拆为三家族——`openai-family`（matches=1 兜底）/ `gemini-family`（2）/ `deepseek-family`（3），判定器共享 `_detectors.py`（`_` 前缀不当作插件），`resolve` 取最高分；flags 与拆分前逐点等价（等价矩阵 13 用例全过）。
- **UI 层存储收口（Phase C）** (`app/core/backend.py` + `plugins/system/storages/sqlite.py`): `backend.session_store` 切到 `StorageRegistry.get_active()`（冷启动防御复用门面 warmup）；SQLite 引擎补 SessionStore 兼容方法集（`clear_old_subagent_tasks / force_cleanup_project / record_file_operation` 等，委托内部 SessionStore 单例），main_widget 与 FileOperationRecorder 调用点零改动迁移，db 连接不分叉。
- **消息序列化插件化（Phase B）** (`app/plugins/contracts/message_serializer.py` + `app/plugins/registries/serializer_registry.py` + `plugins/system/serializers/openai.py`): 新增 `MessageSerializer` 契约（`serialize_messages` ≡ 旧 `messages_to_api` / `serialize_responses` ≡ 旧 `messages_to_responses_input`）+ `SerializeContext` + `SerializerRegistry` 单例（按 id 解析，回退 `openai`）；`message_content` 三函数变薄壳委托（签名与导出不变，调用点零改动，行为逐点等价）；`ProtocolFlags` 扩展 `serializer_id` 字段（本阶段只立不消费，走覆盖式替换）；kernel/plugin_manager/reloaders/plugin.json 登记 serializers 组件（热重载 watcher 生效）。插件可替换消息序列化行为（E2E 验收：user 根覆盖 system 默认 openai）。
- **存储契约能力接口（Phase B）** (`app/plugins/contracts/storage.py` + `plugins/system/storages/sqlite.py`): 新增可选能力接口 `SessionTitleCapability` / `SessionCountsCapability` / `InputHistoryCapability`（消费方 `isinstance` 探测，无能力安全降级）；SQLite 引擎声明实现并覆盖消费方方法（`save_session/get_sessions_lightweight/get_session_count/update_session_project/archive_sessions_by_project` 等委托 SessionStore，行为零变化）；backend 新增 `get_session_storage()` 门面（注册表空时幂等加载系统插件），history_manager / memory_manager / session_handler 改走门面（UI 层消费点迁移属 Phase C）。
- **冷启动回归修复（Phase B）** (`app/core/workers/chat_worker.py` + `subagent_worker.py`): ModelAdapter resolve 空注册表时幂等触发系统插件扫描再重试（仍空才抛错），`tests/test_reasoning_content_required.py` 恢复全绿。
- **运行时三接口插件化（Phase A）** (`app/plugins/` + `app/core/workers/chat_worker.py`): 「万物即插件」运行时层——新增 `contracts/`（ModelAdapter / LoopPolicy / SessionStorageEngine 契约）+ 三注册表 + 系统插件默认实现（`plugins/system/{model_adapters,loop_policies,storages}/`，行为与旧 `builtin_runtime` 逐点等价）；chat_worker 协议检测与循环判定委托注册表（行为零变化）；kernel 登记三个新组件类型，插件可替换模型适配/循环策略/存储引擎（附 minimal 极简策略验收插件）。
- **服务商插件化（providers 组件）** (`app/plugins/` + `plugins/system/providers/`): 「万物为插件」——服务商支持全面插件化。新增 `app/plugins/registries/provider_registry.py`（ProviderDef + ProviderRegistry 单例，聚合 models/icon/default_config/quota_keys/models_dev_map/family_caps/余额/套餐用量查询）+ `app/plugins/loaders/provider_loader.py`（扫描 `plugins/*/providers/*.py` + 热重载 watcher + user 覆盖 system + 插件启用过滤 + 插件图标目录注入）。PluginManager 组件检测新增 providers 组件
- **14 家内置服务商迁移为系统插件** (`plugins/system/providers/`): DeepSeek/SiliconFlow（余额查询 fetcher）、MiniMax/智谱AI/OpenAI/火山方舟/OpenCode Zen/OpenCode Go（套餐用量 fetcher 逻辑迁入插件），Anthropic/Gemini/Groq/Ollama/百度千帆/阿里云 纯数据声明；用量查询额外字段（server_id/cookie/workspace_id/csrf_token/x_web_id）随插件声明并在编辑卡片动态渲染
- **服务商图标插件化** (`app/utils/provider_icons.py`): 新增 `get_provider_icon`——插件自带图标目录（`providers/icons/` 深色 + `icons_light/` 浅色，主题感知）优先，缺省回退 qrc；与 tools 图标机制对称
- **服务商注册表懒加载预热** (`app/plugins/registries/provider_registry.py`): `ensure_loaded()` 幂等预热，兼容启动早期 Settings → opencode 免费模型注入链路
- **Gateway 平台插件化（E2）** (`app/gateway/` + `plugins/system/gateways/` + 社区仓 drifox-plugins2): 6 个内置平台适配器迁出主程序，新增 `GatewayPlatformDef` 契约与 `GatewayPlatformRegistry` 注册表（零平台 if 分支）；Platform 枚举 str-mixin 化打通第三方平台 id；设置卡 registry 驱动自动渲染；Telegram 试点全链路迁出主程序，行为等价
- **插件配置契约（E1）** (`app/plugins/` + `plugins/system/`): 新增 `PluginConfigStore`（env→存储→默认三级链）+ `PluginConfigRegistry`（`config_schema` 声明式渲染卡）+ 统一存储 `<app_data>/plugins/<plugin>/config.json`；websearch 工具/配置卡零主程序改动迁移到自包含配置
- **性能基准测试套件** (`benchmarks/`): 新增 memory/import/startup/session-leak 基准脚本，用于回归对比与性能监控

- **插件热重载增强（事件编排 + 活跃窗口 UI 刷新）** (`app/core/backend.py` + `app/main_widget.py` + `app/plugins/registries/ui_plugin_registry.py` + `app/widgets/message_card.py` + `tests/`): 热重载事件按序编排（event sequencing），重建后主动刷新活跃窗口 UI（输入区按钮/右键菜单/设置卡经 `_on_plugin_hot_reload` 重建）；backend 调度与 config_sync 协同；新增 `tests/plugins/test_input_button_hot_reload.py`（117 行）等收敛热重载回归
- **AutoLoop 完全插件化** (`plugins/autoloop/`): AutoLoop 对话引擎从 `app/core/` 迁移至独立插件 `plugins/autoloop/`，主程序移除 `AutoLoopController` 实现（仅保留 11 行薄壳委托），对话卡与运行卡随插件自带 UI 资源加载；运行卡生命周期（懒创建时序 + 作用域绑定）改由插件自治。
- **浮动卡 hide_sidebar 元数据** (`app/plugins/registries/ui_plugin_registry.py`): `FloatingCardInfo` 新增 `hide_sidebar: bool` 字段，autoloop 等双卡型插件声明 `hide_sidebar=True` 后，对应的对话卡 / 运行卡不进入侧边栏聚合列表，避免侧边栏出现冗余入口。
- **浮动卡 per-tab 可见集合投影** (`app/plugins/registries/ui_plugin_registry.py` + `app/widgets/tab_panel.py`): 浮动卡可见集合按当前 tab 投影，切换 tab 时仅渲染该 tab 注册的浮动卡，避免跨 tab 状态串扰与不必要的卡片驻留。

### 🐛 Bug 修复 (Bug Fixes)

- **system-cleaner 插件版本与缓存目录处理** (`plugins/system-cleaner/.drifox-plugin/plugin.json` + `plugins/system-cleaner/ui/scanner.py`): 插件版本升至 0.1.1，增强缓存目录处理健壮性，修复扫描器在缓存路径下的异常行为。

- **PyInstaller 打包缺失懒加载模块致启动崩溃** (`build.py`): `app.core` 使用 PEP 562 懒加载（`__getattr__` + `importlib.import_module` 动态字符串导入），PyInstaller 静态分析无法发现，需在 `build.py` 的 `_hidden_imports` 用 `collect_submodules("app.core")` 显式收集（含 `app.core.workers.topic_summary` 等）。修复前打包后运行报 `ModuleNotFoundError: No module named 'app.core.workers.topic_summary'`。已补入 build.py 并重发 v0.5.3。
- **新建项目 NameError 中断链** (`app/main_widget.py`): 补 `TAB_KEY_DOCUMENTS` 局部导入，恢复新建会话/Tab 图标同步。
- **关键文档项根目录按钮位置** (`app/widgets/`): 根目录按钮从行尾移到行首（icon 之前）。
- **团队构建继承构建者标签页模型** (`app/main_widget.py`): 恢复时还原成员最后使用的模型。
- **file-tree 右键「在资源管理器中打开」层级错位** (`plugins/file-tree/ui/cards.py`): 目录原本用 `explorer /select,` 会打开父目录并高亮（层级往外多一层），改为 `explorer <dir>` 直接打开该目录；文件仍用 `/select,` 打开所在文件夹并选中；上层对文件不再取 `dirname`（否则传入目录再次退回父目录）。macOS/Linux 同步按目录/文件分派。
- **Gateway 连接泄漏修复** (`app/manager/` + `app/core/`): 卸载/重装 gateway 插件先关闭平台连接并清理模块引用，热更新重建 adapter 生效；adapter 实例清理防连接泄漏；builtin_reloaders 等待平台 stop 再 purge/rebuild
- **幽灵窗口根因修复** (`app/widgets/`): 卡片销毁路径 `setParent(None)` 前先 `hide`；欢迎卡片 `_is_effectively_visible` 遍历全部 QStackedWidget 层级解决幽灵窗口
- **团队 auto-compact 竞态** (`app/core/team/`): 修复 auto-compact 清空与团队邮件重发/子智能体回调竞态导致回复丢失
- **安装器锁定文件处理** (`app/plugins/marketplace/` + installer): 卸载/安装时 robust_move 重定位被锁 `.pyd` 文件并抑制 watcher，修复首次 WinError 5；plugin-marketplace 修复 QFileDialog 改变 cwd 致缓存写入 FileNotFoundError
- **启动时运行卡未绑定即中止** (`plugins/autoloop/`): 修复启动序列中运行卡在尚未绑定到会话时即触发中止逻辑的时序 bug——改为懒创建（lazy create）+ 会话作用域（session-scoped binding），首次访问会话时才创建并绑定，避免启动期空绑定导致的误终止。
- **用户气泡宽度与主题颜色透明度** (`app/widgets/message_card.py`): 调整用户气泡最大宽度并优化主题色透明度变量，在浅色/深色主题下均有更舒适的视觉对比度。
- **对话框自适应与插件精确加载** (`app/widgets/dialogs.py` + `app/plugins/`): 对话框自适应尺寸与内容拟合；单插件 unload/reload 不影响其余工具，轮询 watcher 退役并入 watchfiles 主链

### ♻️ 代码重构 (Refactoring)

- **openai 适配器拆协议家族（Phase C）** (`plugins/system/model_adapters/`): 旧单适配器 `openai.py` 删除，拆为 `openai_family.py / gemini_family.py / deepseek_family.py` + 共享判定器 `_detectors.py`；worker 序列化调用全部收敛到 `_serialize_for_api` 单入口（chat_worker 9 处 + subagent_worker 2 处）。
- **openai 适配器判定器拆分（Phase B 铺路）** (`plugins/system/model_adapters/openai.py`): 三个 `_method` 拆为模块级纯函数 `detect_is_gemini / detect_requires_reasoning / detect_use_responses`（逻辑逐字搬运，行为零变化），`protocol_flags` 组合之，为协议家族适配器铺路。
- **插件体系收口 `app/plugins` 独立包** (`app/plugins/`): 插件相关代码从 `app/core/` 与 `app/tools/` 迁入 `app/plugins/managers/`（PluginManager）、`registries/`（ProviderRegistry/UIPluginRegistry/coding_plan_fetcher）、`loaders/`（plugin_tool_loader/provider_loader），按职责分子目录
- **服务商硬编码全部移除** (`app/constants.py` 等): 删除 `PROVIDER_MODELS`/`FREE_PROVIDERS`/`PROVIDER_ICONS`/`QUOTA_EXCLUDE_KEYS` 常量（保留函数委托）；`MODELS_DEV_PROVIDER_MAP`/`PROVIDER_CAPABILITIES`/`BALANCE_APIS`/`coding_plan_fetcher` 注册表全部迁移至 ProviderRegistry 聚合（后两者变薄壳委托）；消费方（usage_service/balance_display/model_capabilities/models_dev_sync/provider_profile/workers/UI 卡片/main_widget/config/cli）全部改读注册表
- **opencode 免费模型注入保留** (`app/utils/config.py`): `_ensure_default_opencode_provider` 逻辑不变，数据源从 `FREE_PROVIDERS` 改为注册表 `OpenCode Zen` 插件定义，回归测试通过
- **Gateways 其余 5 平台迁出主程序** (`app/gateway/`): manager/config 零平台分支，平台插件定居社区仓 drifox-plugins2
- **插件体系收口 `app/plugins` 独立包** (`app/plugins/`): backend/worker 去 `ensure_builtin_*` 调用，依赖系统插件加载；轮询 watcher 退役，tools/providers 变更并入 watchfiles 主链

- **gitignore 处理简化与用户配置尊重** (`plugins/system/hooks/format_memory_context.py`): 重写 `.gitignore` 处理逻辑，尊重用户既有配置，移除冗余分支（净 -82 行）
- **autoloop 图标统一复用主程序 `无限.svg`** (`plugins/autoloop/`): 删除插件自带的 `无限.svg` 副本，统一引用主程序 `resources/icons/`，消除图标重复维护。
- **AGENTS.md 精简执行路径** (`AGENTS.md`): 梳理项目规范与命令速查，移除冗余描述并强化关键约束（铁律/插件化/提交规范）。

### ⚡ 性能优化 (Performance)

- **模块懒加载** (`app/`): 实施模块懒加载降低导入耗时，改善启动与冷启动性能

### 📚 文档 (Documentation)

- **服务商插件开发指南** (`plugins/system/providers/README.md`): ProviderDef 字段、查询函数签名、额外配置字段机制、旧硬编码迁移对照表
- **系统插件声明 providers 组件** (`plugins/system/.drifox-plugin/plugin.json`)
- **Gateway 断连纪律** (`docs/gateway/`): 适配器断连清理协议——lark SDK 无停止机制的泄漏教训与连接泄漏修复记录
- **AGENTS.md 同步** (`AGENTS.md`): 同步插件配置契约（E1）与 Gateway 平台插件化（E2）说明；hooks caveman 压缩追问预测 prompt 降 token

### 🔧 其他 (Chores & Build)

- **测试**: 新增 `tests/core/test_provider_registry.py`（8 用例：注册/聚合/余额/用量/系统 14 家加载）；更新 `test_models_dev_sync.py`/`test_default_opencode_provider.py`/`test_provider_icon_widget.py`
- **插件市场自动重建** (`plugins/`): `chore(marketplace): auto-regenerate from plugin.json [skip ci]` 重复提交
- **Py2 except 残留括号化** (`app/` + `plugins/`): dev 长期遗留的 Python 2 风格 `except X, Y:` 语法（52 处，零业务语义）在某些语法高亮/解析器下损坏，统一改为 `except (X, Y):` 括号写法以提升可读性与跨工具兼容性。
- **AutoLoopController 实现移除** (`app/core/team/`): 配合 autoloop 插件化，从 `controller.py` 移除 `AutoLoopController` 实现类，逻辑全部下沉到 `plugins/autoloop/`，主程序仅保留 11 行薄壳委托。

#### 📦 附加变更（v0.5.3 重发补丁 | 自初次 tag 之后 24 个新提交）

> v0.5.3 初次 tag（`6a6e49c4` "re-release with autoloop plugin + late fixes"）后追加的优化与修复：对话引擎（Engines）插件化体系打通、`cache` 命中率口径与服务商对齐、`config_schema` 字段类型扩展、UI 细节优化与文档补全。

##### ✨ 新功能 (New Features, 14)

- **对话引擎（Engines）插件化体系打通** (`app/plugins/contracts/` + `app/core/conversation/stack_factory.py` + `app/core/backend.py` + `app/core/isolated_context.py` + `app/plugins/registries/` + `app/core/kernel.py` + `app/core/plugin_manager.py` + `app/plugins/loaders/`): 新增引擎工厂契约 `EngineFactory` + `ClassEngineFactory`、`EngineRegistry` 单例 + `create_engine_for_slot` 实例化入口（`isinstance` 安全网）、`EngineHost` Protocol 服务面语义契约 + 漂移守卫、`ConversationStackFactory` 执行栈构建面契约；后端/isolated_context 的 ChatEngine 经工厂创建（单例语义保留），加载器经 `_make_engine_loader + ensure_engine_watcher + warmup` 接入 engines 组件；kernel.PROBES + builtin_reloaders 完成组件类型登记；gateway 槽位经工厂接入（单例语义保留）。补 `plugins/runtime-engines.md` 端到端开发指南。
- **UI 引擎槽位选择卡** (`app/widgets/cards/settings/llm_settings_card.py` + `app/utils/config.py`): 设置页展示/选择各槽位引擎（选择持久化，消费后补）
- **ui-services 加 conversation_stack 入口（EP2 主仓前置）** (`app/core/conversation/stack_factory.py` + `app/main_widget.py` + `app/plugins/contracts/engine_host.py`): 执行栈服务面经 `EngineHost.conversation_stack` 暴露
- **`hide_floating_card_globally` 公开 API（EP6）** (`app/plugins/registries/`): 插件隐藏浮动卡片无需再触碰 `_card_manager / _window_id` 内部细节
- **`config_schema` 扩展 `select` / `number` / `textarea` 字段类型（L2）** (`app/plugins/contracts/plugin_config.py` + `app/widgets/cards/settings/plugin_config_card.py`): 自动卡字段文字换 `BodyLabel` 随主题刷新，`textarea` 用主题感知 `TextEdit`，`select` 用自绘 `ComboBox`；兼容旧 `text / password / bool` schema 零迁移
- **简洁模式下助手消息气泡悬浮动作按钮** (`app/widgets/message_card.py`): `reduced mode` 下 `assistant` 消息气泡 hover 浮现动作按钮
- **移除独占模式的新建会话/发送消息软件级拦截** (`app/main_widget.py`): 移除独占模式下对新建会话/发送消息的软件级拦截，回归自然行为

##### 🐛 问题修复 (Bug Fixes, 6)

- **缓存命中率口径与服务商对齐** (`app/core/workers/cache_tracker.py` + `app/core/workers/chat_worker.py` + `app/main_widget.py` + `app/plugins/registries/provider_registry.py`): 修复启发式误判（非白名单前缀模型真实 99% 命中被估算拉低至 70%）与刷新不及时（工具循环期间显示旧快照）。根因：模型名前缀白名单误判 + 虚构 `cache_write`；`backend` 旧快照优先于活 worker。统一口径 `read / (read + uncached_input)`；`ProviderDef` 新增 `usage_semantics / usage_normalizer` 钩子供插件自定义 usage 解析
- **deepseek-v4 思考强度不生效** (`app/core/models_dev_sync.py`): `reasoning_options` 同时含 `toggle + effort` 时 `effort` 优先，`thinking_param` 统一 `reasoning_effort`，修复参数卡/输入区无思考强度调节且请求只发 `thinking` 布尔的问题
- **输入区插件按钮图标随深浅主题切换** (`app/main_widget.py` + `app/plugins/registries/ui_plugin_registry.py`): `InputButtonInfo` 增加 `icon_light_path`，构建/刷新按主题选图标
- **dock 停靠区按卡片独立记忆 splitter 尺寸** (`app/widgets/cards/card_container.py`): 修复 tab 切换折叠重开后宽度恢复默认的问题。根因：横向恢复逻辑记忆 < natural 时丢弃回默认（拖窄必丢）；`_dock_last_size` 容器级单值（不同插件卡片互相覆盖）。修复：`_dock_card_sizes` 按 `card_id` 记忆，`splitterMoved` 实时写入可见卡片，折叠兜底记忆到最后可见卡片；LEFT/RIGHT 宽度 + BOTTOM 高度均生效
- **横向 dock 最小宽度调整与异常语法修正** (`app/widgets/cards/card_container.py`): 调整水平停靠区最小宽度，修正遗留的 Python2 `except X, Y:` 异常处理语法
- **预览模式宽度上限随尺寸更新** (`app/widgets/message_card.py`): 修复预览模式下窗口 resize 时宽度上限未及时更新导致文本截断的问题

##### ♻️ 代码重构 (Refactoring, 3)

- **改进短消息气泡宽度计算** (`app/widgets/message_card.py`): `bubble width calculation` 优化短消息宽度估算
- **版本与状态标签清理 emoji** (`plugins/plugin-marketplace/ui/cards.py` + `plugins/plugin-marketplace/ui/proxy.py`): 移除版本号/状态标签中的 emoji 字符，显示更整洁
- **删除未使用的 `EngineSlotCard`** (`app/widgets/cards/settings/engine_slot_card.py`): 已由 `LLMSettingsCard` 集成，统一设置入口

##### 📚 文档 (Docs, 3)

- **`runtime-engines.md` 文档补全** (`docs/plugins/runtime-engines.md`): 补 `conversation_stack` 服务入口 + `EngineHost` / `ConversationStack` 契约文件入相关表；补 gateway 槽位/契约/选择卡说明 + 修 Task 8 收尾遗留测试；对话引擎插件开发指南 + 架构文档补 engines 一行

##### 🔄 其他变更 (Other, 1)

- **引擎插件端到端测试** (`tests/plugins/`): 引擎插件 e2e 测试覆盖扫描/替换/安全网回退/卸载场景

#### 📦 附加变更（v0.5.3 二次重发补丁 | 自上一 tag d488ea4d 之后 4 个新提交）

> v0.5.3 上一 tag（`d488ea4d` "docs: add v0.5.3 re-release changelog"）后追加的 bug 修复：message_card 编辑工具结果后补全框、tab_panel 手动折叠/归档后 icon 刷新、card_container 窗口缩放后 dock 记忆。

##### 🐛 问题修复 (Bug Fixes, 4)

- **编辑工具结果后补全框消失** (`app/widgets/message_card.py` + `app/main_widget.py`): 编辑工具结果后确保 lazy markdown 回调被重置，防止补全框消失（新增 `tests/widgets/test_message_card_edit_tool_stop_swallow.py` 回归）
- **手动折叠后拉宽窗口自动退出折叠** (`app/widgets/tab_panel.py` + `app/widgets/tab_manager_window.py`): 修复手动折叠（点按钮）后拉宽窗口/面板自动退出折叠模式的问题（新增 `tests/widgets/test_tab_manager_window.py` 回归）
- **归档项目后左侧 tab 项目 icon 未刷新** (`app/main_widget.py`): 归档当前项目后同步刷新左侧 tab 项目 icon（新增 `tests/widgets/test_tab_project_icon_new_project.py` 回归）
- **窗口缩放后 dock 记忆丢失** (`app/widgets/cards/card_container.py`): splitter 几何变化补恢复记忆尺寸，修复窗口缩放后 dock 记忆丢失（新增 `tests/widgets/test_card_container_dock_card_size_memory.py` 回归）

## [v0.5.2] - 2026-08-17

自上一版本以来的变更 | 提交数：50 · 文件变更：373 · +19359/-10585 | 贡献者：dingma, mading

### ✨ 新功能 (New Features)

- **工具系统插件化** (`plugins/system/tools/` + `app/tools/`): 实施"工具即插件"架构，34 个内置工具全部迁移为系统插件 `plugins/system/tools/*.py`，通过 `register(registry)` 统一注册 schema/impl/icon/cn_name/danger/group/description/aliases；registry 单一数据源驱动 LLM schema、消息渲染、权限卡片、ToolNameMapper 别名；支持文件增删改自动热插拔/热更新（跨根优先级保护 + 轮询检测）
- **工具逻辑自包含** (`plugins/system/tools/`): 纯逻辑工具（文件 9 个/网络 2 个/桌面 3 个）impl 用标准库/第三方库独立实现（mtime 检测、unified diff、图片 base64、命令安全等），不依赖主程序 BuiltinTools；平台工具（bash/subagent/MCP/LSP/CodeGraph/团队/todo/question/skill/上传）通过 `tool_ctx["services"]` 能力接口调用（不暴露 BuiltinTools 内部）；图标随插件（`tools/icons/` 深色 + `tools/icons_light/` 浅色，主题感知 data URI 加载）
- **四类系统工具插件齐套** (`plugins/system/tools/`): subagents / tasks / terminal / web utilities 完整实现（含 mtime 检测、unified diff、图片 base64、命令安全、JOB 进程树、紧急停止热键、reactive diff 渲染），主程序对应实现全部下沉
- **工具权限卡片动态分组 + 源标签** (`app/widgets/cards/settings/tool_control_card.py`): 分组与描述、源标签、社区/系统徽章全部从 registry 读取（功能域分组，危险工具 🔥 标记 + 组内危险在前），registry 热更新自动重建卡片
- **registry 变更信号 + 防抖重建** (`app/widgets/cards/settings/tool_control_card.py`): 新增 registry 变化信号，UI 接收信号后防抖重建（避免热重载时频繁刷新）
- **单工具权限策略 + UI 集成** (`app/core/tool_executor.py` + `app/widgets/cards/settings/tool_control_card.py`): 单工具 ask/always/deny 权限模型，UI 卡片可视化编辑
- **工具热重载风险提示** (`app/widgets/cards/settings/tool_control_card.py`): 工具热重载后弹出 MaskDialog 风险提示，可永久关闭
- **插件市场 + 系统插件保护** (`app/plugins/marketplace/`): 新增 `drifox-system` 市场源，系统插件在"已禁插件"列表中显示禁用按钮（普通插件移除此按钮，避免误卸载系统插件）
- **插件市场统一生成** (`app/plugins/marketplace/`): 整合 drifox-system 源与社区插件源，新增插件更新机制及 UI 交互
- **drifox-system 市场源** (`app/plugins/marketplace/`): 新增系统市场源，区别于社区插件源，便于集中管理
- **团队解散空白会话守卫** (`app/core/team/`): 团队解散时检测空白会话，拦截并保存有效会话逻辑
- **ToolExecutor / IsolatedChatContext todo 增强** (`app/core/tool_executor.py` + `app/core/engines/`): 改进 todo 状态管理与错误处理，状态迁移到 `app/tools/`
- **window_state 服务** (`app/tools/services/`): 通用窗口隔离状态服务，工具可注册窗口作用域状态（替换硬编码全局状态）
- **render_mode expand** (`plugins/system/tools/`): 工具新增 `expand` 渲染模式，禁用折叠统一渲染；render/preview/summarize 闭包完整迁移到插件
- **message_card 增量渲染** (`app/widgets/message_card.py`): 流式输出长段落软边界检测；流式渲染时 tail inline 即时刷新；不再阻塞 diff-render 在 stale tool_dom_dirty
- **Windows Job Object 进程树管理** (`app/tools/process_job.py`): 创建 → kill-on-close → 子进程入 Job 的进程树容器；`command_safety.run_safe/run_with_shell` 新增可选 `job=` 参数，命令启动后自动入 Job（S3）
- **后台任务 Job 杀树 + 事件广播** (`app/tools/terminal_tools.py`): `BackgroundTaskManager.stop` 优先用 Job Object 杀进程树（内核级），新增 `on_task_event` 事件广播（started/stopped/completed），UI 可观测（S4）
- **持久 Shell 会话** (`app/tools/pty_session.py`): Windows ConPTY（pywinpty）交互式会话，cwd/env/函数跨调用保留，超时可配置（默认 300s）；生命周期挂靠 ProcessJob kill-on-close（S5）
- **工具结果截断** (`app/core/context_builder.py`): 超过 8192 字符的工具输出保留头 4096 + 尾 1024，中间省略标记（DSH tool-result-pruner 对齐）；仅在发送给 LLM 的上下文层裁剪，会话原始存储不受影响（S1）
- **上下文用量投影对齐** (`app/core/engines/ui/engine.py` + `app/widgets/context_usage_ring.py` + `context_usage_tooltip.py`): 用量快照按截断后口径估算（与实际发送一致），环形图 tooltip 显示「工具结果截断节省 X tokens」（S2）
- **视觉工具集成增强** (`app/core/tool_executor.py` + `plugins/system/tools/`): 视觉工具完善图片处理与测试，新增图像处理能力
- **文件树增量刷新** (`app/widgets/file_tree.py`): 文件树模型实现子节点与根节点的增量刷新，保留节点状态（展开/选中/滚动）
- **多团队支持 TeamManager** (`app/core/team/team_manager.py` + `app/widgets/team_management.py`): 实现多团队创建/切换/解散；团队管理 UI 适配多团队场景，TeamManager 状态机与成员表同步

### 🐛 问题修复 (Bug Fixes)

- **QMenu 菜单项样式** (`app/widgets/`): 调优 QMenu 菜单项样式，修复在浅色/深色主题下可见性差的问题（**v0.5.2 重发修复**）
- **plugin-marketplace 系统插件禁用按钮** (`app/plugins/marketplace/`): 为 `system-cleaner` 等系统插件暴露禁用按钮，修复此前无法停用的问题
- **tool control card SysShadow 幽灵窗口** (`app/widgets/cards/settings/tool_control_card.py`): 修复 SysShadow 幽灵窗口 + 策略下拉懒创建
- **tool control card 双重重建 + 重建卡顿** (`app/widgets/cards/settings/tool_control_card.py`): 修复幽灵窗口、registry 变化双重重建；防抖优化重建性能
- **command card 可见项计算** (`app/widgets/cards/settings/command_card.py`): 调整可见项计算逻辑，避免过多空白；分割线高度与项数/分割线对齐
- **tool reload notice 元组化** (`app/widgets/cards/settings/tool_control_card.py`): 拖尾逗号导致"工具重载通知"内容变为元组
- **BodyLabel 旧版兼容** (`app/widgets/`): 用 `BodyLabel(parent)+setText` 兼容旧版 qfluentwidgets
- **团队成员批量创建后激活首个 tab** (`app/widgets/team_management.py`): 修复批量创建团队成员后未激活第一个成员 tab
- **tab manager 项目图标同步** (`app/widgets/tab_manager_window.py`): 新建项目后同步 tab 项目图标
- **团队消息发送者角色** (`app/core/team/`): 修复团队消息中发送者角色与成员表不一致
- **tools 窗口隔离 todo 状态恢复** (`app/tools/services/`): 恢复 window-isolated todo 状态（M5）
- **tools inline diff 渲染恢复** (`app/tools/` + `app/widgets/`): 恢复 inline diff 渲染，状态迁移到 `app/tools/`，权限参数统一化
- **message_card 软边界检测** (`app/widgets/message_card.py`): 长段落软边界检测，增强增量渲染
- **message_card 流式 tail inline** (`app/widgets/message_card.py`): 流式渲染时由 diff 路径切换回 inline tail 即时刷新
- **message_card 解除 stale tool_dom_dirty 阻塞** (`app/widgets/message_card.py`): 解除 stale tool_dom_dirty 对 diff-render 的阻塞，让流式 body 及时刷新为 html
- **message_card finished tool id 误判** (`app/widgets/message_card.py`): 停止将已完成的 tool id 视为 active tool DOM，恢复流式 diff-render
- **context/tools pruned_tokens 联动** (`app/core/context_builder.py` + `app/widgets/context_usage_ring.py`): 将 pruned_tokens 接入环形图 tooltip；后台任务 completed 状态守卫
- **pty session 生命周期挂靠** (`app/tools/pty_session.py`): pty 会话生命周期挂靠 ProcessJob kill-on-close（S5）
- **subagent/title 默认模型服务商记录歧义** (`app/main_widget.py`): 修复通过命令卡枚举选择「服务商名+模型名」配置 `/subagents`、`/title-gen` 默认模型时，保存的是 `provider_name` 而非 `display_name`；存在两个同 `provider_name` 的配置（如两个 OpenCode Zen：`df810bab` 与当前主用 `68ea6d92`）时，重新解析因 `_resolve_service_provider` 按 `display_name` 优先匹配，歧义命中错误的 config——表现为「用的服务商与保存记录的不一致」。保存改为使用 `display_name`（与枚举 value 一致且唯一），闭环无歧义

### ♻️ 代码重构 (Refactoring)

- **tools diff 渲染全插件驱动** (`plugins/system/tools/` + `app/tools/`): diff 渲染完全由插件驱动，移除主程序 fallback
- **tools 权限/总结/保护/交互迁插件 registry** (`plugins/system/tools/`): permission/summarize/protect/interactive 逻辑全部迁移到 plugin registry
- **tools 消除硬编码工具名渲染分支** (`plugins/system/tools/`): 消除所有硬编码工具名渲染分支，全部使用 render/preview 闭包
- **task/team 工具迁插件架构** (`plugins/system/tools/`): 任务与团队工具全部迁移到插件架构
- **mcp_list_servers 工具分组迁移** (`plugins/system/tools/`): `mcp_list_servers` 工具分组从原位置迁移到 interaction & skills
- **移除废弃 automation 工具插件** (`plugins/system/tools/`): 移除已废弃的 automation 工具插件（410 行删除）

### 📚 文档 (Documentation)

- **plugin-creator 渲染闭包文档** (`docs/plugins/plugin-creator.md`): 文档化 render/preview/summarize 闭包、`render_mode=expand`、metadata flags、group capability 语义
- **S1-S5 Unreleased 变更记录** (`CHANGELOG.md`): 增补工具截断/用量投影/Job 杀树/pty 会话等 S1-S5 阶段的 Unreleased 变更记录

### 🔧 其他 (Chores & Build)

- **废弃 automation 工具插件** (`plugins/system/tools/`): 移除已废弃的 `tools/automation_tools.py`（被 process_job + 插件化实现取代）
- **依赖移除**: `app/tools/__init__.py` 中静态 `TOOL_SCHEMAS`（~860 行）删除，schema 聚合改读 ToolRegistry（版本号驱动缓存失效）
- **web 搜索 token 迁移至环境变量** (`app/utils/config.py` + `app/core/tool_executor.py` + `plugins/system/tools/web_tools.py`): `websearch` 的 `TAVILY_API_KEY`/`TINYFISH_API_KEY` 不再存储于应用配置（config.py 硬编码默认值移除），改由环境变量提供；未设置时 `websearch` 返回「搜索失败：无可用搜索引擎」
- **依赖新增**: `pywinpty>=3.0.5; sys_platform == 'win32'`（持久 shell 会话基础，S5）
- **测试**：新增 `tests/test_tool_plugin_system.py`（19 用例：registry/系统插件/热插拔/渲染/权限联动）+ `tests/test_codegraph_plugin_contracts.py`（codegraph 插件加载与错误处理契约测试）+ `tests/test_plugin_system_isolation.py`（插件系统窗口状态隔离增强）
- **版本号**: v0.5.1 → v0.5.2（`pyproject.toml` + `app/utils/config.py` + `dist/installer.iss` + `README.md` 同步）

### 🚑 Hotfix (2026-08-17 第二次重发布)

> **重大缓存失效 bug 修复**：chat_worker 工具迭代路径（发送全量）与 `build_messages` 路径（S1 截断）对同一工具结果产生不同 content，导致 prompt 前缀分叉、API 提示词缓存命中失效。

#### ✨ 新功能 (New Features)

- **API 工具内容截断统一** (`app/core/message_content.py`): 在 `to_api_message` 与 `messages_to_responses_input` 转换层统一调用 `prune_tool_result`（S1 截断，与 `context_builder.build_messages` 同参数），消除两条发送路径对同一工具结果的字节分叉；`build_messages` 已截断内容 < 阈值时再截断为 no-op，无副作用；修复 API 缓存命中失效（`6e98e1fe`）

#### 📚 文档 (Documentation)

- **README + leader.md 表述优化** (`README.md` + `docs/leader.md`): 文档表述优化与功能描述补充（`57f885eb`）

### 🚑 Hotfix (2026-08-18 第四次重发布)

> **自上一 tag (v0.5.2 `d5f65d33`) 以来的变更** | 提交数：12 · 文件变更：28 · +2449/-2182 | 贡献者：dingma, mading, drifox-bot

#### ✨ 新功能 (New Features)

- **Marketplace 探索模式（网格卡片）** (`plugins/plugin-marketplace/`): 新增 `_ExploreCard`（固定宽度垂直布局网格展示插件）+ `_ExploreGridSection`（分类插件网格分组）；`MarketplaceCard` 默认打开 featured explore 页并带刷新随机化分类；列表/探索视图插件状态同步（安装/更新时 UI 一致更新）（`2ef28807`）
- **模型推理能力动态支持（reasoning_effort_values）** (`app/core/model_capabilities.py` + `app/core/models_dev_sync.py` + `app/core/usage_service.py` + `app/widgets/cards/settings/model_config_card.py` + `app/widgets/cards/settings/model_selector_card.py`): 模型能力表新增 `reasoning_effort_values` 字段，动态读取每个模型支持的推理档位（下拉枚举）；`models_dev` 同步、`usage_service` 轮询、模型选择器/配置卡片均接入；新增 `test_models_dev_sync` 与 `test_usage_service_poll` 覆盖（`0c7256b3`）

#### ♻️ 代码重构 (Refactoring)

- **Hook 配置存储双轨化** (`app/core/`): 重构 Hook 配置存储，支持插件 hooks 与系统 hooks 双轨，确保状态持久化与旧数据迁移（`6ffe9c73`）

#### 🐛 问题修复 (Bug Fixes)

- **输入区字体样式刷新** (`app/main_widget.py`): 修复输入区字体样式刷新异常（`1a1d9baa`）
- **团队权限恢复真正生效** (`app/core/team/`): 恢复用户权限现已实际生效——agent 模板仅在 agent 命令激活时拦截（`42a651a9`）；成员快照调用守卫 None `team_run_id`（`91d20157`）；高频保存中成员快照按 run_id 过滤并携带 `team_members`（`db9714f5`）
- **Explore 网格文字颜色** (`plugins/plugin-marketplace/`): 修正 Explore 网格区标题与工具提示文字颜色，提升浅色/深色主题可见性（`550705e5`）
- **消息卡片与欢迎卡片** (`app/widgets/message_card.py` + `app/widgets/welcome_card.py`): 修复消息卡片渲染与欢迎卡片幽灵窗口问题（`1a1d9baa`）
- **Marketplace featured 空数据** (`plugins/plugin-marketplace/`): 修复 featured 无数据场景处理（`1a1d9baa`）
- **团队延迟注册保留权威 run_id** (`app/main_widget.py`): 延迟注册新成员时强制 `keep_team_name=True`，确保权威 `run_id` 不被覆盖（`aeb15f9e`）
- **多团队成员归属严格匹配 + 批量建标签页幽灵窗口** (`app/core/team_manager.py` + `app/main_widget.py` + `app/widgets/message_card.py`):
    - `get_team_member_snapshot` 多团队并存下严格按 `run_id` 匹配，不再兜底无 `run_id` 的遗留记录（修复合并条目混入其他团队成员）
    - `new_team_member_window` 透传 `run_id`/`team_label`/`team_name`，由调用方锁定目标团队，避免新成员窗口漂移到错误团队框
    - `MessageCard` 新增 `_is_effectively_visible` 守卫，按 `QStackedWidget` 当前页判断真实可见性，修复批量建标签页时 `QWebEngineView` 弹出幽灵窗口
    - 同步更新 `test_team_member_snapshot` 与 `test_welcome_card_ghost_window`（`21307e72`）

#### 🔧 其他 (Chores & Build)

- **插件市场自动生成** (`plugins/plugin-marketplace/`): 由 GitHub Actions 依据 `plugin.json` 自动重新生成 [skip ci]（`c1d79b24`）
- **插件市场版本** (`plugins/plugin-marketplace/`): 升级至 0.3.1（`a0584068`）

### 🚑 Hotfix (2026-08-18 第五次重发布)

> **自上一 tag (v0.5.2 `23c2c93`) 以来的变更** | 提交数：10 · 文件变更：23 · +1048/-47 | 贡献者：mading

#### 🐛 问题修复 (Bug Fixes)

- **历史会话空消息覆盖** (`app/utils/history_manager.py` + `app/widgets/`): 修复内存消息释放与延迟保存竞态导致历史会话被空消息覆盖（`ce75a506`）
- **思考强度徽章配色区分** (`app/widgets/cards/settings/model_selector_card.py` + `model_config_card.py`): 思考强度徽章改紫色与多模态青蓝彻底区分，low 等级用饱和绿提对比度（`b482e0d3`）
- **compact 短会话覆盖** (`app/widgets/` + `tests/widgets/test_auto_compact_clear.py`): 调整消息清理阈值，避免短会话中被清空覆盖（`026ab08d`）
- **团队 workdir 重置** (`app/core/team/` + `tests/core/test_team_workdir_reset.py`): 新团队加载时 `team_workdir` 重置回源目录（`c3ee8bb8`）
- **团队新建任务死锁** (`app/core/team/` + `tests/core/test_team_new_task_interrupt.py`): 修复"团队新建任务"打断流式导致的收发死锁 + `run_id` 成员数据回写（`418b7232`）

#### 🎨 样式改进 (Style)

- **Badge 主题自适应配色** (`app/widgets/cards/settings/`): 根据主题动态调整 badge 颜色提升可见性（`19c035d0`）
- **模型胶囊纵向分隔** (`app/widgets/tab_panel.py`): 模型胶囊内增加纵向分隔线区分布局（`bafca7af`）
- **思考强度胶囊可点轮换** (`app/widgets/cards/settings/model_selector_card.py`): 思考强度胶囊独立可点，点击直接循环轮换等级（`db907099`）
- **思考强度胶囊按等级变色** (`app/core/model_capabilities.py` + `app/widgets/cards/settings/` + `app/core/workers/`): 思考强度胶囊按等级变色 + 等级强制校验回退中间值（`a47a8077`）
- **分支/新建标签按钮字号** (`app/widgets/tab_panel.py`): 统一分支与新建标签按钮字号提升一致性（`58d48041`）

### 🚑 Hotfix (2026-08-19 第六次重发布)

> **自上一 tag (v0.5.2 `70b78cff`) 以来的变更** | 提交数：1 · 文件变更：2 · +132/-9 | 贡献者：mading

#### 🐛 问题修复 (Bug Fixes)

- **缓存内容版本机制** (`app/core/models_dev_sync.py` + `tests/core/test_models_dev_sync.py`): 将"内容版本"从 schema 结构版本中分离，新增 `CACHE_CONTENT_VERSION` 与 `_is_content_version_stale`；开发者改了产出内容代码（解析/映射逻辑、服务商白名单、免费模型源、默认值、合并规则等非结构变更）后将该值 +1，本地缓存即便仍在 24h TTL 内也判定为内容过期、后台强制重拉 models.dev，让用户及时用上新逻辑而无需等 TTL 自然到期（`a1b3468b`）

## [v0.5.1] - 2026-08-14

自上一版本以来的变更 | 提交数：22 · 文件变更：22 · +3472/-380 | 贡献者：mading

### ✨ 新功能 (New Features)

- **Responses API 推理渲染支持** (`app/core/`): 支持 GPT-5.x 模型与子智能体 Responses API，解析更多 reasoning 事件并渲染思考内容
- **Hook 配置源文件写回** (`app/core/` + 插件 hooks.json): 插件 Hook 开关和配置直接写回源文件，系统 Hook 使用覆盖层持久化
- **旧版 Hook 状态一次性迁移** (`app/core/`): 启动时将旧版状态迁移到新的存储结构
- **对话区滚轮事件转发** (`app/widgets/tab_manager_window.py`): TabManagerWindow 在限宽居中的左右留白区拦截滚轮事件并转发给当前对话/覆盖层的滚动区域，避免留白处滚轮失效

### 🐛 问题修复 (Bug Fixes)

- **MCP 连接并发竞态** (`app/core/`): 防止启动全量连接与插件热重载单服务器连接互相取消，避免后台线程长时间阻塞
- **插件热重载 MCP 连接** (`app/core/`): 热重载后自动连接新增且启用的 MCP 服务器
- **Hook 热重载顺序与索引** (`app/core/`): 恢复规则位置并重新对齐 Hook 索引，保持事件顺序和分组映射稳定
- **Hook 状态持久化重复与覆盖** (`app/core/`): 修复文件顺序变化导致的重复 ID、错误覆盖及多实例状态竞争
- **Hook 源标签热重载后失真** (`app/core/hook_manager.py`, P021): 修复热重载后 Hook 所属来源/插件标签错位，确保 `plugins/<name>` 与系统来源标签稳定
- **OpenAI 模块导入死锁** (`app/core/`): 预加载资源子模块，避免启动时导入死锁

### ♻️ 代码重构 (Refactoring)

- **Hook 状态共享** (`app/core/`): 在 HookManager 实例间共享状态，避免快照互相覆盖

### 🔧 其他 (Chores & Build)

- **版本号升级到 v0.5.1** (`pyproject.toml` + `app/utils/config.py` + `dist/installer.iss` + `README.md`): 同步更新项目、配置、安装器和 README 版本号
- **移除过时设计文档** (`docs/`): 清理项目 dashboard、Hook 配置重构和 Responses API 支持的过时设计文档

## [v0.5.0] - 2026-08-13

自上一版本以来的变更 | 提交数：38 · 文件变更：N · +5085/-1282 | 贡献者：dingma, mading

> 重点：**插件市场 GitHub 代理 + 下载/更新记录**（内置代理直连回退、代理开关配置化、下载更新历史持久化与失败一键重试、并发任务队列）；**欢迎卡片动态 Tab 与模式切换**（插件注册欢迎 tab、sessions/changelog 模式切换、echarts 骨架加载、主题注入）；**OpenCode 免 key 改造**（移除内置共享 key、剥离 Authorization 头匿名调用）；**CommandCard 虚拟化**（性能优化）；**消息卡片渲染重构**（灵活分节与样式更新）。

### ✨ 新功能 (New Features)

- **插件市场下载/更新记录** (`plugins/plugin-marketplace/ui/records.py` + `cards.py` + `installer.py`): 「代理」页新增「下载 / 更新记录」区块，持久化记录安装/更新的成功/失败事件（上限 100 条，跨会话可见），失败记录保留插件元数据并支持一键重试原动作（安装失败→重新安装，更新失败→重新更新），记录区占满代理页剩余空间
- **插件市场 GitHub 代理管理器** (`plugins/plugin-marketplace/ui/proxy.py`): 新增 GitHub 代理配置管理器，支持 URL 重写与直连回退；「代理」页新增代理配置 Tab 与分组卡片 UI，应用 ComboBoxStyles 与上下文字体
- **插件市场实时下载量** (`plugins/plugin-marketplace/`): 实现社区插件实时下载量获取，`DownloadsFetcher` 带 TTL 缓存与失败去重，市场合并时按同名插件累加下载数
- **欢迎卡片动态 Tab 与模式切换** (`app/widgets/welcome_card.py` + `plugins/system/hooks/ui_plugin_registry.py`): 支持插件动态注册欢迎 tab；欢迎模式新增 sessions/changelog 切换与 changelog 展示支持
- **欢迎 Tab ECharts 骨架加载** (`app/widgets/welcome_card.py` + UI 插件创建器): 在轻量骨架中预加载 ECharts 供插件 tab 使用，附加 echarts 支持与校验清单
- **ui-plugin-creator 欢迎 tab 开发** (`plugins/system/skills/ui-plugin-creator/`): 增强欢迎 tab 的 echarts 支持与校验清单；同步欢迎 tab (HTML 注入式 session 卡片) 开发技术到参考文档；长参考文档加行号目录
- **CommandCard 虚拟化** (`app/widgets/cards/command_card.py`): 实现 CommandCard 虚拟化渲染，提升大量命令项下的滚动性能

### 🐛 问题修复 (Bug Fixes)

- **OpenCode 免 key 改造** (`app/core/conversation/`): keyless 请求剥离 Authorization 头而不是使用占位 key；允许无 API key 发送消息
- **插件市场 git clone 错误处理** (`plugins/plugin-marketplace/ui/installer.py`): 增强 git clone 错误处理，GitHub URL 自动补 `.git` 后缀
- **插件市场 row busy 状态** (`plugins/plugin-marketplace/ui/`): 修复 tab 切换重渲染后 row busy 状态丢失；披露行需尊重当前搜索过滤器，避免过滤被重置
- **插件市场更新失败兜底** (`plugins/plugin-marketplace/ui/installer.py`): 更新失败时保留旧版本，先下载成功再切换
- **插件市场代理 Tab 渲染** (`plugins/plugin-marketplace/ui/proxy.py` + 代理卡片): 代理 Tab 折叠为单层外框加 section 分隔线，移除内层卡片边框；卡片背景使用 `WA_StyledBackground` 渲染，未保存的开关状态跨 tab 切换保持；模块顶部导入 `get_proxy_config` 避免 NameError
- **插件市场代理开关自动保存** (`plugins/plugin-marketplace/ui/proxy.py`): 代理开关切换自动保存启用状态，配置立即生效
- **欢迎 Tab 主题注入** (`app/widgets/welcome_card.py`): 将 Qt 主题 `is_dark` 注入插件渲染上下文，修复深浅色不匹配
- **欢迎 Tab 配置持久化** (`app/widgets/welcome_card.py` + `app/utils/config.py`): 插件注入的欢迎 tab 通过专用配置字段持久化
- **全局卡片防重入** (`app/widgets/cards/global_card.py`): `ensure_settings_popup` 防止重入，避免重复设置卡片
- **插件市场安装缓存预热** (`plugins/plugin-marketplace/ui/`): 在任务线程预热 installed/status 缓存，避免安装完成时 UI 卡顿
- **插件市场任务序列化** (`plugins/plugin-marketplace/ui/`): 安装/更新/管理任务串行入队，防止并发安装互相打断

### ♻️ 代码重构 (Refactoring)

- **OpenCode 移除内置共享 key** (`app/utils/config.py`): 移除 OpenCode 历史内置共享 key，迁移到免 key 匿名调用，清理 `_LEGACY_OPENCODE_BUILTIN_KEYS` 常量与对应迁移方法
- **消息卡片渲染重构** (`app/widgets/message_card.py`): 会话历史渲染改用灵活分节与更新样式
- **插件市场命令重载与缓存** (`plugins/plugin-marketplace/ui/installer.py`): 增强插件安装器命令重载逻辑与缓存管理
- **插件市场 FlowContainer 清理** (`plugins/plugin-marketplace/ui/`): 移除未使用的 `_FlowContainer` 及相关高度处理逻辑
- **插件市场 busy 状态加固** (`plugins/plugin-marketplace/ui/`): 根据 review 加固 busy 状态恢复

### 🎨 样式改进 (Style)

- **插件市场代理 Tab 样式** (`plugins/plugin-marketplace/ui/proxy.py`): 代理 Tab 重命名为「代理」，使用 Fluent PushButton 与右对齐图标，应用上下文字体族
- **插件市场 ComboBox 样式统一** (`plugins/plugin-marketplace/ui/`): 代理模式下拉框统一为应用全局 ComboBoxStyles；代理 Tab 标签使用上下文字体大小

### 📚 文档 (Docs)

- **ui-plugin-creator 文档同步** (`plugins/system/skills/ui-plugin-creator/`): 同步欢迎 tab (HTML 注入式 session 卡片) 开发技术到参考文档；长参考文档加行号目录
- **移除过时的 issue 跟踪文档** (`docs/`): 移除与 issue 跟踪与 triage 流程相关的过时文档与技能
- **移除 widgets-utils 冗余工具** (`docs/` + `plugins/`): 移除数字格式化、token 估算、日期格式化等冗余工具的文档与工具函数，简化插件功能

### 🔧 其他 (Chores & Build)

- **版本号升级到 v0.5.0** (`pyproject.toml` + `app/utils/config.py` + `dist/installer.iss` + `README.md`): 跨版本号 (v0.4.14 → v0.5.0) 同步更新


#### 📦 附加变更（v0.5.0 一次重发补丁 | 自初次 tag 之后 14 个新提交）

> v0.5.0 初次 tag 后追加的启动性能优化与 bug 修复：SQLite 初始化后台化（首帧省 ~2s）、backend 非首帧组件 QTimer 错峰创建、config-sync 写回 diff 短路与主题刷新分片、watcher 同步窗口抑制、主题 reload mtime 指纹缓存短路；修复命令卡片过滤后高度跟随收缩、插件市场更新后 UI 重载、错峰/会话存储审查问题；更新 release 海报资源。贡献者：mading, dingma

##### ⚡ 性能优化 (Performance, 5)

- **SQLite 初始化后台化** (`app/core/store/session_store.py`): 会话存储 SQLite 初始化移出主线程，首帧节省 ~2s
- **非首帧组件 QTimer 错峰创建** (`app/core/backend.py`): 非首帧组件延迟错峰创建，避免启动集中构建
- **config-sync 写回 diff 短路** (`app/core/config_sync.py`): ConfigItem 写回 diff 短路，主题刷新拆 QTimer 分片
- **watcher 同步窗口抑制** (`app/core/backend.py` + `app/core/config_sync.py`): watcher 同步窗口抑制 + pending 合并重载
- **主题 reload mtime 指纹缓存** (`app/utils/theme_manager.py`): 主题未变时短路跳过重载

##### 🐛 问题修复 (Bug Fixes, 3)

- **命令卡片过滤后高度跟随** (`app/widgets/cards/`): 过滤后容器高度跟随卡片收缩，消除底部留白
- **插件市场更新后 UI 重载** (`plugins/plugin-marketplace/`): 更新后重载插件 UI（watchfiles 空组件不触发）
- **错峰/会话存储审查修复** (`app/core/` + `app/utils/`): 修复错峰与会话存储审查问题

##### 🔧 其他 (Chores & Build, 2)

- **release 海报资源更新** (`images/` + `release-poster-v0.5.0.html`): 新增 release 海报，移除过时海报图片与 HTML

#### 📦 附加变更（v0.5.0 二次重发补丁 | 自初次 tag 之后 11 个新提交）

> v0.5.0 一次重发后追加的 project-dashboard 插件完整生命周期：设计规范与实现规划文档 → 插件骨架 → 数据采集层（git 统计 + 文件扫描） → HTML 生成层（4 图 echarts 报告） → welcome tab 与 function 命令注册 → 修复 iframe 概要 markdown 与贡献者姓名 → 异步采集并直接嵌入 echarts、移除 command 与 iframe → 4 图改 2×2 四宫格 + 全局 tooltip → 回滚整套插件；同时修复 project_root 被软件启动路径错误继承的 bug，以及插件市场卡片在数据无变化时的重复渲染与闪烁。贡献者：mading

##### ✨ 新功能 (New Features, 5)

- **project-dashboard 插件骨架** (`plugins/project-dashboard/.drifox-plugin/` + `commands/` + `icon*.svg`): 搭建 plugin.json 元数据、命令定义与亮/暗图标
- **project-dashboard 数据采集层** (`plugins/project-dashboard/ui/dashboard.py` + `tests/test_project_dashboard_data.py`): 基于 git log + 文件扫描采集提交统计、贡献者、文件树、近期变更
- **project-dashboard HTML 生成层** (`plugins/project-dashboard/ui/dashboard.py` + `tests/test_project_dashboard_html.py`): 输出 4 图 echarts 报告 HTML（趋势/分布/类型/作者）
- **project-dashboard welcome tab 与 function 命令注册** (`plugins/project-dashboard/ui/__init__.py`): 注册欢迎 tab 与 `/project-dashboard` function 命令
- **project-dashboard 2×2 四宫格 + 全局 tooltip** (`plugins/project-dashboard/ui/dashboard.py` + `tests/test_project_dashboard_html.py`): 4 图改为 2×2 网格布局，加入全局 tooltip 提升可读性

##### 🐛 问题修复 (Bug Fixes, 2)

- **project-dashboard iframe 渲染** (`plugins/project-dashboard/ui/dashboard.py`): iframe 概要行去除 markdown 语法，贡献者只取姓名（不再带邮箱）
- **project_root 错误继承修复** (`app/core/backend.py` + `app/core/tool_executor.py` + `app/main_widget.py` + `plugins/plugin-marketplace/ui/cards.py`): 未配置项目工作目录时 `_workdir` 默认为初始化兜底路径，`get_workdir()` 在 `_sync_working_directory` 同步前会返回软件启动路径被 PreUserMessage hook 误注入为"项目根目录"；新增 `_workdir_user_set` 区分用户显式设置与初始化兜底，统一转为空串；同步修复插件市场卡片在数据无变化时的重复渲染与闪烁（`_render_pending` 防重入 + 顺序未变时复用已有行）

##### ♻️ 代码重构 (Refactoring, 2)

- **project-dashboard 异步采集 + 直接嵌入 echarts** (`plugins/project-dashboard/ui/__init__.py` + `dashboard.py` + `collector.py` + `tests/test_project_dashboard_html.py`): 数据采集改为异步线程，HTML 直接嵌入 echarts，移除 command 调用与 iframe 包装
- **移除 project-dashboard 插件** (`plugins/plugin-manager/__init` + `plugins/project-dashboard/`): 回滚整套插件骨架与 UI 组件，清理空 plugin-manager 模块

##### 📚 文档 (Docs, 2)

- **project-dashboard 设计规范** (`docs/.../specs/2026-08-13-project-dashboard-design.md`): 新增 project-dashboard 插件设计 spec（架构、数据流、ECharts 选型）
- **project-dashboard 实现规划** (`docs/.../plans/2026-08-13-project-dashboard.md`): 新增 project-dashboard 完整实现规划（分阶段交付、回滚策略）


#### 📦 附加变更（v0.5.0 三次重发补丁 | 自二次 tag 之后 6 个新提交）

> v0.5.0 二次 tag 后追加的欢迎卡片会话 Tab 体验深化与稳定性修复：会话列表改卡片行列表（图标徽章 / hover 高亮 / 箭头滑入）+ 双列 3 行网格 + 重排序（最活跃优先于最近）+ 内置 mode 跳过重渲染修复动画播两遍；同时修复多窗口下欢迎卡片内容串项目、UI 插件多次同步重建卡顿、project_root 误注入软件启动目录、加载更多按钮主题切换文字色不更新。贡献者：dingma

##### ✨ 新功能 (New Features, 4)

- **欢迎卡片会话 Tab 视觉升级** (`app/widgets/message_card.py` `_render_sessions_body`): 「会话」模式从胶囊流改为卡片行列表——每行左侧渐变图标徽章（最近 💬 / 最活跃 ⚡）+ 标题省略号 + 副标题（时间/消息数），hover 时背景加深、蓝色边框高亮、箭头滑入并轻微右移；分区标题改为「图标 + 标题 + 数量徽章」。复用 `.context-tag` 点击事件链（`data-type="session"` + `data-session-id` 不变），JS 拦截逻辑与打开会话行为不受影响
- **欢迎卡片会话 Tab 双列 3 行** (`app/widgets/message_card.py` `_render_sessions_body` + `app/main_widget.py`): 会话列表改双列网格，最近/最活跃各固定显示 3 行（6 张），无折叠与加载更多；`main_widget.py` 数据量提升到最近 6 / 最活跃 6，stagger 进入动画跨分区全局连贯
- **欢迎卡片会话 Tab 重排序** (`app/widgets/message_card.py` `_render_sessions_body`): 「最活跃」分区置于「最近」之上，推荐优先展示高频会话；调整对应 `start_idx` 偏移保证 stagger 动画索引连续
- **加载更多按钮主题切换文字色同步** (`plugins/plugin-marketplace/ui/cards.py` `MarketplaceCard`): 加载更多按钮 `objectName` 标记为 `loadMoreBtn`，主题刷新时正则替换 stylesheet 中的 `color` 字段；创建按钮时即注入当前主题文字色，主题切换后旧主题色不再残留

##### 🐛 问题修复 (Bug Fixes, 4)

- **欢迎卡片会话 Tab 动画播两遍修复** (`app/main_widget.py` `_rerender_welcome_card`): workdir 延迟同步（启动 2s / 项目切换）后 `_sync_working_directory` 调用 `_rerender_welcome_card` 强制重渲染——sessions/changelog 内置 mode 不依赖 project_root，重渲染纯属多余，且 `_render_welcome_with_body` 每次生成随机 greeting 使 HTML 必然变化，`updateContent` 重建 DOM 导致卡片进入动画（stagger fade-in）重复播放一遍。修复：内置 mode 跳过重渲染，仅插件 tab（project-dashboard 类）保留强制刷新
- **多标签页欢迎卡片内容串项目** (`app/widgets/message_card.py` + `app/main_widget.py`): 欢迎卡片渲染插件 tab 时只传 `{"is_dark"}`，插件只能回读全局状态（`Settings.current_project` / 全局 workdir）取项目信息，多窗口下 A 窗口欢迎卡片会被渲染成 B 窗口的项目内容。修复：`create_welcome_card` 新增 `context_provider` 参数（传入窗口 `_build_ui_context`），`_render_welcome_body` 把窗口级 project_root / project_name / window_id 合并注入插件 `render_func(ctx)`，每个窗口渲染自己项目的内容
- **UI 插件多时欢迎卡片重建卡顿** (`app/core/ui_plugin_registry.py`): 插件批量加载/卸载时每个注册 welcome tab 的插件都同步触发 `_refresh_welcome_cards`，N 个插件 = N 次 QWebEngineView 重建（100-500ms/次）。修复：① `_schedule_welcome_refresh` debounce（`QTimer.singleShot(0)` 合并同一事件批次为单次刷新）；② 刷新重建走 `_schedule_initial_welcome` 交错时间片调度，不再同步阻塞。注意：**主程序不缓存插件 render_func 结果**——异步采集型插件（如 project-dashboard）首次渲染返回「加载中」占位，采集完成后重渲染拿真实图表，缓存占位会阻塞该机制（插件自身缓存数据即可）
- **UI 插件 project_root 误注入软件启动目录** (`app/main_widget.py` `_build_ui_context` + `_sync_working_directory`): UI 插件上下文 workdir 兜底原为 `os.getcwd()`，软件启动/项目切换瞬间 workdir 尚未同步到 tool_executor 时会把**软件启动目录**（源码根/exe 目录）当作 project_root 注入；若该目录本身是 git 仓库（如 `D:/work/DriFox`），project-dashboard 看板会误展示启动目录的 git 信息。修复：① `_build_ui_context` 未设置 workdir 时返回空串（与 `backend._build_worktree_context` 修复模式一致，绝不回退 `os.getcwd()`）；② `_sync_working_directory` workdir 变化后强制重渲染欢迎卡片（新增 `_rerender_welcome_card`，同 mode 重渲染 body 不重建 QWebEngineView），看板自动从"未检测到 git 项目"恢复为正确项目。配套 project-dashboard 插件 v0.2.2 移除自身 `os.getcwd()` 兜底

##### 🔧 其他 (Chores & Build, 1)

- **v0.5.0 三次重发 tag 覆盖** (`CHANGELOG.md` + git tag): 删除远程/本地 v0.5.0 tag 重新打在当前 HEAD，覆盖二次重发 tag 指向三次重发补丁后的最新代码


#### 📦 附加变更（v0.5.0 四次重发补丁 | 自三次 tag 之后 1 个新提交）

> v0.5.0 三次 tag 后追加的历史面板 UI 同步 bug 修复：会话保存/自动保存后历史面板 UI 仍停留在保存前快照，导致「历史面板已展开但列表缺失最新会话」。修复：新建/保存/自动保存三处触发点调用 `refresh_history_card_if_visible` 同步内存缓存到 UI（仅历史卡片可见时执行，0 开销）；配套回归测试覆盖。贡献者：mading

##### 🐛 问题修复 (Bug Fixes, 1)

- **历史面板保存后刷新（避免快照过时）** (`app/main_widget.py` `_create_new_session` / `_save_current_session` / `_auto_save_current_session` + `tests/widgets/test_history_panel_refresh_on_save.py`): 旧版 `_auto_save_current_session` 与 `_save_current_session` 完成后仅清脏标记 + 更新预览，未触发历史面板 UI 同步——历史面板若已展开，列表会停留在保存前快照（旧会话缺失 / 标题过时）；`_create_new_session` 同理。修复：上述三处触发点新增 `refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)` 调用，仅在历史卡片可见时执行刷新（不可见时 0 开销），保证 UI 与内存缓存同步；新增 `test_history_panel_refresh_on_save.py` 覆盖三处调用点

##### 🔧 其他 (Chores & Build, 1)

- **v0.5.0 四次重发 tag 覆盖** (`CHANGELOG.md` + git tag): 删除远程/本地 v0.5.0 tag 重新打在当前 HEAD，覆盖三次重发 tag 指向四次重发补丁后的最新代码

## [v0.4.14] - 2026-08-09

自上一版本以来的变更 | 提交数：20 · 文件变更：37 · +2207/-2245 | 贡献者：dingma

> 重点：**团队工作目录管理**（团队级工作目录同步与成员牵引）；**tab_panel 自动展开体验系列**（挤压态自动展开、动画时序与用户交互处理、宽度管理）；**插件市场管理增强**（安装组件展示与按钮态更新、插件管理器模块、卸载流程 UI 主线程卸载与锁释放）；**消息卡片流式渲染修复**（骨架缓存、未闭合尾部即时格式化）。

### ✨ 新功能 (New Features)

- **插件管理器模块** (`plugins/plugin-manager/`): 新增插件管理器模块初始实现
- **插件市场 UI 主线程卸载** (`plugins/plugin-marketplace/ui/`): 后台操作前在主线程卸载插件 UI 组件，规避线程问题
- **插件卸载锁提前释放** (`plugins/plugin-marketplace/ui/installer.py`): 卸载前释放 UI 插件锁，修复 Windows 文件占用
- **团队工作目录管理与同步** (`app/`): 团队成员工作目录统一管理，tab 顺序优先团队负责人并保持成员
- **tab_panel 自动展开系列** (`app/widgets/tab_panel.py` + `app/widgets/tab_manager.py`): 挤压态自动展开与按钮样式统一、相对增长条件扩展逻辑、动画时序与用户交互适配、宽度配置优化
- **插件市场管理增强** (`app/widgets/` + `app/core/`): 插件详情对话框展示已安装组件与更新按钮态、插件管理功能整体增强
- **筛选栏重构与布局优化** (`app/widgets/`): 来源/标签筛选栏重构，标题与布局优化
- **UI 组件更新** (`app/widgets/`): 上下文使用统计、快捷键管理器等组件标题与布局调整

### 🐛 问题修复 (Bug Fixes)

- **消息卡片流式渲染修复** (`app/widgets/message_card.py` + `app/core/workers/`): 骨架缓存版本号递增 + tail 节点改 div 修复流式期间不渲染、流式期间即时格式化未闭合尾部消除最终内容不符
- **tab_manager 侧边栏修复** (`app/widgets/tab_manager.py`): 启动时恢复侧边栏宽度，修复欢迎卡片渲染后左面板被挤压误折叠

### ♻️ 代码重构 (Refactoring)

- **卸载确认对话框简化** (`plugins/plugin-marketplace/ui/cards.py`): 确认处理改用 accept/reject 方法简化流程

### 🎨 样式改进 (Style)

- **系统插件样式优化** (`plugins/`): 优化所有系统插件样式

### 🔧 其他 (Chores & Build)

- **版本号同步** (`pyproject.toml` + `app/utils/config.py` + `dist/installer.iss` + `README.md`): v0.4.14 版本号升级与同步


#### 📦 附加变更（v0.4.14 一次重发补丁 | 自初次 tag 之后 10 个新提交）

> v0.4.14 初次 tag 后追加的 bug 修复与体验优化：插件市场卡片布局/渲染稳定性、问题卡片布局、图标尺寸一致性、市场列表滚动性能、回归测试覆盖、Windows 构建清理冗余 libcrypto DLL。贡献者：mading

##### 🐛 问题修复 (Bug Fixes, 6)

- **插件市场卡片渲染稳定性** (`plugins/plugin-marketplace/`): 修复渲染稳定性，避免压缩帧
- **内容尺寸同步** (`app/widgets/`): 视口与窗口 resize 时同步内容尺寸，消除布局不一致
- **市场获取 worker 信号增强** (`plugins/plugin-marketplace/`): 信号包含 generation 跟踪，提升数据一致性
- **卡片容器布局处理** (`app/widgets/`): 增强卡片容器与问题部件的布局处理，避免多余空白
- **图标尺寸处理增强** (`app/widgets/`): `PluginIconWidget` 与 `SquircleAvatar` 图标尺寸处理增强，视觉一致性提升
- **Windows 构建移除冗余 libcrypto DLL** (`build.py` 或 `dist/`): 从 Windows 构建配置中移除不必要的 libcrypto DLL

##### ⚡ 性能优化 (Performance, 1)

- **插件市场列表滚动优化** (`plugins/plugin-marketplace/`): 优化列表滚动流畅度

##### 🔄 其他变更 (Other, 3)

- **插件市场体验优化** (`plugins/plugin-marketplace/`): 整体使用体验优化
- **MarketplaceCard 过滤与 resize 测试** (`tests/`): 新增过滤与 resize 测试，防止空白间隙
- **问题卡片布局测试** (`tests/`): 布局诊断中 pump 事件循环以保证 headless 检查可靠


#### 📦 附加变更（v0.4.14 二次重发补丁 | 自初次 tag 之后 7 个新提交）

> v0.4.14 初次 tag 后再追加的优化：models.dev 动态数据后台刷新与线程安全 UI 更新、问题卡片动态高度、config-sync token 本地独立性、tab-manager 面板宽度常量、流截断 finish_reason 检测、tab 面板宽度持久化移除、PluginIconWidget 下载超时重试。贡献者：mading, dingma

##### ✨ 新功能 (New Features, 3)

- **models.dev 动态数据后台刷新** (`app/core/models_dev_sync.py` + 相关): 实现后台刷新与线程安全的 UI 更新，避免阻塞主线程
- **问题卡片动态高度计算** (`app/widgets/cards/question_card.py`): 改进动态高度计算以解决换行布局问题
- **PluginIconWidget 下载超时与重试** (`app/widgets/plugin_icon_widget.py`): 增加图标下载超时控制与重试逻辑，提升稳定性

##### 🐛 问题修复 (Bug Fixes, 2)

- **tab-manager 默认面板宽度** (`app/widgets/tab_manager.py`): 默认面板宽度改用常量统一管理
- **chat-worker 流截断检测** (`app/core/workers/chat_worker.py`): 通过 finish_reason 检测流式截断，避免静默中途停止

##### ♻️ 代码重构 (Refactoring, 2)

- **config-sync token 本地独立性** (`app/core/config_sync.py`): 更新 token 处理以保证本地独立，防止多设备冲突
- **tab 面板宽度持久化移除** (`app/widgets/tab_panel.py` + `app/widgets/tab_manager.py`): 移除 tab 面板宽度与折叠状态持久化，改为固定默认几何


#### 📦 附加变更（v0.4.14 三次重发补丁 | 自初次 tag 之后 1 个新提交）

> v0.4.14 初次 tag 后追加：插件安装/更新下载量统计上报。贡献者：mading

##### ✨ 新功能 (New Features, 1)

- **插件下载量统计上报** (`plugins/plugin-marketplace/ui/installer.py` + `cards.py`): 安装/更新成功后向 CountAPI 异步上报 +1（countapi.mileshilliard.com），详情对话框与插件行展示下载量徽标，失败不影响安装流程


## [v0.4.13] - 2026-08-07

自上一版本以来的变更 | 提交数：107 · 文件变更：371 · +33168/-8048 | 贡献者：mading, dingma

> 重点：**流式渲染性能大修**（差量化渲染末帧渲染量降 98%、渲染移出主线程、WebEngine renderer 内存强回收、会话历史驻留清理、关闭窗口异步化）；**tab_panel 侧边栏体验系列**（自动折叠/展开、滞回阈值、宽度动画）；**团队协作链路完善**（团队框按钮返工、会话记录支持同角色多成员、模板列表直读 user-custom）；**MCP stdio 命令安全校验**；**文件工具 glob 标准语义修复**。

### ✨ 新功能 (New Features)

- **模型选择卡片成本展示与思考字段一致性修复** (`app/widgets/cards/settings/model_selector_card.py` + `app/core/models_dev_sync.py` + `app/core/model_capabilities.py`): 解析 models.dev 的 cost 字段（$/M tokens），卡片行内显示 💰input/output/cache_read 紧凑格式、悬停显示 Input/Output/Cache read/write 明细；修复三处思考字段不一致——`reasoning=True` 但 `reasoning_options=[]` 的模型（deepseek-reasoner、MiniMax-M2.7 等）不再被误判为不支持思考、跨 provider 同名模型合并取"更支持"、动态数据不再把硬编码的 supports_thinking=True 降为 False；缓存 schema v2 强制重拉一次带上新字段。模型选择卡片服务商内部 🧠🖼️ emoji 列按最长模型名对齐，🧠/🖼️ 各自悬停显示能力说明（支持思考开关/支持多模态输入），模型描述改为 ❓ emoji 悬停显示完整描述
- **Tab 内联关闭确认 + 关闭按钮 tooltip 增强** (`app/widgets/tab_panel.py`): 关闭内联确认（防误关）、关闭按钮 tooltip 与样式调整、品牌标题与版本左对齐
- **MCP stdio 命令安全校验** (`app/tools/mcp_tools.py`): stdio 命令白名单/黑名单校验，防止任意命令执行
- **文件提及卡片预编译忽略规则与后台扫描** (`app/widgets/cards/floating/file_mention_card.py`): 忽略规则预编译 + 后台扫描避免阻塞 UI
- **卡片槽位小窗口优先显示** (`app/widgets/`): 小窗口优先显示 / dock 卡片布局改善
- **任务中断协议增强与错误处理** (`app/core/`): 中断协议完善、异常处理增强
- **团队框按钮返工** (`app/main_widget.py`): 新建任务（全员新会话+新 run_id）/ 快速新建成员（可重复角色）按钮
- **团队协作链路整体完善** (`app/main_widget.py` + `app/widgets/team/`): 邮件状态机修复、round 口径统一、成员按钮、能力可见性
- **UIPluginRegistry 支持 unload_ui 回调** (`app/core/ui_plugin_registry.py`): 热重载/卸载时释放子进程资源
- **组件测试补充** (`tests/`): tab_panel 关闭按钮、团队分组头、streaming dock、viewer 等测试

### 🐛 问题修复 (Bug Fixes)

- **流式渲染性能修复系列** (`app/widgets/message_card.py` + `app/core/workers/`): 差量化渲染（自然边界闭合段增量追加，末帧渲染量降 98%）、渲染移出主线程（线程池+序号校验）、WebEngine renderer 强回收（LRU+护栏 kill 离屏进程）、虚拟滚动回收保留数据、词法缓存限容、worker QThread 释放与 cleanup 竞态收敛、tooltip 注册表自注销、关闭窗口异步化不阻塞、历史驻留清理、IPC 瘦身
- **tab_panel 侧栏体验修复** (`app/widgets/tab_panel.py` + `app/widgets/tab_manager.py`): 自动折叠/展开逻辑与阈值统一（消除灰色区间）、展开阈值滞回区（>=110）、侧边栏宽度动画、品牌标题与版本左对齐 stretch、展开/折叠图标一致性
- **团队修复** (`app/main_widget.py` + `app/widgets/team/`): 会话记录支持同角色多成员（window_id 维度）、成员能力标签不再显示禁用标记、load 模板列表直接解析 user-custom 目录、恢复路径清理历史窗口快照（窗口数不随恢复膨胀）
- **config_sync 修复** (`app/core/config_sync.py`): 多设备 refresh_token 轮换导致的误清绑、启动同步延迟到空闲窗口并合并上传往返
- **widgets 布局修复** (`app/widgets/`): dock 最小宽度对齐硬约束 300、中等窗口卡片被压扁/裁切、卡片关闭/移除后释放 min 锁
- **消息卡片修复** (`app/widgets/message_card.py`): 全局事件过滤器判活加固与对称卸载、合并为单例注册表、compact 渲染 think/tool 块、问题卡片内容自适应高度
- **命令/子智能体修复** (`app/core/command_manager.py`): 手打参数行尾空格误判为离开、subagents --create= 降级 prompt 注入、hot_reload 新增插件子智能体触发全量重载
- **其他修复** (`app/widgets/` + `app/main.py`): tooltip eventFilter 漏捕 HideToParent 残留、后台 finalize 访问已销毁 node_preview 崩溃守卫、streaming dock 状态管理、懒加载兜底 None 崩溃
- **右侧对话区恢复半透明背景** (`app/widgets/tab_manager_window.py`): `#chatFrame` 背景由 `CARD_BG_SOLID`（实色）改回 `CARD_BG.format(alpha=150)`，与左侧 `#tabFrame` 对称，恢复窗口背景图在右侧对话区透出
- **对话框内容区背景完全透明** (`app/main_widget.py`): `OpenAIChatToolWindow` 移除自身 `QPalette window_bg` + `setAutoFillBackground(True)` 背景层及主题刷新/透明度更新的半透明背景设置，改为 `background: transparent`——对话框内容不再叠加独立背景，直接透出外层 `#chatFrame` 半透明背景与全局背景图；仅保留 `_window_bg_color` 字段兼容旧引用
- **套餐用量圆环轮询失效修复** (`app/core/usage_service.py` + `tests/core/test_usage_service_poll.py`): 轮询 QTimer 为 singleShot，`request_coding_plan` 的缓存命中/in_flight 路径不重启 timer，导致 tick 触发一次后轮询永久死亡——用量圆环只在新建标签页重新请求时才刷新一次。修复：缓存命中/并发去重路径保持 timer 存活，`_on_poll_tick` 尾部兜底重启；新增 4 条轮询回归测试

### ♻️ 代码重构 (Refactoring)

- **移除旧多窗口模式** (`app/widgets/`): ToolPopupDialog 删除、相关符号迁移、死代码清理、注释残留清理

### ⚡ 性能优化 (Performance)

- **主题 YAML 加载缓存** (`app/utils/theme_manager.py`): 主题加载加缓存
- **config_sync 后台化** (`app/core/config_sync.py`): 启动同步延迟到空闲窗口并合并上传往返
- **UI 多标签渲染优化** (`app/widgets/`): 隐藏页零空转、卡片懒加载、主题补刷收窄、粘贴图异步、广播过滤
- **SQLite 批量事务与 IO 异步化** (`app/core/`): SQLite 批量事务、历史预热、配置/服务/worktree IO 异步化
- **hook SessionStart 异步化** (`app/core/hook_manager.py`): command 异步化与注入顺序修复、timeout 收紧、sid 校验、PostAssistantMessage 语义恢复
- **团队批量解散优化** (`app/main_widget.py`): 批量删除 O(N²)→O(N)、同步去重、rmtree 异步、写盘合并、watcher 时序、sendPostedEvents 异步化

### 🔧 其他 (Chores & Build)

- **worker 完成日志降级为 debug** (`app/core/`): 减少噪声日志
- **subagents 命令测试修正** (`tests/`): ruff format 适配
- **移除过时参考文档与配置校验工具** (docs/): 删除 CONFIG-REFERENCE、NODE-GUIDE、OUTPUT-FORMAT、PITFALLS、PROMPT-TEMPLATE、sanitize_config.py

---

#### 📦 附加变更（v0.4.13 重发补丁 | 自初次 tag 之后 30 个新提交）

#### 📦 附加变更（v0.4.13 重发补丁 | 自初次 tag 之后 34 个新提交）

> v0.4.13 初次 tag 后追加的优化与修复：性能大修（10 项 perf）、模型选择器体验、tab 状态反馈、scanner 进程清理、卡片高度/宽度同步布局修复、插件市场体验优化。

##### 🐛 二次重发补丁 (Bug Fixes, 3)

- **卡片高度上限 80% 窗口高** (`app/`): SystemCardFrame 溢出内容限高防遮挡
- **卡片宽度同步改用视口直查** (`app/`): 防止布局同步错位
- **模型 tooltip 增强** (`app/widgets/model_selector.py`): 补充成本详情与能力信息

##### ✨ 三次重发补丁 (New Features & Fixes, 4)

- **增量更新尾文本丢失修复** (`app/`): updateContentAppend 传参 tailText 防止增量更新丢失尾部文本
- **插件安装与更新流程优化** (`app/widgets/cards/`): 优化插件安装/更新流程
- **更新徽标替换为 InfoBadge** (`app/widgets/cards/`): MarketplaceCard 徽标可见性提升
- **插件市场字体大小体验优化** (`app/widgets/cards/`): 市场界面字号适配

##### ⚡ 性能优化 (Performance, 10)

- **B4 强回收双重阈值** (`app/`): web 子进程 RSS 双重阈值 + kill 统计日志
- **懒渲染批量时间片预算** (`app/`): 批量渲染时间片预算分配
- **设置弹窗按需构建** (`app/`): 取消 3.5s 预构建，改为按需构建
- **file_mention 增量扫描防抖与上限** (`app/widgets/cards/floating/file_mention_card.py`): 减少 tab 切换卡顿
- **perf_regression --quick 快速启动门禁** (`tools/perf_regression.py`): 快速启动回归门禁模式
- **流式高度报告批量化** (`app/`): 高度上报 3 帧批量，降低流式期间 IPC 次数
- **卡片宽度同步仅视口可见** (`app/`): resize 时仅同步视口可见卡片宽度
- **markdown 渲染缓存收缩** (`app/`): 64→16 收缩 markdown 渲染缓存以降低每轮内存
- **tool start processEvents 限次** (`app/`): 减少主线程阻塞

##### ✨ 新功能 (New Features, 6)

- **模型名 tooltip 显示成本与能力** (`app/widgets/model_selector.py`): ModelItem 名称 tooltip 展示成本与能力
- **UI 响应性与主题支持增强** (`app/widgets/`): 跨组件 UI 响应式与主题适配（模型选择器、命令卡片等）
- **tab 错误/提问状态 shimmer 动效** (`app/widgets/tab_panel.py`): tab item 错误/提问状态 shimmer 效果
- **tab_panel 布局增强** (`app/widgets/tab_panel.py`): footer stretch + 独立 emoji 标签
- **模型能力展示与架构版本化** (`app/widgets/model_selector.py`): 成本显示 + schema 版本号
- **scanner 遗留 QtWebEngine 进程清理** (`app/core/scanner.py`): 主动终止残留 WebEngine 进程以释放内存

##### 🐛 问题修复 (Bug Fixes, 10)

- **模型能力动态处理细化** (`app/`): 准确支持 thinking 特性
- **tooltip 在鼠标按下/分组删除前隐藏** (`app/widgets/`): 防止 tooltip 残留显示
- **team load 时项目重置为源 tab 项目** (`app/widgets/team/`): 团队加载时项目重置
- **cost 显示格式化去尾空格** (`app/`): 移除 cost 末尾空格
- **shimmer 与左指示器选中态联动** (`app/widgets/tab_panel.py`): 根据选中态更新 shimmer 与左指示器可见性
- **thinking 开关严格跟随 models.dev** (`app/widgets/model_selector.py`): 不再用硬编码覆盖动态数据
- **model selector thinking 开关显式控制** (`app/widgets/model_selector.py`): 仅在 models.dev 给出显式 reasoning 控制时显示
- **file_mention set 切片崩溃修复** (`app/widgets/cards/floating/file_mention_card.py`): 增量扫描 set 切片崩溃修复
- **图片资源版本更新** (`assets/`): 替换为新版本图标
- **prompt 规则更新** (`app/`): 强化简洁输出与禁止冗余规则

##### ♻️ 代码重构 (Refactoring, 1)

- **问题滚动高度管理简化** (`app/widgets/`): 移除冗余计算，简化高度管理
- 版本号升级至 v0.4.13（pyproject / config / installer / README）

## [v0.4.12] - 2026-08-04

自上一版本以来的变更 | 提交数：18 · 文件变更：67 · +4599/-781 | 贡献者：dingma, mading

> 重点：**消息卡片工具/思考折叠框顺序修复收官**（data-order 计算修正 + 简洁模式/流式完成瞬间时序 + 清缓存）；**config-sync 模型/主题跟随修复**（仅未手动选模型的窗口跟随云端、自定义主题不被静默回退、窗口模型选择跟随云端 SelectedModel）；**DeepSeek 思考模式兼容**（reasoning_content 必填处理）；**diff-viewer 默认统一视图**；**SSE 流内 503 自动重试**；**provider 图标系统字号适配**；**团队协作工具与图标**；**插件 watcher 重启修复**。

### ✨ 新功能 (New Features)

- **窗口模型选择跟随云端 SelectedModel** (`app/core/config_sync.py` + `app/main_widget.py`): gitee 同步后窗口模型选择跟随云端 SelectedModel
- **DeepSeek 思考模式兼容** (`app/core/message_content.py` + `app/core/workers/chat_worker.py` + `app/core/workers/subagent_worker.py`): 实现 reasoning_content 必填要求，thinking 模式下兼容 DeepSeek
- **团队协作工具与图标** (`app/utils/icon_name_map.py` + `app/widgets/render_helpers.py` + `icons/`): 新增团队消息/成员列表工具及「团队」「邮件-发送」SVG 图标（明暗双主题）
- **消息卡片渲染与团队模板创建增强** (`app/widgets/message_card.py` + `app/main_widget.py`): skeleton HTML 缓存版本化、innerHTML 替换容错、团队模板子代理创建公共规范抽取
- **命令执行流程与错误处理增强** (`app/core/command_manager.py` + `app/main_widget.py`): 命令降级为 prompt 注入机制、缺失团队成员时的降级流程

### 🐛 问题修复 (Bug Fixes)

- **流式工具块 data-order 计算修正** (`app/widgets/message_card.py`): 修复流式工具块 data-order 计算，确保思考与工具顺序正确；简洁模式与流式完成瞬间折叠框顺序错乱修复（方案D + 清缓存 + 补齐 data-order）
- **SSE 流内 503 请求队列已满自动重试** (`app/core/workers/chat_worker.py`): 503 请求队列已满错误加入自动重试
- **云端主题同步后全量刷新** (`app/core/config_sync.py` + `app/utils/theme_manager.py`): 主题变化时完整刷新所有窗口
- **仅未手动选模型的窗口同步跟随云端** (`app/main_widget.py`): 手动选过模型的窗口不再被云端覆盖
- **gitee 同步后自定义主题静默回退时序修复** (`app/core/config_sync.py`): 自定义主题不被同步时序静默回退默认
- **diff-viewer 默认统一直列视图** (`app/utils/diff_viewer.py`): 默认进入 unified 视图而非并排 split
- **provider 图标系统字号适配** (`app/widgets/cards/settings/provider_setting_card.py`): 适配系统字号 + 字母回退跳过非字母字符
- **插件 watcher 重启** (`app/core/backend.py`): 所有窗口关闭后重新打开时重启插件 watcher

### ♻️ 代码重构 (Refactoring)

- **团队加载与会话恢复逻辑重构** (`app/main_widget.py` + `app/core/message_content.py`): `_handle_team_load` 缺失成员改为 CommandNeedDegrade 异常、废弃 `_degrade_team_load_to_prompt`、消息卡片渲染顺序增强

### 🔧 其他 (Chores & Build)

- 版本号升级至 v0.4.12（pyproject / config / installer / README）

## [v0.4.11] - 2026-08-03

自上一版本以来的变更 | 提交数：60 · 文件变更：204 · +14300/-2486 | 贡献者：dingma, mading

> 重点：**团队协作重大升级（方案 A 阶段 1-5）** —— 会话团队元数据落库（team_run_id/team_name/agent_name 列）、TeamManager run_id 注入、历史面板按 run_id 分组与一键恢复、合并展示 + 归档按钮；**Leader 智能体** —— 并行-DAG 任务编排、角色描述、多窗口上下文工具 schema；**Tab 视觉分组** —— 同团队标签页用 QFrame 容器圈出，独立 tab 与团队框分层；**侧边栏折叠态紧凑化** —— Tab/团队框折叠态不再破版（图标 + 状态条 + 团队首字符头像）；**opencode 免费模型实例级刷新去重** —— 300s 缓存 + in-flight 合并；**OpenCode 默认服务商配置** —— `OPENCODE_SHARED_API_KEY`；**legacy API key 升级机制**；**perf 工具链** —— pympler 对象跟踪与泄漏回归脚手架；**主题摘要 fallback**。

### ✨ 新功能 (New Features)

- **历史面板团队会话合并为单条展示（方案 A 阶段 4-5）** (`app/utils/history_manager.py` + `app/widgets/cards/settings/history_card.py` + `app/main_widget.py`): 取消历史面板顶部团队分组区，团队会话在普通会话列表内按 run_id 合并为单一条目（`get_history_list(merge_team=True)`）并与普通会话按 last_time 天然混排。合并条目卡片（`_TeamGroupCard` 改造）显示 👥 团队名 + 相对时间 + 「N 位成员 · M 轮」元信息 + 团队首问预览（`get_team_first_question` 取最早会话的第一条 user 消息）+ 「恢复团队」「归档」按钮；点击卡片仅切换成员列表展开/收起（不再触发恢复），展开区渲染成员行（角色胶囊 + 标题 + 相对时间），点击成员行直接进入该成员会话（`_load_session_from_record` 公共逻辑，不依赖面板 index 规避漂移）。数据层 `_merge_team_lightweight` 合并条目含 `members` 成员轻量记录列表
- **团队合并条目「归档」按钮** (`app/widgets/cards/settings/history_card.py` + `app/main_widget.py`): 合并条目卡片新增 `archiveRequested(run_id)` 信号 → `HistoryCard.teamArchiveRequested` → `_on_team_archive_requested`：按 run_id 收集全部成员会话 → `archive_sessions_by_run_id` 逐条归档（写 JSON + 从内存/SQLite 删除）；若当前会话属于该团队，归档后自动切换新会话（复用 `create_new_session_state` + `init_new_session_after_archive`）；归档区逐条显示成员会话不合并
- **恢复团队前自动解散现有团队** (`app/main_widget.py` `_disband_current_team_for_restore`): 一键恢复前解散当前团队并关闭全部团队窗口——每个团队窗口停 watcher（幂等）+ `leave_team` + 清空团队标记；主窗口保留并刷新独立模式 UI + 新建空白会话（`_create_new_session` 在 `_team_run_id` 清空后调用，避免污染旧团队会话记录）；其他团队窗口从 Tab 管理器移除 + close。恢复窗口补全 `_join_new_window_for_template` 完整初始化（`track_arrange=False` 旁路模板排列计数、`keep_team_name=True` 保留会话记录团队名）

### 🐛 问题修复 (Bug Fixes)

- **侧边栏折叠态 Tab/团队框紧凑化，折叠后不再破版** (`app/widgets/tab_panel.py`): 折叠侧边栏（46px）时 Tab 项与团队分组框不再挤成一团。折叠态下 TabItem 仅保留图标 + 状态指示条（标题/角色胶囊/关闭按钮隐藏，margin 收紧）；团队框 header 仅保留团队 icon（无项目 icon 时用团队名首字 + 主题色占位，tooltip 显示完整团队名）；团队成员在团队模式下用**角色首字符 + 胶囊同色**绘制头像（原本团队模式隐藏项目 icon，折叠后成员可区分）；展开侧边栏逐控件完整还原（含图标数据/可见性/margin 恢复现场，往返无残留）。三入口（手动 toggle / 启动恢复 / 拖拽展开）统一收口；折叠态新建 Tab、加入团队、布局重建均即时应用紧凑态；hover 不再误弹关闭按钮；团队分组框背景对比度折叠态提升（alpha 40→70）。配套测试 `tests/widgets/test_tab_panel_compact.py`（19 用例）
- **opencode 免费模型实例级刷新去重** (`app/core/models_dev_sync.py`): 新建多标签页时同一实例被重复拉取（每窗口初始化 3s 后各拉一次）。新增模块级 300s 时间窗口缓存 + in-flight 并发合并（同实例并发只发 1 路）+ 失败可重试（仅成功写缓存）+ 缓存键含实例参数 `(config_id, api_url, api_key)`（用户修改 URL/Key 后不命中旧缓存）+ 惰性清理过期条目
- **新建标签页日志刷屏收敛** (`app/main_widget.py` `_on_coding_plan_result`): 无 fetcher 的 provider 每次 request 同步广播 None 到所有窗口，导致「[CodingPlan] 无数据，隐藏圆环」DEBUG 一秒刷 5~8 次。新增 `_coding_plan_hidden` 状态标志，仅显示→隐藏状态变化时打日志，已隐藏则静默返回。配套 `app/core/agent.py`：`[TeamToolsSchema]` 日志 INFO→DEBUG 降级
- **save_session 分支清空被挤出内存的团队会话元数据（数据损坏）** (`app/utils/history_manager.py`): `save_session` 的 `team_run_id/team_name/agent_name` 默认值由 `""` 改为 `None`，新增 None→保留现值语义（与 `update_session` 对齐）——会话被 `_history_limit` 挤出内存（`find_index_by_session_id` 返回 None）后走 save 分支（INSERT OR REPLACE）时不再用空串覆盖团队元数据，防止团队会话从历史分组消失（不可逆）；显式传空串仍清空，全新会话回落空串
- **团队首问预览选错成员** (`app/utils/history_manager.py` `get_team_first_question`): 最早会话判断由轻量记录 last_time（=updated_at 保存时刻，同轮保存区分度不足）改为完整记录 `messages[-1].timestamp` 参与 min 比较，确保选到真正最早产出的成员
- **恢复窗口补全完整初始化** (`app/main_widget.py`): 恢复路径为每个恢复窗口调度 `_join_new_window_for_template`（`_on_agent_changed`/`_apply_agent_command_permissions`/`_refresh_team_ui`/`_start_team_watcher`），恢复后成员窗口可正常收发团队邮件

- **历史面板团队分组 + 一键恢复（方案 A 阶段 3）** (`app/widgets/cards/settings/history_card.py` + `app/main_widget.py` + `app/widgets/tab_manager_window.py`): 历史面板列表顶部新增「团队对话」分组区块——按 run_id 聚合（`_build_team_groups`：agent 去重、按最后活跃时间倒序、无 run_id 会话不进分组），每组显示团队名 + 成员角色胶囊 + 「恢复团队」按钮（`teamRestoreRequested(run_id)` 信号）。点击恢复：`_on_team_restore_requested` 按 run_id 从 SQLite 会话记录收集成员（权威数据源，不受 team.json 成员清理影响）→ `_create_fresh_window` 为每个角色建窗口 + `_branch_session_data` 复用分支机制（showEvent 自动加载历史消息，避免与 `_create_new_session` 竞态）+ 团队标记 + `join_team` 重新登记 + `start_team_run` 新 run_id + 延后网格排列。Tab 分组 key 从 `_team_name` 改为 `_team_run_id`（`_resolve_tab_team_id`：run_id 优先，老窗口回落团队名，非团队空串）——同模板多次加载的多个团队不再混组。新增 11 个测试（tests/widgets/test_team_restore.py）
- **团队运行标识 run_id 注入（方案 A 阶段 2）** (`app/core/team_manager.py` + `app/main_widget.py`): TeamManager 新增 `start_team_run()` / `get_team_run_id()`——`/team --load` 加载模板时生成 uuid4 写入 team.json **顶层**（与 members 平级，`_cleanup_stale_members` 清理成员不丢失），幂等复用；`main_widget` 窗口新增 `_team_run_id` 属性，模板加载/手动加入/延后 join 各路径注入，`_auto_save_current_session` 把 `team_run_id`/`team_name`/`agent_name` 透传到 save_session（update_session 仅当 `_team_run_id` 非空才传，None 保留现值避免普通编辑清空团队元数据）。老团队无 run_id 时保持空串不注入，行为与现状一致。新增 10 个测试（tests/core/test_team_run_id.py）+ 1 个 update_session 保留现值回归用例
- **会话团队元数据落库（方案 A 阶段 1 数据层）** (`app/core/store/session_store.py` / `app/core/store/session_repository.py` / `app/utils/history_manager.py`): sessions 表新增 `team_run_id` / `team_name` / `agent_name` 三列（TEXT DEFAULT ''，第 8 个迁移 `_migrate_add_team_columns`，新库建表即含、老库 ALTER 非破坏性兼容）；`save_session` / `_build_session_record` 签名加 3 个 team 参数（默认空串向后兼容），`update_session` 最小侵入（传 None 保留现有值），`get_history_list` 轻量记录透传 3 字段。为团队会话一键恢复（方案 A）打基础，本阶段不注入、不改 UI。新增 6 个测试（tests/core/test_session_team_columns.py：新库建表/老库迁移/save-load 往返/轻量透传/history_manager 透传）
- **Tab 面板同团队标签页视觉分组** (`app/widgets/tab_panel.py`): `TabPanel` 新增 `set_tab_team(index, team_id)` 公开方法 + `_item_team`/`_team_groups` 数据层；同 team 的 TabItem 被一个 QFrame 容器（`objectName="teamGroup"`）包裹，QSS 给容器加 1px 边框 + 卡片背景 + 6px 圆角；`_rebuild_team_layout()` 按"team 容器置顶在上、独立区在下、stretch 始终在最末"规则重建视觉布局，**`_items` 保持扁平索引不破坏现有索引 API**；`refresh_style` 同步刷新团队分组框样式（主题切换跟随）。`TabManagerWindow.refresh_capsule_for_window` 与 `add_window` 在已有胶囊同步基础上追加 `set_tab_team` 调用，胶囊与分组框共同表达团队归属；新增 `_rebuild_team_layout` 快照保护（`_item_team` 内容 + `_items` 数量未变时直接 return），add_tab/remove_tab 重复调用零开销

### 🐛 问题修复 (Bug Fixes)

- **团队一键恢复成员不全 + 历史会话无法加载** (`app/main_widget.py` + `app/core/team_manager.py` + `app/core/store/session_repository.py` / `session_store.py` + `app/utils/history_manager.py` + `app/widgets/cards/settings/history_card.py`): 恢复后成员从 4 个只剩 1 个、且无法加载历史会话的两个根因修复 —— ①高频保存路径（`_save_current_session_to_history` 三处 save/update + `_on_topic_summary_generated` 首存）缺 team 元数据透传，导致仅 leader 会话落库团队字段，其余成员会话无 run_id/agent_name 无法聚合；②`_create_fresh_window` 分支数据赋值晚于 add_window（showEvent 已触发）导致竞态走 `_create_new_session`，历史消息无法加载。修复：`_create_fresh_window(branch_data=...)` 在 add_window 前赋值 `_branch_session_data` 并透传 project（同步 `_project_label`/backend/tool_executor）；`start_team_run(force=True)` 恢复时强制生成新 run_id；恢复按 agent_name 去重（每组取最新会话、空消息 agent 不跳过建窗口）；恢复路径去掉 `set_template` 篡改（团队名直接用会话记录）；`_TeamGroupCard.update_group` 增量刷新成员胶囊 + `set_team_groups` 清理过期缓存 + 分组用全量历史构建；SessionRepository 新增 `get_by_team_run_id` 直查 SQLite 绕开 `_history_limit=500` 截断。新增 12 个回归用例
- **弹窗遮罩 parent 统一为顶层主窗口（4 处）** (`app/main_widget.py`): `_handle_team_load`（加载团队模板确认框）、`_on_archived_session_deleted`（删除归档会话确认框）、`_on_import_project_from_url`（URL 导入项目输入框）、`_on_project_folder_dropped`（拖拽文件夹新建项目输入框）的 ConfirmDialog / SingleInputDialog parent 由 `self`（嵌入 TabManagerWindow QStackedWidget 的子 widget）改为 `self.window()`（顶层主窗口 TabManagerWindow）。MaskDialogBase 用 parent 尺寸铺遮罩（`setGeometry(0, 0, parent.width(), parent.height())`），传子 widget 导致遮罩只覆盖聊天区、弹窗层级/定位异常；改为顶层窗口后遮罩铺满主窗口、随窗口 resize 同步、以主窗口为中心居中
- **命令卡片枚举值模式 tooltip 显示当前值描述** (`app/widgets/cards/floating/command_card.py` + `app/main_widget.py`): 值选择列表（`--model=` / `--join=` / `--load=` / `--delete=` / `--plugin=` 等枚举参数）原来只显示枚举值本身、无任何描述。现在枚举值条目支持 `{"value", "description"}` 结构（兼容纯字符串），`ValueItemWidget` 携带描述字段，值选择模式下复用列表模式的顶部悬浮气泡（`_desc_tooltip_label`）显示当前选中枚举值的描述；无描述时隐藏气泡（与列表模式空描述行为一致）。数据源带描述：model 用 models.dev/硬编码能力字典 note、agent 用 `Agent.description`、模板用 `Template.description`、插件用 `PluginInfo.description`
- **枚举值选中后 tooltip 残留（review 补充修复）** (`app/widgets/cards/floating/command_card.py`): `_exit_value_selection` 退出值选择模式后不清理 tooltip，气泡残留显示刚选中值的描述直到下次切换/关闭。在方法末尾补 `self._update_desc_tooltip()`（此时 `_value_selection_mode=False`，走 detail 分支自动隐藏气泡）。新增回归用例 `test_exit_value_selection_hides_tooltip`
- **团队模板加载完全新建窗口，不劫持已有标签页** (`app/main_widget.py` `_handle_team_load`): 加载模板时不再把模板 agents 按序分配给所有活跃窗口；模板 N 个角色全部通过 `_safe_duplicate_window` 新建独立窗口，已有窗口保持原样（不切换 agent、不改标题、不改变团队状态、不参与选中/排列）；新增 `before_ids` 用于识别新建窗口；保留 `set_template` 模板上下文注入与 `_do_team_window_arrange` 窗口排列；`_handle_team_save` 描述计数修正为"实际非已关闭窗口数"
- **新建团队成员无会话标题时胶囊不显示** (`app/main_widget.py` `_refresh_team_ui` / `app/widgets/tab_manager_window.py`): 新建空白窗口默认标题为 "飘狐"；`setWindowTitle` 在标题未变时不发射 `windowTitleChanged` 信号，导致 `TabManagerWindow._on_win_title_changed` 胶囊更新分支不执行。新增 `TabManagerWindow.refresh_capsule_for_window(window)` 公开方法，主动基于 `_team_agent_name` 调用 `update_tab_capsule`/`clear_tab_capsule`，不依赖信号触发；`_refresh_team_ui` 末尾在 Tab 模式下调用，胶囊加入/离开团队时立即生效；`TabPanel._update_tab_title` 中团队窗口标题改用会话标题（保持 Windows 任务栏可区分各窗口），不再被 agent 名覆盖
- **团队模式下 Tab 标题被角色名覆盖** (`app/widgets/tab_manager_window.py`): 团队窗口的 Tab 标题改用 `_get_window_session_title` 取会话标题（topic_summary/name），不再用 `windowTitle()`（会被 `_refresh_team_ui` 设为角色名覆盖）；宿主窗口标题同样修复。角色名只进胶囊/边框颜色，不进标题
- **团队分组框背景过透明（review Bug #1）**: `Colors.HOVER_BG = "rgba(255, 255, 255, 0.08)"` 不含 `{alpha}` 占位符，原写法 `.format(alpha=40)` 是空操作（alpha 始终是字面 0.08）。改用 `Colors.CARD_BG.format(alpha=40)`（`"rgba(33, 33, 38, {alpha})"`，含占位符）正确代入 alpha，背景透明度足够高，分组框明显
- **add_tab/remove_tab 未触发布局重建，独立 tab 与 team 容器顺序错乱（review Bug #2）**: `add_tab` 用 `insertWidget(idx, item)` 在已有 team 容器时会把新独立 tab 插到容器之后，违反"team 容器置顶"；`remove_tab` 只脱绑 widget，不重新排序。改为 `add_tab`/`remove_tab` 末尾均追加 `self._rebuild_team_layout()`（有快照保护，开销可忽略），保证布局顺序正确
- **Tab 面板布局损坏：stretch 丢失 + 同一 widget 重复入布局（回归修复）** (`app/widgets/tab_panel.py`): 上述 Bug #2 的浅层修复未覆盖根因 —— `add_tab` 的 `insertWidget(idx)` 使用扁平索引（= 独立 + 团队 tab 数），在团队容器含多 tab 时 idx ≥ 布局实际项数（= 独立 + 容器数 + 1），Qt 将其越界追加到 stretch **之后**；随后 `_rebuild_team_layout` 的 `takeAt(count-1)` 假定"stretch 恒在最末"被破坏，取到的是新 tab 而非 stretch —— 真 stretch 在清空循环中被静默丢弃（永久丢失），新 tab 的 widget item 又被 `addItem` 误当 stretch 加回（同一 widget 重复入布局），引发 tab 框撑开空白 / 加入团队后 tab 消失。修复：`add_tab` 不再直接 `insertWidget`（统一交 `_rebuild_team_layout` 摆放）；`_rebuild_team_layout` 改为扫描全布局定位 `QSpacerItem` 再 takeAt，找不到时重建末尾 `addStretch()` 自愈（修复历史损坏），且只有真 stretch 会被 `addItem`；`_maybe_remove_empty_group` 删除容器前先脱绑内部残留 widget，避免 `deleteLater` 连带销毁仍在 `_items` 管理的 tab。新增 4 个回归用例（`TestTeamGroupLayout`：单 stretch 无重复/团队框置顶独立在后、新 tab 入队不重复、stretch 丢失自愈、移除团队 tab 重建）
- **权限请求回调未触发 TabPanel 问题动画 + 浅色主题「预览参数」按钮文字不可读** (`app/main_widget.py` + `app/widgets/cards/floating/question_floating_widget.py`): ①权限请求回调 `_on_permission_approval_requested` 没有像 `_on_question_asked` 那样调 `_set_ai_state("question")`，导致 `TabManagerWindow._on_ai_state_changed` 听不到 state 变化、`update_tab_question` 不被调用，多 Tab 场景下用户分不清"哪个窗口在等权限"。在权限回调顶部补 `_set_ai_state("question")`（与回答/取消路径的 `streaming` / `idle` 切换对齐）。②`_preview_btn` QSS 硬编码 `color: rgba(255,255,255,0.72)` / `0.95`（白字），浅色主题（如 crema：`realtime_bg: rgba(244, 234, 212, 248)` 浅奶 + `realtime_tag_bg` 浅黄）下变成"白字淡黄底"几乎不可读。改用 `Colors.REALTIME_TEXT` 跟主题走：深色主题=浅色字、浅色主题=深色字，与卡片整体配色一致。新增 2 个回归文件（`test_permission_ai_state_emit.py` AST 检查 + `test_question_floating_preview_button_theme.py` QSS 校验）

### 📝 文档 (Docs)

- **`plugins/system/commands/team.md`**: 新增「用户可见行为」节，明确加载团队模板完全新建窗口（已有窗口不动）、Tab 标题保持会话名/胶囊仅显示角色、团队分组框圈出同团队标签页等行为
- **`CHANGELOG.md`**: 本条目

## [v0.4.10] - 2026-08-01

自上一版本以来的变更 | 提交数：14 · 文件变更：39 · +2192/-403 | 贡献者：dingma

> 重点：**MCP 系统全面强化** — 状态指示灯四态化、事件循环死锁修复、多窗口连接踩踏、超时子进程残留、环境变量继承、mcp 2.0 不兼容兼容、stderr 落盘、超时调整至 90s、缓存 TTL 失效、热重载链路修正、stdio 服务器类型识别、http/sse headers 拉伸、编辑默认 JSON 模式、插件路径占位符兜底；**插件市场** — 源配置迁移至 user-custom、官网跳转、内容查看、字体修复、加载更多按钮跨筛选防御；**CardManager 三向容器共存** — LEFT/RIGHT/BOTTOM 同时启用且系统卡片按可见性切换；**PluginRow 本地目录快捷打开**；**TrayManager 最小化恢复**；**ConfigSyncService 云端单一来源**。

### ✨ 新功能 (New Features)

- **MCP 状态指示灯四态化**: 新增 `MCPState` 状态机（`connecting`/`connected`/`failed`/`disabled`），设置页 MCP 列表左侧圆点按状态着色 —— 启动中**黄色**（全程保持，不再被 3 秒轮询覆盖成灰色）、启动失败**红色**（tooltip 展示子进程真实报错）、已关闭**黑色**（暗色主题降级为深灰保证可辨识）、连接成功**绿色**（tooltip 展示工具数量）。`get_status()` 现在返回注册表中的全部 server（含失败/启动中/已关闭），旧实现只返回连接成功的条目，UI 因此永远读不到"启动中"和"失败"
- **MCP 超时延长与 per-server 配置**: 默认连接超时从 30s 提升至 90s，适配 `npx`/`uvx` 首次冷启动（联网拉包）；支持 per-server `timeout` 字段；`connect_all` 外层不再叠加 60s 超时（服务器较多时会提前放弃并误报失败）
- **MCP 启动失败 stderr 落盘**: 子进程 stderr 默认指向 `sys.stderr`，打包后无控制台导致错误信息全部丢失。改为落盘到 `<appdata>/logs/mcp/<name>.stderr.log`，失败时回读尾部内容拼进错误提示与 tooltip
- **插件市场源配置迁移到 user-custom**: 市场源列表（`sources.json`）从 `.drifox/cache/marketplaces/` 迁移到 `.drifox/plugins/user-custom/marketplaces/`，随 user-custom 插件一起被云端备份/同步；旧版 cache 中的配置首次启动自动迁移（旧文件改名 `.bak` 备份）；拉取的市场数据缓存仍保留在 cache 目录
- **插件市场条目新增官网跳转**: 每个插件条目右侧新增链接按钮，点击在浏览器中打开插件官网 —— URL 优先取元数据 `homepage`/`website`/`url` 字段，无则回退到 `source` 仓库地址（github repo / git-subdir url / raw 地址自动转为仓库主页）
- **插件市场已安装条目新增内容查看**: 已安装插件按钮由禁用的「已安装」改为可点击的「查看」（**绿色高亮**，与「安装」默认蓝、「更新」橙色区分），点击弹出 MaskDialogBase 风格详情弹窗，展示该插件包含的组件清单 —— 🧩 技能、🔌 MCP、📁 命令、🤖 Agents、🔗 Hooks、🎨 主题（内容区可滚动，组件名可选中复制）；未安装仍为「安装」，有新版仍为「更新」
- **PluginRow 本地目录快捷打开**: 已安装插件条目新增 📁 文件夹按钮，点击在系统文件管理器中打开插件所在目录（仅已安装时可见，安装完成后自动出现）；单行渲染失败时不再中断整个批次，错误状态降级展示（160a709e）
- **MCPTools 占位符兜底展开**: 插件 `.mcp.json` 中 `command`/`args` 的 `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PLUGIN_DATA}` 占位符，运行时在 `MCPClientManager._connect_single` 真正拉起子进程前调用 `_resolve_plugin_paths()`，以 `config['_source']`（`.mcp.json` 路径）反推 plugin_root 解析为绝对路径 —— 防御旧进程或旧版打包产物残留字面量占位符导致 `python can't open file '...\${CLAUDE_PLUGIN_ROOT}\mcp\server.py'` 错误；已展开的绝对路径不受影响（幂等）。新增回归测试 `TestResolvePluginPaths`（db8d924e）
- **CardManager 三向容器共存**: LEFT/RIGHT/BOTTOM 三个容器同时启用并存，新增系统卡片可见性更新逻辑，按容器位置独立管理（fef27d20）
- **MCP 缓存失效机制**: `PluginManager.rescan_plugin()` / `rescan()` 在每次重扫后主动调用 `invalidate_mcp_cache()`；热重载处理器在刷新/断连前再兜底失效一次（直接以 `PluginManager` 取最新列表，不依赖卡片存活）。新增回归测试 `tests/test_plugin_manager_mcp_cache.py`

### 🐛 问题修复 (Bug Fixes)

- **MCP 事件循环死锁（服务经常启动不起来的主因）**: `MCPClientManager._disconnect_single` 在持有 `self._lock`（`threading.Lock`）的情况下 `await` 生命周期 Task。事件循环线程持锁挂起期间，其它协程或 UI 线程（3 秒轮询 `get_status()`）一旦 `with self._lock` 就会阻塞该线程，导致锁永远无法释放 —— 整个 MCP 事件循环死锁、设置页卡死。改为锁内只做摘取与状态标记，`await` 全部移到锁外；等待 Task 退出改用 `asyncio.wait`（不会把 `CancelledError` 抛给调用方）
- **MCP 多窗口连接踩踏**: 每个窗口 `_init_mcp_connections` 都会调一次 `connect_all_background`，而 `_connect_all` 开头无条件执行 `_disconnect_all()`，后启动的窗口会把前一个窗口刚连好的连接全部拆掉。改为增量同步：跳过已连接且配置未变的 server、只断开已移除/禁用的 server，并在 `connect_all_background` 增加全局去重（已有一轮进行中则跳过）
- **MCP 超时后子进程残留**: `_connect_single` 超时仅 `task.cancel()` 就返回，不等待回收，且失败记录不入 `_connections`，导致下次连接同名 server 时不会清理旧进程 —— 残留进程占住端口/文件锁使新进程起不来。改为超时后等待 Task 完全退出，并保留失败记录供下次连接前清理
- **MCP 子进程环境变量丢失**: stdio 连接直接把用户 `env` 传给 SDK，而 SDK 仅继承 `DEFAULT_INHERITED_ENV_VARS`（PATH/APPDATA 等十余个），丢掉 `HTTP_PROXY`/`HTTPS_PROXY`/`NODE_EXTRA_CA_CERTS`/npm 镜像等变量，导致 `npx`/`uvx` 拉包失败后超时。新增 `_build_stdio_env()` 显式继承完整父进程环境（过滤 shell 函数导出）；同时丢弃超过 Windows 单变量 32766 字符上限的超长变量（宿主注入的 `ACC_PRODUCT_CONFIG_V3` 等），否则 `subprocess.Popen` 会直接抛 `ValueError: the environment variable is longer than 32767 characters`，使 stdio 子进程创建失败、服务起不来
- **MCP 服务端 mcp 2.0 不兼容（MiniMax 启动即崩）**: `minimax-coding-plan-mcp` 0.0.4 用 `from mcp.server.fastmcp import FastMCP`，但其依赖只写下限 `mcp>=1.6.0` 未锁上界，uvx 拉到最新的 2.0.0 后该模块已被移除 → `ModuleNotFoundError` → 子进程启动即崩、stdio 管道关闭报 `Connection closed`。在 `.mcp.json` 的启动参数加 `--with "mcp<2"` 将 mcp 锁在 1.x（含 `fastmcp`），已对 system 默认与 user-custom/pre_bind 配置同步修改（其它 uvx 启动的 MCP server 若用旧 `fastmcp` API，遇到 mcp 2.0 会同样崩，需同样加 `--with "mcp<2"`）
- **MCP 热重载后列表行不刷新（计数更新、列表不更新）**: `_on_plugin_hot_reload` 仅当 `result['mcp']` 为真才调用 `MCPListSettingCard._refresh()`。但 `PluginManager.rescan_plugin` 在【每次】插件热重载时都会失效 MCP 缓存（`invalidate_mcp_cache`），MCP 列表头部的 `x/x` 计数由 3 秒定时器读取新缓存会自动更新，而列表行（仅由 `_refresh()` 重建）在触发重载的并非 `.mcp.json`（例如插件 Python 代码变更被归类到其它 component）时便残留旧数据。改为只要发生了插件重载（任一组件标志为真）就刷新一次 MCP 列表（仍尊重自触发抑制，避免开关时整卡闪烁）
- **MCP 热重载后列表仍不刷新（抑制标记卡死）**: 上一轮放宽 `result['mcp']` 触发条件后仍无效的根因是 `MCPListSettingCard._suppress_hot_reload` 是布尔标记——全局开关写入的是 `settings`（不触发插件热重载），导致标记永久停在 `True`，把后续所有热重载的列表刷新全部吞掉（现象：头部 `x/x` 计数更新、列表行不更新，因为计数由独立 3 秒定时器读取已失效的缓存）。改为带时间戳的自动过期抑制（窗口 3s > watchfiles 的 2s 防抖）：自触发刷新在窗口内仍被正确抑制，窗口过后自动失效，彻底避免卡死
- **MCP 开关整卡重建（性能）**: 开关 MCP 服务器/全局开关时，`serversChanged` 信号经 GlobalCardController 无条件触发 `MCPListSettingCard._refresh()` 全量重建（删行+重建+processEvents+高度重算），叠加连接结果回调与多窗口热重载广播对共享卡片的重复刷新，一次开关最多触发 3 次整卡重建。改为：开关操作仅做行级更新（`row.set_enabled`/`set_status`），连接结果仅刷状态灯，增删改在操作点自行 `_refresh()`，热重载 MCP 广播对全局唯一共享卡片只刷新一次；断开 `serversChanged → _on_mcp_servers_toggled` 全量刷新链路
- **MCP 编辑卡 http/sse headers 不拉伸**: 类型为 `http`/`sse` 时，headers 输入框原固定 `maxHeight=100` 且行不可拉伸。现改为该类型下 header 行 `setStretchFactor=1`、输入框 `verticalSizePolicy=Expanding`、取消 100px 上限，拉伸填满剩余纵向空间；切回 `stdio` 时恢复约束
- **MCP 编辑卡进入编辑默认 JSON 模式**: 编辑已有服务器时默认进入 JSON 模式（`_stack` 显示 JSON 页），因表单字段不易配置；新增服务器仍默认表单模式。同时修复切回表单模式时未重新调用 `_on_type_changed` 导致 http/sse 字段显隐错乱的既有 bug（切换后 `url`/`headers` 不显示或 `command`/`args` 残留可见）
- **MCP 编辑卡 stdio 服务器被误判为 sse**: 编辑一个同时带 `url` 字段的 stdio 服务器（模板/复制配置常见残留），切到表单后类型下拉框显示 `sse`。根因：`_build_json_from_data` 对 stdio 省略 `type` 但保留 `url`，而 `_normalize_server_data` 的自动识别规则 `elif "url" in data: type="sse"` 把带 url 的 stdio 误判成 sse。修复识别优先级：有 `command` 即判定为 `stdio`（优先于 `url`），其次 `--transport http/sse`、再次 `url`→`sse`、最后兜底 `stdio`
- **插件删除/服务器移除后已启动的 MCP 未断开**: 插件热重载删除插件或 `.mcp.json` 移除服务器后，子进程一直残留运行。新增 `MCPClientManager.disconnect_missing(valid_names)`，并在 `main_widget._on_plugin_hot_reload` 的 MCP 分支刷新列表后调用，断开所有不在「启用服务器列表」中的运行连接（后端删除分支已置 `result['mcp']=True` 以触发此分支）
- **插件市场 item 字体丢失系统字体**: `_retheme` 对 QPushButton 无条件追加 `font-family: '{ff}'`，上下文未提供字体家族（`ff` 为空）时产生 `font-family: ''` 空值，导致按钮/条目字体异常。改为仅当 `ff` 非空时才追加字体家族，否则保持系统默认字体
- **插件市场加载更多按钮跨筛选残留/重复插件**: 防御性修复 —— ① 单行渲染失败（异常市场数据，如 `source` 非 dict）不再中断整个批次，避免部分渲染后 `_rendered_count` 未推进导致后续批次从头重复渲染；② `_compute_homepage` 增加 `source` 类型校验；③ `_remove_load_more_button` 删除全部匹配按钮而非仅第一个；④ `_on_load_more` 增加越界防御，已全部加载时只移除按钮不重复渲染
- **chat-window 欢迎卡片缓存失效**: 项目 / 代理 / 会话变更时使欢迎卡片缓存失效，防止切换到不同上下文后仍展示旧项目/旧代理的欢迎卡片（配合 v0.4.9 的 `_welcome_card_cache` 生命周期管理）
- **OpenAIChatToolWindow 快捷键去重**: 命令重载时清除快捷方式缓存，防止重复注册导致快捷键触发多次
- **message-card 右键复制选中文本**: 修复右键点击消息卡片时复制全部内容的问题，改为仅复制用户选中的文本
- **CodeWebViewer 对话框处理增强**: 修复非遮罩对话框（无 mask）导致 WebView 被意外隐藏的问题，改进对话框显示/隐藏交互
- **TrayManager 最小化窗口状态恢复**: 修复 Windows 上最小化窗口状态恢复异常的问题（4dfc3aaa）
- **ConfigSyncService 云端单一来源**: 强制 Gitee tokens 以云端为单一来源，避免 refresh_token 漂移导致 401/refresh_token 失效问题（05e7f72e）

### 🔧 其他 (Chores & Build)

- **版本号升级**: config / installer / README / pyproject 同步更新至 v0.4.10
- **changelog 增量更新**: v0.4.10 changelog 因 re-release 与 bug 修复多次增补合并

## [v0.4.9] - 2026-07-31

自上一版本以来的变更 | 提交数：59 · 文件变更：187 · +6768/-5624 | 贡献者：dingma, mading

> 重点：**Tab 卡片系统全面重构** — 引入 `GlobalCardController` 统一管理全局卡片生命周期，新增 `TabManagerWindow` / `CardContainer` dock 模式与可调分割器比例；**内嵌卡片替代弹窗** —— file-undo、sub-agent 会话改为全局内嵌卡片；**diff-viewer 增强** —— 侧边栏折叠/窄屏自动折叠/默认视图改为 unified；**团队协作系统 (Team)** 全面升级 —— leader 强制首位、team mail 流式注入、`.teams/` 文件级结果存储、self-identity 步骤、成员状态汇总；**UI 插件行增强** —— 卡片位置右键菜单 + SVG 位置图标 + 插件选项帮助；**消息卡片性能 v2 优化** —— `reorganizeContent` 单次扫描 + 顺序哈希 diff，跳过简洁模式 think-block save/restore；**Gitee OAuth token 双入口收敛** —— 统一到 `_ensure_valid_token()`。

### ✨ 新功能 (New Features)

- **Tab/卡片系统重构** (`GlobalCardController` 上线): 全局卡片控制器统一管理 file-undo、diff-viewer、sub-agent、settings 等系统级卡片的生命周期（注册/显示/隐藏/销毁/缓存失效）；新增 `TabManagerWindow` / `CardContainer` dock 模式与可调分割器比例；`enable_tab_manager` / `panel_width` 等配置项接入；`TabPanel` 内嵌 UI 插件列表完全承担插件入口（旧 `UIPluginEdgeLauncher` 移除）；后台图片迁移到 `TabManagerWindow` 单例以优化多 Tab 性能
- **Tab 管理器增强**: 新增 icon-only 新标签按钮（紧凑模式）；标签标题管理与 UI 更新跨组件统一；侧边栏折叠状态同步；floating card container 新增 `full` 选项用于沉浸式展示
- **Gitee 账户行重构**: 头像+设置按钮双形态（avatar-only / avatar + 名称 + 设置）；紧凑模式可折叠边栏；标签面板整合 Gitee 仓库与绑定操作；OAuth token 刷新收敛到 `_ensure_valid_token()` 并新增 `ConfigSyncService.pause_upload()` 抑制上传竞态
- **内嵌卡片替代弹窗**: `file-undo` 不再使用独立弹窗，改为 global card 内嵌显示并通过 diff viewer 切换；`sub-agent` 会话日志改为内嵌卡片实时刷新（替代 `SubAgentSessionDialog` 弹窗）
- **diff-viewer 折叠与视图优化**: view-bar 新增 `sidebar-toggle` 按钮（chevron 图标）手动折叠侧边栏；窗口宽度 <900px 时自动折叠且不覆盖用户手动选择；`generate_html_report` 默认视图由 `"split"` 改为 `"unified"`
- **团队协作 (Team) 全面升级**: 强制 leader 角色为团队模板首位；新增团队模板上下文管理并接入 session-start hook；成员会话启动增加 self-identity 步骤；成员结果强制写入 `.teams/` 目录；team mail 作为 hook 消息在流式期间注入（`_inject_team_mail_as_hook` + `_injected_team_mails` 跟踪 + 手动停止同步 + 流结束时固化）；成员状态展示包含任务摘要；`TeamManager` 新增 `get_running_tasks` 方法；`TeamManager` / `TeamTools` 增强成员状态上报与代码可读性
- **UI 插件行增强**: `ui-plugin-row` 新增右键菜单用于选择卡片位置（positionChanged 信号）；新增 SVG 位置图标并附 tooltip；插件管理支持 options 与帮助文档
- **使用统计卡片字体自适应**: 根据 context 设置动态调整 usage stats card 字体大小

### ♻️ 代码重构 (Refactoring)

- **团队协作机制重构**: 重构 team 模块并优化对话框层级管理（`refactor(team)`）
- **移除 UI 插件左侧边缘入口**: 彻底删除 `UIPluginEdgeLauncher` 及其辅助函数 `_find_edge_launchers` / `_hide_edge_launcher` / `_show_edge_launcher` / `_hide_shared_launcher`，移除 `main_widget.py` 中的导入/实例化/resizeEvent/主题刷新/热重载调用，插件入口完全由 `TabPanel` 内嵌 UI 插件列表承担

### ⚡ 性能优化 (Performance)

- **消息卡片 `reorganizeContent` 增量重排 v2**: 将原版 4 次独立 forEach 重复扫描合并为单次扫描；过期 think-block / tool-block 清理合并为单次遍历；引入"顺序哈希 diff"取代无脑 sort+appendChild（流式期间 ~80% updateContent 走快路径跳过 sort）。理论加速 1.4x~2.6x（按块数），长会话（100+ 块）卡顿显著改善
- **简洁模式跳过 `think-block` 展开状态 save/restore**: 简洁模式下 completed 思考块是 `think-compact` 纯文本行（无折叠），`expandedStates` Map 始终为空。短路 save/restore 两段 querySelectorAll + Map 操作；非简洁模式行为不变
- **Tab 管理器批量更新**: `TabManagerWindow` 新增窗口添加时的批量更新机制，减少布局开销

### 🐛 问题修复 (Bug Fixes)

- **chat-window 欢迎卡片生命周期**: `_hide_welcome_cards` 仅调用 `hide()` 而未 `removeWidget()`，导致多次显示/隐藏循环后布局中累积孤儿 widget；`_load_message_batch` 卡片提取循环改用逆序遍历避免索引漂移；显式跳过 `_is_welcome` 标记的卡片，防止被 `_cache_current_session_cards` 的 `deleteLater` 误删（欢迎卡片由 `_welcome_card_cache` 独立管理生命周期）
- **chat-window 欢迎卡片不刷新（点击新建 / 切换项目后）**: `_welcome_card_cache` 仅按 `_window_id` 缓存，但卡片内容依赖 `_current_project` / `_current_agent`；原唯一失效路径 `sip.isdeleted(cached)` 存在竞态——`_clear_chat_area` 之后 `QTimer.singleShot(0, _show_initial_welcome)` 可能在 `deleteLater` 实际执行前先触发，导致缓存命中返回旧卡片。新增 `_invalidate_welcome_card()` 显式失效辅助，在 `_create_new_session` 入口、`_on_project_selected`、`_on_agent_changed`、`_on_archived_session_deleted/renamed` 等变更点同步 pop 缓存 + `setParent(None)` + `deleteLater()`，消除竞态。同窗口内无变化的重复调用仍走缓存命中路径，性能优化保留
- **Gitee OAuth token 刷新双入口**: 收敛 `ConfigSyncService._sync_token` 的 token 刷新入口到 `GiteeOAuthBackend._ensure_valid_token()`，避免与 `GiteeUploader` / `gitee_card` 并发/时序竞争导致 Gitee `refresh_token` rotation 漂移（内存新、磁盘旧 → 下次冷启动必报"refresh_token 无效或已被撤销"）。新增 `ConfigSyncService.pause_upload()` 公开方法，token 刷新写盘前抑制 watcher 防止误触发云端上传
- **MaskDialog 遮罩穿透 webview 文字**: 提升 `ConfirmDialog` / `InfoDialog` 遮罩 alpha（76 → 180 / 140），避免暗色遮罩被 Chromium GPU 合成的代码块文字"透出"
- **message-card 紧凑模式流式滚动**: 防止 compact 模式下 tool-content 在流式输出时意外滚动到顶部
- **OpenAIChatToolWindow 压缩缓存**: 简化 `_compaction_cache_summary` 消息逻辑
- **团队邮件流式注入**: team mail 在流式期间未通过 hook 注入（Qt 事件循环调度延迟导致）；新增直接 `TeamManager` 检查解决；后续 revert 后再次以更准确方案修复
- **团队模板输出目录**: 修正 team template 文件创建的输出目录描述与实际行为不一致
- **message_card 事件过滤器**: 修正事件过滤器事件号（24→17、9→8）及信号（`destroyed` → `finished`）
- **persister JSON 死循环**: 格式化持久化内容避免单行 JSON 死循环
- **tab 命令带后缀不触发参数卡片**: 带 `-agent` / `-prompt` 后缀的命令名无法触发参数卡片（命令解析修正）
- **strip system-reminder wrapper**: 团队邮件与 subagent 消息中剥除 `system-reminder` 包装（防止其意外泄漏给模型）
- **team tools 权限检查**: 团队成员绕过所有权限检查（安全修复）
- **异常处理统一**: 多文件改用元组语法捕获多个异常，提升代码可读性和一致性
- **CommandCard 可见性同步**: 在 show/hide 事件中同步可见性状态，防止数据刷新问题
- **TrayManager / TabManagerWindow 注册**: 改进异常处理并确保 `TabManager` 引用正确注册
- **label styling 优化**: 统一标签样式与刷新逻辑，提升性能与一致性
- **CardContainer resize + CommandItem + SystemCardFrame**: 优化 resize 处理与动画性能，防止布局级联问题；精简 `CommandItemWidget` 标签样式应用以提升状态变更性能；细化 `SystemCardFrame` 样式刷新逻辑与布局处理
- **系统 UI 插件硬编码优化**: 清理 `app/widgets/` 内 UI 插件的硬编码内容

### 🔧 其他 (Chores & Build)

- **question-floating 主题色 token**: ruff format + 改用主题色 token（导航按钮颜色统一从主题读取）
- **global-card 占位符**: 为 file-undo diff 相关辅助方法新增占位签名

### 📚 文档 (Docs)

- **消息卡片紧凑模式性能优化说明**: 文档化 `reorganizeContent` v2 与 think-block save/restore 跳过的设计权衡

## [v0.4.8] - 2026-07-29

自上一版本以来的变更 | 提交数：115 · 文件变更：275 · +17684/-9501 | 贡献者：dingma, mading, martin98-afk

> 重点：**Tab 管理器全面重构** — 自定义标题栏、侧边栏折叠/展开、渐变玻璃风格、项目级图标感知、Gitee 账户快捷入口；Hook 管理器并行执行；消息卡片渲染性能优化；异常处理元组语法一致性重构；窗口拖拽性能优化与卡顿分析器。

### ✨ 新功能 (New Features)

- **Tab 管理器全面重构**: 全新的 Tab 模式 UI —— 自定义标题栏（拖拽/最大化/恢复）、侧边栏折叠/展开动画（QVariantAnimation）、渐变玻璃风格面板（glow 指示器、渐变激活态）；品牌头部、新标签按钮、分割线、会话计数；标签分支信号（tabBranchRequested）、项目级图标感知（DPI 感知、颜色缓存、主题变更失效）；侧边栏折叠/展开同步（IconStripWidget）
- **Tab 管理器开关与环境集成**: LLM 设置页添加 Tab 管理器切换开关；系统托盘支持 Tab 模式（简化菜单、热键切换）；新增 `enable_tab_manager`、`panel_width` 等配置项；main.py 添加 Tab 模式启动路径；`_duplicate_window` 添加 Tab 模式分支
- **Gitee 账户快捷入口**: 标签面板中添加 Gitee 账户快捷操作，支持仓库和绑定操作；紧凑绑定状态展示；渲染时避免同步
- **消息卡片性能优化**: 减少安全渲染间隔，使用异步 JavaScript 执行；缓存渲染 HTML，延迟移除字符计数；ElidedLabel 悬停时展示完整文本
- **Hook 管理器并行执行**: 实现 UI 线程安全的并行 Hook 执行机制；增强命令执行和环境变量处理，兼容第三方插件
- **主题刷新协调器**: 实现 ThemeRefreshCoordinator 优化主题管理和缓存刷新
- **FileTreeView 重构**: 将 FileTreeWidget 重构为 FileTreeView，增强模型和委托结构
- **UI 插件注册增强**: 新增 `system_card` 参数，支持更好的卡片管理
- **卡片容器智能展开**: showEvent 检测可见卡片时自动展开
- **Tab 管理器 resize 处理增强**: 优化 resize 事件处理和事件过滤，实现更流畅的 UI 体验
- **ChatBackend hook_states.json 变更检测**: 新增对 hook_states.json 文件的变更检测支持，提升 Hook 状态管理的实时性和可靠性
- **Gitee 账户行设置入口改进**: GiteeAccountRow 使用设置按钮替换更多按钮，提供更直观的设置访问入口
- **Gitee 弹出窗口置顶切换**: GiteeMorePopup 添加窗口置顶开关功能，提升多窗口场景使用便捷性
- **Tab 管理器窗口置顶功能**: TabManagerWindow 实现窗口置顶功能（基于配置开关），方便用户固定管理窗口
- **拖拽卡顿分析器 (Drag Stall Profiler)**: 实现窗口拖拽期间的 UI 卡顿检测与分析功能，优化 LSP/MCP 命令可用性检查机制，提升拖拽场景下的 UI 响应性能

### 🎨 样式改进 (Style)

- **文件树字体样式统一**: 更新文件树字体大小和样式，提升 UI 一致性
- **GiteeAccountRow 布局间距优化**: 调整 Gitee 账户行布局边距，改善视觉一致性和间距一致性
- **GiteeCard 控件尺寸调整**: 优化头像和按钮尺寸，提升 UI 一致性
- **GiteeAccountRow 设置按钮改进**: 调整设置按钮大小和图标样式，优化交互体验
- **Tab 管理器标签区域视觉增强**: 为标签区域添加圆角边框和统一边距，提升细节质感

### 🐛 问题修复 (Bug Fixes)

- **Tab 管理器窗口稳定性增强**: 修复 macOS 隐藏适配（hide() 替代 close() 保留状态）；SIP 保护；QColor 解析、windowTitle 同步、任务栏标志修复；首次启动居中、保存/恢复位置限制到屏幕边界；销毁 title_bar 重建修复；拖拽性能诊断；主题刷新处理增强；流式/错误状态指示修正；标签图标缩放和主题变更刷新；EdgeLauncher 检测与保护；关闭时事件处理优化；分割器手柄宽度优化，在 relayout 时保留用户调整
- **代码查看器滚动修复**: 优化 CodeWebViewer 滚动事件处理，防止意外自动滚动
- **差异视图主题适配**: render_helpers 中差异分隔符颜色跟随主题切换
- **UI 插件处理增强**: 改进 TabPanel 和 TabManagerWindow 中问题状态指示
- **ConfigSyncService 全量手动同步修复**: 确保配置文件到内存的全量手动同步，绕过静默加载失败问题
- **Tab 管理器 Aero Snap 修复**: 在 showEvent 中重新应用 _enable_snap_layout 并清理原生拖拽状态，支持 Windows Aero Snap
- **ToolPopupDialog Aero Snap 支持**: 添加 nativeEvent 和 WS_THICKFRAME 实现 Windows Aero Snap 窗口吸附
- **ChatBackend 热重载修复**: 跳过未知组件目录外的插件变更，避免无效热重载
- **输入框高度抖动修复**: 调整底部输入框高度计算，添加 resize 消除回弹
- **ChatBackend 插件根目录删除卸载修复**: 修复插件根目录被删除时无法触发完整组件卸载的问题，提升插件生命周期管理可靠性
- **拖拽卡顿分析器默认禁用**: 默认关闭拖拽分析器，避免在非调试场景下产生意外性能影响
- **Tab 管理器拖拽检测增强 (nativeEvent)**: 使用原生 Windows 事件（WM_NCHITTEST/WM_MOVING）增强拖拽检测，提升拖拽期间性能表现
- **Tab 管理器拖拽处理与布局优化**: 优化拖拽处理和布局更新逻辑，防止拖拽窗口时 UI 阻塞
- **Tab 管理器窗口拖拽检测与性能优化**: 实现窗口拖拽检测与性能优化机制；FileTreeCard 拖放确认对话框增强；FileTreeView 外部拖放处理优化与自拖拽防护

### ♻️ 代码重构 (Refactoring)

- **异常处理语法统一**: 多处改用元组语法捕获多个异常，提升代码可读性和一致性（涉及 25+ 个文件）
- **LSP 集成重构**: 优化 CodeWebViewer 的 LSP 集成，提升性能
- **清理废弃规范**: 移除 Tab 面板渐变玻璃设计规范及相关测试文件
- **TabManagerWindow 标准窗口重构**: 切换到标准系统窗口以支持原生 Aero Snap，大幅精简代码（-743 行）

### ⚡ 性能优化 (Performance)

- **快照与保存性能优化**: O-01 token 单次扫描累加计算 + O-04 _content_hash_cache 线程安全改进，减少重复计算提升响应速度

### 📚 文档 (Docs)

- Tab 面板渐变玻璃设计实现计划与规范（含折叠功能）
- Gitee 账户行实现计划与设计规范
- UI 插件列表实现计划与设计规范
- Tab 管理器设计规范更新与审查反馈

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
- **Windows Shell 元字符正则增强**: 修正 `WINDOWS_SHELL_META` 正则匹配逻辑，正确处理 Windows 路径分隔符 `\` 与字面 `^`，避免路径误判
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
