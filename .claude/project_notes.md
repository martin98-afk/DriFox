# 项目开发规范

## 1. 目标与边界

### 允许的操作
- 读取、修改 `README.md`、`AGENTS.md`、`CONTRIBUTING.md` 等顶层文档
- 读取、修改 `docs/`、`prompts/`、`skills/`、`tools/config/`、`tools/external/` 下的文档与代码
- 执行 lint、检查、构建命令；新增功能、修复问题；提交符合规范的 commit

### 禁止的操作
- 修改 `.github/workflows/` 中的 CI 配置、`LICENSE`、`CODE_OF_CONDUCT.md`
- 在代码中硬编码密钥、Token 或敏感凭证
- 未经确认的大范围重构

## 2. 提交规范

遵循简化 Conventional Commits：
```
feat|fix|docs|chore|refactor|test: scope - summary
```

## 3. 修改约束

- 保持根目录扁平，避免巨石文件
- 禁止"顺手重构/大范围改动"除非任务明确要求
- 禁止删除现有测试用例（除非任务要求）
- 文档、注释、日志使用中文；代码符号统一英文

## 4. 强制同步规则

任何功能/命令/配置/目录/工作流变化必须同步更新相关文档，不确定的内容用 TODO 标注。

---

## 关键文件索引

| 文件 | 说明 |
|------|------|
| `app/main_widget.py` | 主窗口（6000+ 行），重型导入需延迟 |
| `app/tool_popup.py` | 工具弹窗，必须在 `main()` 内延迟导入（触发 `Colors.refresh()`） |
| `app/core/gateway_engine.py` | Gateway 引擎，macOS/Windows 差异注意 |
| `app/core/engines/` | 引擎模块重构新路径 |

## 关键修复记录

### 2026-05-21 macOS 启动报错 `'_popup_btn'`
**文件**: `app/tool_popup.py` → `ToolPopupDialog.eventFilter`
**根因**: `_popup_btn` 定义在 `ToolWindowTitleBar`，但 `ToolPopupDialog.eventFilter` 引用了 `self._popup_btn`。macOS 安装了全局事件过滤器，所有控件事件都经过它，Windows 上过滤器未安装所以隐藏了 bug。
**修复**: 移除 `eventFilter` 中冗余的 `_popup_btn` hover 代码（已由 CSS `ToolButton:hover` 实现）。

### 2026-05-21 启动性能优化
- `main_widget.py` → 重型导入移至方法内部（`AutoLoopConfig`, `AutoLoopWorker`, `UpdateChecker`, `DiffViewerWindow` 等）
- `hook_manager.py` → `_resolve_command_cwd()` 增加 30s 缓存，避免每次 SessionStart 扫描磁盘
- `git_worktree.py` → `_CACHE_TTL` 从 2s 提高到 30s；`detect_git()` 用 `os.path.isdir()` 替代 `os.path.exists()` 避免文件路径报错

### 2026-05-20 开机自启不生效
**根因**: `SwitchSettingCard` 保存到 qfluentwidgets 默认文件而非项目配置文件。
**修复**: `llm_settings_card.py` → `_on_toggled` 增加 `self.cfg.save()`；`startup_manager.py` → 新增 `sync_auto_start_from_config()` 启动同步。

### 2026-05-20 加载历史会话后消息被清空
**根因**: `ConversationCore.create()` 内部创建全新空 `SessionManager`。
**修复**: `conversation/core.py` → `create()` 增加可选 `session_manager` 参数；`chat_engine.py` → 传入共享的 `self._session_manager`。

### 2026-05-20 Gateway/AutoLoop 会话保存异常
- 会话标题错误 → `gateway_engine.py` → `on_finished` 增加时间戳去重
- AutoLoop 消息丢失 → `auto_loop_worker.py` → `on_messages_updated` 用覆盖式更新替代追加
- Gateway 无用户消息 → `gateway_engine.py` → `add_session()` 追加后设置 `current_index`
- 钉钉收到两遍回复 → `gateway/base.py` → 增加 `_active_message_ids` 去重

### 2026-05-20 错误处理模块重构
`app/core/` 下的 error_handler 移动到 `app/core/workers/error_handler/`，旧文件保留为薄重导出层。

### 2026-05-20 AutoLoop completion_signal 修复
- `auto_loop_card.py` → `_default_config = AutoLoopConfig()` 读取默认值
- `prompt_composer.py` / `engine.py` → 硬编码 "DONE" 改为 `config.completion_signal` 动态值

### 2026-05-20 插件切割技术债清理
`app/tool_window.py` + `app/side_dock_area.py` → 合并为 `app/tool_popup.py`。
