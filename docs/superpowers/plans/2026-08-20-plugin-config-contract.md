# 插件配置契约标准化（E1）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 插件在 plugin.json 声明式描述自己的可配置项，主程序自动提供「统一存储 + 环境变量覆盖 + 默认值回退 + 设置面板自动渲染」，插件零样板代码。

**Architecture:** 三层：① 契约层 `PluginConfigField/PluginConfigSchema`（纯 dataclass）+ 注册表（PluginConfigRegistry 单例）；② 存储层 `PluginConfigStore`（`<app_data_dir>/plugins/<plugin_name>/config.json`，读取三级优先级：env → 存储值 → schema 默认，对齐 websearch 现有语义）；③ 渲染层 `PluginConfigCard`（schema 驱动自动生成 qfluentwidgets 设置行，经 Phase D `register_settings_card` 扩展点注入设置面板）。websearch 作为首个迁移方验收（行为零变化 + 旧配置文件一次性迁移）。

**Tech Stack:** Python 3.14 + PyQt5 + qfluentwidgets + pytest（现有栈，零新依赖）

## Global Constraints

- 行宽 120、双引号、ruff 格式化（项目现状）
- 注释中文、命名 snake_case
- 主程序零硬编码插件名（websearch 迁移后主程序不含 "tavily"/"websearch" 字样）
- 插件配置存储独立于主程序 `Settings`（沿用 websearch 「不占用主程序配置存储」决策，测试断言 import 不出现 `from app.utils.config import Settings`）
- 环境 → 存储 → 默认 三级优先级必须与现 websearch `_api_key` 行为逐点等价
- 每个 Task 结束 `ruff check . && ruff format --check .` + `pytest tests/plugins/ -x -q` 全绿
- 提交格式 `feat|fix|docs|chore: scope - summary`

## 背景：现状样板（本计划要消灭的重复）

当前 websearch 插件为提供两个 API key 配置，手写了：

1. **~50 行存储代码**（`plugins/system/tools/web_tools.py:38-103`）：`_config_path()` + `get_api_key_config()` + `set_api_key_config()` + `_api_key()` 三级优先级链，存 `<app_data_dir>/tools/web_search_keys.json`
2. **~114 行 UI 卡片**（`plugins/system/ui/__init__.py`）：手写 `WebSearchKeySettingsCard(QWidget)`（两行 `_KeyInputRow(SettingCard)` + 保存按钮 + 回显逻辑），经 `register_ui(registry)` → `registry.register_settings_card("system", "websearch-keys", ...)` 注入

下一个需要配置的插件将复制全部样板。本计划把这套模式泛化为声明式契约。

## 文件结构总览

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/plugins/contracts/plugin_config.py` | Create | `PluginConfigField` / `PluginConfigSchema` dataclass + `parse_config_schema(plugin_name, raw)` 解析函数 |
| `app/plugins/registries/plugin_config_registry.py` | Create | `PluginConfigRegistry` 单例：register/get/list/unregister_plugin |
| `app/plugins/managers/plugin_config_store.py` | Create | `PluginConfigStore`：按插件读写 JSON、三级优先级读取、一次性迁移钩子 |
| `app/widgets/cards/settings/plugin_config_card.py` | Create | `PluginConfigCard(QWidget)`：schema 驱动自动渲染（text/password/bool）+ `make_card_class()` |
| `app/plugins/managers/plugin_manager.py` | Modify | `_scan_one_plugin_dir` 解析 plugin.json `config_schema` 字段 → 注册 schema + 自动挂设置卡 |
| `plugins/system/.drifox-plugin/plugin.json` | Modify | 追加 `config_schema`（tavily/tinyfish 两字段） |
| `plugins/system/tools/web_tools.py` | Modify | `_api_key` 改走 store；删手写存储函数；删内置默认常量 |
| `plugins/system/ui/__init__.py` | Delete | 删 `WebSearchKeySettingsCard` 手写卡（由自动卡接管） |
| `AGENTS.md` | Modify | 插件约定追加 config_schema 段 |
| `tests/plugins/test_plugin_config_contract.py` | Create | 契约解析 + 注册表测试 |
| `tests/plugins/test_plugin_config_store.py` | Create | 存储读写/优先级/迁移/容错测试 |
| `tests/plugins/test_plugin_config_card.py` | Create | 自动卡渲染测试 |
| `tests/plugins/test_websearch_config_contract.py` | Create | websearch 迁移等价测试（替代旧 `test_websearch_api_key_config.py` 断言面） |

---

### Task 1: 契约 dataclass 与解析函数

**Files:**
- Create: `app/plugins/contracts/plugin_config.py`
- Test: `tests/plugins/test_plugin_config_contract.py`

**Interfaces:**
- Consumes: 无（纯新增）
- Produces（后续 Task 依赖的精确签名）:
  - `PluginConfigField(key: str, label: str, type: str, default: Any = "", env: str = "", placeholder: str = "", description: str = "")`（frozen dataclass；`type ∈ {"text","password","bool"}`；bool 的 default 为 `bool`，text/password 为 `str`）
  - `PluginConfigSchema(plugin_name: str, title: str, fields: List[PluginConfigField])`（frozen dataclass；提供 `get_field(key) -> Optional[PluginConfigField]`）
  - `parse_config_schema(plugin_name: str, raw: dict) -> Optional[PluginConfigSchema]`（raw 为 plugin.json 的 `config_schema` 对象；字段非法/类型未知返回 None 并记 warning 日志，不抛异常）

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/test_plugin_config_contract.py
# -*- coding: utf-8 -*-
"""插件配置契约：dataclass 与 plugin.json config_schema 解析。"""

import pytest

from app.plugins.contracts.plugin_config import (
    PluginConfigField,
    PluginConfigSchema,
    parse_config_schema,
)


class TestPluginConfigField:
    def test_text_field_defaults(self):
        f = PluginConfigField(key="k", label="显示名", type="text")
        assert f.default == ""
        assert f.env == ""

    def test_frozen(self):
        f = PluginConfigField(key="k", label="n", type="text")
        with pytest.raises(Exception):
            f.key = "other"


class TestParseConfigSchema:
    def test_parse_full_schema(self):
        raw = {
            "title": "网页搜索 API Key",
            "fields": [
                {
                    "key": "tavily_api_key",
                    "label": "Tavily 搜索",
                    "type": "password",
                    "default": "tvly-xxx",
                    "env": "TAVILY_API_KEY",
                    "placeholder": "TAVILY_API_KEY",
                },
                {"key": "require_proxy", "label": "走代理", "type": "bool", "default": True},
            ],
        }
        schema = parse_config_schema("system", raw)
        assert schema is not None
        assert schema.plugin_name == "system"
        assert schema.title == "网页搜索 API Key"
        assert len(schema.fields) == 2
        f0 = schema.fields[0]
        assert f0.type == "password"
        assert f0.env == "TAVILY_API_KEY"
        f1 = schema.fields[1]
        assert f1.default is True
        assert f1.type == "bool"

    def test_get_field(self):
        raw = {"title": "t", "fields": [{"key": "a", "label": "A", "type": "text"}]}
        schema = parse_config_schema("p", raw)
        assert schema.get_field("a") is not None
        assert schema.get_field("missing") is None

    def test_unknown_type_returns_none(self):
        raw = {"title": "t", "fields": [{"key": "a", "label": "A", "type": "json"}]}
        assert parse_config_schema("p", raw) is None

    def test_missing_required_key_returns_none(self):
        # 字段缺 key → 整个 schema 视为无效（宁缺毋滥，配置项解析失败静默跳过）
        raw = {"title": "t", "fields": [{"label": "A", "type": "text"}]}
        assert parse_config_schema("p", raw) is None

    def test_none_raw_returns_none(self):
        assert parse_config_schema("p", None) is None

    def test_empty_fields_returns_none(self):
        raw = {"title": "t", "fields": []}
        assert parse_config_schema("p", raw) is None

    def test_type_defaulted_to_text(self):
        raw = {"title": "t", "fields": [{"key": "a", "label": "A"}]}
        schema = parse_config_schema("p", raw)
        assert schema is not None and schema.fields[0].type == "text"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/plugins/test_plugin_config_contract.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.plugins.contracts.plugin_config'`

