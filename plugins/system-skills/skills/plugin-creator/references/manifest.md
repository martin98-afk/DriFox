---
description: plugin.json 字段定义、校验规则、E1 配置契约（config_schema）与完整示例
---

# Plugin Manifest 参考

> `plugin.json` 是插件的「身份证」，DriFox 识别与加载插件的唯一入口。

---

## 位置与命名

```
<plugin-name>/.drifox-plugin/plugin.json
```

路径**必须**是 `<plugin-name>/.drifox-plugin/plugin.json`，不能换名。

---

## Schema 校验

所有合法 manifest 必须通过 [schemas/plugin.schema.json](https://github.com/martin98-afk/drifox-plugins/blob/main/schemas/plugin.schema.json) 校验：

```bash
# 在 drifox-plugins 的本地 clone 中执行
python tools/validate_plugins.py
```

> 没有本地 clone？先 `git clone https://github.com/martin98-afk/drifox-plugins.git`

---

## 字段总表

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 插件名，必须与目录名一致，小写 kebab-case `^[a-z][a-z0-9-]{1,63}$` |
| `description` | string | 一句话说明（≤200 字） |
| `version` | string | SemVer 2.0，如 `"1.0.0"` |
| `components` | object | 启用的组件清单（至少启用一个） |

### 选填字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `author` | string \| object | 无 | 作者，string 或 `{"name":"x","email":"x@y.z","url":"..."}` |
| `homepage` | string (URI) | 无 | 插件主页 |
| `repository` | string (URI) | 无 | 源码仓库 |
| `license` | string | 无强制默认 | SPDX 标识符（现有用户插件 GPL-3.0-or-later 与 MIT 均有） |
| `type` | enum | `"user"` | `"user"`（用户插件）\| `"system"`（系统级，需签名） |
| `keywords` | string[] | 无 | 检索关键词 |
| `drifox` | object | 无 | DriFox 兼容性声明 |
| `dependencies` | object | 无 | 插件间依赖 |
| `icon` | object | 无 | `{"light": "icon.svg", "dark": "icon_dark.svg"}` |
| `config_schema` | object | 无 | E1 配置契约，见下节 |
| `api_version` | int | `1` | 插件 API 契约版本。`>= 2` 时 `capabilities` 缺失按空清单处理（从严） |
| `capabilities` | string[] | 无 | 能力池声明，见下节 |

### `components` 子字段

```json
"components": {
  "commands": true,
  "agents": true,
  "skills": true,
  "themes": true,
  "hooks": true,
  "mcp": true,
  "lsp": true,
  "ui": true,
  "tools": true,
  "providers": true,
  "team_templates": true
}
```

每个 `true` 的 flag 必须有对应目录/文件（否则 `validate_plugins.py` 报错）。

### `drifox` 兼容性

```json
"drifox": {
  "min_version": "0.5.0",
  "max_version": "1.x",
  "events": ["SessionStart", "PostToolUse"]
}
```

### `dependencies` 依赖

```json
"dependencies": {
  "evolver": ">=1.0.0",
  "code-review": "^2.1.0"
}
```

---

## 能力声明（capabilities + api_version）

新插件一律写 `"api_version": 2` 并显式声明清单，运行时经 `ctx["capabilities"]` 取用；无 ctx 的场景（模块级初始化等）统一走 `from app.plugins.sdk import caps_for`，不要直连主程序内部模块。

```json
{
  "api_version": 2,
  "capabilities": ["session.read", "input.read", "ui.notify", "state.kv", "config.read"]
}
```

**宿主门槛硬闸**：manifest 声明值高于 `app/plugins/version_gate.py` 的 `HOST_PLUGIN_API_VERSION`（当前 **2**）→ 整个插件拒载（load_blocked，工具/UI/hooks 全不加载），日志一行 `[VersionGate] 插件 'x' api_version=N 高于宿主支持的 M，拒载`。回归守卫 `tests/core/test_api_version_contract.py` 扫全仓 manifest，声明超标即测试失败。宿主升版前别抢先声明更高值。

裁剪规则：

| 状态 | 行为 |
|---|---|
| 有 `capabilities` | 按清单裁剪（支持 `"tool.*"` 与 `"*"`） |
| `"capabilities": []` | 只剩 `state.kv` / `plugin.log` |
| 无该字段且 `api_version <= 1` | 兼容期全量放行 + warning |
| 无该字段且 `api_version >= 2` | 等同 `[]` |

⚠ **静默失效陷阱**：`api_version >= 2` 但漏写某项能力 → 该能力直接消失，无
报错、无 warning（只有「未声明则全量放行」那一条 warning 会消失）。**每次往
manifest 加 `api_version: 2`，必须同时把用到的能力全部列上。** 排查手法：
`plugins.log` 搜 `[CapabilityAudit] plugin=<name>`，看是「全量注入」还是按清单。

**加载期校验**：声明了 CATALOG 不存在的 ID（typo、已删除能力）→ 插件加载即产
`[CapabilityManifest] 未知能力 ID` warning（manifest_warnings，随 PluginInfo
可见），不必等运行期撞 `CapabilityDenied`。

**差集审计**（在 DriFox 主仓库跑，读本机日志）：`python tools/capability_report.py`
（`--since 日期` 排除兼容期全量放行时期的历史使用污染，`--json` 机器可读）报
四类差集：兼容期插件的建议声明清单（可直接照抄进 manifest）/ 已声明但从未用
（可从清单删）/ 用到声明外能力（异常 = 存在绕过路径）/ 孤儿插件。升
`api_version: 2` 前跑一次校准清单。

### 能力目录全集

唯一真相是主程序 `app/plugins/capabilities/catalog.py`（`CATALOG`）。快照
（新增能力以主程序为准，本表可能滞后）：

| 能力 ID | 说明 | surfaces |
|---|---|---|
| `model.metadata` | 当前模型 provider/名称/上下文/能力元数据 | ui, tool |
| `model.schema` | 指定 agent 视角的工具 schema | ui, tool |
| `model.config` | **完整模型配置（含已解密明文 API key）** | ui, tool |
| `provider.list` / `model.enumerate` | 服务商清单（**不含密钥与 URL**）/ 服务商可选模型列表（含视觉/思考标记） | ui, tool |
| `session.read` / `session.write` | 会话读写（write 写库 + 刷 UI + 发事件） | ui, tool |
| `session.list` | 窗口内存会话列表（`host.session_manager.sessions`） | ui |
| `session.history` / `session.history.write` | **持久化**历史会话只读面 / 改写（置顶·移动·归档·恢复·删除·导入） | ui |
| `session.manage` / `session.invalidate` | 切换·新建当前会话 / 失效 system_prompt 缓存 | ui |
| `input.read` | 输入框文本与附件列表 | **ui** |
| `input.write` | 写入输入框文本 | **ui** |
| `agent.read` | agent 列表/提示词/管理器 | ui, tool |
| `subagent.run` | 派生子智能体 | tool |
| `workdir.read` / `workdir.write` | 工作目录读 / 切换 | ui（read 含 tool） |
| `project.read` / `project.write` | 项目读（当前/清单/元数据/根目录）/ 写（切换·新建·归档·导入导出·拖拽） | ui |
| `project.workdir` | **写库式**切换项目工作目录（关键文档登记 + 持久化 + 根目录标记恢复）；与 `workdir.write`（会话内）互补 | ui |
| `theme.read` | 主题 id/名称/配色/字体/字号 | ui |
| `tool.read` / `tool.execute` | 工具元信息 / 执行任意工具 | ui, tool |
| `tool.inject` | 向 LLM 注入工具（改变模型可见行为） | **ui** |
| `engine.session` / `engine.stack` | 创建引擎会话（消耗 token）/ 执行栈工厂 | ui |
| `platform.send` / `platform.read` | 通讯平台收发 | ui |
| `mcp.call` / `lsp.query` / `net.upload` | MCP 调用 / LSP 查询 / Gitee 上传 | tool |
| `ui.notify` `ui.card.show` `ui.card.hide` | 弹 InfoBar / 显示浮动卡 / 隐藏浮动卡 | ui |
| `ui.artifacts.show` | 向消息卡片成果栏推成果条目（image/video/html/text/file）；工具侧常规通道是 `ToolResult.artifacts` 免声明字段 | ui, tool |
| `ui.exclusive` `ui.dialog` `ui.events` | 独占 UI 模式 / 文件与确认对话框 / UI 事件总线 | ui |
| `stream.subscribe` | 订阅对话流事件（工具/流/错误/上下文/会话切换） | ui |
| `window.active` `window.current` `window.host` | 活跃窗口 / 窗口语义快照 / 宿主顶层窗口 | ui |
| `window.list` `window.events` `window.workbench` | 窗口 id 集 / tab 切换事件 / 右侧工作台页 | ui |
| `plugin.read` / `plugin.control` | 已安装插件列表 / 重载启停其他插件 | ui |
| `command.read` / `command.reload` | 命令列表 / 命令热重载 | ui |
| `team.read` / `team.send` | 团队成员与运行状态 / 派发任务邮件（send 仅 tool） | ui, tool / **tool** |
| `team.session` | 团队历史会话恢复 / 归档 / 进入成员会话（跨窗口开标签页） | ui |
| `cache.purge` | 清理主程序进程内 LRU 缓存 | ui |
| `process.start` | 启动后台进程（argv 数组、无 shell，独立于宿主生命周期） | ui, tool |
| `config.read` | 按 key 读主程序配置项（**不含密钥**，密钥走 `model.config`） | ui |
| `host.raw` | 裸 `main_widget` / `backend` 对象引用（逃生舱） | ui |
| `plugin.log` / `state.kv` | 带前缀的 logger / 窗口级 KV（**恒放行，无需声明**） | - |

支持 `"tool.*"` 与 `"*"` 通配；`state.kv`、`plugin.log` 是 UNGATED，不声明也有。

### 取能力的两条路

| 场景 | 入口 |
|---|---|
| 有 `ctx`（浮动卡 / 工作台页 / 输入按钮回调） | `ctx["capabilities"][cap_id]`，或 `ctx["services"][legacy_key]` |
| **无 ctx（独立弹窗 / 后台线程 / 模块级）** | `from app.plugins.sdk import caps_for; caps_for(cap_id)` |

⚠ **独立弹窗（`register_window`）拿不到 `ctx["services"]`**：`PluginWindow` 只
注入 `context_provider`，不注入 `services` / `capabilities`。弹窗内一律走
`caps_for(cap_id)`（host 由宿主解析：插件绑定窗口 → 活跃窗口）。实测
`caps_for("input.write")` 在弹窗内可取到。

⚠ `legacy_key` 只覆盖部分能力（见 catalog 的 `LEGACY_MAP`）。`input.write`、
`theme.read`、`ui.dialog` 等**没有 legacy 键**，`ctx["services"]` 里查不到，
必须走 `capabilities` 或 `caps_for`。

⚠ 依赖 host 的能力（`input.*`、`session.*`、`window.*`）在无窗口上下文里会
失败（如 `theme.read` 报 `'NoneType' object has no attribute ...`）。取到
`None` 要有兜底，不能假设一定可用。

能力清单（含 `config.read`：按 key 读主程序非敏感配置，密钥类字段拒读回落 default）、SDK 稳定性分级契约（`app/plugins/sdk.py` 是唯一稳定契约面，符号分 stable / provisional 两级；越界 import 自检 `python tools/check_sdk_imports.py`）、新增能力的四步流程见 [docs/plugins/capabilities.md](../../../../../docs/plugins/capabilities.md)。

---

## E1 配置契约（config_schema）

在 manifest 里声明 `config_schema`，主程序自动在设置页渲染配置卡片；用户保存后值写入 `PluginConfigStore`，插件代码随时读取。

### 结构

```json
"config_schema": {
    "title": "配置卡片标题",
    "fields": [ { "...字段属性见下表...": "..." } ]
}
```

### 字段属性（14 个）

| 属性 | 必填 | 说明 |
|------|------|------|
| `key` | ✅ | 字段键，插件读配置的 key，插件内唯一 |
| `label` | ✅ | 设置页展示名 |
| `type` | ✅ | 8 种类型之一（见下表） |
| `default` | 建议 | 默认值（三级链兜底；≤8KB） |
| `env` | 可选 | 环境变量名，取值优先级最高 |
| `placeholder` | 可选 | 输入框占位文本 |
| `description` | 可选 | 字段说明（≤2KB，卡片内展示） |
| `options` | select 必配 | 下拉选项（三形态见下） |
| `min` / `max` | number 可选 | 数值范围 |
| `step` | number 可选 | 步长 |
| `rows` | textarea 可选 | 行数 |
| `url` | link 必配 | 链接地址 |
| `action` | action 必配 | 工具编排配置（见下） |

### type 全集（8 种）

| type | 控件 | default 示例 |
|------|------|-------------|
| `text` | 单行文本 | `"wss://example.com/ws"` |
| `password` | 密文输入（不明文回显） | `""` |
| `bool` | 开关 | `false` |
| `select` | 下拉选择，配 `options` | `"auto"` |
| `number` | 数字输入（别名 `int`/`integer` 自动归一为 `number`） | `30` |
| `textarea` | 多行文本 | `"line1\nline2"` |
| `link` | 链接（打开浏览器，不存值场景） | `"https://..."` |
| `action` | 动作按钮（编排插件注册的工具，点击即调用） | 无 |

### select 的 options 三形态

```json
// 形态一：字符串数组（值=显示名）
"options": ["auto", "on", "off"]

// 形态二：对象数组（value/label 分离）
"options": [
    {"value": "auto", "label": "自动（推荐）"},
    {"value": "on",   "label": "强制开启"}
]

// 形态三：对象映射（key=value，label 为注释序）
"options": {"auto": "自动", "on": "开启", "off": "关闭"}
```

### action：工具编排

`type: "action"` 的字段在卡片上渲染为按钮，点击后调用插件注册的工具（tools 组件），用于「测试连接」「立即刷新」类动作：

```json
{
    "key": "test_connection",
    "label": "测试连接",
    "type": "action",
    "action": {
        "tool": "test_connection",
        "args": {"timeout": 10},
        "poll": {
            "interval_ms": 2000,
            "max_rounds": 30,
            "stop_when": {"data.status": ["done"]}
        },
        "image_width": 480
    }
}
```

约束：`poll.interval_ms` 必须 ≥1000；`stop_when` 是**对象**——键为结果寻址路径（如 `data.status`），值为命中集合（如 `["done"]`），写成字符串会被宽容解析丢弃，导致轮询永不提前终止；`ToolActionSpec` 无 `dialog_title/dialog_width/dialog_height` 字段，声明会被忽略。

### 取值三级链：env → 存储 → 默认

插件读取一个配置值时按此优先级：

```
① env 指定的环境变量（若非空）   ← 最高
② PluginConfigStore 存储值       ← 设置卡保存的用户值
③ config_schema 的 default       ← 兜底
```

### 存储路径与读取代码

存储位置：`<app_data>/plugin_data/<plugin>/config.json`（`<plugin>` = plugin.json 的 `name`；旧版散存位置由主程序自动迁移，无需手工处理）。

插件代码读取（tools/gateways 等任意组件通用）：

```python
from app.plugins.managers.plugin_config_store import PluginConfigStore

store = PluginConfigStore()
endpoint = store.get("my-plugin", "endpoint") or "wss://example.com/ws"  # ③ 默认兜底
secret = store.get("my-plugin", "api_secret") or ""
```

设置卡保存回调（gateway 组件完整链路见 `examples/config-schema-plugin/`）：

```python
PluginConfigStore().set_values("my-plugin", {"endpoint": endpoint, "api_secret": secret})
```

### 什么时候不要用 E1 自动卡

需要可编辑下拉、运行时选项、字段联动、行尾外链或「测试连接」按钮时，写自绘卡并注册 `register_settings_card(..., priority=1)` 覆盖自动卡。布局口径、控件选型、折叠高度、连通性测试范式与审查清单见 [settings-card.md](settings-card.md)。

> 覆盖后 `config_schema` 里的 `link` / `action` 字段不再显示，自绘卡必须自己补回这些入口。

---

## 完整示例（含 config_schema）

```json
{
    "name": "my-plugin",
    "description": "一句话描述插件功能",
    "version": "0.1.0",
    "author": {
        "name": "Your Name",
        "email": "your@email.com"
    },
    "homepage": "https://github.com/your/repo",
    "license": "GPL-3.0-or-later",
    "type": "user",
    "icon": {"light": "icon.svg", "dark": "icon_dark.svg"},
    "keywords": ["drifox", "my-plugin", "example"],
    "components": {
        "commands": true,
        "providers": true,
        "team_templates": true,
        "ui": true,
        "tools": true
    },
    "config_schema": {
        "title": "我的插件配置",
        "fields": [
            {"key": "enabled", "label": "启用", "type": "bool", "default": false},
            {"key": "endpoint", "label": "接入点", "type": "text",
             "default": "wss://example.com/ws", "placeholder": "wss://..."},
            {"key": "api_secret", "label": "Secret", "type": "password", "default": ""},
            {"key": "interval", "label": "刷新间隔（秒）", "type": "number",
             "default": 30, "description": "0 表示不自动刷新"},
            {"key": "mode", "label": "模式", "type": "select", "default": "auto",
             "options": [
                 {"value": "auto", "label": "自动"},
                 {"value": "manual", "label": "手动"}
             ]}
        ]
    },
    "drifox": {
        "min_version": "0.5.0"
    }
}
```

---

## 校验规则速查

| 规则 | 说明 |
|------|------|
| `name` | 必须 `^[a-z][a-z0-9-]{1,63}$`，与目录名一致 |
| `version` | 必须符合 SemVer `^\d+\.\d+\.\d+(-[a-z0-9.-]+)?$`；每次修改后更新 |
| `components.*` 启用 | 对应目录/文件必须存在 |
| `dependencies.*` | 被引用的插件也必须存在 |
| `description` | ≤200 字 |
| `config_schema.fields[].key` | 插件内唯一；存储 key 与插件 `name` 绑定 |
| `config_schema.fields[].type` | 8 种之一；`int`/`integer` 别名归一为 `number` |
| `config_schema` 资源上限 | fields ≤50；单个 default ≤8KB；description ≤2KB |
| JSON Schema | 必须通过 `schemas/plugin.schema.json` |

---

## 版本演进策略

```jsonc
// 开发阶段：0.1.x
"version": "0.1.0"
// 首次发布：1.0.0
// 破坏性变更 → 升 major；新增功能 → 升 minor；Bug 修复 → 升 patch
```
