# Changelog
All notable changes to this project will be documented in this file.

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