- [ ] **Step 3: 最小实现**

```python
# app/plugins/contracts/plugin_config.py
# -*- coding: utf-8 -*-
"""插件配置契约 — plugin.json 声明式配置 schema 的数据结构与解析。

设计（万物即插件 E1）：插件在 plugin.json 里声明 "config_schema"：
    "config_schema": {
        "title": "网页搜索 API Key",
        "fields": [
            {"key": "tavily_api_key", "label": "Tavily 搜索", "type": "password",
             "default": "<内置默认>", "env": "TAVILY_API_KEY", "placeholder": "TAVILY_API_KEY"}
        ]
    }
主程序据此自动提供：统一存储（PluginConfigStore）+ 设置面板渲染（PluginConfigCard）。
插件不再手写存储与 UI 样板（参照 websearch 迁移前 ~164 行手写代码 → 0 行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from loguru import logger

# 支持的字段类型（渲染映射见 plugin_config_card.py）
FIELD_TYPES = ("text", "password", "bool")


@dataclass(frozen=True)
class PluginConfigField:
    """单个配置字段声明

    Attributes:
        key: 存储键（插件内唯一；对应 config.json 的顶层键）
        label: 设置面板显示名
        type: text（单行输入）/ password（密码输入）/ bool（开关）
        default: 默认值（bool 字段为 bool，其余为 str）
        env: 环境变量名（非空时环境变量优先级高于存储值）
        placeholder: 输入框占位文本
        description: 字段说明（渲染为卡片 content）
    """

    key: str
    label: str
    type: str = "text"
    default: Any = ""
    env: str = ""
    placeholder: str = ""
    description: str = ""


@dataclass(frozen=True)
class PluginConfigSchema:
    """一个插件的完整配置声明"""

    plugin_name: str
    title: str
    fields: List[PluginConfigField] = field(default_factory=list)

    def get_field(self, key: str) -> Optional[PluginConfigField]:
        for f in self.fields:
            if f.key == key:
                return f
        return None


def parse_config_schema(plugin_name: str, raw: Optional[dict]) -> Optional[PluginConfigSchema]:
    """解析 plugin.json 的 config_schema 对象。

    容错原则：任一字段非法 → 整个 schema 返回 None（记 warning，不抛异常），
    插件照常加载，只是没有配置 UI。
    """
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or plugin_name)
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        logger.warning(f"[PluginConfig] {plugin_name} config_schema.fields 为空或缺失，忽略")
        return None
    fields: List[PluginConfigField] = []
    for item in raw_fields:
        if not isinstance(item, dict):
            logger.warning(f"[PluginConfig] {plugin_name} 字段非对象，忽略整个 schema: {item!r}")
            return None
        key = str(item.get("key") or "").strip()
        if not key:
            logger.warning(f"[PluginConfig] {plugin_name} 字段缺 key，忽略整个 schema: {item!r}")
            return None
        ftype = str(item.get("type") or "text")
        if ftype not in FIELD_TYPES:
            logger.warning(f"[PluginConfig] {plugin_name} 字段 {key} 类型未知({ftype})，忽略整个 schema")
            return None
        default = item.get("default", "" if ftype != "bool" else False)
        if ftype == "bool" and not isinstance(default, bool):
            default = bool(default)
        fields.append(
            PluginConfigField(
                key=key,
                label=str(item.get("label") or key),
                type=ftype,
                default=default,
                env=str(item.get("env") or ""),
                placeholder=str(item.get("placeholder") or ""),
                description=str(item.get("description") or ""),
            )
        )
    return PluginConfigSchema(plugin_name=plugin_name, title=title, fields=fields)
```

