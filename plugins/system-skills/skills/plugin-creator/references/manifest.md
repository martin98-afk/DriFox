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
    "license": "MIT",
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
