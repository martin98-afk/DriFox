# config-schema-example — 带 config_schema 的插件示例

## 这是什么

展示 DriFox **配置契约（E1）**的完整链路：`plugin.json` 里的 `config_schema` 被主程序渲染成设置卡，用户保存后经回调写入 `PluginConfigStore`，插件运行时再从存储读回。

`config_schema` 覆盖全部四类字段：

| type | 展示 | 本例 key |
|---|---|---|
| `bool` | 开关 | `enabled` |
| `text` | 单行输入（可带 `placeholder`/`description`） | `endpoint` |
| `password` | 密文输入框 | `api_secret` |
| `number` | 数字输入 | `reconnect_interval` |

参照自真实插件 `gateway-qq`（配置回调链路）与 `workflow`（number 字段、工具侧读取）。

## 何时参照

- 插件需要**用户可配置项**（设置里出现你的配置卡片）
- 需要**密文字段**（token/secret 用 `password` 类型，不明文回显）
- 忘记 `config_builder` / `config_writer` / `build_config_values` / `validate_config` 四个回调怎么接
- 想知道插件代码里怎么读配置（`PluginConfigStore().get(plugin_name, key)`）

## 对应 SKILL.md 章节

- §2.2 11 类组件速查（gateways 组件）
- §6 测试与验证
- §8 常见陷阱 — Manifest 命名不一致

## 如何使用

1. 复制本目录到 `~/.drifox/plugins/config-schema-example/`
2. 搜索 `[改名]` / `[必改]` 标记：平台标识、Adapter、存储 key 与 plugin.json `name` 对齐
3. 重启后在设置里能看到「示例网关配置」卡片；改动字段并保存
4. 纯工具插件想加配置：把本例的 `config_schema` 与读取代码移植过去即可（tools 组件同样支持 config_schema，见 workflow 插件）

> 注意：存储 key 必须与 plugin.json 的 `name` 一致，否则设置卡保存的值插件读不到。