注意：若 `app/plugins/contracts/__init__.py` 有显式 `__all__` 导出清单，追加 `plugin_config` 相关导出；没有则跳过。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/plugins/test_plugin_config_contract.py -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add app/plugins/contracts/plugin_config.py tests/plugins/test_plugin_config_contract.py
git commit -m "feat(plugins): 插件配置契约 dataclass 与 plugin.json config_schema 解析（E1）"
```

---

### Task 2: 配置注册表

**Files:**
- Create: `app/plugins/registries/plugin_config_registry.py`
- Test: `tests/plugins/test_plugin_config_contract.py`（追加 TestPluginConfigRegistry 类）

**Interfaces:**
- Consumes: Task 1 的 `PluginConfigSchema`
- Produces:
  - `PluginConfigRegistry`：`register(schema) -> None`（同 plugin_name 覆盖，幂等）、`get(plugin_name) -> Optional[PluginConfigSchema]`、`list_schemas() -> List[PluginConfigSchema]`（注册序）、`unregister_plugin(plugin_name) -> None`（热重载清理）
  - `PluginConfigRegistry.get_instance() -> PluginConfigRegistry`（线程安全单例，与 ModelAdapterRegistry 同款写法）

- [ ] **Step 1: 写失败测试**

在 `tests/plugins/test_plugin_config_contract.py` 追加：

```python
class TestPluginConfigRegistry:
    def _make_schema(self, plugin_name="p1"):
        return parse_config_schema(
            plugin_name,
            {"title": "T", "fields": [{"key": "a", "label": "A", "type": "text"}]},
        )

    def test_register_and_get(self):
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

        reg = PluginConfigRegistry()
        schema = self._make_schema()
        reg.register(schema)
        assert reg.get("p1") is schema
        assert [s.plugin_name for s in reg.list_schemas()] == ["p1"]

    def test_register_overwrites_same_plugin(self):
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

        reg = PluginConfigRegistry()
        reg.register(self._make_schema("p1"))
        reg.register(self._make_schema("p1"))  # 幂等覆盖（rescan 重复注册）
        assert len(reg.list_schemas()) == 1

    def test_unregister_plugin(self):
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

        reg = PluginConfigRegistry()
        reg.register(self._make_schema("p1"))
        reg.unregister_plugin("p1")
        assert reg.get("p1") is None
        reg.unregister_plugin("p1")  # 幂等

    def test_get_instance_singleton(self):
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

        assert PluginConfigRegistry.get_instance() is PluginConfigRegistry.get_instance()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/plugins/test_plugin_config_contract.py::TestPluginConfigRegistry -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 最小实现**

```python
# app/plugins/registries/plugin_config_registry.py
# -*- coding: utf-8 -*-
"""插件配置注册表 — config schema 的进程级单例（E1）。

PluginManager 扫描 plugin.json 时注册；PluginConfigCard 渲染与
PluginConfigStore 默认值查询消费。热重载：rescan 覆盖同 plugin_name，
插件删除时 unregister_plugin 清理。
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from app.plugins.contracts.plugin_config import PluginConfigSchema


class PluginConfigRegistry:
    """plugin_name → PluginConfigSchema（注册序保持，同名覆盖）"""

    def __init__(self) -> None:
        self._schemas: Dict[str, PluginConfigSchema] = {}
        self._lock = threading.Lock()

    def register(self, schema: PluginConfigSchema) -> None:
        with self._lock:
            self._schemas[schema.plugin_name] = schema

    def get(self, plugin_name: str) -> Optional[PluginConfigSchema]:
        with self._lock:
            return self._schemas.get(plugin_name)

    def list_schemas(self) -> List[PluginConfigSchema]:
        with self._lock:
            return list(self._schemas.values())

    def unregister_plugin(self, plugin_name: str) -> None:
        with self._lock:
            self._schemas.pop(plugin_name, None)


def get_instance() -> PluginConfigRegistry:
    """进程级单例访问（与 ModelAdapterRegistry.get_instance 对齐的模块级入口）"""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is None:
            _instance = PluginConfigRegistry()
    return _instance


_instance: Optional[PluginConfigRegistry] = None
_instance_lock = threading.Lock()

# 类级访问器（调用方统一 PluginConfigRegistry.get_instance()）
PluginConfigRegistry.get_instance = staticmethod(get_instance)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/plugins/test_plugin_config_contract.py -v`
Expected: PASS（含 Task 1 用例不回归）

- [ ] **Step 5: 提交**

```bash
git add app/plugins/registries/plugin_config_registry.py tests/plugins/test_plugin_config_contract.py
git commit -m "feat(plugins): PluginConfigRegistry 配置 schema 注册表单例（E1）"
```

---

### Task 3: 配置存储（三级优先级 + 迁移钩子）

**Files:**
- Create: `app/plugins/managers/plugin_config_store.py`
- Test: `tests/plugins/test_plugin_config_store.py`

**Interfaces:**
- Consumes: Task 2 的 `PluginConfigRegistry.get_instance()`
- Produces（websearch 迁移与渲染卡依赖的精确语义）:
  - `PluginConfigStore()`：构造无参
  - `get(plugin_name: str, key: str) -> Any`：**环境变量 → 存储值 → schema 默认值**。无 schema 且无存储值时返回 None。bool 字段经 bool() 归一，str 字段经 str() 归一
  - `get_all(plugin_name: str) -> Dict[str, Any]`：全部字段当前生效值（渲染卡回显用）
  - `set_values(plugin_name: str, values: Dict[str, Any]) -> bool`：合并写 `<app_data_dir>/plugins/<plugin_name>/config.json`（只写 values 里出现的键；空串/None 视为清除此键 → 回退默认）；目录自动创建；写失败返回 False 不抛
  - `reset(plugin_name: str) -> bool`：删除存储文件（全部回默认）
  - `migrate(plugin_name: str, legacy_path, key_map: Dict[str, str]) -> bool`：一次性迁移（见 Step 3 详细语义）

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/test_plugin_config_store.py
# -*- coding: utf-8 -*-
"""PluginConfigStore：统一存储 + 三级优先级 + 迁移。"""

import json

import pytest

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.plugins.managers.plugin_config_store import PluginConfigStore


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """把 app_data_dir 指到临时目录，隔离真实用户数据。"""
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry()
    reg.register(
        parse_config_schema(
            "plug-x",
            {
                "title": "X",
                "fields": [
                    {"key": "api_key", "label": "Key", "type": "password",
                     "default": "built-in-default", "env": "PLUG_X_KEY"},
                    {"key": "verbose", "label": "详细", "type": "bool", "default": False},
                ],
            },
        )
    )
    yield tmp_path, monkeypatch
    reg.unregister_plugin("plug-x")


