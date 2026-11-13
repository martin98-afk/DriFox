# Gateway 平台插件开发指南

> 适用对象：第三方平台（Teams / Line / WhatsApp Business / 自研 IM 等）开发者。
> 主程序 6 内置平台（wecom / dingtalk / telegram / discord / feishu / slack）已迁至
> 社区仓 [`drifox-plugins2`](https://github.com/martin98-afk/drifox-plugins2)
> 的 `plugins/gateway-<id>/`，本指南同样适用——只是入口仓库不同。

---

## 1. 插件结构

第三方平台插件目录长这样（社区仓 `drifox-plugins2/plugins/gateway-teams/`
或 user 根 `~/.drifox/plugins/gateway-teams/`）：

```
gateway-teams/
├── .drifox-plugin/
│   └── plugin.json        # 必填：name / version / components.gateways
├── gateways/              # 必填：运行时组件目录
│   └── teams.py           # 暴露 register(registry)，注册 GatewayPlatformDef
├── deps/                  # 可选：vendor SDK（如 aiohttp / websockets）
│   ├── teams_sdk/
│   └── teams_sdk-1.2.3.dist-info/
├── icon.svg               # 可选：设置卡图标（深色主题）
├── icon_dark.svg          # 可选：浅色主题
└── README.md
```

**`plugin.json` 最小模板**：

```json
{
  "name": "gateway-teams",
  "version": "1.0.0",
  "type": "user",
  "components": {
    "gateways": true
  },
  "config_schema": {
    "title": "Microsoft Teams 配置",
    "fields": [
      {"key": "tenant_id",     "label": "Tenant ID",   "type": "text",     "default": "", "env": "TEAMS_TENANT_ID"},
      {"key": "bot_app_id",    "label": "Bot App ID",  "type": "text",     "default": ""},
      {"key": "bot_app_secret","label": "App Secret",  "type": "password", "default": ""},
      {"key": "enabled",       "label": "启用 Teams 网关", "type": "bool","default": false}
    ]
  }
}
```

主程序（`PluginManager`）扫描 `gateways/` 目录（`runtime_component_loader` 硬编码
组件名，**`components.gateways` 字段非必需**——目录存在即探到，声明仅为自描述）；
看到 `config_schema` 即自动注册 `PluginConfigRegistry` + 渲染 `PluginConfigCard` 设置面板。

### config_schema 字段类型（通用契约，所有插件共用）

| type | 控件 | 附加属性 |
|---|---|---|
| `text` | 单行输入 | `placeholder` |
| `password` | 密码输入 | `placeholder` |
| `bool` | 开关 | — |
| `select` | 下拉选择 | **必填** `options`（见下） |
| `number` | 整数输入 | `min` / `max` / `step`（默认 0 / 2^31-1 / 1） |
| `textarea` | 多行文本 | `rows`（显示行数，默认 3）、`placeholder` |
| `link` | 外链按钮 | **必填** `url`（强制 http/https，纯展示不存储） |
| `action` | 声明式工具调用按钮 | **必填** `action`（ToolActionSpec：tool + args + 可选 poll，见 README §4.1） |

`select` 的 `options` 三种声明形态（value 为存储值，label 为显示名）：

```json
"options": {"a": "模式A", "b": "模式B"}            // dict: value → label
"options": ["a", "b"]                              // list[str]: value=label
"options": [{"value": "a", "label": "模式A"}]      // list[dict]
```

---

## 2. `GatewayPlatformDef` 字段一览

`app/plugins/contracts/gateway_platform.py` 定义的 frozen dataclass：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `platform_id` | `str` | ✅ | 平台唯一 ID（任意字符串，建议 `kebab-case`，与内置平台不冲突） |
| `display_name` | `str` | ✅ | **仅作日志 tag**（`[PlatformManager] xxx adapter loaded`）；设置卡标题走 `config_schema.title` |
| `adapter_factory` | `() -> BasePlatformAdapter` | ✅ | 适配器工厂：无参调用，适配器在 `__init__` 里自己读插件配置 |
| `check_requirements` | `() -> bool` | ⬜ | 缺省 `lambda: True`；SDK 缺失时返回 False 让 manager 跳过 |
| `is_enabled` | `() -> bool` | ⬜ | 缺省 False。`start_all` / `get_status` 据此决定该平台是否参与连接 |
| `set_enabled` | `(bool) -> None` | ⬜ | 写回启用状态（设置卡开关、`platform.control` 能力调它）；缺省 None 表示只读 |
| `validate` | `() -> (bool, str)` | ⬜ | **adapter 加载/重建前**校验配置可用性（manager 启动装配与配置更新重建分支），非保存前校验 |
| `ui_order` | `int` | ⬜ | 仅影响 `GatewayPlatformRegistry.list_platforms()` 排序（缺省 100）；**不影响设置卡顺序** |
| `icon_hint` | `str` | ⬜ | **保留字段，当前无消费方**。图标按插件根 `icon.svg`/`icon_dark.svg` 文件约定自动选，无字段参与 |
| `source` | `str` | ⬜ | 注册来源（loader 自动填 `plugin:<name>`，手写注册可省略） |

`source` 与 `id` 是注册表与 runtime loader 内部用，**插件开发者不需要手动设**。

---

## 3. 最小可工作模板

```python
# plugins/my-gateway/gateways/my_platform.py
PLUGIN_NAME = "my-gateway"

_CAPS: dict = {}


def _cap(cap_id: str):
    """取宿主能力（按 manifest capabilities 裁剪；后台线程无 ctx 也可用）。"""
    cap = _CAPS.get(cap_id)
    if cap is None:
        from app.plugins.sdk import caps_for

        cap = caps_for(cap_id, plugin=PLUGIN_NAME, surface="ui")
        if cap is None:
            return None
        _CAPS[cap_id] = cap
    return cap


def _config() -> dict:
    """读本插件配置（plugin.config.read 能力，一次取全量）。"""
    reader = _cap("plugin.config.read")
    values = (reader() if reader else {}) or {}
    return {"enabled": bool(values.get("enabled")), "token": values.get("token") or ""}


def _set_enabled(enabled: bool) -> None:
    writer = _cap("plugin.config.write")
    if writer is not None:
        writer("enabled", bool(enabled))


def _validate() -> tuple:
    return bool(_config()["token"]), "Token 未配置"


class MyAdapter(BasePlatformAdapter):
    platform_id = "my-platform"   # registry 键
    name = "My Platform"

    def __init__(self) -> None:
        super().__init__()
        self._token = _config()["token"]

    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    async def send(self, chat_id, content, **kwargs): ...
    async def get_chat_info(self, chat_id): ...


def register(registry) -> None:
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    registry.register(
        GatewayPlatformDef(
            platform_id="my-platform",
            display_name="My Platform",
            adapter_factory=MyAdapter,
            check_requirements=lambda: True,
            is_enabled=lambda: _config()["enabled"],
            set_enabled=_set_enabled,
            validate=_validate,
            ui_order=99,
        )
    )
```

manifest 至少声明 `components.gateways` 与 `capabilities: ["plugin.config.read", "plugin.config.write"]`，
配置字段写 `config_schema`（宿主渲染设置卡，插件自己也从同一份存储读）。

## 4. SDK deps 自包含指引

**背景**：DriFox 主程序打包（PyInstaller）排除所有可选平台 SDK（钉钉/Telegram/
Discord/飞书/Slack/Teams SDK 等），主程序体积可控。运行时平台 SDK 完全由插件
自提供——vendor 到 `<插件>/deps/`。

### 4.1 vendor 方法

```bash
# 推荐：用 pip download 只取 wheel，解压到 deps/
cd plugins/gateway-teams
mkdir deps && cd deps
pip download --no-deps --dest . teams-sdk==1.2.3
# 解压 *.whl / *.tar.gz 到当前目录（或写脚本自动化）
```

或直接 `pip install --target deps teams-sdk==1.2.3`（生成扁平目录）。

### 4.2 sys.path 注入

模块**顶层**放注入段（每个 `.py` 文件一份或放共享 `__init__.py`）：

```python
import os as _os, sys as _sys
_deps = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "deps"))
if _deps not in _sys.path:
    _sys.path.insert(0, _deps)
```

主程序**无全局注入机制**——每个插件自己在入口处注入（参考
`drifox-plugins2/plugins/gateway-dingtalk/gateways/dingtalk.py` 与
`drifox-plugins2/plugins/codegraph-tools/tools/codegraph.py` 同款模式）。

### 4.3 SDK 本体延迟导入（历史教训：2026-06-16）

**`gateways/<id>.py` 模块顶层绝不 `import teams_sdk`**。原因：
- 主程序任意位置 `from app.gateway.base import MessageEvent` 触发 `app.gateway`
  包初始化，若包内某适配器顶层 import SDK，SDK 缺失即导致整个 gateway 包加载失败。
- 第三方插件目录扫描在 `runtime_component_loader` 阶段逐文件 `exec_module`，
  顶层 import 异常即整个根扫描失败。

正确做法：

| 位置 | 允许 | 不允许 |
|---|---|---|
| 模块顶层 | `sys.path` 注入、标准库、主程序 API | SDK 顶层 import |
| 函数体 / 类方法 | SDK import（`check_requirements`、`__init__` 等） | — |
| `check_requirements()` | `try: import sdk; ...` 探测 | `import sdk` 无 try |

### 4.4 跨插件同名 SDK 冲突

两个插件同时 vendor 不同版本的同一 SDK 时，宿主**不再**允许「先注入者 wins」这种
静默结果。加载链路现在的处理（2026-11）：

1. 全部插件扫描合并后，`deps_loader.find_deps_conflicts` 对同一规范化包名的版本
   区间求交，不可调和即判冲突（`specs_conflict`，保守口径：读不懂的约束不算冲突）
2. 裁决优先级 `system > claude > user`，同类型按插件名字典序，保证每次启动结果一致
3. 让出方标记 `deps_conflict_reason` → `PluginInfo.load_blocked` 为真，其 deps 路径
   从 `sys.path` 摘除，tool/provider/runtime/UI 四类组件一律不加载
4. 插件市场「诊断报告」的异常项会列出被拦截插件与具体冲突包，启用插件前也会做同样
   检查，冲突直接拒绝并在 `last_error` 里说明

插件侧建议不变：同一生态内的 SDK 尽量收敛版本；确有版本分裂需求时走私有路径 +
`sys.modules` 别名，别指望宿主替你把两个版本同时跑起来（同进程内 C 扩展只能有一个）。

依赖版本判定也变了：`missing_pip_deps` 现在读 `deps/*.dist-info/METADATA` 的
`Version` 与声明范围比对，声明 `httpx>=0.28` 而 deps 里装着 0.20 会判缺失并触发
重装（重装前按 RECORD 精确清理旧分发，避免新旧文件共存）。

### 4.5 适配器断连纪律（历史教训：2026-08-20 连接泄漏）

**`disconnect()` 必须真正关闭连接/线程/loop，且幂等可重入**。反面教材：
gateway-feishu 曾靠 `hasattr(client, "stop"/"close"/"_running")` 探测 SDK 停止
方法——lark_oapi ws `Client` 三者皆无（`start()` 永久阻塞于
`run_until_complete(_select())`，ping/receive 均 `while True`，`auto_reconnect`
默认无限重连），三分支全部落空 → 连接/线程/loop 永生，每次 stop/热重载泄漏
一条长连接 + deps `.pyd` 句柄不释放（卸载失败）。

纪律清单（见 `gateway-feishu/gateways/feishu.py::disconnect` 范本）：

1. **禁 SDK 自动重连**（`client._auto_reconnect = False`），否则断连后 SDK 自己连回来
2. **经 SDK 线程自己的 loop** 调度断连（`run_coroutine_threadsafe(client._disconnect(), ws_loop)`），
   不能跨 loop 直接 await
3. **cancel loop 全部任务 + 停 loop**（`call_soon_threadsafe` 回调内 `task.cancel()` + `loop.stop()`），
   阻塞中的 `run_until_complete` 才会返回、线程才会退出
4. **join 线程带超时**（防 SDK 卡死拖垮 stop 协程），join 后再清引用
5. **connect 重入防御**：connect 开头若上一轮线程仍存活，先 `await self.disconnect()`
   再重连，防连接叠加
6. 主程序侧配合（已内置）：`PlatformManager.stop_plugin_platforms` 无条件调度 stop
   （不查 `is_connected`——泄漏实例 `_running=False` 但连接活着，前置检查会跳过清理）；
   热重载路径 `_reload_gateways` 以 `wait=True` 等 stop 完成再 purge/rebuild，防双连接竞态

回归测试：社区仓 `tests/test_gateway_feishu_lifecycle.py`（FakeWSClient 走真实
`_run_feishu_client` 线程协议，断言线程退出/loop 停止/幂等/防御清理）。

---

## 5. 新增平台三步

**场景**：为 Microsoft Teams 写第三方平台插件。

### 步骤 ①：建插件骨架

在社区仓 `drifox-plugins2/plugins/` 下新建 `gateway-teams/`：

```
gateway-teams/
├── .drifox-plugin/plugin.json   # 填 name/version/components.gateways/config_schema
├── gateways/teams.py            # 写 TeamsAdapter + register（参考第 3 节模板）
├── deps/                        # vendor teams-sdk 及其传递依赖
├── icon.svg / icon_dark.svg     # 可选
└── README.md                    # 平台说明 + 配置截图（可选）
```

### 步骤 ②：plugin.json 声明

最小声明见第 1 节模板；`config_schema` 列出所有用户输入字段（key/label/type/
default/env），主程序自动渲染设置卡 + `PluginConfigStore` 统一存储。

### 步骤 ③：测试 + 发布

- **本地调试**：把 `gateway-teams/` 软链/拷贝到 `~/.drifox/plugins/`，重启
  DriFox 或等待 `watchfiles` 热重载；进入「设置 → 网关」看到 Teams 卡片即注册成功。
- **E2E 测试模式**：`tests/plugins/gateways/test_e2e_third_party_platform.py`
  给出 monkeypatch `_plugin_roots` + 临时插件目录构造验收链（4 测试 PASSED）；
  写新平台时复用同款 fixture 即可。
- **发布**：社区仓 PR（参考 `gateway-dingtalk/` 的 README + plugin.json 结构）；
  主程序无需任何改动——`GatewayPlatformRegistry` 自动拾取新 def。

### 验证清单

- [ ] 临时 user 插件根扫到 `teams` def 且 `source == "plugin:gateway-teams"`
- [ ] `PlatformManager._load_adapters` 注入 `mgr._adapters["teams"]`
- [ ] 设置面板出现 Teams 卡片，字段与 `config_schema` 一致
- [ ] 写入 `bot_app_secret` → 重启 → `_config()["bot_app_secret"]` 读出同值
- [ ] SDK 缺失（`deps/` 删除）→ `check_requirements()` 返回 False → manager 跳过且日志记录

---

## 6. 参考实现

主程序契约层：

- `app/plugins/contracts/gateway_platform.py` — `GatewayPlatformDef` 定义
- `app/plugins/registries/gateway_platform_registry.py` — 进程级单例
- `plugins/system-im/core/manager.py::_load_adapters` — registry 唯一消费方
- `plugins/system-im/core/manager.py` — 启用/校验/装载全部走 def 回调（无配置结构）
- `app/plugins/contracts/plugin_config.py` — E1 config_schema 解析

社区仓参考实现：

- `drifox-plugins2/plugins/gateway-dingtalk/` — Stream SDK + vendor deps 完整示例
- `drifox-plugins2/plugins/gateway-telegram/` — polling/webhook 双模式示例
- `drifox-plugins2/plugins/gateway-feishu/` — Event 订阅 + 长连接示例
- `drifox-plugins2/plugins/codegraph-tools/tools/codegraph.py` — 同款 `deps/` 注入模式（工具插件）

测试参考：

- `tests/plugins/gateways/test_e2e_third_party_platform.py` — E2E 验收（monkeypatch + 临时根 + manager 拾取 + E1 配置链）
- `tests/plugins/gateways/test_gateway_component_plumbing.py` — loader/reloader/kernel 单元
- `tests/plugins/test_websearch_config_contract.py` — E1 config_schema 三级优先级样例