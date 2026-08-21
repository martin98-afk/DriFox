# 对话引擎（UIEngine）插件开发指南

> 适用对象：需要替换 / 扩展主程序对话引擎行为（消息渲染、流式输出、工具调用装配、
> 卡片接线等）的插件开发者。对齐五类运行时组件（model_adapters / loop_policies /
> storages / serializers / engines）对称约定，零插件时零行为变化。

---

## 1. 插件结构

引擎插件目录（用户根 `~/.drifox/plugins/<name>/` 或社区仓 `plugins/<name>/`）：

```
my-engine/
├── .drifox-plugin/
│   └── plugin.json        # 必填：name / version / components.engines
├── engines/               # 必填：运行时组件目录
│   └── my_engine.py       # 暴露 register(registry)，注册 ClassEngineFactory
├── icon.svg               # 可选：设置卡图标
├── icon_dark.svg          # 可选：浅色主题
└── README.md
```

**`plugin.json` 最小模板**：

```json
{
  "name": "my-engine",
  "version": "1.0.0",
  "type": "user",
  "components": {
    "engines": true
  }
}
```

主程序（`PluginManager`）看到 `components.engines: true` 即交给
`runtime_component_loader` 扫描 `engines/*.py`。

> **重要**：替换类必须 `isinstance(UIEngine)` —— 见 §3 安全网。引擎不是无依赖的
> "引导插件"（如 LoopPolicy），它的"默认实现"就是内置类本身；零插件时
> `EngineRegistry` 无注册，`create_engine_for_slot` 直接回退内置 `ChatEngine`，
> 行为完全不变。

---

## 2. `EngineFactory` 契约

`app/plugins/contracts/dialogue_engine.py` 定义：

```python
@runtime_checkable
class EngineFactory(Protocol):
    id: str                                # 目标槽位（当前仅 "ui"）
    def create(self, **kwargs) -> Any: ... # kwargs 与 UIEngine.__init__ 完全一致
```

| 槽位常量 | 值 | 说明 |
|---|---|---|
| `ENGINE_SLOT_UI` | `"ui"` | 主程序 `ChatEngine`（app/core/engines/ui.py） |

**便捷工厂 `ClassEngineFactory(slot, cls)`**：直接包装引擎类，
`create(**kwargs) == cls(**kwargs)`。绝大多数场景用它就够了。

---

## 3. 安全网（isinstance 校验）

`create_engine_for_slot(slot, fallback_cls, **kwargs)` 三条回退路径：

1. **无工厂** → 直接 `fallback_cls(**kwargs)`（零插件时零行为变化）
2. **工厂 create 抛异常** → 记录错误 + 回退内置
3. **产出实例非 `fallback_cls` 子类** → 记录错误 + 回退内置
   （**替换类必须继承内置引擎** —— `main_widget` 对引擎有大量属性 / 回调接线，
   鸭子类型不足以保证兼容，`isinstance` 是硬约束）

> 运行中的引擎实例不热替换 —— `ChatBackend._chat_engine` 与
> `IsolatedContext` 持有的实例在创建期固化；插件更新后仅影响后续创建。
> 切窗口 / 重启会话才生效。

---

## 4. 最小可工作模板

`engines/my_engine.py`：

```python
# -*- coding: utf-8 -*-
"""自定义对话引擎示例（继承内置 ChatEngine，叠加日志/统计）。"""

from app.core.engines.ui import ChatEngine
from app.plugins.contracts.dialogue_engine import ENGINE_SLOT_UI, ClassEngineFactory


class MyEngine(ChatEngine):
    """最小继承即可工作；按需覆盖 _stream / _assemble_messages 等钩子"""

    async def stream_chat(self, *args, **kwargs):
        # 在内置行为之前 / 之后插入自定义逻辑
        async for chunk in super().stream_chat(*args, **kwargs):
            yield chunk


def register(registry):
    registry.register(ClassEngineFactory(ENGINE_SLOT_UI, MyEngine))
```

启动期一次性扫描：插件目录命中 → `register(registry)` 经 `_RegistryProxy`
强制 `source="plugin:<name>"` 入表。`ChatBackend._deferred_create_engines` 与
`IsolatedContext.create_chat_engine` 通过 `create_engine_for_slot("ui", ChatEngine, ...)`
拿到实例 —— 无插件时直接 `ChatEngine(...)`，行为不变。

---

## 5. 高级用法

### 5.1 完全自定义工厂

不继承内置类时（**注意**：会被 §3 安全网回退，需同时继承 `ChatEngine`）：