class TestPriorityChain:
    def test_falls_back_to_schema_default(self, store_env):
        s = PluginConfigStore()
        assert s.get("plug-x", "api_key") == "built-in-default"

    def test_stored_value_wins_over_default(self, store_env):
        s = PluginConfigStore()
        assert s.set_values("plug-x", {"api_key": "user-key"})
        assert s.get("plug-x", "api_key") == "user-key"

    def test_env_wins_over_stored(self, store_env):
        tmp_path, monkeypatch = store_env
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "user-key"})
        monkeypatch.setenv("PLUG_X_KEY", "env-key")
        assert s.get("plug-x", "api_key") == "env-key"

    def test_empty_string_clears_back_to_default(self, store_env):
        # 与旧 websearch 语义等价：空串 = 清除用户配置 → 回退内置默认
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "user-key"})
        s.set_values("plug-x", {"api_key": ""})
        assert s.get("plug-x", "api_key") == "built-in-default"

    def test_bool_normalization(self, store_env):
        s = PluginConfigStore()
        s.set_values("plug-x", {"verbose": "true"})  # UI/JSON 里可能是字符串
        assert s.get("plug-x", "verbose") is True

    def test_unknown_key_without_schema_returns_none(self, store_env):
        s = PluginConfigStore()
        assert s.get("no-such-plugin", "k") is None


class TestPersistence:
    def test_get_all_effective_values(self, store_env):
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "k1"})
        vals = s.get_all("plug-x")
        assert vals["api_key"] == "k1"
        assert vals["verbose"] is False  # 未配置 → 默认

    def test_reset(self, store_env):
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "k1"})
        assert s.reset("plug-x") is True
        assert s.get("plug-x", "api_key") == "built-in-default"

    def test_corrupt_json_tolerated(self, store_env):
        tmp_path, _ = store_env
        cfg_dir = tmp_path / "plugins" / "plug-x"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text("{ not json", encoding="utf-8")
        s = PluginConfigStore()
        assert s.get("plug-x", "api_key") == "built-in-default"  # 损坏容错回默认


class TestMigration:
    def test_migrate_legacy_file(self, store_env):
        tmp_path, _ = store_env
        legacy = tmp_path / "tools" / "web_search_keys.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            json.dumps({"tavily_api_key": "legacy-key"}), encoding="utf-8"
        )
        s = PluginConfigStore()
        ok = s.migrate(
            "plug-x", legacy, key_map={"tavily_api_key": "api_key"}
        )
        assert ok is True
        assert s.get("plug-x", "api_key") == "legacy-key"
        # 旧文件改名 .bak（不再参与读取，保留现场）
        assert not legacy.exists()
        assert (tmp_path / "tools" / "web_search_keys.json.bak").exists()

    def test_migrate_noop_when_target_exists(self, store_env):
        tmp_path, _ = store_env
        legacy = tmp_path / "old.json"
        legacy.write_text(json.dumps({"tavily_api_key": "old"}), encoding="utf-8")
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "already-set"})
        ok = s.migrate("plug-x", legacy, key_map={"tavily_api_key": "api_key"})
        assert ok is False  # 已有新配置不覆盖
        assert s.get("plug-x", "api_key") == "already-set"

    def test_migrate_missing_file(self, store_env):
        s = PluginConfigStore()
        assert s.migrate("plug-x", "/no/such/file.json", key_map={}) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/plugins/test_plugin_config_store.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 最小实现**

```python
# app/plugins/managers/plugin_config_store.py
# -*- coding: utf-8 -*-
"""插件配置统一存储（E1）。

路径：<app_data_dir>/plugins/<plugin_name>/config.json
读取优先级：环境变量 → 存储值 → schema 默认（与 websearch 迁移前
_api_key 三级链逐点等价：env 最高、空串=清除、内置默认兜底）。
独立于主程序 Settings（插件数据不占用主程序配置文件）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from app.plugins.contracts.plugin_config import PluginConfigField, PluginConfigSchema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


class PluginConfigStore:
    """按插件读写配置（无状态，可随时实例化）"""

    def _schema(self, plugin_name: str) -> Optional[PluginConfigSchema]:
        return PluginConfigRegistry.get_instance().get(plugin_name)

    def _path(self, plugin_name: str) -> Path:
        from app.utils.utils import get_app_data_dir

        return Path(get_app_data_dir()) / "plugins" / plugin_name / "config.json"

    def _read_raw(self, plugin_name: str) -> Dict[str, Any]:
        try:
            path = self._path(plugin_name)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"[PluginConfigStore] {plugin_name} 配置读取失败（按空处理）: {e}")
        return {}

    def _write_raw(self, plugin_name: str, data: Dict[str, Any]) -> bool:
        try:
            path = self._path(plugin_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"[PluginConfigStore] {plugin_name} 配置写入失败: {e}")
            return False

    # ── 读 ──────────────────────────────────────────────

    def get(self, plugin_name: str, key: str) -> Any:
        """当前生效值：环境变量 → 存储值 → schema 默认"""
        schema = self._schema(plugin_name)
        f = schema.get_field(key) if schema else None
        if f is not None and f.env:
            env_val = os.environ.get(f.env)
            if env_val:
                return self._normalize(f, env_val)
        raw = self._read_raw(plugin_name)
        if key in raw and raw[key] not in ("", None):
            return self._normalize(f, raw[key]) if f is not None else raw[key]
        return f.default if f is not None else None

    def get_all(self, plugin_name: str) -> Dict[str, Any]:
        """全部字段当前生效值（渲染卡回显用；无 schema 返回空 dict）"""
        schema = self._schema(plugin_name)
        if schema is None:
            return {}
        return {f.key: self.get(plugin_name, f.key) for f in schema.fields}

    @staticmethod
    def _normalize(f: Optional[PluginConfigField], value: Any) -> Any:
        if f is None:
            return value
        if f.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("1", "true", "yes", "on")
        return str(value)

    # ── 写 ──────────────────────────────────────────────

    def set_values(self, plugin_name: str, values: Dict[str, Any]) -> bool:
        """合并写；空串/None = 清除该键（回退默认）。只落 values 出现的键。"""
        raw = self._read_raw(plugin_name)
        for key, val in values.items():
            if val is None or val == "":
                raw.pop(key, None)
            else:
                raw[key] = val
        return self._write_raw(plugin_name, raw)

    def reset(self, plugin_name: str) -> bool:
        """删除存储文件（全部回默认）"""
        try:
            path = self._path(plugin_name)
            if path.exists():
                path.unlink()
            return True
        except Exception as e:
            logger.warning(f"[PluginConfigStore] {plugin_name} 重置失败: {e}")
            return False

    # ── 一次性迁移 ──────────────────────────────────────

    def migrate(self, plugin_name: str, legacy_path, key_map: Dict[str, str]) -> bool:
        """旧配置文件迁移（旧键 → 新键）。

        语义：新存储文件已有内容 → 不覆盖返回 False；
        旧文件不存在 → False；迁移成功后旧文件改名 .bak。
        """
        legacy = Path(legacy_path)
        if not legacy.exists():
            return False
        target = self._path(plugin_name)
        if target.exists():
            existing = self._read_raw(plugin_name)
            if existing:
                return False
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[PluginConfigStore] 迁移源解析失败 {legacy}: {e}")
            return False
        if not isinstance(data, dict):
            return False
        values = {new_key: data[old_key] for old_key, new_key in key_map.items() if data.get(old_key)}
        if not values:
            return False
        if not self._write_raw(plugin_name, values):
            return False
        try:
            legacy.rename(legacy.with_name(legacy.name + ".bak"))
        except Exception as e:
            logger.warning(f"[PluginConfigStore] 旧文件改名失败（不影响迁移结果）: {e}")
        return True
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/plugins/test_plugin_config_store.py -v`
Expected: PASS 全部

