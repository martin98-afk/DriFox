# 插件能力池（Capability Pool）

> 插件与主程序之间**唯一的能力调用入口**。插件声明用什么、主程序按声明注入什么。

---

## 0. 一句话

插件不再 `import app.main_widget` 或直接摸 `ctx["main_widget"]` 的内部字段，而是一律通过 `ctx["capabilities"]` 拿能力。插件在 `plugin.json` 里声明需要哪些能力，主程序按清单裁剪。

**能力池是权限收敛层，不是沙箱。** 它阻止"合理误用"与"隐蔽越权"，让审核有清单可读；它**不能**阻止恶意插件直接用 `os` 读文件系统。真要隔离得走 `docs/security/sandbox.md` 那条独立路线。

---

## 1. 插件依赖的三条边界

| 类型 | 走哪 | 例子 |
|---|---|---|
| **能力型**（要宿主运行时状态、有副作用） | `ctx["capabilities"]` | `session.write`、`platform.send`、`stream.subscribe` |
| **静态型**（无宿主依赖，import 期就要用） | `app.plugins.sdk` | `Colors`、`ToolResult`、`ElidedLabel`、`GitWorktreeDetector` |
| **注册型**（加载期向宿主登记扩展点） | 现有 `register_*` | `register_content_renderer`、`register_floating_card`、工具 / hook 注册 |

判定：需要窗口 / 会话 / 配置实例才成立的 → 能力型；不需要 → 静态型；是"告诉宿主我有这个东西" → 注册型。

三者合起来构成插件唯一合法依赖面。除此之外的 `app.*` import 由 `tools/plugin_import_guard.py` 报违规。

---

## 2. 在 plugin.json 声明

```json
{
  "api_version": 2,
  "capabilities": ["session.read", "input.read", "ui.notify"]
}
```

### 支持通配

- `"tool.*"`：`tool` 组全部能力
- `"*"`：全部能力（审核时一眼看出"这插件要一切"，慎用）

### 裁剪规则

| manifest 状态 | 行为 |
|---|---|
| 有 `capabilities` 字段 | 按清单 + 通配裁剪 |
| `"capabilities": []` | 只剩 `state.kv` 与 `plugin.log` |
| 无该字段，`api_version <= 1`（含缺省） | 兼容期：全量放行 + 一行 warning |
| 无该字段，`api_version >= 2` | 等同 `[]` |

新插件一律写 `"api_version": 2` 并显式声明清单。存量插件（`api_version` 缺省 = 1）继续全量放行，不受影响。

### 扁平名兼容

历史 manifest 可能写 `["lsp", "mcp", "gitee", "subagent", "window_state"]`，这些会被映射到命名空间 ID（`lsp.query` / `mcp.call` / `net.upload` / `subagent.run` / `state.kv`）。

---

## 3. 插件代码怎么写

```python
def register_ui(registry):
    registry.register_input_button(
        button_id="my-btn",
        icon_path="icons/a.svg",
        on_click=_on_click,
    )


def _on_click(ctx):
    caps = ctx["capabilities"]

    # dict 风格取值（首次访问才执行 provider）
    session = caps["session.read"]()
    print(session["session_id"])

    # 判断是否放行
    if "platform.send" in caps:
        caps["platform.send"]("feishu", chat_id, "任务完成")

    # 审核用：已放行的全部能力
    print(caps.names())

    # 按组取
    models = caps.group("model")
```

### 未声明时抛异常

```python
app.plugins.capabilities.pool.CapabilityDenied:
插件 my-plugin 未获授权能力 session.write。
在 .drifox-plugin/plugin.json 的 capabilities 数组加入 "session.write" 后重载插件
```

异常里带可直接照做的修复指引。开发期看到这条就别去 `ctx["main_widget"]` 绕路。

### 旧键仍然可用（兼容投影）

`ctx["services"]` 继续存在，由能力池按 `LEGACY_MAP` 投影渲染。旧键被裁掉时 `services` 里就没这个键，因此存量插件的行为与它的声明严格一致。

新代码请用 `caps[...]`。`services` 只为存量代码保留。

---

## 4. 能力清单

`level` 用于审核分级，不参与运行时裁剪（运行时只看声明）。

### 模型

| ID | level | 说明 |
|---|---|---|
| `model.metadata` | low | 当前模型的 provider / 名称 / 上下文长度 |
| `model.schema` | low | 指定 agent 视角的工具 schema |
| `model.config` | **high** | 完整模型配置，**含已解密明文 API key** |

> `model.config` 是插件取任意服务商配置的唯一正确入口。密钥模式下磁盘 `app.config` 里的 API_KEY 是密文或空串，插件自行 `json.load` 会拿到不可用的 key（表现为 401）。

### 会话

