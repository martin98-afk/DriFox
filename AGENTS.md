# 项目开发规范

AI Agent 操作手册与约束。

## 1. 目标与边界
- **允许**: 读写顶层 `/docs`/`skills`；执行 lint/构建/测试；增改功能修复问题
- **禁止**: 改 CI 配置（除非明确要求）、改 LICENSE、硬编码密钥、大范围重构
- **敏感**: `.github/workflows/*.yml`、`.env*`

## 2. 执行路径
```bash
git pull --rebase origin dev
uv sync --group dev
ruff check . && ruff format --check .   # lint
pyright .                                # 类型检查
pytest tests/ -x                         # 测试
git add -A && git commit -m "feat|fix|docs|chore: scope - summary"
git push origin dev
```
- 单测: `pytest tests/test_xxx.py -v` 打包: `python build.py`

## 3. 结构
| 目录 | 职责 |
|---|---|
| `app/core/` | 引擎：backend/chat_session/hook_manager/workers |
| `app/gateway/` | 多平台网关适配层 |
| `app/tools/` | 工具框架(registry/loader/classifier/mapper)+共享服务(task/team/mcp)+基础设施 |
| `app/widgets/` | UI 组件、设置卡片、像素宠物 |
| `app/plugins/contracts/` | 运行时契约 Protocol：ModelAdapter/LoopPolicy/SessionStorageEngine/MessageSerializer |
| `app/plugins/registries/` | 四注册表(单例，可覆盖)：adapter/loop policy/storage/serializer |
| `plugins/system/` | 系统插件：model_adapters/loop_policies/storages/serializers/tools(+hooks/skills/themes/commands) |
| `.drifox/plugins/` | 用户级社区插件(watchfiles 热扫描) |

## 4. 插件化硬约束
**工具**：`plugins/system/tools/<模块>.py` 用 `register(registry)` 注册(schema+impl+danger+icon+cn_name+group+description+aliases)。逻辑自包含——纯逻辑工具独立实现；平台工具经 `tool_ctx["services"]`(todo/terminal/subagent/team/lsp/codegraph/mcp/ask_user/skills/gitee/diagnostics) 调能力，不暴露 BuiltinTools。图标 `<插件>/tools/icons/*.svg`(深)+`icons_light/*.svg`(浅)，data URI 加载。registry 为单一数据源(驱动 LLM schema/图标/分组/ToolNameMapper 别名)；第三方同理放 `plugins/<name>/tools/*.py`，增删改热生效。

**配置契约 E1**：插件在 `.drifox-plugin/plugin.json` 声明 `config_schema`(title+fields[{key,label,type,default,env,placeholder,description}])；type 支持 `text`/`password`/`bool`/`select`(需 options)/`number`(可选 min/max/step)/`textarea`(可选 rows)，主程序自动渲染设置卡(经 `register_settings_card`)+统一存储 `<app_data_dir>/plugins/<plugin>/config.json`，三级链 环境变量→存储→默认。代码内 `PluginConfigStore().get(plugin, key)` 读取。复杂 UI 仍可手写设置卡(自动卡 card_id=`<plugin>-config`)。

**运行时组件**：`model_adapters/*.py`、`loop_policies/*.py`、`storages/*.py`、`serializers/*.py` 各自 `register(registry)`，含 `id`+策略方法，user 根覆盖 system 根。激活：`LoopPolicyRegistry.get_instance().set_active(<id>)`。序列化单入口 `MessageSerializer.serialize(messages, ctx)`(按 `ctx.flags.use_responses_api` 路由，默认 openai)。协议家族(openai/gemini/deepseek)共享 `_detectors.py`，`resolve` 取最高分。存储经 `ChatBackend.get_session_storage()` 门面，能力用 `isinstance` 探测(SessionTitle/Counts/InputHistoryCapability)。

**Gateway 平台**：`gateways/<platform>.py` 注册 `GatewayPlatformDef`(platform_id/display_name/adapter_factory/check_requirements/config_builder/config_writer/build_config_values/validate_config/ui_order/icon_hint/source)；主程序查 `GatewayPlatformRegistry.get_instance()`，零平台 if。内置 6 平台(wecom/dingtalk/telegram/discord/feishu/slack)已迁社区仓 `drifox-plugins2`，配置闭包读主程序 Settings 零迁移；第三方在 `~/.drifox/plugins/` 或社区仓建 `gateway-<id>/` 即可，`platform_id` 任意 str。配置仍走 E1 契约。

**SDK 自包含**：平台 SDK vendor 到 `<插件>/deps/`，顶层 `sys.path.insert(0,_deps)` 优先，本体函数内延迟导入(教训 2026-06-16：dingtalk_stream 顶层导入致 gateway 包加载失败)。详见 `docs/plugins/gateway-platforms.md`。

**UI 扩展点**(插件 `ui/__init__.py` 导出 `register_ui`)：`register_content_renderer`(custom 块)/`register_welcome_tab`+`register_message_factory`/`register_floating_card`(top/bottom/left/right/full+侧边栏派生)/`register_sidebar_item`(与 floating card 解耦，并存时优先)/`register_input_button`(工具栏末，热重载重建；icon_path 深色 + icon_light_path 浅色，主题切换自动刷新)/`register_context_menu_action`(target∈message_card/tab；`action_func` 返 False=完成关菜单)/`register_settings_card`(插件分区，打开重建)。回调 context 含 window_id/main_widget/item_id/button_id/tab_index；`unregister_plugin` 幂等清理。

## 5. 依赖与风格
- **依赖**: Python 3.14+、PyQt5、PyQt-Fluent-Widgets、openai、loguru、httpx、mcp、pygls、pyright
- **格式化**: ruff(行宽120，双引号)；**类型**: pyright 严格(pyproject 配置)
- **导入**: 标准→三方→本地；**命名**: 英文代码、小写中划线文件、中文注释；**设计**: 函数短小单一职责
- **异常语法(Python 3.14+)**: 支持无括号多异常 `except A, B:`（PEP 758，等价于 `except (A, B):`），低于 3.14 会 `SyntaxError`。注意 `except A, e:` 旧式赋值语法在 3 系已移除，逗号仅用于分隔异常类型时末项须为类型而非单个标识符，避免歧义。ruff/pyright 已支持该语法，可直接使用，但勿在需兼容 <3.14 的代码中出现。

## 6. 提交规范
`feat|fix|docs|chore|refactor|test: scope - summary`
强制同步更新文档；不确定用 TODO。