- [ ] **Step 5: 提交**

```bash
git add app/plugins/managers/plugin_config_store.py tests/plugins/test_plugin_config_store.py
git commit -m "feat(plugins): PluginConfigStore 统一存储（env→存储→默认三级链 + 迁移钩子，E1）"
```

---

### Task 4: plugin.json 解析接线 + 设置面板自动渲染卡

**Files:**
- Create: `app/widgets/cards/settings/plugin_config_card.py`
- Modify: `app/plugins/managers/plugin_manager.py`（`_scan_one_plugin_dir`，解析 manifest 处追加 config_schema 注册；具体插入点：plugin.json 解析出插件信息对象后、返回前）
- Test: `tests/plugins/test_plugin_config_card.py`

**Interfaces:**
- Consumes: Task 1/2/3 全部产物；Phase D `UIPluginRegistry.register_settings_card(plugin_name, card_id, title, widget_class, group="plugin", priority=0, metadata=None)`
- Produces:
  - `PluginConfigCard(QWidget)`：`__init__(self, plugin_name: str, parent=None)`；自动渲染 schema 全部字段 + 保存按钮；属性 `_plugin_name`、`_rows: Dict[str, QWidget]`、`save_btn`
  - `make_card_class(plugin_name: str) -> type`（动态生成绑定 plugin_name 的无参构造 QWidget 子类，满足 register_settings_card 的 widget_class 约定）
  - PluginManager 内部：扫描到合法 config_schema → `PluginConfigRegistry.get_instance().register(schema)` + `UIPluginRegistry.get_instance().register_settings_card(plugin_name, f"{plugin_name}-config", schema.title, make_card_class(plugin_name))`
  - 渲染规则：`text` → `LineEdit` 行；`password` → `PasswordLineEdit` 行（qfluentwidgets）；`bool` → `SwitchButton` 行；每行左侧图标统一 `FluentIcon.SETTING`

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/test_plugin_config_card.py
# -*- coding: utf-8 -*-
"""声明式配置自动渲染卡（QApplication 离屏渲染）。"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.widgets.cards.settings.plugin_config_card import (
    PluginConfigCard,
    make_card_class,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def schema_env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry()
    reg.register(
        parse_config_schema(
            "plug-ui",
            {
                "title": "UI 测试",
                "fields": [
                    {"key": "name", "label": "名称", "type": "text", "default": "abc"},
                    {"key": "secret", "label": "密钥", "type": "password", "default": "sk-1"},
                    {"key": "on", "label": "开关", "type": "bool", "default": False},
                ],
            },
        )
    )
    yield tmp_path
    reg.unregister_plugin("plug-ui")


def test_card_renders_all_field_rows(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    # 三行字段控件 + 标题 + 保存按钮
    assert card._rows["name"] is not None
    assert card._rows["secret"] is not None
    assert card._rows["on"] is not None
    assert card.save_btn is not None


def test_card_echoes_effective_values(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    assert card._rows["name"].text() == "abc"      # 默认值回显
    assert card._rows["secret"].text() == "sk-1"
    assert card._rows["on"].isChecked() is False


def test_card_save_persists(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    card._rows["name"].setText("changed")
    card._save()
    assert PluginConfigStore().get("plug-ui", "name") == "changed"


def test_make_card_class_zero_arg_construction(qapp, schema_env):
    cls = make_card_class("plug-ui")
    widget = cls()  # register_settings_card 的 widget_class 约定：无参构造
    assert isinstance(widget, PluginConfigCard)
    assert widget._plugin_name == "plug-ui"


def test_card_without_schema_renders_empty(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    card = PluginConfigCard("never-registered")
    assert card._rows == {}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/plugins/test_plugin_config_card.py -v`
Expected: FAIL，`ModuleNotFoundError: ... plugin_config_card`

- [ ] **Step 3: 实现渲染卡**

```python
# app/widgets/cards/settings/plugin_config_card.py
# -*- coding: utf-8 -*-
"""声明式插件配置自动渲染卡（E1）。