| ID | level | 说明 |
|---|---|---|
| `session.read` | medium | 当前 session_id 与消息列表 |
| `session.write` | medium | 消息写回当前会话（入库 + 刷 UI + 发事件） |
| `session.list` | medium | 历史会话列表与元数据 |
| `session.manage` | **high** | 新建 / 切换 / 删除会话（破坏性） |

### 输入框

| ID | level | 说明 |
|---|---|---|
| `input.read` | medium | 输入框文本与附件列表 |
| `input.write` | medium | 写入输入框文本 |

### 智能体

| ID | level | 说明 |
|---|---|---|
| `agent.read` | low | agent 列表 / 提示词 / 管理器 |
| `subagent.run` | **high** | 派生子智能体 |

### 工作目录

| ID | level | 说明 |
|---|---|---|
| `workdir.read` | low | 当前工作目录 |
| `workdir.write` | medium | 切换工作目录（一次封装：实例缓存 + executor 同步 + 团队广播 + 事件） |

### 外观 / 上下文

| ID | level | 说明 |
|---|---|---|
| `theme.read` | low | 主题 id / 名称 / 配色 / 字体 / 字号 |
| `context.compact` | medium | 上下文压缩器 |

### 工具

| ID | level | 说明 |
|---|---|---|
| `tool.read` | low | 工具注册表元数据查询 |
| `tool.inject` | **high** | 向 LLM 注入工具（改变模型可见行为） |
| `tool.execute` | **high** | 执行任意工具 |

### 对话引擎

| ID | level | 说明 |
|---|---|---|
| `engine.session` | **high** | 创建对话引擎会话（消耗 token） |
| `engine.stack` | **high** | 对话执行栈工厂 |

### 通讯平台

| ID | level | 说明 |
|---|---|---|
| `platform.send` | **high** | 向通讯平台发送消息（数据外发） |
| `platform.read` | medium | 平台与会话列表 |

### 内部资源

| ID | level | 说明 |
|---|---|---|
| `mcp.call` | **high** | MCP 工具调用 |
| `lsp.query` | medium | LSP 查询 |
| `net.upload` | **high** | Gitee 上传（数据外发） |

### 界面

| ID | level | 说明 |
|---|---|---|
| `ui.notify` | low | 弹 InfoBar 通知 |
| `ui.card.show` | low | 显示浮动卡 |
| `ui.card.hide` | low | 隐藏浮动卡 |
| `ui.exclusive` | medium | 独占 UI 模式 |
| `ui.dialog` | low | 文件 / 目录选择、确认框、单行输入（跟随主题） |

### 对话流 / 窗口

| ID | level | 说明 |
|---|---|---|
| `stream.subscribe` | medium | 订阅对话流事件（工具调用 / 结果 / 流起止 / 错误 / 上下文更新 / 会话切换） |
| `window.active` | low | 当前活跃聊天窗口**对象**（要调它的方法时用这个） |
| `window.current` | low | 当前窗口的**语义快照** dict（只读字段：backend/session_id/project/workdir） |
| `window.host` | low | 宿主顶层窗口（弹窗 / InfoBar 的合适 parent） |
| `window.list` | low | 存活窗口 id 集合 |
| `window.events` | medium | 订阅 tab 切换等窗口级事件 |
| `window.workbench` | medium | 打开 / 刷新右侧工作台页 |

> **选哪个**：只想要字段 → `window.current`（拿不到窗口对象，无从摸私有状态）；
> 需要**调窗口的方法**（如 `win._on_history_session_selected(...)`）→ `window.active`。
> 两者混用会出 `AttributeError: 'dict' object has no attribute '_xxx'`。
> 已有回归守卫：`tests/plugins/test_capability_window_current_guard.py`。

```python
# 语义快照：只给字段，不给对象
snap = caps["window.current"]()
snap["project"], snap["workdir"], snap["session_id"]

# 窗口对象：需要调方法时用
win = caps["window.active"]()
win._on_history_session_selected(index)
```

`stream.subscribe` 支持两种形态：

```python
# 列表 + 单一回调（按参数形状自行分派）
caps["stream.subscribe"](["stream_started", "stream_finished"], cb)

# 映射（同签名信号无法按参数区分时用这个）
caps["stream.subscribe"]({
    "stream_finished": on_finished,
    "error_occurred": on_error,
})
```

连接登记到能力注册表，插件卸载时由 `UIPluginRegistry.unload_plugin` → `release_plugin` 统一断开。**插件不需要自己 disconnect。**

### 插件管理 / 命令

| ID | level | 说明 |
|---|---|---|
| `plugin.read` | low | 已安装插件列表 / 元数据 |
| `plugin.control` | **high** | 重载 / 启停其他插件 |
| `command.read` | low | 命令列表 |
| `command.reload` | medium | 命令热重载 |
| `sync.register` | **high** | 把插件数据接入云端同步（数据外发） |
| `team.read` | low | 团队成员 / 角色 / 运行状态 |

