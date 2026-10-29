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

> **⚠️ 升 `api_version` 前先确认宿主门槛**：`app/plugins/version_gate.py` 的
> `HOST_PLUGIN_API_VERSION` 是硬闸，插件声明值高于它会被 `VersionGate` 拒载
> （load_blocked，**该插件的任何组件都不加载**：工具 / UI / hooks 全无），
> 日志只有一行 `[VersionGate] 插件 'x' 插件 api_version=2 高于宿主支持的 1，拒载`。
> 当前宿主值为 **2**（capabilities 契约自它起生效）。
> 回归守卫：`tests/core/test_api_version_contract.py::test_shipped_plugins_declare_supported_api_version`
> 会扫仓库内全部 manifest，声明值高于宿主的直接测试失败。

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
| `provider.list` | low | 已配置服务商清单（id / 名称 / 模型数，**不含密钥与 URL**） |
| `model.enumerate` | low | 指定服务商的可选模型列表（含视觉 / 思考能力标记） |

### 服务商

| ID | level | 说明 |
|---|---|---|
| `provider.list` | low | 已配置服务商清单（见上，与模型组同一能力） |

> `model.config` 是插件取任意服务商配置的唯一正确入口。密钥模式下磁盘 `app.config` 里的 API_KEY 是密文或空串，插件自行 `json.load` 会拿到不可用的 key（表现为 401）。

### 模型下拉怎么选（示例）

渲染「选服务商 / 选模型」下拉**不要**自己 `json.load(~/.drifox/app.config)`，
走这两个能力：

```python
caps = ctx["capabilities"]

providers = caps["provider.list"]()          # [{"id","provider_name","display_name",
                                             #   "is_current","model_count","models_dev_id"}]
models = caps["model.enumerate"]("MiniMax")  # [{"name","is_current","supports_vision",
                                             #   "supports_thinking","context_limit","note"}]

# 取到完整配置（含明文 key）再走 model.config —— 枚举结果里没有密钥
cfg = caps.legacy("get_provider_config", "model.config")(
    providers[0]["id"], models[0]["name"]
)
```

要点：

- **两级而非扁平**：`provider.list` 带 `model_count`，调用方先判断要不要展开。
  一步返回全量会在「大服务商 × 多模型」场景拼出上万组合。
- **不带 `API_KEY` / `API_URL`**。URL 看着无害，配合社工手法能把插件流量引到
  别处；需要完整配置走 `model.config`。
- **`supports_vision` 走「声明\_支持图像」优先**。第三方网关的额度与能力跟官方
  API 各自成立，用户在模型参数卡的显式声明不被 models.dev 推断值反向覆盖。
- **未知服务商返回空列表**，不静默串到别的服务商。
- **tool surface 也可用**：`provider.list` / `model.enumerate` 在 ui 与 tool
  双 surface 注册，工具里挑模型（如图片转发给视觉模型）不需要改 manifest surface。

### 会话

| ID | level | 说明 |
|---|---|---|
| `session.read` | medium | 当前 session_id 与消息列表 |
| `session.write` | medium | 消息写回当前会话（入库 + 刷 UI + 发事件） |
| `session.list` | medium | 窗口内存会话列表与元数据（`host.session_manager.sessions`） |
| `session.history` | medium | **持久化**历史会话只读面（列表 / 归档 / 项目聚合 / 按 session_id 定位 / 团队会话查询） |
| `session.history.write` | **high** | 改写持久化历史（置顶 / 移动项目 / 归档 / 恢复 / 删除 / 导入） |
| `session.manage` | **high** | 切换 / 新建当前会话（加载历史会话、清空开新会话） |
| `session.interrupt` | medium | 停止当前流式输出（入队，本轮结束后执行） |
| `session.new` | medium | 新建会话（入队，停流后执行） |
| `session.send` | medium | 发消息驱动一轮对话（入队；`ephemeral` 不写会话、不渲染气泡） |

> `session.list` 与 `session.history` 不是一回事：前者是**当前窗口内存里的活跃会话对象**（`host.session_manager.sessions`），后者是 `HistoryManager` 的**跨会话落库记录**（含归档、项目归属、团队 run_id）。取历史面板数据必须用 `session.history`。

> `session.history.write` 的写动作**内部已含宿主联动通知**（历史页刷新 + 欢迎卡片失效 + 跨窗口广播）。插件调完不必再自己调窗口方法。

> `session.interrupt` / `session.new` / `session.send` 是**入队语义**：插件在工具执行期调用只登记动作，主程序在本轮结束（`stream_finished`）后按登记顺序执行。原因：工具跑在 worker 线程，同步停流会杀掉自己所在的 worker。队列按 `window_id` 隔离，拿不到 id 一律拒绝（写操作回退「活跃窗口」会在多窗口同时交接时串窗）。

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

### 项目

