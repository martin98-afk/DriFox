# DriFox 项目笔记

AI Agent 操作手册与约束。Python 3.14+ / PyQt5 桌面 LLM 聊天应用。

## 1. 目标与边界
- **允许**: 读写顶层 `/docs`；执行 lint/构建/测试；增改功能、修复问题
- **禁止**: 改 CI（除非明确要求）、改 LICENSE、硬编码密钥、大范围重构
- **敏感**: `.github/workflows/*.yml`、`.env*`

## 2. 构建/测试命令
```bash
uv sync --all-groups          # 全平台依赖（Win）
ruff check . && ruff format --check .   # lint（行宽120）
pyright .                              # 类型
pytest tests/ -x                       # 全量测试（pytest-asyncio auto 模式）
pytest tests/test_xxx.py -v            # 单文件
pytest tests/test_xxx.py::test_name -v # 单用例
pytest tests/ -m perf                  # 仅性能基准
```
- **依赖组**：dev/build/mac-build/linux-build/gateway；Windows 用 `--all-groups`，mac 用 `build+mac-build`，Linux 用 `build+linux-build`
- **打包**：`python build.py`（Win+Linux）；mac 额外需 `dmgbuild`+`Pillow`
- **pytest markers**：`perf`（基准）、`stress`（稳定性）；`asyncio_mode=auto`

## 3. 目录结构
| 目录 | 职责 |
|---|---|
| `app/core/` | 引擎：backend/chat_session/hook_manager/workers + lsp/store/team |
| `app/gateway/` | 多平台网关适配层 |
| `app/tools/` | 工具框架(registry/loader/classifier/mapper)+共享服务(task/team/mcp) |
| `app/widgets/` | UI 组件、设置卡片、像素宠物 |
| `app/plugins/contracts/` | Protocol：ModelAdapter/LoopPolicy/SessionStorageEngine/MessageSerializer |
| `app/plugins/registries/` | 四注册表单例(adapter/loop policy/storage/serializer) |
| `plugins/system/` | 系统插件：tools/model_adapters/loop_policies/storages/serializers/hooks/skills/themes/commands/ui |
| `~/.drifox/plugins/` | 用户级社区插件(watchfiles 热扫描) |
| `tests/` | 与源码按模块对齐：core/widgets/plugins/utils/perf/gateway/debug |
| `docs/` | plugins/perf/security/superpowers 四大知识库 |

## 4. 插件化硬约束
**工具注册**：`plugins/<name>/tools/<模块>.py` 用 `register(registry)` 注册（schema+impl+danger+icon+cn_name+group+description+aliases）。registry 为单一数据源（驱动 LLM schema/图标/分组/ToolNameMapper 别名）；增删改热生效。

**配置契约 E1**：`.drifox-plugin/plugin.json` 声明 `config_schema`（title+fields[{key,label,type,default,env,placeholder,description}]）。type：`text/password/bool/select/number/textarea`。主程序自动渲染设置卡 + 存储 `<app_data_dir>/plugins/<plugin>/config.json`。三级链：环境变量→存储→默认。

**运行时组件**：`model_adapters/*.py`、`loop_policies/*.py`、`storages/*.py`、`serializers/*.py` 各自 `register(registry)`。user 根覆盖 system 根。激活 `LoopPolicyRegistry.get_instance().set_active(<id>)`。序列化单入口 `MessageSerializer.serialize(messages, ctx)`，按 `ctx.flags.use_responses_api` 路由。

**Gateway**：插件在 `gateways/<platform>.py` 注册 `GatewayPlatformDef`；主程序查 `GatewayPlatformRegistry.get_instance()`，零平台 if。SDK vendor 到 `<插件>/deps/`，顶层 `sys.path.insert(0,_deps)` 优先，本体函数内延迟导入（教训：dingtalk_stream 顶层导入致 gateway 包加载失败）。

**UI 扩展点**（插件 `ui/__init__.py` 导出 `register_ui`）：`register_content_renderer`/`register_welcome_tab`/`register_message_factory`/`register_floating_card`/`register_sidebar_item`/`register_input_button`（icon_path 深色 + icon_light_path 浅色，主题切换自动刷新）/`register_context_menu_action`/`register_settings_card`。回调 context 含 window_id/main_widget/item_id/tab_index；`unregister_plugin` 幂等清理。

## 5. 代码风格
- **格式化**：ruff（双引号）；**类型**：pyright 严格；**导入**：标准→三方→本地
- **命名**：英文代码、小写中划线文件、中文注释；**设计**：函数短小单一职责
- **Python 3.14+** 支持无括号多异常 `except A, B:`（PEP 758），勿用于 <3.14 兼容代码

## 6. CI / 提交
- CI：`.github/workflows/marketplace.yml` + `release.yml`
- 提交：`feat|fix|docs|chore|refactor|test: scope - summary`，强制同步更新文档

## 7. 调试 / 部署
- **单实例**：`app/core/single_instance.py` 保证唯一进程
- **日志**：`logs/` 目录（loguru）
- **会话存储**：`ChatBackend.get_session_storage()` 门面，能力用 `isinstance` 探测
- **热更新**：watchfiles 监控 `~/.drifox/plugins/`，插件 reload 触发 `PluginChanged` 钩子
- **多平台打包**：Win PyInstaller（onefile，see `Drifox.spec`）；mac dmgbuild；Linux AppImage