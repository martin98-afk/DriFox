# DriFox 万物即插件拆解方案

> 版本：v2（终稿，已整合 T5 审查 7 条必改项）
> 依据：T1（DeepSeek Harness 理念调研）+ T2（DriFox 现有架构与 UI 插件机制摸底）+ T3（整体插件化方案设计）+ T5（审查意见 7 条）
> 状态：审查通过（有条件）后修订终稿

## 目录

- [1. 目标与原则](#1-目标与原则)
- [2. 总体架构分层](#2-总体架构分层)
  - [2.1 三层架构](#21-三层架构)
  - [2.2 现有模块映射表](#22-现有模块映射表)
- [3. 插件契约设计](#3-插件契约设计)
  - [3.1 manifest schema](#31-manifest-schema)
  - [3.2 注册与发现](#32-注册与发现)
  - [3.3 加载生命周期（五态）](#33-加载生命周期五态)
    - [3.3.1 卸载契约注册点清单](#331-卸载契约注册点清单)
    - [3.3.2 热插拔保护线不可动](#332-热插拔保护线不可动)
  - [3.4 扩展点与钩子清单](#34-扩展点与钩子清单)
  - [3.5 Seam 三层](#35-seam-三层)
  - [3.6 事件总线线程模型](#36-事件总线线程模型)
- [4. 拆解清单（按模块）](#4-拆解清单按模块)
- [5. 迁移路径（三阶段）](#5-迁移路径三阶段)
- [6. 兼容策略](#6-兼容策略)
- [7. 风险与反模式](#7-风险与反模式)
  - [7.1 契约继承项](#71-契约继承项)
- [附录 A：现状补充 — 现有组件总表与加载链路](#附录-a现状补充--现有组件总表与加载链路)
- [附录 B：现状补充 — 耦合点清单](#附录-b现状补充--耦合点清单)
- [附录 C：现状补充 — 三条关键实施建议](#附录-c现状补充--三条关键实施建议)

---

## 1. 目标与原则

### 1.1 收益目标

| 收益 | 说明 |
|---|---|
| 可替换 | 换 Provider 全局生效（如换 LLM 后端 / 换存储后端不触其他模块） |
| 可复用 | 能力跨窗口 / 跨团队共享（同一插件随处可用） |
| 可测试 | 能力隔离单测（插件作为独立单元注入 mock 依赖测试） |
| 低耦合 | ctx 键查找替代 import（插件间不 import 实现，只查服务键） |
| 生态化 | 第三方插件市场（manifest + entry_point 即可发布） |
| 热插拔 | 注册可逆副作用（enable/disable/unload 全链路自动回收） |

### 1.2 边界原则 —— 什么必须留核心

**内核不可插件化**（微内核骨架）：

- `PluginManager` 骨架（app/core/plugin_manager.py）
- 服务注册表（ctx 等价物）
- 事件总线（emit / waterfall / parallel / serial 四分发模式）
- 配置层（Settings + overlay，app/utils/config.py）
- 进程 / 窗口生命周期（MainWindow 骨架，main.py / app/main_widget.py）

**其余全部插件化候选**：命令、Agent、技能、工具、主题、Hook、MCP 连接器、LLM 适配器、UI 组件（浮动卡片 / 渲染器 / 消息工厂）、数据源、存储后端、遥测。

**判据（对齐 DSH "无特权核心"）**：

- 任何能力若「从配置可整体替换」即应插件化；
- 凡属于「所有插件共同依赖的基础设施」才留核心。

---

## 2. 总体架构分层

### 2.1 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│ L3 插件层（全部可替换）                                       │
│ UI 插件│工具插件│Agent 插件│技能插件│命令插件│主题插件│       │
│ Hook 插件│MCP 插件│LLM 适配器│数据源│存储后端│遥测            │
├─────────────────────────────────────────────────────────────┤
│ L2 服务层（Seam：接口定义 / Provider / 消费方）               │
│ ctx.tools  ctx.llm  ctx.sessions  ctx.agents  ctx.teams     │
│ ctx.memory  ctx.commands  ctx.jobs                          │
├─────────────────────────────────────────────────────────────┤
│ L1 内核（微内核，不插件化）                                    │
│ DI 容器 + 服务注册表│事件总线(emit/waterfall/parallel/serial) │
│ PluginManager│配置 overlay                                  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 现有模块映射表

实际路径以 T2 摸底为准，此处为模块 → 目标层的迁移规划：

| 现有模块 | 目标层 | 插件类型 |
|---|---|---|
| `app/core/plugin_manager.py` | L1 内核（保留骨架） | — |
| `app/core/ui_plugin_registry.py` | L2 seam + L3 | UI 插件 |
| `app/tools/plugin_tool_loader.py` | L2 seam | 工具插件 |
| `plugins/system/{commands,agents,skills,themes,hooks,tools}/` | L3 | 对应类型插件 |
| `plugins/*/.mcp.json` / MCP 管理（`app/tools/mcp_tools.py`） | L2 seam + L3 | MCP 插件 |
| `app/core/agent.py`（AgentManager）/ `app/core/team_manager.py` / `subagent_dag` | L2 seam | Agent / 调度插件 |
| `app/core/store/`（MemoryManagerCore / SessionManager） | L2 seam | 存储/会话插件 |
| 多模型后端（OpenAI 兼容 10+ 家，`app/core/backend.py` + `app/core/provider_profile.py`） | L2 seam | LLM 适配器插件 |
| `plugins/context-usage-stats`、`file-tree`、`plugin-marketplace`、`share-history`、`shortcut-manager`、`system-cleaner`（6 个内置 UI 插件） | L3 | UI 插件（改造样板） |

---

## 3. 插件契约设计

### 3.1 manifest schema

现有 plugin.json（T2 实测，`plugins/system/.drifox-plugin/plugin.json` 与 `plugins/context-usage-stats/.drifox-plugin/plugin.json` 同构）：

```json
{
  "icon": {"light": "icon.svg", "dark": "icon_dark.svg"},
  "name": "context-usage-stats",
  "description": "对话上下文用量统计",
  "version": "0.1.0",
  "author": {"name": "DriFox Contributors"},
  "homepage": "https://github.com/martin98-afk/DriFox",
  "license": "MIT",
  "type": "user",
  "components": {"ui": true}
}
```

**向后兼容扩展（新增字段全部可选，旧插件零改动继续加载）**：

```json
{
  "dependencies": {"tools": "^0.1", "sessions": "^1.0"},
  "entry_point": "drifox_plugin.py:Plugin",
  "provides": ["fs.provider", "llm.adapter"],
  "requires": {"ui.slot": ["floating-card", "content-renderer"]},
  "events": {
    "listens": ["session/event", "tools/pre-execute"],
    "emits": ["my/event"]
  },
  "hooks": {
    "on_load": "fn",
    "on_enable": "fn",
    "on_disable": "fn",
    "on_unload": "fn"
  }
}
```

**插件本体（对齐 Cordis）**：

- 插件对象 = Python 类 / 函数，实现 `def apply(ctx)`；
- 服务在 ctx 上以稳定键注册（如 `ctx.tools`）；
- 插件间按 key 查找服务，**禁止 import 具体实现**；
- 依赖用 `inject` 声明，加载序由依赖决定而非启动顺序。

> **T5 审查必改④ — 废弃 `backend._COMPONENT_ORDER` 硬编码加载序**：
> 现状 backend.py:1555 `_COMPONENT_ORDER = {...}` 是写死的组件重载顺序（agents→hooks→commands→themes→…），`_identify_all_components_from_changes` 与 `_reload_single_plugin` 都按它排序。
> 新契约：组件加载/重载顺序改由**依赖注入图拓扑排序**决定（manifest `dependencies` 字段 → 有向无环图 → 拓扑序），`_COMPONENT_ORDER` 不再作为排序依据。
> 阶段 1 验收补充断言：**按依赖序加载的结果与旧 `_COMPONENT_ORDER` 顺序等价**（系统插件无显式依赖时，拓扑序回落与旧顺序一致的默认序）。

### 3.2 注册与发现

**目录通道（保留，已有）**：

- `plugins/system/`（系统，打包进 exe，`PluginManager._SYSTEM_PLUGIN_DIR`）
- `<app_data>/plugins/`（用户，`PluginManager._USER_PLUGIN_DIR_NAME`）
- `.claude/` 兼容目录（`~/.claude/skills`、`~/.claude/plugins/cache`）
- 入口：`.drifox-plugin/plugin.json`（优先）/ `.claude-plugin/plugin.json`（兼容）
- components 自动探测（T2 实测）：物理目录为准 `commands/ agents/ skills/ themes/ hooks/(+hooks.json) .mcp.json .lsp.json ui/(+__init__.py) team_templates/(+yaml) tools/`

**包通道（新增）**：

- `pyproject` 的 `[project.entry-points."drifox.plugins"]` → `importlib.metadata` 发现
- 端口插件可直接 `pip install` 安装

**优先级（保留已有）**：system < claude < user；同名用户覆盖系统。

> **T5 审查必改⑤ — 覆盖语义继承（原样保留并写入契约）**：
> 1. **tools 跨根覆盖语义**：`ToolRegistry` + `plugin_tool_loader` 的 `_root_tracker`（首注册根追踪）——`user` 根（高优先级）可覆盖 `system` 根（低优先级）同名工具；同根/同级按「先注册者优先」；同根内已被占用时不覆盖。跨根覆盖关系跨多次 scan 持续生效（`_root_tracker` 由 watcher 持久持有，plugin_tool_loader.py）。
> 2. **user-custom 常驻特殊通道**：`<app_data>/plugins/user-custom/` 无 plugin.json 也始终加载（PluginManager 多处硬编码其子目录：commands 最高优先级 p.753-775、hooks p.834、skills 等子目录 p.922-942、MCP .mcp.json 自动创建 p.1177）。契约保留：user-custom 是最高优先级用户通道，任何插件不得占用其名。
> 3. **命令/技能命名空间前缀**：用户插件命令/技能注入 `plugin_name:` 前缀（如 `/my-plugin:command`），系统插件短名直用。契约保留：前缀规则不变，卸载时按前缀回收（意见① 第 ②⑥ 项）。

**配置层叠（对齐 DSH patch 机制）**：基础 bundle（系统默认）→ 用户 `overrides.yml` 按行 id 覆盖配置项，上层永远可改下层；提供插件配置预览命令。

### 3.3 加载生命周期（五态）

```
discover → load → enable → disable → unload
```

| 状态 | 动作 |
|---|---|
| discover | 扫描目录 / entry_points，解析 manifest |
| load | 解析 manifest、校验 schema、注入依赖 |
| enable | 注册副作用：服务 / 事件监听 / UI 组件 / 工具 |
| disable | 回收副作用，保留目录注册 |
| unload | 移除配置 |

**注册 = 可逆副作用（核心升级）**：

- 现有（T2 实测）`enable_plugin/disable_plugin` 是「调用方联动 reload」（backend.py `_reload_single_plugin` 逐组件手写分支）；
- 改为**注册方自述 disposer**：每个注册项由 `ctx.effect(lambda: 注册, lambda: 回收)` 成对声明；
- disable / unload 按逆序自动回收，杜绝残留和内存泄漏。

### 3.3.1 卸载契约注册点清单（T5 审查必改①）

7 类注册点逐一列回收动作与幂等要求（阶段 1 单测按本清单逐项断言 enable → disable → re-enable 无残留）：

| # | 注册点 | 回收动作 | 幂等要求 |
|---|---|---|---|
| ① | UI 卡片实例（`UIPluginRegistry._card_widget_instances[window_id]` 内 widget） | 从 `CardManager` 注销；删除实例；从容器布局移除（四向容器 top/bottom/left/right/full） | 重复回收不抛异常；容器布局 takeAt/deleteLater 可重入 |
| ② | floating_card 联动命令名（`_unregister_command_for_card(card_id)`，ui_plugin_registry.py:778） | 注销命令（含 `plugin_name:` 前缀形式）；同步清理 `_ui_command_names` 与主窗口 `_function_command_handlers` 双路径 | 命令不存在时静默跳过；不误删其他插件同名命令 |
| ③ | sys.modules 残留（`ui_plugin_{name}` 及子模块） | 卸载即清理：删除 `sys.modules` 中 `module_name` 与其 `module_name.` 前缀全部条目；删 `__pycache__`（Windows 句柄冲突时降级忽略，依赖 mtime 校验） | 热重载循环中重复清理安全；不影响进程内其他插件模块 |
| ④ | MCP 配置缓存（`PluginManager.get_mcp_servers()` TTL 缓存） | enable/disable 主动 `invalidate_mcp_cache()`（现有 plugin_manager 已做，保留并纳入契约） | 缓存失效幂等；同名插件重启用后配置为新值 |
| ⑤ | hooks 注册（`HookManager.dynamic_unregister_hook(skill_name, event_name, hook_index)`，hook_manager.py:1313） | 按插件名（skill_name）注销其挂载的全部事件钩子 | 未注册的 skill_name 返回 False 不抛错；重载前全量注销再注册 |
| ⑥ | 技能命名空间（`plugin:name` 前缀注入） | 回收前缀注入映射，技能列表移除该插件条目 | 前缀回收幂等；同名覆盖后恢复旧插件前缀 |
| ⑦ | 工具 registry 条目（`ToolRegistry.unregister(name)`，registry.py:232） | 按插件名注销其全部工具；清理 `_loaded` 记录与跨根 `_root_tracker` 引用 | 快照机制下执行中的调用不受影响；重复注销返回 False 不抛错 |

### 3.3.2 热插拔保护线不可动（T5 审查必改③）

现 watchfiles 热重载链的 5 项机制**原样保留**，仅将「重载动作」换为「enable/disable 全链路」：

| # | 保护线 | 现状（backend.py + plugin_tool_loader.py，T2 实测） | 约束 |
|---|---|---|---|
| 1 | 2s 防抖轮询 | `PluginToolWatcher.start(poll_interval=2.0)`（plugin_tool_loader.py:420）+ 命令重载 500ms 防抖（builtin_commands._RELOAD_DEBOUNCE_MS） | 保留；防抖窗口不缩短 |
| 2 | 10s 去重 | `_DEDUP_INTERVAL = 10.0`（backend.py:1103）+ `_watcher_dedup_cache` | 保留；同一 (插件,组件) 10s 内重复变更合并 |
| 3 | 路径索引重建防自触发循环 | `_rebuild_watcher_prefixes()`（backend.py:1709） | 保留；重载写回触发自身 watch 事件时靠索引重建过滤 |
| 4 | watcher 引用计数 + stop_event | `_plugin_watcher_refcount` / `_plugin_watcher_stop`（backend.py:1033+） | 保留；最后一个窗口关闭时停线程，重开恢复 |
| 5 | 多窗口 plugin_changed 广播 | `_hot_reload_requested`(pyqtSignal) → `_on_hot_reload_requested` 广播到 `ChatBackend._active_instances`（backend.py:247/1032/1692） | 保留；信号连接队列方式不变 |

> ⚠️ UI 目录物理分家（T5 强调）：改造 `_identify_all_components_from_changes` 时，`KNOWN_COMPONENTS = {"agents","hooks","commands","themes","skills","mcp","lsp","ui"}`（backend.py:1593/1639）必须包含 ui 且不回归——UI 组件从插件目录/运行时目录分离后，路径识别逻辑仍能正确分派。

### 3.4 扩展点与钩子清单

| 域 | 事件 | 语义 | DriFox 对应 |
|---|---|---|---|
| 生命周期 | plugin/loaded, enabled, disabled, unloaded | emit | 新增 |
| 对话/会话 | session/event（append-only 持久日志）、turn/*、step/* | emit/serial | AgentManager + `app/core/store/session_repository.py` 会话记录改造 |
| Agent | agent/pre-step（重写/拒绝输入）、agent/turn-stopping | waterfall/serial | 对话引擎拦截点（`app/core/engines/`） |
| 工具 | tools/pre-execute（审批/改写）、tools/post-execute | waterfall/emit | `app/tools/plugin_tool_loader.py` + `app/core/tool_permission_controller.py` |
| LLM | llm/stream（多模型路由中间件） | waterfall | `app/core/backend.py` 多模型后端改造 |
| 能力 | fs/*、memory/*、telemetry/* | emit | 新增 |
| UI | ui/card-registered、ui/slot-render（预定义插槽注入） | emit | `app/core/ui_plugin_registry.py` |

### 3.5 Seam 三层

新增能力必须同时交付三件套，三者缺一不立项：

1. **Service Definition**（接口定义）
2. **Provider**（实现）
3. **Consumer**（消费方）

换 Provider 即全产品替换，消费方不感知。

### 3.6 事件总线线程模型（T5 审查必改⑥）

**分发线程约束**：

| 事件域 | 线程要求 |
|---|---|
| UI 域（ui/*） | **强制主线程分发**（Qt 信号或 queued 入队），禁异步线程直接触碰 widget |
| 生命周期域（plugin/*） | 允许异步（watcher 线程 emit），但副作用落地走主线程 |
| 工具域（tools/*） | 允许异步（worker 线程内 waterfall） |

**过渡保留（阶段 1 不得删旧 API）**：现 watchfiles 线程 → Qt 信号桥 → 主线程重载链路原样保留——`_hot_reload_requested`（pyqtSignal, backend.py:247）由 watcher 线程 emit、队列连接（queued）到主线程 `_on_hot_reload_requested`，再经 `plugin_changed` 广播到各窗口。事件总线新机制只在其上做包装层，不拆除旧连接。

---

## 4. 拆解清单（按模块）

| # | 模块 | 当前状态（T2 实测） | 插件化方案 | 难度 | 优先级 |
|---|---|---|---|---|---|
| 1 | 事件总线 + DI 容器 | 无统一（现为 Qt signal / 单例 get_instance 直连） | 内核新增 ctx 注册表与 4 分发模式事件总线 | 中 | P0 |
| 2 | 生命周期规范化 | 启停 = 配置双写（enabled/disabled）+ 手搓联动 reload | 五态生命周期 + 可逆副作用（ctx.effect 成对） | 中 | P0 |
| 3 | UI 插件 | 已有 `app/core/ui_plugin_registry.py`（4 扩展点） | 补 seam 接口声明；UI 与运行时目录物理分家 | 低 | P0 |
| 4 | 工具插件 | 已有 `app/tools/plugin_tool_loader.py` | 注册/注销逆操作补齐；tools/* 事件 | 低 | P0 |
| 5 | Hook 插件 | 已有 `plugins/system/hooks/` + `app/core/hook_manager.py` | 事件域标准化 | 低 | P0 |
| 6 | LLM 适配器 | 多模型在引擎内（backend + provider_profile） | 拆 ctx.llm seam + 路由 middleware | 中 | P1 |
| 7 | 会话/记忆 | `app/core/store/` 仓储 + MemoryManagerCore 单例 | 会话事件持久化；存储 seam | 中 | P1 |
| 8 | 团队/调度 | `app/core/team_manager.py` + TeamManager + subagent_dag 已有 | 调度器 seam | 中 | P1 |
| 9 | MCP 桥接 | `.mcp.json` 已有（PluginManager 合并配置） | seam 化 + 插件级 MCP 配置合并保留 | 低 | P1 |
| 10 | 主题 | `plugins/system/themes/` 已有（17 个） | CSS 变量式零侵入换肤；主题 = 资源插件 | 低 | P1 |
| 11 | 遥测/成本 | context-usage-stats 单插件 | 一等公民化：turns/steps/Token/缓存命中内置事件 | 中 | P2 |
| 12 | 数据源插件 | 无 | 新 seam：文件/网页/知识库数据源 | 高 | P2 |

---

## 5. 迁移路径（三阶段）

### 阶段 1「契约基建」（T5 审查必改⑦：拆分 2 轮）

**轮 1：事件总线 + DI + 生命周期（地基，独立验收）**

交付：

- DI 容器 + 事件总线（内核）
- 五态生命周期 + 可逆副作用（`ctx.effect` 成对）
- manifest schema 扩展（全可选）
- 卸载契约注册点清单落地（§3.3.1 7 类注册点 disposer 成对）

独立验收（轮 1 完成即达）：

- 单测覆盖 enable → disable → re-enable 无残留（按 §3.3.1 清单逐项断言）
- 按依赖序加载与旧 `_COMPONENT_ORDER` 结果等价（§3.1 意见④）
- 事件总线线程模型合规（§3.6：UI 域主线程、其余域允许异步）
- 遥测仅**埋事件**（turns/steps/Token/缓存命中 emit），**不建 seam**，P2 不前置

**轮 2：UI + 工具 + Hook 三插件按新契约走通**（依赖轮 1）

交付：

- 三插件（`ui/`、`tools/`、`hooks/`）按新契约（apply(ctx) + 事件监听 + disposer）走通
- 双轨加载：新机制缺省时回落旧逻辑（§6）

验收（阶段 1 整体）：

- 内置 6 UI 插件 + 全部系统插件无 manifest 改动继续工作
- `tests/` 全绿

风险：事件总线替换既有信号连接 → 用包装层过渡，不删旧 API（§3.6）。

### 阶段 2「服务 Seam 化」（3+ 轮）

**交付**：

- LLM / 会话 / 存储 / 团队 / MCP 逐个转 seam
- `ctx.*` 稳定键冻结为公共契约
- 遥测事件入内核

**验收**：

- 任一 seam 可换 Provider 跑通 demo
- 原功能无回归

### 阶段 3「生态化」（持续）

**交付**：

- 插件市场 schema 升级（`app/tools/` 或 `plugins/plugin-marketplace/`）
- 数据源 seam
- 主题深度
- `drifox.plugins` 入口点通道

**验收**：

- 第三方插件仅写 manifest + entry_point 即可发布
- 插件市场可安装/卸载不重启生效

---

## 6. 兼容策略

| 策略 | 说明 |
|---|---|
| 新字段全可选 | 旧 plugin.json 不新增字段也按默认契约加载，33+ 现有插件零迁移（T2 实测：`plugins/system/` + 6 内置 UI 插件 + `~/.drifox/plugins/` 50+） |
| 双轨加载 | 阶段 1 同时支持「组件目录约定」（旧）与「apply(ctx) 服务注册」（新），新机制缺省时回落旧逻辑 |
| 启用状态不破坏 | enabled_plugins / disabled_plugins 语义与存储格式不变，仅内部实现换生命周期驱动（T2 实测：`plugin_manager._get_enabled_set/_get_disabled_set` 双集合并存 → 改为单来源 + 派生） |
| UI 与运行时分离 | 先物理分目录、后独立契约，窗口隔离语义保留（T2 实测：`ui_plugin_registry._card_widget_instances[window_id]` 按窗口隔离） |
| 回归保障 | 每阶段出指令前先跑回归：现有 `tests/`（core/gateway/utils/widgets）+ 6 内置 UI 插件冒烟 |

---

## 7. 风险与反模式

| 风险/反模式 | 对策 |
|---|---|
| 过度设计 | 不要照搬 DSH 全量（20 万行 TS / 230 workspace），只抽微内核 + seam + 事件三域三重机制 |
| 递归插件化 | PluginManager 本身绝插件化；内核别拆薄到无法自举 |
| 卸载不彻底 | 禁止「调用方联动式」清理；强制注册 = disposer 成对，否则热插拔泄漏 |
| 依赖循环 | inject 图引入循环检测，加载期 fail-fast |
| 配置双写漂移 | enabled/disabled 双集合改单来源 + 派生，防不一致 |
| 事件域混用 | 持久事实（对话日志）与实时事件（进行中拦截）必须分域 |
| UI 绞进运行时 | 改主题/面板必须不动执行链路，违者视架构违例 |

### 7.1 契约继承项（T5 审查必改②）

现有安全/权限机制**原样继承进新契约**，不因插件化而弱化：

| # | 继承项 | 现状（T2 实测） | 契约要求 |
|---|---|---|---|
| 1 | 工具 danger 强制声明 + source 防伪注入 | `_PluginRegistryProxy`（plugin_tool_loader.py）：register 必须显式声明 danger（registry 层拒绝未声明插件工具）；source 强制为 `plugin:<name>`，忽略插件自报，防伪造 builtin 绕过 danger 校验 | 新契约沿用：插件工具注册必经代理，danger 缺失即拒绝，source 不可伪造 |
| 2 | system 类型禁删/禁禁用 | `disable_plugin` 依据 manifest type == "system" 拒绝禁用（plugin_manager.py:429）；installer 判定对齐 | 新契约沿用：manifest type=system 的插件（含 plugin-marketplace、system）不可禁用、不可卸载 |
| 3 | 危险工具分组权限过滤 | `app/core/tool_permission_controller.py` + 权限卡片分组（registry 驱动 schema/icon/cn_name/danger/group） | 新契约沿用：danger 分组驱动 UI 权限卡片与审批流，插件不可绕过 |
| 4 | 安全类能力排除可替换性（L3 排除项） | `plugins/system/hooks/safety_guard.py`（PreToolUse 危险操作拦截：rm -rf /、fork 炸弹、强推 main、写 .git、密钥硬编码等）+ `app/tools/command_safety.py`（命令安全） | **L3 可替换性不适用**：safety_guard hook 与 command_safety 归内核必留 / 仅 system 插件可修改，用户插件不得覆盖或禁用 |

---

## 附录 A：现状补充 — 现有组件总表与加载链路

> 来源：T2 摸底（build@win_1972 产出）。组件在 `plugin_manager._scan_plugins` 中按物理目录自动探测（`app/core/plugin_manager.py`）。

| 组件 | 入口目录/文件 | 注册表/子系统 | 加载函数 | 热更新方式 |
|---|---|---|---|---|
| commands/ | `plugins/*/commands/*.md` | CommandManager（`app/core/command_manager.py`） | `builtin_commands._load_commands_from_plugins` + `reload_all_commands`（500ms 防抖 + 缓存） | backend watchfiles → reload |
| agents/ | `plugins/*/agents/*.md` | AgentManager（`app/core/agent.py`） | `_load_agents_from_dir`；同时 `_register_builtin_agents_as_commands` 注册 /agent 命令 | backend watchfiles → reload_plugin_agents |
| skills/ | `plugins/*/skills/*/SKILL.md` | 技能系统（懒加载） | `get_local_skills()` + `invalidate_skills_cache()` 失效 | 懒失效 |
| themes/ | `plugins/*/themes/` | ThemeManager（`app/utils/theme_manager.py`） | `theme_manager.reload()` + `update_theme_options()` | backend watchfiles → reload |
| hooks/ | `plugins/*/hooks/hooks.json` + py | HookManager（`app/core/hook_manager.py`） | `load_hooks_from_directory_flat`；事件点 BuildSystemPrompt/SessionStart/PreUserMessage/PreToolUse/PostToolUse/Stop/PluginChanged + matcher + 类型(prompt/python/http/command) + 决策(continue/block/defer) | backend watchfiles → 仅重载 hooks |
| mcp/ (.mcp.json) | `plugins/*/.mcp.json` | mcp_tools（`app/tools/mcp_tools.py`） | `PluginManager.get_mcp_configs()` 合并 + `get_mcp_servers()`（30s TTL 缓存） | 懒失效（invalidate_mcp_cache） |

### PluginChanged 事件（插件/MCP 变更通知）

> 环境事件：插件或 MCP 发生变化时触发，不在会话流内。hook 输出经
> `_hook_message_queue` 排队，AI 下一轮对话可见（`add_output_to_context` 可关）。

**触发点与 action 语义**（matcher 可混合两类值 pipe 多选）：

| action / 子动作 | 触发点 | 说明 |
|---|---|---|
| installed / updated / uninstalled | `backend.emit_plugin_changed()`（安装/更新/卸载/热重载统一出口；installer 传精确值，watcher 路径自动推断） | `context.plugin_name` + `components`（各组件重载结果） |
| enabled / disabled | `plugin_manager.enable_plugin / disable_plugin` 末尾（覆盖市场与所有启停入口） | `context.plugin_name` |
| mcp_added / mcp_removed / mcp_updated | `plugin_manager.add/remove/update_mcp_server` | `context.server_name` + `server_config` |
| mcp_connected / mcp_disconnected / mcp_failed | `MCPServerConnection.set_state` 状态迁移（CONNECTING 不触发、同态过滤） | `context.server_name`；connected 附 `mcp_tools`（发现的工具名列表），failed 附 `error` |
| tools_added / tools_removed / tools_updated | **子动作**（`context.sub_actions`，由 diff 非空键派生）：工具新增/移除/同名 schema 变更（签名 hash 对比） | matcher 写 `"tools_added\|tools_updated"` 可仅在工具增删改时触发 |

**diff 明细**：事件自动附加 `context.diff` + `context.sub_actions`
（`hook_manager._compute_plugin_snapshot_diff`，模块级快照基线，首次触发只建基线）：
`{"tools_added": [...], "tools_removed": [...], "tools_updated": [...], "mcp_added": [...], "mcp_removed": [...]}`。

**全局触发辅助**：`hook_manager.trigger_plugin_changed_hook(context)` — 无 backend
引用的模块（plugin_manager/mcp_tools）统一入口；任意线程安全（线程池异步投递），
自动附加 diff；无活跃 backend 或无注册 hook 时静默跳过（仍刷新快照基线）。

### 对话引擎 Hook 规范化（HookPolicy）

> 插件自建对话执行栈（autoloop/chinese-chess 等经 `services["conversation_stack"]`
> 或 `services["create_engine_session"]`）复用主程序 tool_executor 时，hook 是否
> 触发由**引擎声明的 HookPolicy 决定**，不再被动执行全局 hooks。

`ConversationConfig.hook_policy`（`app/core/conversation/config.py`）三档：

| 级别 | 消息级（PreAssistantMessage/PostAssistantMessage/Stop） | 工具级（PreToolUse/PostToolUse） | 适用 |
|---|---|---|---|
| `ALL`（默认） | 触发 | 触发 | UI 主对话 / Gateway（行为零变化） |
| `TOOL_EVENTS_ONLY` | 不触发 | 触发 | 需保留安全审查类工具 hook 的插件引擎 |
| `NONE` | 不触发 | 不触发 | 插件后台循环（默认推荐） |

传导链：`ConversationConfig.hook_policy` → executor 注入 worker_kwargs →
ChatWorker `_hook_policy` 归一化（None/非法 → ALL 兼容旧调用方）→
①消息级：`_trigger_worker_hook` 入口短路；②工具级：`_execute_tool` 传
`trigger_hooks` per-call 参数 → `tool_executor.execute(trigger_hooks=False)`
跳过 PreToolUse/PostToolUse（per-call 线程安全，subagent 并发不受影响）。

> ⚠️ **枚举必须翻译成 `hook_policy_id` 才生效**（2026-09-02 修复）。
> `ChatWorker._hook_policy_obj_resolve()` 只在**显式 id** 存在时按 id 取策略；
> 没给 id 时它会忽略枚举、直接回落 `get_active(SCOPE_MAIN)`（= `all`）。
> 所以引擎只传 `hook_policy=HookPolicy.NONE` 而没传 `hook_policy_id` 时，
> 声明形同虚设 —— Stop hook 的续命提醒照样会注入插件引擎的消息流。
> `EngineSessionImpl.__init__` 现在负责翻译（`ALL→"all"` /
> `TOOL_EVENTS_ONLY→"tool_only"` / `NONE→"none"`，注意枚举值与插件 id
> **不同名**）；主对话 / Gateway 引擎不声明 `hook_policy`，枚举保持默认
> `ALL` 且 `hook_policy_id=None`，仍按用户激活策略走，行为零变化。
> 守卫：`tests/plugins/test_engine_session_stale_and_hook_policy.py`。

### EngineSession — 插件对话引擎最通用驱动原语（EP3）

> 契约 `app/plugins/contracts/engine_session.py`；实现
> `app/core/conversation/engine_session.py`；注入
> `services["create_engine_session"](engine_name, **kwargs)`。

**设计原则：不预设对话流程，最大化自由度。**

```python
session = services["create_engine_session"]("my-engine")   # 默认 hook_policy="none"
r = session.turn(user="你好", timeout=60)                    # 最简一轮
r = session.turn(messages=msgs, tools=phase_tools,          # 全控一轮
                 callbacks={"content_received": on_chunk})
session.executor.execute(...)                               # 逃生舱（core/executor 公开）
```

`turn()` 只做「执行一轮 + 同步等待」；messages/tools/callbacks 全量透传
（finished/error 由会话持有以保证同步语义，其余回调原样转发 worker）；
`auto_history` 可选多轮累积（默认关闭，上下文由调用方管理）；返回
`ChatResult(text/error/cancelled/timed_out/messages/ok)`。

实现收编插件曾各自维护的五类样板（隔离 ConversationCore / threading.Event
同步 Adapter / stale worker 复位防御 / 会话初始化 / 空响应兜底恢复），
插件只剩业务逻辑。插件应在自己的 QThread 中调用 `turn()`（阻塞），
UI 更新经 Qt 信号转发。

**kwargs（透传给 `EngineSessionImpl`）**：

| kwarg | 说明 |
|---|---|
| `model_config_override` | 模型配置覆盖（在窗口当前配置之上叠加，如关思考/改温度） |
| `hook_policy` / `hook_policy_id` | hook 参与级别（见上文 HookPolicy） |
| `permission_strategy` | 权限策略（`auto_allow` / `auto_deny` / …） |
| `loop_policy_id` | **引擎级循环策略 id**（见下） |

#### 引擎级循环策略（loop_policy_id）

插件自建引擎常只需要"一回合"（记忆整理 / 定时 prompt / 状态机判定）。
`LoopPolicyRegistry` 的激活槽是**全局共享**的，`set_active()` 会连主对话
一起改；正确姿势是用 `loop_policy_id` 让**本引擎**按 id 直接取策略对象：

```python
session = services["create_engine_session"](
    "my-engine",
    loop_policy_id="my-single-turn",   # 按 id 取，不碰全局激活槽
)
r = session.turn(messages=msgs, tools=[], timeout=180)
```

传导链：`ConversationConfig.loop_policy_id` → `ConversationExecutor.execute()`
→ `OpenAIChatWorker._loop_policy_id`（类级默认 `None`）→ `_loop_policy()`
按 id 解析，未设置/未注册时回落 `get_active()`（主对话零行为变化）。
完整说明见 `docs/plugins/loop-policies.md` §4.2。

参考实现：`plugins/assistant_hub/loop_policies/single_turn.py` +
`plugins/assistant_hub/core/llm_client.py`（记忆/Dream/经验整理单回合调用）。

| lsp/ (.lsp.json) | `plugins/*/.lsp.json` | LspManager（`app/core/lsp/`） | `add_plugin_servers` / `remove_plugin_servers` 增量 | backend watchfiles → 增量重载 |
| ui/ | `plugins/*/ui/__init__.py`（必须暴露 register_ui） | UIPluginRegistry（`app/core/ui_plugin_registry.py`） | `load_plugin`：sys.path 注入 → importlib 动态加载 → `register_ui(self)`；4 扩展点：register_content_renderer / register_message_factory / register_floating_card / register_welcome_tab；右工作台 tab：register_workbench_tab | backend watchfiles → reload_plugin（先卸旧后载新、清 sys.modules + pycache） |
| tools/ | `plugins/*/tools/*.py`（必须暴露 register(registry)） | ToolRegistry（`app/tools/registry.py`） | `plugin_tool_loader.load_plugin_tools`：source 强制 `plugin:<name>`、danger 必填、user 覆盖 system | 独立 PluginToolWatcher 轮询线程（2s 签名对比 → scan_now 全量重扫，幂等+锁） |
| team_templates/ | `plugins/*/team_templates/*.yaml` | TeamManager + `app/core/team/template_manager.py` | `get_template` 查询 | 懒加载 |
| engines/ | `plugins/*/engines/*.py`（必须暴露 register(registry)） | EngineRegistry（`app/plugins/registries/engine_registry.py`） | `runtime_component_loader._make_engine_loader` + `ensure_engine_watcher`；backend `create_engine_for_slot("ui", ChatEngine, ...)` 工厂化创建；替换类必须 `isinstance(ChatEngine)` 安全网回退内置 | `runtime_component_loader` watcher 轮询 → `builtin_reloaders._reload_engines` 精准卸载/重载单插件 |

**UI 插件的扩展点（4 类原始 + 三期扩展）**：

**原始 4 类（T2 实测 `ui/__init__.py` 内 register_ui 用法）**：

1. `register_floating_card(plugin_name, card_id, widget_class, container, title, ...)` → 自动注册 `/card_id` 命令（用户插件加 `plugin_name:` 前缀）；container ∈ top/bottom/left/right/full；挂 Tab 级四向容器（`app/widgets/cards/card_manager.py` ContainerType）；向卡片注入 set_context_provider / set_context 上下文。**Tab 模式下卡片 widget 单实例挂全局容器，但可见状态按标签页隔离**：registry 维护 per-tab 可见集合（`_tab_card_visibility`），切标签时由 `TabManagerWindow._on_tab_selected → sync_floating_cards_to_tab` 投影 show/hide（走 CardManager 标准路径，互斥/容器展开/覆盖层切换自动生效）——一个标签页打开 full 覆盖卡不再影响其他标签页的对话区。**三期扩展**：LEFT/RIGHT 容器支持多卡堆叠（声明 `metadata={"stack": True}`，使用 Pivot 切换），TOP/BOTTOM 系统卡互斥逻辑零改动。详见 [`docs/plugins/ui-workspace.md`](./plugins/ui-workspace.md)。
2. `register_content_renderer(plugin_name, type_name, render_func)` → `app/core/message_content.py` 遇 custom_type 内容块时查表渲染 HTML。
3. `register_message_factory(plugin_name, name, condition_func, factory_func)` → `app/main_widget.py::_create_message_widget` 按 priority 尝试构造 widget。
4. `register_welcome_tab(plugin_name, mode_key, label, render_func)` → `app/widgets/message_card.py` 欢迎卡片 tabs；加载/卸载后 debounce 刷新。

**三期新增（Phase G）**：

5. `register_workspace_page(plugin_name, page_id, title, widget_class, ...)` → 插件注册完整主页面（非对话形态），挂载到 `TabManagerWindow._content_area`（QStackedWidget）+ 自动注册侧边栏入口 + `/<plugin_name>:<page_id>` FUNCTION 命令直达。懒创建、卸载清理。详见 [`docs/plugins/ui-workspace.md`](./plugins/ui-workspace.md)。

**三层灵活性模型**：条目级（Phase E：SlotEntry）/ 模块级（Phase F：UIModule）/ 页面级（Phase G：WorkspacePage）。

**插件目录三类优先级（T2 实测）**：`plugins/system/`（system）< `~/.claude/`（claude）< `<app_data>/plugins/`（user），同名用户覆盖系统。

---

## 附录 B：现状补充 — 耦合点清单

> 来源：T2 摸底。行数为 T2 实测（2026-08-17），重构时以实际情况为准。

**B1. 巨型文件（强内聚）**

| 文件 | 行数 | 问题 |
|---|---|---|
| `app/main_widget.py` | 20389 | OpenAIChatToolWindow 单类：UI 装配 + 会话交互 + 快捷键 + 命令面板 + 欢迎卡片全揉一个类；`setup_ui()` 3000+ 行手工装配，无 UI 分区抽象（**Phase F 二期**：`setup_ui` 已收敛为根布局 + compose 五模块；装配代码迁至 `app/widgets/modules/`，详见 [`docs/plugins/ui-modules.md`](./plugins/ui-modules.md)） |
| `app/core/backend.py` | 3384 | ChatBackend 三重身份：引擎工厂 + 插件热更新调度器 + 子系统协调器 |
| `app/core/hook_manager.py` | 2677 | Hook 全生命周期 + 并行执行 + 事件匹配 |
| `app/widgets/message_card.py` | 11009 | 消息渲染 + 欢迎卡片 + 交互巨复杂 |

**B2. backend._reload_single_plugin 8 分支硬编码**

- `app/core/backend.py` 的 `_reload_single_plugin` / `reload_plugin_subsystems` 内手写 `agents/hooks/commands/themes/skills/mcp/lsp/ui` 逐组件 if 分支 + `_identify_*` 路径判定。
- **新增插件组件类型必须改 backend.py** → 违反开闭原则。

**B3. UI 鸭子协议耦合**

- `ui_plugin_registry` 与 `TabManagerWindow` / `OpenAIChatToolWindow` 通过鸭子类型约定属性强耦合：`_card_manager` / `_window_id` / `_top_card_container` / `register_system_card` / `hide_card` 等。
- `main.py` 里 `FakePage` 硬编码了一套窗口协议（workflow_name / global_variables_changed / show_splitter...），新宿主窗口必须照抄。

**B4. 双 watcher 并存**

- 后台 watchfiles 插件监听（backend.py `_start_plugin_watcher`，类级单例 + 引用计数，事件 → `_reload_single_plugin` 分派）。
- 工具轮询 watcher（`app/tools/plugin_tool_loader.py::PluginToolWatcher`，2s 签名对比 + scan_now）。
- 两套扫描/去重逻辑并存 → 组件卸载/重载路径不统一。

**B5. 单例风暴**

- `PluginManager ⇄ UIPluginRegistry ⇄ CommandManager ⇄ AgentManager ⇄ HookManager ⇄ Settings ⇄ TabManagerWindow ⇄ ToolRegistry ⇄ TeamManager` 大量 `get_instance()` 单例直接互 import，测试和模块化困难。

**B6. 双路径命令注册**

- UI 插件卡片命令：`main_widget._init_ui_plugins_deferred` for 循环手动为每个 card 包 handler（`_function_command_handlers`）；
- registry 自身 `_register_command_for_card` 也注册命令 → 两条路径易漂移（当前靠 `re_register_all_commands()` 兜底同步）。

**B7. 其他硬编码**

- 核心模块直接 import 业务：`plugin_manager` 内 `from app.utils.config import Settings`、`from app.utils.utils import get_app_data_dir`；
- `ui_plugin_registry._register_command_for_card` 直接 import CommandManager 并注册（跨系统紧耦合）。

---

## 附录 C：现状补充 — 三条关键实施建议

> 来源：T2 摸底。「整体插件化」的最短路径。

**建议① 组件 reloader 注册表化（backend 8 分支 → component_reloaders 表）**

- 现状：`backend.py::_reload_single_plugin` 手写 8 个组件的 reload 分支，新增组件类型必须改 backend。
- 方案：把 8 分支重构成注册表 `component_reloaders: Dict[str, Callable[plugin, result]]`，组件名 → reloader 函数。
- 收益：插件可注册新组件 + reloader，backend 核心不再需要为新组件改代码——「万物即插件」的骨架扩展点。

**建议② UI 装配契约化（IWindowHost 协议替代鸭子类型）**

- 现状：`ui_plugin_registry` / `main.py::FakePage` 靠鸭子类型约定属性耦合。
- 方案：将鸭子协议（`_card_manager` / `_window_id` / 四向容器 / `register_system_card`）收敛为显式 `IWindowHost` Protocol / 基类。
- 收益：新宿主窗口（Tab / 独立弹窗 / 未来形态）实现协议即接入，消除手抄协议与测试 stub 漂移。

**建议③ 统一热更新（双 watcher 合并为单一 PluginWatcher + on_change 回调）**

- 现状：backend watchfiles 与 PluginToolWatcher 轮询两套机制并存。
- 方案：合并为单一 PluginWatcher，插件侧注册 `on_change(component)` 回调替代 backend 手写 if/elif。
- 收益：一套扫描/去重/分派逻辑；组件 reloader 注册表（建议①）即 watcher 的回调表，二合一。