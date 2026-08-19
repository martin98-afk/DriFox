# 项目开发规范

AI Agent 操作手册与约束。

---

## 1. 目标与边界

**允许**: 读写顶层/docs/skills等；执行 lint/构建/测试；增改功能修复问题。
**禁止**: 改 CI 配置（除非明确要求）、改 LICENSE、硬编码密钥、大范围重构。
**敏感**: `.github/workflows/*.yml`、`.env*`。

---

## 2. 推荐执行路径

```bash
git pull --rebase origin develop
uv sync --group dev
ruff check . && ruff format --check .    # 代码检查
pytest tests/ -x                         # 测试
# 修改...
pytest tests/ -x                         # 复测
git add -A && git commit -m "feat|fix|docs|chore: scope - summary"
git push origin develop
```

**单测**: `pytest tests/test_xxx.py -v` **打包**: `python build.py`

---

## 3. 项目结构

| 目录 | 职责 |
|---|---|
| `app/core/` | 引擎：backend、chat_session、hook_manager、workers |
| `app/gateway/` | 多平台网关（钉钉/Telegram/Discord/飞书） |
| `app/tools/` | 工具框架（registry/plugin_tool_loader/tool_classifier/tool_name_mapper）+ 共享服务（task/team/mcp）+ 基础设施（command_safety/process_job/pty_session） |
| `app/widgets/` | UI 组件、设置卡片、像素宠物 |
| `app/plugins/contracts/` | 运行时契约层：ModelAdapter / LoopPolicy / SessionStorageEngine 接口（Protocol） |
| `app/plugins/registries/` | 运行时注册表：adapter / loop policy / storage 三注册表（单例，插件可覆盖） |
| `plugins/system/{model_adapters,loop_policies,storages}/` | 系统插件默认运行时实现（openai 适配 / default 循环策略 / sqlite 存储），行为与旧实现逐点等价，插件可覆盖 |
| `plugins/system/tools/` | 系统内置工具插件（33 个工具，register(registry) 注册 schema/impl/icon/cn_name/danger/group） |
| `.drifox/plugins/` | 社区插件目录（用户级，watchfiles 自动扫描）：如 `codegraph-tools/`（codegraph_explore 引擎） |
| `plugins/system/` | 插件：hooks、skills、themes、commands、tools |
| `tests/` | 测试 |

> **工具插件化约定**：新增/修改工具在 `plugins/system/tools/<模块>.py` 中通过
> `register(registry)` 注册（schema + impl + danger + icon + cn_name + group +
> description + aliases）。工具逻辑**自包含**：
> - 纯逻辑工具（文件/网络/桌面）：impl 用标准库/第三方库独立实现，不依赖主程序
> - 平台工具（bash/subagent/MCP/LSP/CodeGraph/团队/todo 等）：impl 通过
>   `tool_ctx["services"]` 调用平台能力接口（todo/terminal/subagent/team/lsp/
>   codegraph/mcp/ask_user/skills/gitee/diagnostics），不暴露 BuiltinTools 内部
> - **icon 自包含**：图标放 `<插件>/tools/icons/*.svg`（深色）+ `tools/icons_light/*.svg`
>   （浅色），渲染按主题选择（浅色优先 icons_light，缺省回退深色/qrc），
>   以 data URI 加载
> registry 为单一数据源，驱动 LLM schema、渲染图标/中文名、权限卡片分组、
> ToolNameMapper 别名。第三方插件同理放在 `plugins/<name>/tools/*.py`，
> 文件增删改自动热生效。
>
> **运行时组件插件化约定**（万物即插件 Phase A）：插件目录可放置
> `model_adapters/*.py`、`loop_policies/*.py`、`storages/*.py`，每文件暴露
> `register(registry)`（与 tools/providers 对称）。注册项带 `id` 属性与策略方法，
> user 根可覆盖 system 根同名实现。循环策略经
> `LoopPolicyRegistry.get_instance().set_active(<id>)` 激活。

---

## 4. 关键依赖

Python 3.14+、PyQt5、PyQt-Fluent-Widgets、openai、loguru、httpx、mcp、pygls。可选组：gateway、dev、build。

---

## 5. 风格规范

- **格式化**: ruff（行宽120，双引号）
- **导入**: 标准→三方→本地
- **命名**: 代码英文；文件名小写中划线；注释中文
- **设计**: 函数短小单一职责

---

## 6. 提交规范

```
feat|fix|docs|chore|refactor|test: scope - summary
```

**强制同步**: 任何变化必须同步更新文档。不确定用 TODO。
