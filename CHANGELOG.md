# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]

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

## [v0.2.11] - 2026-06-24

自上一版本以来的变更 | 提交数：34 · 文件变更：26 · +4313/-350 | 贡献者：dingma, drifox-bot

---

### ✨ 新功能 (New Features)

- **桌宠系统（PixelPetWidget）**: 全新交互式像素狐狸桌宠，支持多状态动画
  - 新增：桌宠显示开关设置（外观样式）、AI 状态通知与恢复处理
  - 新增：拖拽状态（含挣扎动画）、警告状态（存档/工作树操作时触发）
  - 新增：哭泣、惊喜行为、情绪徽章、错误状态视觉增强
  - 新增：主题系统支持 `get_theme_pet()` 自定义桌宠外观
- **drifox-dev 技能升级**: 升级为有状态技能，支持 JSON 状态持久化与自动快照

### 🐛 问题修复 (Bug Fixes)

- **桌宠拖拽鬼影**: 使用 `CompositionMode_Source` 透明清除替代 `eraseRect`，消除残留像素
- **桌宠睡眠姿态**: 从扁平蜷缩球体重新设计为侧卧睡姿，造型更自然
- **桌宠状态恢复**: 闪烁状态后确保正确恢复原始状态，修复状态机竞态
- **桌宠帧间隔优化**: 整合粒子更新到主帧循环，提升动画流畅度
- **Agent 加载去重**: 重复加载 agent 时跳过已存在的条目，避免重复注册
- **LSP 工作目录**: 使用项目根目录（`backend.py` 上三层）替代 `os.getcwd()`，防止 pyright 扫描上级目录下所有项目
- **SQLite 兼容性**: 修复 `PRAGMA auto_vacuum` 返回值可能为整数（2）而非字符串（'incremental'）的兼容问题
- **MCP 日志降级**: 禁用服务器日志从 `info` 降为 `debug`，减少日志噪音

### ♻️ 代码重构 (Refactoring)

- 桌宠精灵图扩展至 12x12 网格，新增 curled sleep + dragging + warning 行
- 桌宠从主题系统或内置 Qt 资源加载精灵图，剥离硬编码路径
- 移除闲置行为配置，简化闲置行为处理逻辑
- 优化精灵图加载与定位计算
- `main_widget` 注册桌宠为主题刷新目标

### 🔧 其他 (Chores & Build)

- 版本升级至 v0.2.11（config / installer）
- 重新生成 Qt 资源文件，适配 192x192 桌宠精灵图
- 移除废弃的 `plugins/system/pets/` 目录
- 更新桌宠图标设计

## [v0.2.10] - 2026-06-23

自上一版本以来的变更 | 提交数：16 · 文件变更：157 · +3730/-2748 | 贡献者：dingma

---

### ✨ 新功能 (New Features)

- **`/release` 工作流命令**: 新增自动化发布命令 (`plugins/system/commands/release.md`)
  - 支持自动生成 CHANGELOG、打 tag、推送触发 CI、更新 Release Notes
  - 参数 `--dry-run` 仅预览不推送
- **CI/CD 流水线增强**: 改进 `.github/workflows/release.yml`
  - 新增 lint 和 import check job，失败则跳过后续构建
  - release job 自动从 `CHANGELOG.md` 提取内容作为 Release Body
- **drifox-dev 开发技能**: 新增 `plugins/system/skills/drifox-dev/SKILL.md`，提供开发指南文档
- **命令过滤增强**: 命令卡片支持按多类型过滤和按关键字搜索
  - `app/widgets/bottom_input_area.py`、`app/widgets/cards/floating/command_card.py`
- **Linux 打包与 DMG**: 增强 Linux 打包流程和 DMG 创建逻辑
  - `build.py`、`create_dmg.py`

### 🐛 问题修复 (Bug Fixes)

- **CI uv 版本与依赖**: 升级 uv 到最新版本以支持 cp314 wheel；移除已废弃的 `libegl1-mesa` 依赖
- **CI uv 配置**: 使用 `setup-uv` 的 `python-version` 参数自动安装 Python 3.14，避免 uv 0.5.x wheel 解析 bug
- **release.yml**: 修复 YAML 语法错误，确保 CI 工作流配置正确
- **macOS 代码签名**: CI 在打包后增加 ad-hoc 签名步骤（`codesign --force --deep --sign -`），解决 PyInstaller 默认产物未签名导致 macOS Gatekeeper 拦截的问题（首次运行仍需右键 → 打开）
- **CI import check**: 避免 PyQt5 lazy load 触发的 Linux SIGSEGV
- **pyproject.toml 依赖组**: 修正 `all` 依赖组为 PEP 735 标准 `include-group` 语法
- **ruff 配置**: 缩窄规则到 E/F 并加入 ignore 列表，适配当前代码库
- **build.py**: 优化清理脚本的 print 语句，提升日志清晰度
- **main.py**: 恢复 `from app.utils import icons_rc` 副作用导入（注册 Qt 图标资源）并添加 `# noqa: F401` 防止 ruff 误删

### ♻️ 代码重构 (Refactoring)

- **pyproject.toml**: 重整依赖组（`[dependency-groups]` 为唯一依赖组定义），添加 ruff 配置；移除 `[project.optional-dependencies]`

### 🎨 样式改进 (Style)

- **ruff auto-fix**: 自动修复 2133 个可修复 lint 问题，并将规则缩窄到 E/F

### 🔧 其他 (Chores & Build)

- **`/release` 工作流**: 增加版本号升级阶段（阶段 B），统一修改 3 个版本号文件
- **gateway 组临时缩减**: `pyproject.toml` 中 gateway 组暂时只保留 `dingtalk-stream`，其余依赖（python-telegram-bot、discord.py、slack-sdk、lark-oapi、aiohttp）注释掉，便于 CI 调试（pywin32 实际来自 mcp 而非 gateway）
- **版本号升级**: v0.2.9 → v0.2.10
  - `pyproject.toml` 第 3 行（不带 v）
  - `app/utils/config.py` 第 242 行（带 v）
  - `dist/installer.iss` 第 7 行（带 v）