由 PluginConfigSchema 驱动：text→LineEdit / password→PasswordLineEdit /
bool→SwitchButton，末尾统一保存按钮。保存后回显当前生效值
（空输入=清除→回默认，对齐 websearch 旧卡语义）。
注册方式：PluginManager 扫描 config_schema 后调
register_settings_card(..., make_card_class(plugin_name))，插件零 UI 代码。
"""

from __future__ import annotations

from typing import Dict

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    SettingCard,
    StrongBodyLabel,
    SwitchButton,
)

from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


def _unified_font(size: int = 13):
    from app.utils.utils import get_unified_font

    return get_unified_font(size)


class _ConfigRow(SettingCard):
    """通用配置行：控件由调用方创建并加入右侧"""

    def __init__(self, title: str, content: str, control: QWidget, parent=None):
        super().__init__(FluentIcon.SETTING, title, content, parent)
        self.setFont(_unified_font())
        control.setFont(_unified_font())
        self.hBoxLayout.addWidget(control, 0, Qt.AlignRight)


class PluginConfigCard(QWidget):
    """schema 驱动的插件配置卡（无 schema 时渲染为空，不报错）"""

    def __init__(self, plugin_name: str, parent=None):
        super().__init__(parent)
        self._plugin_name = plugin_name
        self._rows: Dict[str, QWidget] = {}  # key → 输入控件
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.setFont(_unified_font())

        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return

        title = StrongBodyLabel(schema.title, self)
        title.setFont(_unified_font(14))
        layout.addWidget(title)

        for f in schema.fields:
            if f.type == "bool":
                switch = SwitchButton()
                row = _ConfigRow(f.label, f.description, switch)
                self._rows[f.key] = switch
            else:
                edit = PasswordLineEdit() if f.type == "password" else LineEdit()
                edit.setClearButtonEnabled(True)
                if f.placeholder:
                    edit.setPlaceholderText(f.placeholder)
                edit.setFixedWidth(320)
                row = _ConfigRow(f.label, f.description or f.placeholder, edit)
                self._rows[f.key] = edit
            layout.addWidget(row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.save_btn = PrimaryPushButton("保存配置", self)
        from app.widgets.cards.settings.llm_settings_card import ButtonStyles

        self.save_btn.setStyleSheet(ButtonStyles.primary_action())
        self.save_btn.setFixedWidth(120)
        self.save_btn.setFont(_unified_font())
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.save_btn)
        layout.addLayout(btn_row)

        self._echo()

    def _echo(self):
        """回显当前生效值（默认兜底可见）"""
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        for f in schema.fields:
            control = self._rows.get(f.key)
            if control is None:
                continue
            val = store.get(self._plugin_name, f.key)
            if f.type == "bool":
                control.setChecked(bool(val))
            else:
                control.setText(str(val if val is not None else ""))

    def _save(self):
        """保存：空文本=清除（回默认）；保存后刷新回显"""
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        values = {}
        for f in schema.fields:
            control = self._rows.get(f.key)
            if control is None:
                continue
            if f.type == "bool":
                values[f.key] = control.isChecked()
            else:
                values[f.key] = control.text().strip()
        store.set_values(self._plugin_name, values)
        self._echo()


def make_card_class(plugin_name: str) -> type:
    """生成绑定 plugin_name 的无参构造卡片类（register_settings_card 的 widget_class 约定）"""

    class _BoundConfigCard(PluginConfigCard):
        def __init__(self, parent=None):
            super().__init__(plugin_name, parent)

    _BoundConfigCard.__name__ = f"PluginConfigCard[{plugin_name}]"
    return _BoundConfigCard
```

- [ ] **Step 4: PluginManager 接线（解析 config_schema → 注册表 + 自动设置卡）**

修改 `app/plugins/managers/plugin_manager.py` 的 `_scan_one_plugin_dir`：在解析出 plugin.json 的 raw dict 后追加：

```python
# —— E1 声明式插件配置：解析 config_schema 并注册（含自动设置卡）——
from app.plugins.contracts.plugin_config import parse_config_schema

config_schema = parse_config_schema(plugin_name, manifest.get("config_schema"))
if config_schema is not None:
    from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

    PluginConfigRegistry.get_instance().register(config_schema)
    # 自动挂设置面板插件分区卡（Phase D 扩展点，卡片打开时按注册重建）
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        from app.widgets.cards.settings.plugin_config_card import make_card_class

        UIPluginRegistry.get_instance().register_settings_card(
            plugin_name, f"{plugin_name}-config", config_schema.title, make_card_class(plugin_name)
        )
    except Exception as e:
        logger.warning(f"[PluginManager] config_schema 设置卡注册失败({plugin_name}): {e}")
```

> 实现者注意：`plugin_name` / `manifest` 用该函数内实际变量名（阅读上下文对齐：manifest 为 plugin.json 反序列化后的 dict）。插件移除路径：在插件信息删除分支追加 `PluginConfigRegistry.get_instance().unregister_plugin(plugin_name)`（设置卡清理若 Phase D 卸载钩子已统一处理则挂同一处；`unregister_plugin` 幂等）。

接线单测（追加到 `tests/plugins/test_plugin_config_card.py`）：

```python
def test_plugin_manager_registers_config_schema(tmp_path, monkeypatch):
    """plugin.json 含 config_schema → 扫描后注册表可见 + 设置卡已注册"""
    import json

    plug_dir = tmp_path / "plug-cfg"
    (plug_dir / ".drifox-plugin").mkdir(parents=True)
    (plug_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "plug-cfg",
                "version": "1.0.0",
                "config_schema": {
                    "title": "C 卡",
                    "fields": [{"key": "k", "label": "K", "type": "text"}],
                },
            }
        ),
        encoding="utf-8",
    )
    # 参考 tests/plugins/test_e2e_new_component.py 的既有插件扫描注入模式，
    # 把扫描根指到 tmp_path（按实际可注入点 monkeypatch；PluginManager 生成
    # 插件目录列表的方法）。以下为示意，执行时按真实方法名调整注入：
    from app.plugins.managers.plugin_manager import PluginManager
    from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    pm = PluginManager()
    # 方式 A（优先）：monkeypatch 扫描根列表后 rescan
    # 方式 B（回退）：直接调用 pm._scan_one_plugin_dir(plug_dir)（按实际签名）
    # —— 以能跑通为准，允许调整注入方式，不允许删除断言。
    reg = PluginConfigRegistry.get_instance()
    assert reg.get("plug-cfg") is not None
    ui = UIPluginRegistry.get_instance()
    cards = [c.card_id for c in ui.get_settings_cards()]
    assert "plug-cfg-config" in cards
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/plugins/test_plugin_config_card.py -v`
Expected: PASS（含接线测试）

- [ ] **Step 6: 全量回归**

Run: `python -m pytest tests/plugins/ -x -q`
Expected: 全绿（既有 230+ 用例不回归）

- [ ] **Step 7: 提交**

```bash
git add app/widgets/cards/settings/plugin_config_card.py app/plugins/managers/plugin_manager.py tests/plugins/test_plugin_config_card.py
git commit -m "feat(plugins): config_schema 声明式配置自动渲染卡 + PluginManager 接线（E1）"
```

---

### Task 5: websearch 迁移到新契约（行为零变化 + 旧文件迁移）

**Files:**
- Modify: `plugins/system/.drifox-plugin/plugin.json`（追加 config_schema）
- Modify: `plugins/system/tools/web_tools.py`（删 ~70 行手写存储，`_api_key` 改走 store；新增一次迁移调用）
- Delete: `plugins/system/ui/__init__.py`（手写卡由自动卡接管；Phase D loader 对无 ui/ 目录的插件天然跳过；若 loader 要求目录存在才调用，先验证删除后无报错）
- Test: `tests/plugins/test_websearch_config_contract.py`（新建）；删除 `tests/plugins/test_websearch_api_key_config.py`（断言面已被新文件等价覆盖）；`tests/plugins/test_websearch_config_card.py` 按现状处置（见 Step 3d）

**Interfaces:**
- Consumes: Task 3 `PluginConfigStore.get/migrate`；Task 4 自动卡（plugin.json 声明后自动出现，无需代码）
- Produces: `plugins/system/tools/web_tools.py` 内 `_api_key(tool_ctx, name: str) -> str` 签名不变（`register(...)` 内 schema/impl 不动）；新增模块级 `_ensure_migrated() -> None`

- [ ] **Step 1: 写失败测试（迁移后等价语义）**

```python
# tests/plugins/test_websearch_config_contract.py
# -*- coding: utf-8 -*-
"""websearch 配置迁移到 E1 契约：三级优先级逐点等价 + 旧文件一次性迁移。