| ID | level | 说明 |
|---|---|---|
| `project.read` | low | 当前项目名 + 项目清单 + 元数据（会话数 / 工作目录数）+ 根目录映射 |
| `project.write` | **high** | 切换 / 新建 / 提交 / 归档 / 导入导出 / 打开文件夹 / 拖拽建项目（含团队广播与 Tab 图标联动） |

> `project.write.switch` 内含「切项目 + 同步工作目录 + 新建会话 + 团队广播 + 收起面板」，插件不需要知道其中任何一步。要单独新建会话（不切项目）用 `session.manage.new()`。

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
| `ui.artifacts.show` | medium | 向消息卡片成果栏推送成果条目（image/video/html/text/file 五类分控件；`show(items, target="latest", call_id="")`，返回投递条目数；`ToolResult.artifacts` 字段是工具插件的免声明通道） |
| `ui.exclusive` | medium | 独占 UI 模式 |
| `ui.dialog` | low | 文件 / 目录选择、确认框、单行输入（跟随主题） |


页签徽标（任意元素挂到工作台页签，走 slot region 而非能力池）：

```python
# 宿主已声明 region "workbench:tab"（kind=badge），插件挂载条目：
UIPluginRegistry.get_instance().register_slot_entry(
    "workbench:tab",
    "my-badge",                       # entry_id：region 内唯一
    "my-plugin",
    priority=0,
    payload="3",                      # 三形态：str 文本 / QWidget 实例 / 无参工厂
    metadata={"tab_id": "git-panel", "tooltip": "3 条未读"},
)
# tab_id 必填 = register_workbench_tab 的 page_id；同 tab 多条目 priority 高者生效；
# 插件卸载时条目自动清理并通知面板重挂
```

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
| `team.read` | low | 团队成员 / 角色 / 运行状态（只读门面，写操作不可见） |
| `team.send` | medium | 向团队成员派发任务邮件（写对方邮箱） |
| `team.session` | **high** | 团队历史会话恢复 / 归档 / 进入成员会话（跨窗口开标签页） |

### 存储

| ID | level | 说明 |
|---|---|---|
| `storage.data_dir` | low | 插件持久化数据根目录 `<app_data>`（插件自拼 `plugin_data/<name>/` 自用） |

只给根目录：CapabilityPool 的 provider 全局共享、调用不传插件名，per-plugin
目录隔离没有现成机制。目录约定与 E1 配置契约（`plugin_data/<name>/config.json`）
一致。

### 基础（恒放行）

| ID | level | 说明 |
|---|---|---|
| `state.kv` | none | 窗口级 KV 存储（插件自己的沙箱，不触达宿主状态） |
| `plugin.log` | none | 带 `plugin=<name>` 前缀的 logger（日志可归因） |

这两个不参与声明裁剪。

### 进程级 / 配置

| ID | level | 说明 |
|---|---|---|
| `cache.purge` | low | 清理主程序进程内 LRU 缓存（HTML 渲染 / token 估算 / grep 编译） |
| `config.read` | medium | 按 key 读主程序配置项（非敏感标量） |
| `process.start` | **high** | 启动后台进程（argv 数组、无 shell；宿主退出即全杀） |

`process.start` 启动的进程**绑定宿主存续期**（2026-10 定稿）：每个进程入专属
kill-on-close Job（Windows），宿主退出 / 崩溃 / 被杀时内核自动树杀，不会残留
孤儿进程；插件被禁用或卸载时，主程序立即回收该插件名下全部进程（禁用/卸载前
杀，防插件目录文件占用）。管控面：

- **首启确认**：每插件会话内首次启动弹窗（展示插件名与命令），用户同意后本
  会话内不再询问；拒绝后本会话内该插件一律拒绝（防骚扰式重试），重启主程序
  可重置授权。无 GUI（headless/测试）跳过确认；
- **限额**：单插件最多 8 个后台进程，全局最多 32 个，超限直接拒绝；
- **env 白名单**：子进程只继承宿主环境的白名单子集（PATH/TEMP/系统路径等，
  见 `process_registry.ENV_WHITELIST`）+ 插件 `env` 参数显式增量；宿主密钥类
  环境变量（API key 等）不泄漏给插件启动的进程。

只收 argv 数组（字符串直接 TypeError，杜绝 shell 注入面）；stdout/stderr 丢弃。
要输出回传与审批的一次性命令走工具插件的 command 工具，别用这个：

```python
from app.plugins.sdk import caps_for

start = caps_for("process.start")
info = start(["magpie", "web", "--no-open"],
             env={"MAGPIE_WEB_KEY": "some-key"}, name="magpie-web")
# info == {"pid": 12345, "name": "magpie-web"}
```

`config.read` 刻意**不**返回整个 `Settings` 对象——那等于把 `model.config` 才该给的
明文密钥一起交出去。按 key 取，且密钥类字段（key / token / secret / password /
credential）一律拒绝并记 warning：

```python
from app.plugins.sdk import caps_for

read = caps_for("config.read")
hosts = read("marketplace_allowed_git_hosts", default=[])
assert read("api_key", default="X") == "X"  # 密钥类字段被拒，回落 default
```

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

