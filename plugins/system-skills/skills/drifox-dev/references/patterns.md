# 设计模式与跨模块约定

> 模式选型 / 注册表 / 多窗口隔离 / 热更新 / 信号槽。

---

## 一、注册表模式（插件化的地基）

`app/plugins/registries/` 下 12 个注册表单例，主程序**只查注册表，不写类型 if**：

| 注册表 | 契约（contracts/） |
|--------|-------------------|
| `model_adapter_registry` | `model_adapter.py` |
| `loop_policy_registry` | `loop_policy.py` |
| `storage_registry` | `storage.py` |
| `serializer_registry` | `message_serializer.py` |
| `gateway_platform_registry` | `gateway_platform.py` |
| `hook_policy_registry` | `hook_policy.py` |
| `provider_registry` | — |
| `plugin_config_registry` | `plugin_config.py`（E1 配置契约：schema 驱动设置卡） |
| `engine_registry` | `dialogue_engine.py` / `engine_host.py` / `engine_session.py` |
| `ui_plugin_registry` | `ui_host.py` / `ui_module.py` / `ui_page.py` / `ui_slots.py` |
| `coding_plan_fetcher` | — |

用法：`XxxRegistry.get_instance()` → 插件 `register(registry)` 注册 → 主程序查表。用户级插件根（`~/.drifox/plugins/`）覆盖系统根。
序列化单入口 `MessageSerializer.serialize(messages, ctx)`，按 `ctx.flags.use_responses_api` 路由。
激活循环策略：`LoopPolicyRegistry.get_instance().set_active(<id>)`。

## 二、其它常用模式

| 模式 | 场景 | 说明 |
|------|------|------|
| 单例 | AgentManager / MemoryManager / LspManager / 各 Registry / Settings | `get_instance()`；**修改「看似全局」的组件前先确认它真的是单例** |
| 观察者 | PyQt Signal / `ui_event_bus.py` | 后端 emit → 前端 slot，跨线程自动 QueuedConnection |
| 适配器 | `conversation/adapters/{base,ui,gateway}.py` | UI 与网关两条对话通路 |
| 两阶段停止 | `ConversationExecutor` | `cancel_worker()` → `finalize_stop()` |
| 事件总线 | `worker_event_bus.py` | Worker 事件统一分发 |
| 池化 | `WebViewPool` | 昂贵实例复用（见 `rendering-pipeline.md`） |
| 批量提交 | `HeightCommitBatch` | 合并多次高度上报，保持锚点 |
| 协作式取消 | 后台 worker | 循环边界检查 cancelled，不强杀（见 `perf-playbook.md` §五） |

## 三、多窗口隔离

| 维度 | 策略 |
|------|------|
| 窗口级实例 | ChatBackend、ToolExecutor —— 每窗口独立 |
| 全局单例 | Registry、Manager 类 —— 共享只读 |
| 工作目录 | 每个 ToolExecutor 独立 workdir；团队场景按 run_id 存，不共用单槽 |
| 窗口登记 | `app/core/window_registry.py` |

不要假设跨窗口共享状态。延迟刷新类任务要带序号，过期作废（P047）。

## 四、热更新链路

```
watchfiles 检测 ~/.drifox/plugins/ 变更
  → reload_plugin_subsystems / rescan_plugin
    → 工具 / providers / ui / hook / 命令 / 主题 各子系统刷新
    → 广播 PluginChanged → main_widget._on_plugin_hot_reload（ui / mcp / workbench 各分支）
```

**易错点**
- 300ms 全局去抖会合并同批请求 → 新增组件类型可能漏载，需做「manifest 声明 vs 运行时注册」差异检查。
- `sys.modules` 缓存会让改过的模块不生效 → 加载点加 mtime 自检，或按前缀 purge。
- 重建 QShortcut 前先销毁旧实例，否则 ambiguity。
- UI 插件页刷新要带 `force`，否则签名相同被短路。
- MCP 分支要主动补连（幂等）。

## 五、信号 / 槽

- 后端 → 前端走 Qt Signal（继承 QObject）；横切事件走 `app/core/ui_event_bus.py`。
- 改后端信号必须确认前端有槽连接，否则事件丢失。
- 跨线程共享状态加锁；UI 更新只在主线程。