替代旧 test_websearch_api_key_config.py 的断言面。
"""

import json

import pytest

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    # 注册 schema（模拟 PluginManager 扫描 system 插件 manifest；default 用假值隔离）
    reg = PluginConfigRegistry()
    reg.register(
        parse_config_schema(
            "system",
            {
                "title": "网页搜索 API Key",
                "fields": [
                    {"key": "tavily_api_key", "label": "Tavily 搜索", "type": "password",
                     "default": "tvly-dev-DEFAULT", "env": "TAVILY_API_KEY",
                     "placeholder": "TAVILY_API_KEY"},
                    {"key": "tinyfish_api_key", "label": "TinyFish 搜索", "type": "password",
                     "default": "sk-tinyfish-DEFAULT", "env": "TINYFISH_API_KEY",
                     "placeholder": "TINYFISH_API_KEY"},
                ],
            },
        )
    )
    yield tmp_path
    reg.unregister_plugin("system")


def _websearch_module():
    import importlib

    import plugins.system.tools.web_tools as m

    return importlib.reload(m)


class TestLegacyPriorityEquivalence:
    def test_default_when_nothing_set(self, env):
        m = _websearch_module()
        assert m._api_key(None, "TAVILY_API_KEY") == "tvly-dev-DEFAULT"
        assert m._api_key(None, "TINYFISH_API_KEY") == "sk-tinyfish-DEFAULT"

    def test_env_wins(self, env, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "env-key")
        m = _websearch_module()
        assert m._api_key(None, "TAVILY_API_KEY") == "env-key"

    def test_stored_wins_over_default(self, env):
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        PluginConfigStore().set_values("system", {"tavily_api_key": "user-key"})
        m = _websearch_module()
        assert m._api_key(None, "TAVILY_API_KEY") == "user-key"


class TestLegacyMigration:
    def test_legacy_web_search_keys_migrated_on_first_read(self, env):
        legacy = env / "tools" / "web_search_keys.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"tavily_api_key": "old-key"}), encoding="utf-8")

        m = _websearch_module()
        m._ensure_migrated()

        from app.plugins.managers.plugin_config_store import PluginConfigStore

        assert PluginConfigStore().get("system", "tavily_api_key") == "old-key"
        assert not legacy.exists()  # 已改名 .bak
        # 幂等：二次调用不重复迁移
        m._ensure_migrated()


class TestSelfContained:
    def test_no_manual_storage_boilerplate_left(self):
        """手写存储样板已删（get_api_key_config/set_api_key_config/_config_path 不复存在）"""
        src = open("plugins/system/tools/web_tools.py", encoding="utf-8").read()
        assert "def get_api_key_config" not in src
        assert "def set_api_key_config" not in src
        assert "_config_path" not in src

    def test_no_hardcoded_websearch_card(self):
        """手写 UI 卡已删（自动卡接管）"""
        import os

        ui_init = "plugins/system/ui/__init__.py"
        assert not os.path.exists(ui_init) or (
            "WebSearchKeySettingsCard" not in open(ui_init, encoding="utf-8").read()
        )

    def test_no_settings_dependency(self):
        """插件配置不依赖主程序 Settings（沿用自包含决策）"""
        src = open("plugins/system/tools/web_tools.py", encoding="utf-8").read()
        assert "from app.utils.config import Settings" not in src
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/plugins/test_websearch_config_contract.py -v`
Expected: FAIL（`get_api_key_config` 仍存在 / `_ensure_migrated` 缺失）

- [ ] **Step 3: 实现迁移**

3a. `plugins/system/.drifox-plugin/plugin.json`：`components` 同级追加（**default 值照抄 `plugins/system/tools/web_tools.py` 现有 `_DEFAULT_TAVILY_KEY` / `_DEFAULT_TINYFISH_KEY` 常量原文**，不要在计划/文档中另存副本）：

```json
"config_schema": {
    "title": "网页搜索 API Key",
    "fields": [
        {
            "key": "tavily_api_key",
            "label": "Tavily 搜索",
            "type": "password",
            "default": "<照抄 _DEFAULT_TAVILY_KEY 现值>",
            "env": "TAVILY_API_KEY",
            "placeholder": "TAVILY_API_KEY"
        },
        {
            "key": "tinyfish_api_key",
            "label": "TinyFish 搜索",
            "type": "password",
            "default": "<照抄 _DEFAULT_TINYFISH_KEY 现值>",
            "env": "TINYFISH_API_KEY",
            "placeholder": "TINYFISH_API_KEY"
        }
    ]
}
```

3b. `plugins/system/tools/web_tools.py`：删除 `_DEFAULT_TAVILY_KEY` / `_DEFAULT_TINYFISH_KEY` / `_CONFIG_FILENAME` / `_config_path()` / `get_api_key_config()` / `set_api_key_config()`（约 L34-91），替换为：

```python
# ── E1 插件配置契约：schema 声明在 plugin.json（config_schema），存储走 PluginConfigStore ──


def _ensure_migrated() -> None:
    """旧 <app_data>/tools/web_search_keys.json 一次性迁移到统一存储（幂等）。

    旧文件存在且新存储为空 → 迁移并改名 .bak；否则静默跳过。
    """
    from pathlib import Path

    from app.plugins.managers.plugin_config_store import PluginConfigStore
    from app.utils.utils import get_app_data_dir

    legacy = Path(get_app_data_dir()) / "tools" / "web_search_keys.json"
    if not legacy.exists():
        return
    PluginConfigStore().migrate(
        "system",
        legacy,
        key_map={"tavily_api_key": "tavily_api_key", "tinyfish_api_key": "tinyfish_api_key"},
    )


def _api_key(tool_ctx, name: str) -> str:
    """读取搜索服务 API key：环境变量 → 插件配置 → schema 默认（E1 三级链）

    - 环境变量：TAVILY_API_KEY / TINYFISH_API_KEY（最高优先级）
    - 插件配置：PluginConfigStore（plugin.json config_schema 声明，设置面板自动渲染）
    - 默认值：plugin.json config_schema.default（内置兜底）
    """
    _ensure_migrated()
    from app.plugins.managers.plugin_config_store import PluginConfigStore

    key = "tavily_api_key" if name == "TAVILY_API_KEY" else "tinyfish_api_key"
    val = PluginConfigStore().get("system", key)
    return str(val or "")
```

3c. 删除 `plugins/system/ui/__init__.py`。**删除前先 grep 确认**无其他消费方：

Run: `grep -rn "WebSearchKeySettingsCard" --include="*.py" .`
Expected: 仅 `plugins/system/ui/__init__.py` 自身与 `tests/plugins/test_websearch_config_card.py`。若 UIPluginRegistry 对 system 插件的 ui/ 目录缺失有加载断言（读 `load_plugin` 实现确认容错），再执行删除。

3d. 旧测试处置：
- `tests/plugins/test_websearch_api_key_config.py`（断言手写函数）→ 删除，等价断言已在新文件
- `tests/plugins/test_websearch_config_card.py` → 若断言手动卡注册，删除并在新文件追加自动卡断言：

```python
def test_auto_card_registered_for_system():
    """plugin.json config_schema 声明后，自动卡 system-config 出现在设置卡注册表"""
    # 前置：PluginManager 已扫描 system 插件（集成环境）；单测环境手动走 Task 4 接线段
    from app.plugins.contracts.plugin_config import parse_config_schema
    from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.cards.settings.plugin_config_card import make_card_class

    reg = PluginConfigRegistry.get_instance()
    if reg.get("system") is None:
        import json as _json

        manifest = _json.loads(
            open("plugins/system/.drifox-plugin/plugin.json", encoding="utf-8").read()
        )
        schema = parse_config_schema("system", manifest.get("config_schema"))
        assert schema is not None, "system 插件 plugin.json 必须声明 config_schema"
        reg.register(schema)
        UIPluginRegistry.get_instance().register_settings_card(
            "system", "system-config", schema.title, make_card_class("system")
        )
    ui = UIPluginRegistry.get_instance()
    cards = [c for c in ui.get_settings_cards() if c.card_id == "system-config"]
    assert cards and cards[0].plugin_name == "system"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/plugins/ -x -q`
Expected: PASS 全绿

- [ ] **Step 5: 手动验收（可选但推荐）**

启动应用 → 设置面板 → LLM 设置卡右上角「插件」tab → 「网页搜索 API Key」自动卡出现（无手写 UI 代码）→ 输入 key 保存 → 重启回显；清空保存 → 回显 plugin.json 默认值。旧用户 `tools/web_search_keys.json` 首次调用自动迁移。

- [ ] **Step 6: 提交**

```bash
git add plugins/system/.drifox-plugin/plugin.json plugins/system/tools/web_tools.py tests/plugins/
git rm -f plugins/system/ui/__init__.py tests/plugins/test_websearch_api_key_config.py
git commit -m "feat(plugins): websearch 迁移 E1 配置契约（删 ~164 行手写样板，行为零变化 + 旧配置迁移）"
```

---

### Task 6: 文档同步

**Files:**
- Modify: `AGENTS.md`（工具插件化约定引用块内追加 E1 契约说明）

**Interfaces:** 无代码接口

- [ ] **Step 1: AGENTS.md 追加段落**（与 icon 自包含说明同级）：

```markdown
> **插件配置契约**（E1）：插件在 `.drifox-plugin/plugin.json` 声明 `config_schema`
> （title + fields[{key,label,type∈text|password|bool,default,env,placeholder,description}]），
> 主程序自动提供：统一存储 `<app_data_dir>/plugins/<plugin>/config.json`
> （读取三级链：环境变量→存储值→默认值）、设置面板插件分区自动配置卡
> （PluginConfigCard 声明式渲染，经 register_settings_card 扩展点注入）。
> 插件代码内读取：`PluginConfigStore().get(plugin_name, key)`。
> 需要动态默认值/复杂 UI 的插件仍可手写 Phase D 设置卡（两者不冲突，同 plugin
> 可并存：自动卡 card_id 固定为 `<plugin>-config`）。
```

- [ ] **Step 2: 提交**

```bash
git add AGENTS.md
git commit -m "docs: AGENTS.md 同步插件配置契约（E1）说明"
```

---

## 自审记录（Self-Review）

1. **Spec 覆盖**：声明式 schema（Task 1）→ 注册表（Task 2）→ 存储+迁移（Task 3）→ 自动渲染+接线（Task 4）→ websearch 迁移验收（Task 5）→ 文档（Task 6）。闭环。
2. **占位符扫描**：Task 4 Step 4 的 PluginManager 插入变量名（`plugin_name`/`manifest`）附执行对齐指令与不可删除断言；Task 5 plugin.json default 用「照抄现值」指令（避免文档复制密钥）。均为可执行说明，非 TBD。
3. **类型一致性**：`PluginConfigField(key,label,type,default,env,placeholder,description)` 在 Task 1/3/4/5 使用一致；`PluginConfigStore.get/set_values/migrate` 签名一致；`make_card_class(plugin_name) -> type` 与 `register_settings_card(..., widget_class=...)` 对齐；`_api_key(tool_ctx, name)` 签名在 Task 5 前后不变。
