# 工具 / Hook / 权限系统开发

> SKILL.md 在任务涉及「新增工具、修改 Hook、调整权限」时分派到这里。

---

## 一、工具系统（`app/tools/`）

**BuiltinTools 动态派发**：通过 `__getattr__` 遍历工具模块，**不需要在 `__init__.py` 里手动委托**。

| 工具集 | 模块 | 核心工具 |
|--------|------|---------|
| FileTools | file_tools.py | read / write / edit / multi_edit / grep / glob / list |
| TerminalTools | terminal_tools.py | bash / bg_start / bg_stop / bg_logs / bg_list |
| WebTools | web_tools.py | webfetch / websearch |
| TaskTools | task_tools.py | todowrite / todoread / stage_files |
| MCPTools | mcp_tools.py | mcp_list_servers + MCP 动态工具 |
| AutomationTools | automation.py | mouse / keyboard / screenshot |
| DiagnosticsTools | diagnostics_tools.py | get_diagnostics / lsp |

**新增工具三步**：
1. 在对应模块加函数，签名按 Agent 约定
2. 工具名 → 模块映射由 `__getattr__` 自动处理（无需改 `__init__.py`）
3. 更新 AGENT 工具权限（`agents/*.md` 里的 `tools:`）

## 二、Hook 系统（`app/core/hook_manager.py`）

| 事件 | 触发时机 | 可阻断？ |
|------|---------|---------|
| `SessionStart` | 新会话启动 | 否 |
| `PreUserMessage` / `PostUserMessage` | 用户消息前后 | 否 |
| `PreAssistantMessage` | AI 回复前 | 否 |
| `PreToolUse` / `PostToolUse` | 工具执行前后 | **前者可 BLOCK** |

**Hook 类型**：
- `command`：bash/Python 命令
- `prompt`：直接文本内容插入消息列表（`__prompt__:` 前缀识别）
- `prompt_command`：动态 prompt 注入

**坑点（已知）**：
- `PostToolUse` matcher 之前只匹配用户消息首段，导致 `Write|Edit` 不触发。修复方案：同时大小写不敏感匹配工具名
- `prompt` 类型 hook 应总是加入消息列表，不该被事件过滤

## 三、权限系统（`PermissionResolver`）

策略：**allow / ask / deny**

支持通配符（`Write*`、`Bash(git:*)`）。改权限前先 grep `PermissionResolver` 看现有规则冲突。

## 四、调试工具集

```bash
# 列出当前所有 MCP 服务器状态
python -m <drifox> --mcp-list

# Lint 工具 / Hook / Agent
ruff check app/tools/

# 看 Agent 解析
python -c "from app.core.agent import AgentManager; AgentManager.get_instance().list()"
```