**宿主单例的只读面用 facet 声明**，别在两个 surface 各手写一份字典
（`app/plugins/capabilities/facets.py`）：

```python
FACETS["mycap.read"] = FacetSpec(
    target="app.core.foo.manager:FooManager",   # 模块路径:类名，取 get_instance()
    methods=("get_a", "get_b"),                 # 白名单即能力边界
)


def _mycap_read(host: Any):
    return build_facet("mycap.read")            # 返回 () -> ReadOnlyFacade
```

白名单外的属性访问得到 `AttributeError`（写操作天然隔离）；`tests/plugins/test_capability_provider_contract.py`
遍历 `FACETS` 断言每个名字在目标类上真实存在，typo 在 CI 就炸——手写字典做不到这一点。

**3. 单测**（`tests/plugins/test_capability_*.py`）

**4. 文档**（本节表格）

### 语义动作，不是服务搬家

判据：provider 实现里若只有一行 `return host._x`，它是**搬家不是抽象**，应合并或重设边界。

反例：`workdir.write` 若只是 `return host.backend.tool_executor.set_workdir`，那插件还是要自己处理实例缓存与团队广播。正例是内部一次做完四步，插件从此不知道这些字段存在。

---

## 5b. sdk 是稳定契约面，不是内部实现泄露

插件只能依赖 `app.plugins.sdk` 与 `ctx["capabilities"]`。直连 `app.core.xxx` /
`app.widgets.xxx` 才是真正的不自包含——插件 import 了主程序内部模块的私有实现，
主程序一重构插件就炸，而且没有地方能在破坏前拦住。

sdk 反过来：它是显式契约面，符号分两级（`app/plugins/sdk.py::sdk_stability`）：

| 级别 | 含义 | 破坏前要求 |
|---|---|---|
| `stable` | 主程序承诺不改签名 / 语义 | 必须在 CHANGELOG 记破坏性变更 |
| `provisional` | 临时收录，签名可能随内部实现调整 | 不承诺，插件别写进长期逻辑 |

自检：`python tools/check_sdk_imports.py` 静态核对改动文件的 sdk import 全部命中；
`tests/plugins/test_sdk_stability_contract.py` 断言每个收录符号真能 import 到、
且常用契约在 stable 面。

**没收录怎么办**：把符号加进 `app/plugins/sdk.py` 的 `_SYMBOLS`（同时决定 stable /
provisional），不要图省事直连内部模块。收录判定三条：无宿主运行时依赖、是常量 /
枚举 / dataclass / 纯函数 / 可继承基类、至少一个插件在用。

---

## 6. 排障

**`CapabilityDenied`**
按消息提示在 `plugin.json` 的 `capabilities` 里加 ID 后重载插件。

**声明了但拿不到**
检查加载期日志：typo（如 `session.Read`）、已删除能力或不存在的组通配会在插件加载时直接产出 `[CapabilityManifest] 未知能力 ID` warning（manifest_warnings，随 PluginInfo 可见），无需等运行期。也可用 `caps.names()` 打印实际放行的清单对照。

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
调用方文件路径推断（`plugins/<name>/tools/` 与 `hooks/` → tool surface，
其余 → ui）。

**hook 里的 surface 口径**：hook 跑在引擎事件上下文，无窗口，host 语义与
tool 池（host=None）一致，因此 `hooks/` 调用点推断为 tool surface，
`mcp.call` / `lsp.query` / `net.upload` / `subagent.run` 这类 tool-only
能力在 hook 内可直接 `caps_for` 取到。代价：ui-only 能力（`ui.notify` /
`input.write` 等）在 hook 内拿不到——hook 确实需要它们时显式传
`caps_for("ui.notify", surface="ui")`。

排查用：`declared_for()` 返回该插件已解析的能力清单。

## 8. 检查工具

```bash
# 报告插件越界 import（默认只告警）
python tools/plugin_import_guard.py

# CI：有违规即失败
python tools/plugin_import_guard.py --strict

# 审计差集报告：实际使用 vs 声明（兼容期插件给出建议清单）
python tools/capability_report.py
python tools/capability_report.py --since 2026-10-23   # 只看指定日期后，排除升级前历史
python tools/capability_report.py --json              # 机器可读
```

报告四类差集：兼容期插件的建议声明清单（实际使用 - 恒放行项，可直接照抄进
plugin.json）、已声明但从未使用（可从清单删）、用到声明外能力（异常，出现
即说明存在绕过路径）、本地未安装但日志有使用的孤儿。日志跨插件升级前后时
务必加 `--since`：兼容期（全量放行）时期的使用记录会污染差集判定。

合法面：`app.plugins.contracts` / `app.plugins.registries` / `app.plugins.managers.plugin_config_store` / `app.plugins.sdk` / `app.plugins.capabilities` / `app.plugins.deps_loader` / `app.plugins.managers.plugin_manager`。
