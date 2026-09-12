---
description: UI 插件声明式配置（config_schema）——插件要设置项 / API Key 配置时的正规姿势，设置页自动渲染配置卡，插件零 UI 代码
---

# 插件声明式配置（config_schema / E1）

> 插件需要「用户可配置项」（API Key、引擎选择、开关等）时，**不要手写配置 UI、不要自建存储文件**。
> 在 `plugin.json` 声明 `config_schema`，主程序自动完成：统一存储 + 设置面板渲染（PluginConfigCard）。

## 1. 声明（plugin.json）

```json
{
    "name": "voice-input",
    "version": "0.4.1",
    "config_schema": {
        "title": "语音听写配置",
        "fields": [
            {
                "key": "provider",
                "label": "识别引擎",
                "type": "select",
                "default": "auto",
                "options": [
                    {"value": "auto", "label": "自动（硅基流动优先）"},
                    {"value": "minimax", "label": "仅 MiniMax"}
                ],
                "description": "自动：优先硅基流动免费模型，失败自动转 MiniMax"
            },
            {
                "key": "minimax_api_key",
                "label": "MiniMax API Key",
                "type": "password",
                "default": "",
                "env": "MINIMAX_API_KEY",
                "placeholder": "留空则不启用该引擎"
            },
            {
                "key": "console_link",
                "label": "获取 API Key",
                "type": "link",
                "url": "https://platform.minimaxi.com/user-center/basic-information",
                "placeholder": "前往开放平台创建"
            }
        ]
    }
}
```

字段契约出处：主程序 `app/plugins/contracts/plugin_config.py`（修改字段能力先读它）。
PluginManager 扫描 schema 后自动 `register_settings_card(...)`，设置页出现「<title>」折叠卡。

## 2. 字段类型全表（FIELD_TYPES）

| type | 控件 | 必填/可选属性 |
|------|------|--------------|
| `text` | LineEdit | placeholder 可选 |
| `password` | PasswordLineEdit | 同上 |
| `bool` | SwitchButton（挂卡片 header 右侧） | default 为 bool |
| `select` | 下拉框 | **options 必填**，见 §3 |
| `number` | SpinBox | min/max/step 可选 |
| `textarea` | TextEdit | rows 可选（默认 3） |
| `link` | 超链接（无存储值） | url 必填 |
| `action` | 动作按钮（无存储值） | action 对象（声明式工具编排，见契约 docstring） |

通用属性：`key`（存储键，插件内唯一）、`label`（显示名）、`default`、`env`（环境变量覆盖名）、
`placeholder`、`description`（渲染为卡片说明）。

**保存语义**：每字段即时保存（editingFinished / checkedChanged / valueChanged 触发），
空串/清除 = 回默认值，无「保存」按钮。

## 3. select 的 options 必须 array（高频坑）

```json
"options": [
    {"value": "auto", "label": "自动"},
    {"value": "minimax", "label": "仅 MiniMax"}
]
```

`{"auto": "自动"}` 这种 dict 写法**主程序运行时兼容**（契约归一化），但**插件仓库
`validate_plugins.py` 的 schema 只收 array** → CI 校验 FAIL。一律写
`list[dict]`（或 `list[str]`，value=label）。

## 4. 运行时读取（插件侧范式）

标准读取 + 容错降级（主程序未注入配置系统/热重载竞态时不崩）：

```python
def _read_config() -> dict:
    try:
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        store = PluginConfigStore()
        return {
            "provider": str(store.get(PLUGIN_NAME, "provider") or "auto"),
            "api_key": str(store.get(PLUGIN_NAME, "minimax_api_key") or "").strip(),
        }
    except Exception as e:  # noqa: BLE001 — 配置不可用走默认值
        logger.warning(f"[<plugin>] 读取配置失败，按默认: {e}")
        return {}
```

- **值链**：环境变量（`env` 非空时）→ 存储值 → schema 默认。
- **存储位置**：`<app_data>/plugin_data/<plugin_name>/config.json`（独立于主程序 Settings，
  不进插件目录、**不随插件分发**——API Key 放这里是安全的，别写进任何随包文件）。
- 写：`PluginConfigStore().set_values(plugin, {key: value})`（合并写，空=清除该键）。
- key 只在主线程读，作参数传进 QThread worker；worker 不碰存储。

## 5. 引擎/服务多选项的模式（回退链）

配置里选「自动/仅A/仅B」时，用引擎链表而非 if 嵌套：

```python
def _build_chain(cfg) -> list:
    """[(url, model, key, 名称), ...]，头部优先"""
    chain = []
    if cfg["provider"] in ("auto", "siliconflow") and cfg.get("sf_key"):
        chain.append((_SF_URL, cfg["sf_model"], cfg["sf_key"], "硅基流动"))
    if cfg["provider"] in ("auto", "minimax") and cfg.get("mm_key"):
        chain.append((_MM_URL, "asr-1.0", cfg["mm_key"], "MiniMax"))
    return chain
```

- 链头部失败 → 自动转下一个（本次任务不丢）；单引擎模式失败即报错。
- key 全缺时在入口直接拦截并 InfoBar 提示去设置页填写。
- 实战参照：`drifox-plugins2/plugins/voice-input/ui/__init__.py`（_build_chain / _start_cloud）。
