# 服务商插件（providers）开发指南

服务商支持已全面插件化（万物为插件理念）：服务商的一切——**图标、API URL、
默认参数、模型列表、models.dev 白名单、family 能力、用量查询额外配置字段、
余额查询、套餐用量查询**——全部由 providers 插件声明，主程序不再硬编码任何
服务商数据。

## 1. 目录约定（与 tools 插件完全对称）

```
plugins/<name>/
├── providers/
│   ├── deepseek.py          # 一个文件注册一个（或多个）服务商
│   ├── icons/               # 深色主题图标（可选，<icon>.svg / <icon>.png）
│   ├── icons_light/         # 浅色主题图标（可选；缺省回退深色）
│   └── ...
├── .drifox-plugin/
│   └── plugin.json          # components 声明 "providers": true（自动检测，可选）
```

- 系统内置服务商：`plugins/system/providers/*.py`
- 用户插件：`<app_data>/plugins/<name>/providers/*.py`
- 热重载：ProviderWatcher 后台轮询（path, mtime, size），变更全量重扫；
  user 插件可覆盖 system 同名服务商

## 2. 文件结构

每个文件暴露 `register(registry)` 入口，内部调用
`registry.register(ProviderDef(...))`：

```python
# plugins/system/providers/deepseek.py
from app.plugins.registries.provider_registry import (
    ProviderDef,
    make_bearer_balance_fetcher,
)


def register(registry):
    registry.register(
        ProviderDef(
            name="DeepSeek",                    # 服务商唯一名
            icon="deepseek",                    # 图标 key（icons/ 目录文件名或 qrc）
            api_url="https://api.deepseek.com",
            auth_type="bearer",                 # bearer / bce / none / anthropic
            default_model="deepseek-chat",
            default_params={"温度": 0.7, "最大Token": 200000, "思考等级": "high"},
            register_url="https://platform.deepseek.com/api_keys",
            models=["deepseek-v4-flash", "deepseek-v4-pro"],
            models_dev_id="deepseek",           # models.dev provider id（可选）
            family="deepseek",                  # 能力族（detect 探测同 key）
            capabilities={                      # family 能力（可覆盖默认）
                "context_limit": 320000,
                "supports_thinking": True,
                "thinking_param": "thinking",
            },
            extra_quota_fields=[                # 用量查询额外配置（可选）
                QuotaField(key="server_id", label="Server ID:", placeholder="..."),
            ],
            balance_fetcher=make_bearer_balance_fetcher(   # 余额查询（可选）
                url="https://api.deepseek.com/user/balance",
                balance_key="total_balance",
                currency="¥",
            ),
            coding_plan_fetcher=_fetch_coding_plan,        # 套餐用量查询（可选）
        )
    )
```

## 3. ProviderDef 字段

| 字段 | 说明 | 对应旧硬编码 |
|---|---|---|
| `name` | 服务商唯一名 | FREE_PROVIDERS key |
| `icon` | 图标 key（plugins/providers/icons/ 文件名或 qrc） | PROVIDER_ICONS 值 |
| `icon_dir` / `icon_dir_light` | 插件图标目录（**自动注入**，勿手写） | — |
| `api_url` | 默认 API URL | FREE_PROVIDERS.API_URL |
| `auth_type` | 认证方式 bearer/bce/none/anthropic | FREE_PROVIDERS.认证方式 |
| `default_model` | 默认模型名 | FREE_PROVIDERS.模型名称 |
| `default_params` | 其他默认参数（温度/最大Token/思考模式…） | FREE_PROVIDERS 其余键 |
| `register_url` | 获取 API Key 地址 | FREE_PROVIDERS.获取地址 |
| `models` | 模型列表 | PROVIDER_MODELS |
| `models_dev_id` | models.dev provider id | MODELS_DEV_PROVIDER_MAP |
| `family` | 能力族 | detect_provider_family 返回值 |
| `capabilities` | family 能力 | PROVIDER_CAPABILITIES |
| `extra_quota_fields` | 用量查询额外字段（不进模型参数/API 请求） | QUOTA_EXCLUDE_KEYS + edit_card 硬编码 |
| `balance_fetcher` | 余额查询函数 | BALANCE_APIS |
| `coding_plan_fetcher` | 套餐用量查询函数 | coding_plan_fetcher 注册表 |

## 4. 查询函数签名

**余额 fetcher**：`(config: dict) -> dict | None`
```python
{"balance": 123.4, "currency": "¥"}     # 成功
{"hide": True, "tooltip": "失败原因"}   # 失败/无余额
None                                    # 无 API key 等（不请求）
```
简单 Bearer GET 场景直接用工厂：
`make_bearer_balance_fetcher(url, balance_key, currency="¥")`
（自动处理 `balance_infos[0][key]` / `data.data[key]` / `data[key]` 层级）。

**套餐用量 fetcher**：`(config: dict) -> dict | None`
```python
{"rolling": {"percent": 60, "reset_sec": 123}, "weekly": ..., "monthly": ...}
```
返回 None 表示该服务商暂不支持用量查询。

## 5. 配额字段与 QUOTA_EXCLUDE_KEYS

`extra_quota_fields` 声明的 key 会：
1. 汇入 `ProviderRegistry.quota_exclude_keys()`（全局聚合），这些字段
   **不会**被当作模型参数发送到 API（chat_worker/subagent_worker/model_config_card
   均按该集合排除）。
2. 在服务商编辑卡片「套餐用量查询（可选）」区动态渲染（label+placeholder）。

## 6. 既有硬编码迁移对照

| 旧常量 | 新查询入口 |
|---|---|
| `FREE_PROVIDERS` | `ProviderRegistry.default_config(name)` / `app.constants.provider_default_config` |
| `PROVIDER_ICONS` | `ProviderRegistry.icon_map()` / `get_provider_icon(name)`（渲染） |
| `PROVIDER_MODELS` | `ProviderRegistry.provider_models()` |
| `get_merged_provider_models()` | `constants.get_merged_provider_models()`（委托注册表） |
| `MODELS_DEV_PROVIDER_MAP` | `ProviderRegistry.models_dev_map()` |
| `PROVIDER_CAPABILITIES` | `ProviderRegistry.family_capabilities(family)` |
| `QUOTA_EXCLUDE_KEYS` | `ProviderRegistry.quota_exclude_keys()` / `constants.provider_quota_exclude_keys()` |
| `BALANCE_APIS` | `ProviderRegistry.balance_fetch(name, config)` |
| `coding_plan_fetcher` 注册表 | `ProviderRegistry.coding_plan_fetch(name, config)` |

## 7. 测试

```bash
python -m pytest tests/core/test_provider_registry.py -v
```