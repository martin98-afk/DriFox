# 设计模式与跨模块约定

> SKILL.md 在任务涉及「模式选型 / 多窗口隔离 / 信号槽改造 / 热更新」时分派到这里。

---

## 一、关键设计模式

| 模式 | 应用场景 | 说明 |
|------|---------|------|
| **单例模式** | AgentManager, MemoryManagerCore, LspManager, PluginManager, Settings | `_instance = None` + `__new__` 或 classmethod `get_instance()` |
| **动态派发** | BuiltinTools | 通过 `__getattr__` 遍历工具模块，无需手动委托 |
| **观察者模式** | PyQt Signal/Slot | 后端 emit → 前端 slot，自动跨线程（QueuedConnection） |
| **策略模式** | PermissionResolver | allow / ask / deny |
| **适配器模式** | GatewayAdapter | 统一接口适配不同平台 |
| **两阶段停止** | ConversationExecutor | `cancel_worker()` → `finalize_stop()` |
| **事件总线** | WorkerEventBus | 统一分发 Worker 事件 |

## 二、多窗口隔离策略

| 维度 | 策略 |
|------|------|
| **窗口级组件** | ChatBackend, ToolExecutor — 每窗口独立实例 |
| **全局单例** | AgentManager, MemoryManagerCore, LspManager — 共享只读数据 |
| **工作目录隔离** | 每个 ToolExecutor 有独立 workdir |

不要假设跨窗口共享状态。修改任何「看似全局」的组件前，确认它是否真的是单例。

## 三、热更新链路

```
watchfiles 检测变更
  → PluginManager.rescan_plugin()
    → AgentManager.reload_plugin_agents()
    → HookManager.reload_plugin_hooks()
    → 主题 / 命令刷新
    → UI 重新加载
```

修改了 Agent/Hook/Command/Skill/Theme 定义后，热更新通常会自动触发；首次发布新组件需重启。

## 四、信号 / 槽机制

- 后端 → 前端通过 **Qt Signal**（继承 QObject）
- 后台线程发射信号 → 主线程槽函数执行（自动 QueuedConnection）
- 跨线程安全：敏感资源用 `_stop_lock` 保护
- 修改后端信号时，**必须**确认前端有对应槽函数连接，否则事件丢失
