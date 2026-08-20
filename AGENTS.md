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
| `app/plugins/contracts/` | 运行时契约层：ModelAdapter / LoopPolicy / SessionStorageEngine / MessageSerializer 接口（Protocol） |
| `app/plugins/registries/` | 运行时注册表：adapter / loop policy / storage / serializer 四注册表（单例，插件可覆盖） |
| `plugins/system/{model_adapters,loop_policies,storages,serializers}/` | 系统插件默认运行时实现（openai/gemini/deepseek 三协议家族 / default 循环策略 / sqlite 存储 / openai 序列化器），行为与旧实现逐点等价，插件可覆盖 |
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
>
> **插件配置契约**（E1）：插件在 `.drifox-plugin/plugin.json` 声明 `config_schema`
> （title + fields[{key,label,type∈text|password|bool,default,env,placeholder,description}]），
> 主程序自动提供：统一存储 `<app_data_dir>/plugins/<plugin>/config.json`
> （读取三级链：环境变量→存储值→默认值）、设置面板插件分区自动配置卡
> （PluginConfigCard 声明式渲染，经 register_settings_card 扩展点注入）。
> 插件代码内读取：`PluginConfigStore().get(plugin_name, key)`。
> 需要动态默认值/复杂 UI 的插件仍可手写 Phase D 设置卡（两者不冲突，同 plugin
> 可并存：自动卡 card_id 固定为 `<plugin>-config`）。
>
> registry 为单一数据源，驱动 LLM schema、渲染图标/中文名、权限卡片分组、
> ToolNameMapper 别名。第三方插件同理放在 `plugins/<name>/tools/*.py`，
> 文件增删改自动热生效。
>
> **运行时组件插件化约定**（万物即插件 Phase A/B/C）：插件目录可放置
> `model_adapters/*.py`、`loop_policies/*.py`、`storages/*.py`、`serializers/*.py`，
> 每文件暴露 `register(registry)`（与 tools/providers 对称）。注册项带 `id` 属性与策略方法，
> user 根可覆盖 system 根同名实现。循环策略经
> `LoopPolicyRegistry.get_instance().set_active(<id>)` 激活。
> 序列化（Phase B/C）：`message_content` 的 `to_api_message / messages_to_api /
> messages_to_responses_input` 为兼容薄壳，内部转发序列化器**单入口**
> `MessageSerializer.serialize(messages, ctx) -> SerializeResult`（`messages` ≡ 旧
> `messages_to_api`；`input_items/instructions` ≡ 旧 `messages_to_responses_input`，
> 序列化器内部按 `ctx.flags.use_responses_api` 路由）；worker 经 adapter 解析的
> `flags.serializer_id` 指定序列化器（默认 openai）。
> 协议家族（Phase C）：`plugins/system/model_adapters/` 按家族拆三适配器——
> `openai-family`（matches=1 兜底）/ `gemini-family`（2）/ `deepseek-family`（3），
> 判定器共享 `_detectors.py`（`_` 前缀文件不被 loader 当作插件），`resolve` 取最高分。
> 存储：消费方（history_manager / memory_manager / session_handler）经
> `ChatBackend.get_session_storage()` 门面、UI 层经 `backend.session_store`
> （Phase C 已切到 `StorageRegistry.get_active()`，引擎提供 SessionStore 兼容视图）获取
> 活跃引擎，能力用 `isinstance` 探测（SessionTitleCapability / SessionCountsCapability /
> InputHistoryCapability）。
>
> **UI 插件扩展点约定**（Phase D，8 个扩展点全区域可插拔）：插件 `ui/__init__.py`
> 导出 `register_ui(registry)`，可注册：
> - 内容块渲染器 `register_content_renderer`（custom 块）
> - 欢迎卡片 tab `register_welcome_tab` / 消息元素工厂 `register_message_factory`
> - 浮动卡片 `register_floating_card`（top/bottom/left/right/full 容器 + 侧边栏派生）
> - 侧边栏项 `register_sidebar_item`（独立扩展点，与 floating card 解耦；插件同时
>   注册 sidebar 项与卡片时以 sidebar 为准）
> - 输入区按钮 `register_input_button`（工具栏胶囊末尾，热重载自动重建）
> - 右键菜单项 `register_context_menu_action`（target ∈ message_card/tab；
>   `action_func(context)` 返回 False = 处理完成关菜单；`enabled_func` 置灰）
> - 设置卡片 `register_settings_card`（LLMSettingsCard 末尾插件分区，打开时重建）
> 回调 context 含 `window_id / main_widget / item_id / button_id / tab_index` 等；
> 卸载/热重载自动清理全部注册（`unregister_plugin` 幂等）。

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
