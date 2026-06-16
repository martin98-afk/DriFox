# 项目开发规范

本文件为 AI Agent 提供项目操作手册与约束清单，确保 Agent 行为可控、可复现。
 
---

## 1. 目标与边界

### 允许的操作
- **有关键文档存在时，优先以关键文档作为项目路径进行探索**
- 读取、修改顶层文档：`README.md`、`AGENTS.md`、`CONTRIBUTING.md` 等
- 读取、修改 `docs/`、`prompts/`、`skills/`、`tools/config/`、`tools/external/` 下的文档与代码
- 执行项目规定的 lint、检查、构建命令
- 新增/修改功能、修复问题
- 提交符合规范的 commit

### 禁止的操作
- 修改 `.github/workflows/` 中的 CI 配置（除非任务明确要求）
- 修改 `LICENSE`、`CODE_OF_CONDUCT.md`
- 在代码中硬编码密钥、Token 或敏感凭证
- 未经确认的大范围重构

### 敏感区域（禁止自动修改）
- `.github/workflows/*.yml` - CI/CD 配置
- `.env*` 文件（如存在）

---

## 2. 推荐执行路径

```bash
# 1. 拉取最新代码
git pull --rebase origin develop

# 2. 初始化依赖（如有需要）
# ... 项目特有命令

# 3. 运行 lint 检查
# ... 项目特有命令

# 4. 执行修改任务
# ...

# 5. 再次验证
# ... 项目特有检查命令

# 6. 提交变更
git add -A
git commit -m "feat|fix|docs|chore: scope - summary"
git push origin develop
```

---

## 3. 修改约束

### 架构原则
- 保持根目录扁平，避免巨石文件
- 遵循项目现有架构，不随意改动

### 禁止行为
- 禁止"顺手重构/大范围改动"除非任务明确要求
- 禁止删除现有测试用例（除非任务要求）
- 禁止在代码中硬编码敏感信息

---

## 4. 风格与质量标准

### 格式化工具
- 遵循项目现有代码风格
- 使用项目已有的格式化工具

### 命名约定
- 文档、注释、日志使用中文
- 代码符号统一英文且语义直白
- 文件名小写加中划线或下划线（遵循现有风格）

### 设计品味
- 优先消除分支与重复
- 函数单一职责且短小

---

## 5. 提交规范

遵循简化 Conventional Commits：
```
feat|fix|docs|chore|refactor|test: scope - summary
```

---

## 6. 强制同步规则

**任何功能/命令/配置/目录/工作流变化必须同步更新相关文档**

不确定的内容用 TODO 标注，不允许猜测。

---

## 7. 当前工作状态（2026-06-05 更新）

### Worktree
- 主仓库: `D:/work/DriFox`（分支: dev）

### 已完成
- **代码质量分析**: 扫描 `app/core/`、`app/widgets/`、`app/tools/`、`app/gateway/`，产出 13 项关键问题清单
- **交互式清理（5 阶段）**: 枚举引用修复、`$null` 删除、重复函数合并、死码清除、`bare except:` 修复、`print()→logger`、widget 导入声明修复、`QDesktop→QScreen API`
- **history_compactor.py 拆分**: 1518→~1050 行，提取 `compression/` 子包
- **backend.py 拆分**: 1726→955 行（-45%），提取 3 个独立模块：`plugin_hot_reload.py`、`mcp_discovery.py`、`gateway_bridge.py`
- **app/core/ 按领域分包整理**: 12 个平面文件移入 6 个子包（agent/、command/、chat/、plugin/、provider/、tool/），9 个文件保留原位。全量 import 引用已批量更新，`import app.core` 验证通过
- **修复插件路径计算**: 因文件移入子包导致 `_SYSTEM_PLUGIN_DIR` 路径少算一层（指向 `app/plugins/` 而非 `plugins/`），改用 `parents[3]` 修复。`tool_executor.py` 中 2 处 fallback 路径同步修复
- **修复 PluginManager.get_plugin_path 不存在**: `plugin_hot_reload.py` 的 `build_plugin_path_index()` 调用了不存在的 `pm.get_plugin_path()`，改用 `plugin.path`（`PluginInfo` 数据类已有该属性）。自提取时即存在的 bug，从未生效过

### 进行中（待实施）
- 无

### 已完成
- **适配 Claude Code 插件路径到监控**: 
  - `app/core/plugin_manager.py`: 添加 `_CLAUDE_USER_SKILLS_DIR`/`_CLAUDE_PLUGIN_CACHE_DIR` 常量 + `_discover_claude_plugins()` 方法，在 `initialize()` 和 `rescan()` 中均集成 Claude 插件扫描，优先级：系统 → Claude → DriFox 用户（最高）
  - `app/core/backend.py`: `_start_plugin_watcher()` 的 watch_paths 中新增 `~/.claude/skills/`（自动创建目录）和 `~/.claude/plugins/cache/`（仅存在时监听）

### 新增功能（2026-06-05）
- **会话自动关联 worktree**: sessions 表新增 `worktree_path` 列（含迁移逻辑）
  - 保存会话时自动检测当前工作目录是否为 git worktree，若是则将 worktree 路径保存到会话记录
  - 加载会话时自动切换到该会话关联的 worktree（通过 memory_manager 设置工作目录 + 关键文档）
  - 切换逻辑覆盖：tool_executor 同步、分支标签刷新、记忆卡片 UI 刷新、memory_card._instance_workdir 同步
  - 兼容旧会话（worktree_path 为空时跳过自动切换）
  - 两个加载入口均已覆盖：`_load_history_session_from_popup` + `_switch_to_session_by_id`
  - 🐛 **已修复**: 不在 worktree 中时不再传 worktree_path（否则 update_session 会覆盖已有值）
  - 🐛 **已修复**: _switch_to_worktree 同步更新 memory_card._instance_workdir`

### 新增功能 (2026-06-16)
- **Gitee 图床上传工具**: 将本地文件上传至 Gitee 仓库返回公开下载链接
  - `app/gateway/utils/gitee_uploader.py`: GiteeUploader 单例类（upload_file / upload_bytes）
  - `app/utils/config.py`: 新增 gitee_enabled/token/owner/repo/path/branch 配置项
  - `app/tools/file_tools.py`: 新增 gitee_upload() 方法，AI 可通过 function calling 调用
  - `app/core/tool_executor.py`: TOOL_SCHEMAS 注册 + dispatch map
  - `app/gateway/base.py`: BasePlatformAdapter 新增 send_file_via_gitee() + _try_gitee_upload()
  - `app/gateway/adapters/wecom.py`: send_image/send_file 自动上传到 Gitee 并追加外链
  - Gitee API: `POST https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}`

### 待处理
- **main_widget.py (10,139 行) 拆分**: 架构蓝图已完成但未实施
- **P0 SerpAPI 硬编码密钥**: 用户选择跳过
- 10 个 print() 在 docstring/注释中（非代码逻辑，跳过）
