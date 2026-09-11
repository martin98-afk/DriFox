---
description: Providers（服务商插件化）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Providers（服务商插件化）组件开发

> 服务商支持已全面插件化：**图标、API URL、默认参数、模型列表、models.dev 白名单、
> family 能力、用量查询额外配置、余额/套餐用量查询**全部由 providers 插件声明。

### 文件位置

```
<plugin>/
├── providers/
│   ├── deepseek.py       ← 每个文件暴露 register(registry)
│   └── icons/            ← 图标（icons_light/ 浅色可选）
└── .drifox-plugin/
    └── plugin.json       ← components.providers = true（可声明，loader 自动检测目录）
```

### 最小模板

```python
# providers/deepseek.py
from app.plugins.registries.provider_registry import (
    ProviderDef,
    make_bearer_balance_fetcher,
)


def register(registry):
    registry.register(
        ProviderDef(
            name="DeepSeek",                    # 服务商唯一名
            icon="deepseek",                    # 图标 key
            api_url="https://api.deepseek.com",
            auth_type="bearer",                 # bearer / bce / none / anthropic
            default_model="deepseek-chat",
            default_params={"温度": 0.7, "最大Token": 200000, "思考等级": "high"},
            register_url="https://platform.deepseek.com/api_keys",
            models=["deepseek-chat"],
            models_dev_id="deepseek",           # 可选
            family="deepseek",                  # 能力族（detect 探测同 key）
            capabilities={                      # family 能力（可覆盖默认）
                "context_limit": 128000,
                "supports_thinking": True,
                "thinking_param": "thinking",
            },
            extra_quota_fields=[                # 用量查询额外字段（可选，不进 API 请求）
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

### ProviderDef 字段

| 字段 | 说明 |
|------|------|
| `name` | 服务商唯一名 |
| `icon` | 图标 key（icons/ 文件名或 qrc） |
| `api_url` | 默认 API URL |
| `auth_type` | 认证方式 bearer/bce/none/anthropic |
| `default_model` | 默认模型名 |
| `default_params` | 温度/最大Token/思考模式等 |
| `register_url` | 获取 API Key 地址 |
| `models` | 模型列表 |
| `models_dev_id` | models.dev provider id |
| `family` | 能力族 |
| `capabilities` | family 能力（context_limit/supports_thinking/thinking_param 等） |
| `extra_quota_fields` | 用量查询额外字段（`QuotaField(key, label, placeholder)`，不进 API 请求） |
| `balance_fetcher` | 余额查询函数 |
| `coding_plan_fetcher` | 套餐用量查询函数 |

### 查询函数签名

- 余额 fetcher：`(config: dict) -> dict | None`
  `{"balance": 123.4, "currency": "¥"}`（成功）/ `{"hide": True, "tooltip": "原因"}`（失败）/ `None`（无 key 不请求）
  简单 Bearer GET 直接用工厂 `make_bearer_balance_fetcher(url, balance_key, currency="¥")`
- 套餐用量 fetcher：`(config: dict) -> dict | None`
  `{"rolling": {...}, "weekly": ..., "monthly": ...}`；返回 None 表示暂不支持

### 热插拔

- watcher 后台轮询（path, mtime, size）变更全量重扫（1-3 秒生效）
- 同名服务商：user 插件优先于 system 内置（覆盖是预期行为）

### 参考

- 完整开发指南：`plugins/system-providers/providers/README.md`
- 注册表实现：`app/plugins/registries/provider_registry.py`
- 测试：`python -m pytest tests/core/test_provider_registry.py -v`

---