```python
from app.plugins.contracts.dialogue_engine import ENGINE_SLOT_UI
from app.core.engines.ui import ChatEngine


class MyFactory:
    id = ENGINE_SLOT_UI

    def create(self, **kwargs):
        # 自定义构造（如按 config 选不同子类 / 注入额外依赖）
        return ChatEngine(**kwargs, _extra_marker="my-engine")


def register(registry):
    registry.register(MyFactory())
```

### 5.2 多槽位

首版仅 `ENGINE_SLOT_UI`。未来新增槽位（如独立流式引擎）会在
`dialogue_engine.py` 加新常量 + `engine_registry.create_engine_for_slot` 多分支，
插件按需切换 `factory.id`。

### 5.3 user 根覆盖 system 根

`runtime_component_loader` 与 provider_loader 同构：user 根插件覆盖 system 根
同名实现（`user > system`）。同名引擎插件放 `~/.drifox/plugins/` 即可覆盖内置行为，
无需修改主程序代码。

### 5.4 热重载

`builtin_reloaders._reload_engines` 与 `_reload_model_adapters` 同构：删除路径
`unload_plugin`（精准注销 + 跨根覆盖恢复），更新路径 `reload_plugin`（卸旧 +
恢复覆盖 + 重注册最新 def）。`engines/*.py` 改动触发 watcher 重扫，与其他
运行时组件一致。

### 5.5 kwargs 契约

`create_engine_for_slot("ui", ChatEngine, **kwargs)` 的 kwargs 与
`ChatEngine.__init__` 完全一致。当前主程序两个调用点（`backend.py` /
`isolated_context.py`）传入的字段：

| kwargs | 来源 | 说明 |
|---|---|---|
| `session_manager` | `ChatBackend` / `IsolatedContext` | 会话管理器（必填） |
| `get_model_config` | 同上 | 模型配置回调 |
| `tool_executor` | 同上 | 工具执行器（必填，400ms 批延迟创建） |
| `agent_manager` | 同上 | Agent 管理器 |
| `get_chat_cards` | `ChatBackend` | 聊天卡片上下文；`IsolatedContext` 传 `None` |
| `get_memory_context` | `IsolatedContext` | 记忆上下文；`ChatBackend` 不传 |
| `worker_callbacks` | `IsolatedContext` | 流式回调；`ChatBackend` 走 `_flush_pending_engine_callbacks` |
| `api_mode` | `IsolatedContext` | API 模式开关 |
| `backend` | `ChatBackend` | 后端引用 |

替换类的 `__init__` 必须接受这些 kwargs（多余的用 `**kwargs` 兜底或显式 `super().__init__(**kwargs)`）。

---

## 6. 测试建议

端到端测试覆盖链路见 `tests/plugins/test_e2e_engine_plugin.py`：

1. 临时插件目录 + `ReplacementEngine`（非继承 `ChatEngine` 的替身）
2. `loader.scan_roots` → 工厂入 registry
3. `create_engine_for_slot` 产出非继承基类 → 安全网回退内置
4. 替换为 `CompatibleEngine(ChatEngine)` + `reload_plugin` → 替换生效
   （通过 `sys.modules` 取到插件模块的类对象做 `is` 断言）
5. `rmtree` + `scan_roots` → factory 清理 → 回退内置

第三方插件可借鉴：用 `tmp_path` 构造临时插件目录 → 加载 → `create_engine_for_slot`
拿实例 → `assert isinstance(engine, ChatEngine)`（安全网已确保兼容性）。

---

## 7. 相关文件

| 文件 | 职责 |
|---|---|
| `app/plugins/contracts/dialogue_engine.py` | `EngineFactory` Protocol + `ClassEngineFactory` + `ENGINE_SLOT_UI` |
| `app/plugins/registries/engine_registry.py` | `EngineRegistry` + `create_engine_for_slot`（含 isinstance 安全网） |
| `app/plugins/loaders/runtime_component_loader.py` | `_make_engine_loader` + `ensure_engine_watcher`（五类运行时组件统一入口） |
| `app/plugins/builtin_reloaders.py` | `_reload_engines`（删除/更新路径） |
| `app/core/engines/ui.py` | 内置 `ChatEngine`（替换类的基类） |
| `app/core/backend.py` | `ChatBackend._deferred_create_engines`（工厂化创建点之一） |
| `app/gateway/local_service/isolated_context.py` | `IsolatedContext.create_chat_engine`（工厂化创建点之二） |
| `app/plugins/kernel.py` | `KNOWN_COMPONENTS` / `COMPONENT_ORDER` 含 `engines` 登记 |
| `tests/plugins/test_e2e_engine_plugin.py` | 端到端测试（扫描/替换/安全网/卸载） |
| `tests/plugins/test_engine_registry.py` | 注册表单测（register/unregister/create_engine_for_slot） |