### 基础（恒放行）

| ID | level | 说明 |
|---|---|---|
| `state.kv` | none | 窗口级 KV 存储（插件自己的沙箱，不触达宿主状态） |
| `plugin.log` | none | 带 `plugin=<name>` 前缀的 logger（日志可归因） |

这两个不参与声明裁剪。

### 逃生舱

| ID | level | 说明 |
|---|---|---|
| `host.raw` | **high** | 裸 `main_widget` / `backend` 对象引用 |

理论上没有插件需要它。若实施中发现某插件必须声明 `host.raw`，说明池的能力建模有缺口，应补能力而不是走后门。

---

## 5. 主程序侧：新增一条能力

四步，缺一不可：

**1. catalog 登记**（`app/plugins/capabilities/catalog.py`）

```python
_spec("mycap.read", _L, "一句话说明", _BOTH, ["legacy_service_key"]),
```

`_L/_M/_H/_N` = low/medium/high/none；`_UI/_TOOL/_BOTH` = 可用 surface。`legacy_keys` 是可选的旧服务键，用于 `ctx["services"]` 投影。

> 未在 catalog 登记的 ID 只能以 `experimental=True` 注册，并且插件无法通过 manifest 声明它。

**2. provider 实现**（`app/plugins/capabilities/providers/ui_core.py` 或 `tool_core.py`）

```python
def _mycap_read(host: Any):
    def read():
        return host._something

    return read


def register(registry) -> None:
    registry.register("mycap.read", _mycap_read, surface="ui", owner="core:ui")
```

单参 `(host)` 或双参 `(host, ctx)`（`ctx` 提供 `plugin` / `surface` / `registry`），按参数个数自动分派。

**3. 单测**（`tests/plugins/test_capability_*.py`）

**4. 文档**（本节表格）

### 语义动作，不是服务搬家

判据：provider 实现里若只有一行 `return host._x`，它是**搬家不是抽象**，应合并或重设边界。

反例：`workdir.write` 若只是 `return host.backend.tool_executor.set_workdir`，那插件还是要自己处理实例缓存与团队广播。正例是内部一次做完四步，插件从此不知道这些字段存在。

---

## 6. 排障

**`CapabilityDenied`**
按消息提示在 `plugin.json` 的 `capabilities` 里加 ID 后重载插件。

**声明了但拿不到**
检查 `api_version`：若为 2 且 `capabilities` 写错字（如 `session.Read`），展开后是未知 ID，不匹配任何 provider。用 `caps.names()` 打印实际放行的清单对照。

**兼容期怎么退出**
把插件的 `api_version` 提到 2 并补清单。宿主侧将来若想把默认版本从 1 提到 2，只需改一处默认值，无需逐插件改。

**审计日志**
每次某能力首次被访问记一行：

```
[CapabilityAudit] plugin=my-plugin cap=session.read level=medium
```

`high` 级记 WARNING。用来发现"声明了没用"或"悄悄用了没声明"。

---

## 7. 没有 ctx 的时候：caps_for

后台线程、工具 impl、hook 里拿不到卡片 context，但仍要取能力。统一用 sdk 的
`caps_for`，**不要**再自己 `ensure_registered` + `build_pool(host=None)`：

```python
from app.plugins.sdk import caps_for

writer = caps_for("input.write")          # 未授权 / 未注册 → None
if writer is not None:
    writer("/persona-creator")

# 要多个能力或想判 in，用 pool_for
from app.plugins.sdk import pool_for

pool = pool_for()
if pool is not None and "session.read" in pool:
    snap = pool["session.read"]()
```

**host 由宿主解析**（`app/plugins/capabilities/resolve.py`）：插件绑定的窗口
（卡片 ctx 建立时登记）→ 活跃窗口。所以后台线程里 `window.current` /
`session.read` / `input.read` 这类吃 host 的能力能拿到真实窗口，不再恒为 None。

授权口径与 `ctx["capabilities"]` 完全相同（同一条 `resolve_declared`）：走
`caps_for` 拿不到声明外的能力。参数可显式传 `plugin=` / `surface=`，缺省从
调用方文件路径推断（`plugins/<name>/tools/` → tool surface）。

排查用：`declared_for()` 返回该插件已解析的能力清单。

## 8. 检查工具

```bash
# 报告插件越界 import（默认只告警）
python tools/plugin_import_guard.py

# CI：有违规即失败
python tools/plugin_import_guard.py --strict
```

合法面：`app.plugins.contracts` / `app.plugins.registries` / `app.plugins.managers.plugin_config_store` / `app.plugins.sdk` / `app.plugins.capabilities` / `app.plugins.deps_loader` / `app.plugins.managers.plugin_manager`。
