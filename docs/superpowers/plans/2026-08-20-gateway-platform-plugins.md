# Gateway 平台适配器插件化（E2 / Phase E）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `gateways` 插件组件类型：平台适配器（含依赖检查、配置读写、UI 元数据、校验）全部由插件声明，6 个内置平台迁出主程序（行为零变化），第三方平台（如 Teams/Line）纯插件目录接入、零主程序改动。

**Architecture:** 复用 Phase A 运行时组件骨架（kernel 登记 + runtime_component_loader 扫描 + builtin_reloaders 热重载）：`plugins/*/gateways/*.py` 暴露 `register(registry)`，注册 `GatewayPlatformDef`（platform_id/adapter 工厂/requirements 检查/config 读写回调/校验/UI 元数据）。`PlatformManager._load_adapters`、`GatewayConfigHelper` 的 3 个 7 段 if、`gateway_setting_card` 的 PLATFORM_DEFS 全部改为查 `GatewayPlatformRegistry`。`Platform` 枚举 str-mixin 化打通第三方非枚举 id。内置 6 平台的**存量用户配置仍存主程序 Settings**（config_builder 闭包桥接读 Settings，零迁移风险）；第三方平台经 E1 `config_schema` + `PluginConfigStore` 配置。

**Tech Stack:** 现有栈零新依赖。测试套路复用：`sys.modules.pop` 强制重载 / `monkeypatch` 注入 / `inspect.getsource` 结构断言（源自 `tests/test_gateway_adapters_lazy_import.py` 与 `tests/plugins/` 既有模式）。

## Global Constraints

- 内置 6 平台迁移**行为零变化**：现有用户 Settings 配置不动、启停行为不变、日志语义不变（对齐 D038 迁移原则）
- `pytest tests/plugins/ tests/test_gateway_adapters_lazy_import.py -x -q` 全绿；每个 Task 额外跑全量 gateway 相关测试
- 平台插件文件 SDK 延迟导入纪律：模块顶层不 eager import 平台 SDK（历史教训 2026-06-16，dingtalk_stream 顶层导入曾致整个 gateway 包加载失败）
- 主程序零硬编码平台名：迁移完成后 `app/gateway/` 下不出现 `if platform == Platform.TELEGRAM` 等平台分支（`grep -n "Platform\." app/gateway/manager.py app/gateway/config.py` 仅剩 enum 定义引用与默认值）
- 提交格式 `feat|refactor|test|docs: scope - summary`；每 Task 一个独立 commit，Task 6（UI）单独可回退
- 依赖关系：Task 6 引用 E1（`docs/superpowers/plans/2026-08-20-plugin-config-contract.md`）的 PluginConfigStore/自动卡；若 E1 未执行，Task 6 的「第三方平台配置渲染」子步骤降级为仅内置平台（仍完整可交付）

## 现状硬编码清单（迁移目标面）

| # | 位置 | 内容 |
|---|---|---|
| 1 | `app/gateway/manager.py::_load_adapters`（约 L106-179） | 6 段 `if check_xxx_requirements(): adapters[Platform.X] = XxxAdapter(cfg)` |
| 2 | `app/gateway/manager.py::_start_all_async`（约 L236） | `all_platforms = [Platform.WECOM, ...]` 硬编码 7 项 |
| 3 | `app/gateway/config.py::get_platform_config`（约 L37-110） | 7 段 `if platform == Platform.X: return PlatformConfig(...)` |
| 4 | `app/gateway/config.py::set_platform_config`（约 L120-160） | 7 段写回 Settings |
| 5 | `app/gateway/config.py::is/set_platform_enabled`（约 L175-200） | 7 段启用开关 |
| 6 | `app/gateway/adapters/__init__.py` | `_try_import` 硬编码 6 平台符号表 |
| 7 | `app/gateway/adapters/{wecom,dingtalk,telegram,discord,feishu}.py` + `extra.py(slack)` | ~2560 行适配器实现（迁往插件目录） |
| 8 | `app/widgets/cards/settings/gateway_setting_card.py` | `PLATFORM_DEFS` 字典（L84-142）+ `PlatformEditCard._on_save` if-elif（L564-606）+ 校验段（L254-305） |
| 9 | `app/gateway/manager.py`（约 L254-305） | 平台硬编码 config 校验 |

## 文件结构总览

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/plugins/contracts/gateway_platform.py` | Create | `GatewayPlatformDef` frozen dataclass（见 Task 1 精确定义） |
| `app/plugins/registries/gateway_platform_registry.py` | Create | `GatewayPlatformRegistry` 单例：register/get/list_platforms/unregister_source |
| `app/gateway/base.py` | Modify | `Platform(str, Enum)` mixin 化（Task 2） |
| `app/plugins/kernel.py` | Modify | `KNOWN_COMPONENTS` / `COMPONENT_ORDER` 追加 `"gateways"`（agents 后、tools 前不必须，放 `serializers` 之后） |
| `app/plugins/loaders/runtime_component_loader.py` | Modify | `_make_gateway_loader()` + watcher + `warmup_runtime_components` 注册 `result["gateways"]` |
| `app/plugins/builtin_reloaders.py` | Modify | `_reload_gateways(ctx)` + mapping 登记 |
| `plugins/system/gateways/{telegram,wecom,dingtalk,discord,feishu,slack}.py` | Create | 各平台 Adapter 类（自 `app/gateway/adapters/` 迁入）+ `register(registry)` |
| `app/gateway/adapters/` | Delete | Task 5 完成后整目录删除（含 `extra.py` 的 Slack 段、`platforms/_http_client_limits.py` 判断去留） |
| `app/gateway/manager.py` | Modify | `_load_adapters`/`_start_all_async`/`get_adapter` 查 registry；`_adapters` key str 化 |
| `app/gateway/config.py` | Modify | 3 方法查 def.config_builder/config_writer |
| `app/gateway/__init__.py` | Modify | 导出调整（adapters 符号移除） |
| `app/widgets/cards/settings/gateway_setting_card.py` | Modify | PLATFORM_DEFS 遍历 registry 生成；_on_save 查 def.build_config_values |
| `plugins/system/.drifox-plugin/plugin.json` | Modify | `components.gateways: true` |
| `tests/plugins/gateways/`（新目录） | Create | 见各 Task |
| `AGENTS.md` | Modify | 插件约定追加 gateways 段 |

---

### Task 1: GatewayPlatformDef 契约 + 注册表（纯新增，零接线）

**Files:**
- Create: `app/plugins/contracts/gateway_platform.py`
- Create: `app/plugins/registries/gateway_platform_registry.py`
- Test: `tests/plugins/gateways/test_gateway_platform_registry.py`

**Interfaces:**
- Consumes: `app.gateway.base.BasePlatformAdapter` / `PlatformConfig`（仅类型引用，不触发 SDK 导入）
- Produces（后续所有 Task 的核心类型，签名精确到字段）:

```python
@dataclass(frozen=True)
class GatewayPlatformDef:
    platform_id: str                      # 全局唯一（= Platform 枚举 value 或第三方 str id）
    display_name: str                     # UI 显示名（"Telegram"）
    adapter_factory: Callable[[PlatformConfig], BasePlatformAdapter]
    check_requirements: Callable[[], bool] = lambda: True   # 依赖缺失 → False（跳过加载）
    config_builder: Optional[Callable[[], PlatformConfig]] = None    # 读配置（内置平台闭包读 Settings）
    config_writer: Optional[Callable[[PlatformConfig], None]] = None # 写配置（设置卡保存路径）
    build_config_values: Optional[Callable[[Dict[str, Any], Optional[PlatformConfig]], PlatformConfig]] = None
                                          # UI 编辑卡保存回调：(表单值dict, 旧配置) -> 新配置
    validate_config: Optional[Callable[[PlatformConfig], Tuple[bool, str]]] = None  # (ok, err)
    ui_order: int = 100                   # 设置卡平台排序（内置平台 10-60，第三方默认 100）
    icon_hint: str = ""                   # UI 图标提示（可空）
    source: str = ""                      # loader 代理强制 "plugin:<name>"（热重载清理）
```

```python
class GatewayPlatformRegistry:
    def register(self, platform_def) -> None            # 同 platform_id 覆盖；source 记录
    def get(self, platform_id: str) -> Optional[GatewayPlatformDef]
    def list_platforms(self) -> List[GatewayPlatformDef]  # ui_order 升序、稳定序
    def unregister_source(self, source: str) -> List[str]  # 热重载清理，返回被移除 platform_id
    @staticmethod
    def get_instance() -> "GatewayPlatformRegistry"
```

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/gateways/test_gateway_platform_registry.py
# -*- coding: utf-8 -*-
"""GatewayPlatformDef 契约 + 注册表。"""

import pytest

from app.plugins.contracts.gateway_platform import GatewayPlatformDef
from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry


def _fake_adapter(cfg):
    class _A:
        def __init__(self, config):
            self.config = config

    return _A(cfg)


def _make_def(pid="fake-pt", source="", ui_order=100):
    return GatewayPlatformDef(
        platform_id=pid,
        display_name="Fake PT",
        adapter_factory=_fake_adapter,
        source=source,
        ui_order=ui_order,
    )


class TestDefDefaults:
    def test_defaults(self):
        d = _make_def()
        assert d.check_requirements() is True
        assert d.config_builder is None
        assert d.ui_order == 100

    def test_frozen(self):
        d = _make_def()
        with pytest.raises(Exception):
            d.platform_id = "x"


class TestRegistry:
    def test_register_get_list_order(self):
        reg = GatewayPlatformRegistry()
        reg.register(_make_def("b", ui_order=20))
        reg.register(_make_def("a", ui_order=10))
        assert [d.platform_id for d in reg.list_platforms()] == ["a", "b"]
        assert reg.get("a").display_name == "Fake PT"

    def test_same_id_overwrites(self):
        reg = GatewayPlatformRegistry()
        reg.register(_make_def("x", source="plugin:p1"))
        reg.register(_make_def("x", source="plugin:p2"))  # user 覆盖 system 同 id
        assert len(reg.list_platforms()) == 1
        assert reg.get("x").source == "plugin:p2"

    def test_unregister_source(self):
        reg = GatewayPlatformRegistry()
        reg.register(_make_def("x", source="plugin:p1"))
        reg.register(_make_def("y", source="plugin:p1"))
        reg.register(_make_def("z", source="plugin:p2"))
        removed = reg.unregister_source("plugin:p1")
        assert sorted(removed) == ["x", "y"]
        assert reg.get("z") is not None
        assert reg.unregister_source("plugin:none") == []

    def test_singleton(self):
        assert (
            GatewayPlatformRegistry.get_instance() is GatewayPlatformRegistry.get_instance()
        )
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/plugins/gateways/test_gateway_platform_registry.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现契约与注册表**

```python
# app/plugins/contracts/gateway_platform.py
# -*- coding: utf-8 -*-
"""Gateway 平台插件契约（万物即插件 Phase E）。

plugins/<name>/gateways/<platform>.py 暴露 register(registry)，注册本 def。
主程序 PlatformManager / GatewayConfigHelper / gateway_setting_card 全部
查 GatewayPlatformRegistry，不再出现平台 if-elif 分支。
内置平台插件（plugins/system/gateways/）config_builder 闭包读主程序
Settings（存量用户配置零迁移）；第三方平台建议经 E1 config_schema +
PluginConfigStore 提供配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

if TYPE_CHECKING:  # 仅类型引用，不触发 SDK 导入
    from app.gateway.base import BasePlatformAdapter, PlatformConfig


@dataclass(frozen=True)
class GatewayPlatformDef:
    """一个平台适配器的完整声明"""

    platform_id: str
    display_name: str
    adapter_factory: Callable[..., Any]  # (PlatformConfig) -> BasePlatformAdapter
    check_requirements: Callable[[], bool] = lambda: True
    config_builder: Optional[Callable[[], Any]] = None
    config_writer: Optional[Callable[[Any], None]] = None
    build_config_values: Optional[Callable[[Dict[str, Any], Optional[Any]], Any]] = None
    validate_config: Optional[Callable[[Any], Tuple[bool, str]]] = None
    ui_order: int = 100
    icon_hint: str = ""
    source: str = ""
```

```python
# app/plugins/registries/gateway_platform_registry.py
# -*- coding: utf-8 -*-
"""Gateway 平台注册表 — platform_id → GatewayPlatformDef（进程级单例）。"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from app.plugins.contracts.gateway_platform import GatewayPlatformDef


class GatewayPlatformRegistry:
    def __init__(self) -> None:
        self._defs: Dict[str, GatewayPlatformDef] = {}
        self._lock = threading.Lock()

    def register(self, platform_def: GatewayPlatformDef) -> None:
        with self._lock:
            self._defs[platform_def.platform_id] = platform_def

    def get(self, platform_id: str) -> Optional[GatewayPlatformDef]:
        with self._lock:
            return self._defs.get(platform_id)

    def list_platforms(self) -> List[GatewayPlatformDef]:
        with self._lock:
            return sorted(self._defs.values(), key=lambda d: d.ui_order)

    def unregister_source(self, source: str) -> List[str]:
        with self._lock:
            removed = [pid for pid, d in self._defs.items() if d.source == source]
            for pid in removed:
                self._defs.pop(pid, None)
            return removed


def _get_instance() -> GatewayPlatformRegistry:
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is None:
            _instance = GatewayPlatformRegistry()
    return _instance


_instance: Optional[GatewayPlatformRegistry] = None
_instance_lock = threading.Lock()

GatewayPlatformRegistry.get_instance = staticmethod(_get_instance)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/plugins/gateways/test_gateway_platform_registry.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/plugins/contracts/gateway_platform.py app/plugins/registries/gateway_platform_registry.py tests/plugins/gateways/
git commit -m "feat(gateways): GatewayPlatformDef 契约与平台注册表（E2 Task1，纯新增零接线）"
```

---

### Task 2: Platform 枚举 str-mixin 化（第三方 id 通道）

**Files:**
- Modify: `app/gateway/base.py`（`class Platform(Enum)` → `class Platform(str, Enum)`，一行改动）
- Test: `tests/plugins/gateways/test_platform_str_semantics.py`

**Interfaces:**
- Consumes: 现有 `Platform`
- Produces: `Platform.WECOM == "wecom"` 为 True；`platform.value` 语义不变；dict 可用 str 与枚举混作 key（前提：hash 兼容，本 Task 测试验证）；未知 id 由消费方以 str 直传（registry 层不受枚举闭集限制）

- [ ] **Step 1: 写失败测试（先验证 Python 3.14 语义假设，假设不成立则在本 Task 内调整方案）**

```python
# tests/plugins/gateways/test_platform_str_semantics.py
# -*- coding: utf-8 -*-
"""Platform str-mixin 语义验证：第三方平台 id 打通的前提。"""

from app.gateway.base import Platform


class TestStrMixinSemantics:
    def test_value_equality(self):
        assert Platform.WECOM == "wecom"
        assert "wecom" == Platform.WECOM
        assert Platform.TELEGRAM != "telegram "  # 无空格容错

    def test_value_attribute_unchanged(self):
        assert Platform.FEISHU.value == "feishu"

    def test_dict_key_interop(self):
        # manager._adapters 等内部 dict 的 key 混用安全前提
        d = {Platform.WECOM: 1}
        assert d["wecom"] == 1
        d2 = {"slack": 2}
        assert d2[Platform.SLACK] == 2

    def test_membership(self):
        assert Platform.DINGTALK in {"dingtalk"}
        assert "feishu" in {Platform.FEISHU}

    def test_unknown_id_still_plain_str(self):
        # 第三方平台 id 无枚举成员，消费方直接以 str 使用
        third_party = "teams"
        assert third_party not in {p.value for p in Platform}
        assert Platform.WECOM != third_party
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/plugins/gateways/test_platform_str_semantics.py -v`
Expected: `test_value_equality` / `test_dict_key_interop` / `test_membership` FAIL（当前纯 Enum：`Platform.WECOM == "wecom"` 为 False）

- [ ] **Step 3: 实现（一行）**

```python
class Platform(str, Enum):
```

> 若 Step 1 的 `test_dict_key_interop` 在 mixin 后仍失败（CPython Enum 对 `__hash__` 的实现差异），则改用保底方案并在本 Task 内完成：`_adapters`/session key 等内部 dict 统一经 `_platform_key(p)`（`p.value if isinstance(p, Platform) else str(p)`）转 str —— 该辅助函数放入 `app/gateway/base.py` 并追加用例。**不允许带着红测试进入 Task 3。**

- [ ] **Step 4: 运行确认通过 + 全量 gateway 回归**

Run:
`python -m pytest tests/plugins/gateways/ tests/test_gateway_adapters_lazy_import.py -v`
`python -m pytest tests/ -x -q -k "gateway"`
Expected: PASS 全绿

- [ ] **Step 5: 提交**

```bash
git add app/gateway/base.py tests/plugins/gateways/test_platform_str_semantics.py
git commit -m "refactor(gateway): Platform 枚举 str-mixin 化，打通第三方平台 id（E2 Task2）"
```

---

### Task 3: gateways 组件类型接入插件骨架（kernel/loader/reloader）

**Files:**
- Modify: `app/plugins/kernel.py`（`KNOWN_COMPONENTS` + `COMPONENT_ORDER` 各加 `"gateways"`，置于 `"serializers"` 之后）
- Modify: `app/plugins/loaders/runtime_component_loader.py`（新增 `_make_gateway_loader` + module 级 `_gateway_watcher`，`warmup_runtime_components` 追加 `result["gateways"]`）
- Modify: `app/plugins/builtin_reloaders.py`（`RELOADED_COMPONENTS` + `_reload_gateways` + mapping）
- Modify: `plugins/system/.drifox-plugin/plugin.json`（`components` 追加 `"gateways": true`）
- Test: `tests/plugins/gateways/test_gateway_component_plumbing.py`

**Interfaces:**
- Consumes: Task 1 registry；`runtime_component_loader` 既有 `_RuntimeWatcher`/`_RegistryProxy` 机制（与 serializers 对称）
- Produces:
  - 插件目录 `gateways/*.py`（下划线前缀除外）自动扫描执行 `register(proxy)`，proxy 强制 `source="plugin:<name>"` 并施加 user>system 覆盖规则
  - `_reload_gateways(ctx: ReloadContext)`：重载 = 重新扫描该插件目录 + `unregister_source(f"plugin:{ctx.plugin_name}")` 后重注册；删除（`ctx.plugin is None`）= 仅 unregister
  - `warmup_runtime_components()` 返回 dict 含 `"gateways"` 键

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/gateways/test_gateway_component_plumbing.py
# -*- coding: utf-8 -*-
"""gateways 组件类型：kernel 登记 + loader 扫描 + reloader 行为。"""

from app.plugins import kernel


class TestKernelRegistration:
    def test_known_components_contains_gateways(self):
        assert "gateways" in kernel.KNOWN_COMPONENTS
        assert "gateways" in kernel.COMPONENT_ORDER


class TestLoaderScansGatewayPlugins:
    def test_scan_registers_def_with_source(self, tmp_path, monkeypatch):
        # 构造临时插件根：tmp/plugins/p-gw/gateways/mypt.py
        plug = tmp_path / "plugins" / "p-gw" / "gateways"
        plug.mkdir(parents=True)
        (plug / "mypt.py").write_text(
            "# -*- coding: utf-8 -*-\n"
            "from app.plugins.contracts.gateway_platform import GatewayPlatformDef\n"
            "\n"
            "\n"
            "def _factory(cfg):\n"
            "    return object()\n"
            "\n"
            "\n"
            "def register(registry):\n"
            "    registry.register(GatewayPlatformDef(\n"
            "        platform_id='mypt', display_name='My PT',\n"
            "        adapter_factory=_factory,\n"
            "    ))\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "app.plugins.loaders.runtime_component_loader._plugin_roots",
            lambda: [tmp_path / "plugins"],
        )
        from app.plugins.loaders import runtime_component_loader as rcl
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        reg = GatewayPlatformRegistry()
        try:
            loader = rcl._make_gateway_loader(reg)
            found = loader.scan_roots()
            assert "p-gw" in found or found  # 返回结构对齐其他 loader（集合/字典）
            d = reg.get("mypt")
            assert d is not None
            assert d.source == "plugin:p-gw"
        finally:
            reg.unregister_source("plugin:p-gw")
```

> `_make_gateway_loader(reg)` 的入参形态对齐 `_make_serializer_loader`（阅读其实现：可能是 module 级 watcher 工厂 + 默认 registry 单例，按实际形态调整测试注入；断言 `source` 前缀与 `scan_roots` 调用名不变）。加 `test_watcher_underscore_ignored`：`_shared.py` 不被扫描（对齐既有 loader 约定）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/plugins/gateways/test_gateway_component_plumbing.py -v`
Expected: FAIL（kernel 无 gateways / loader 无 `_make_gateway_loader`）

- [ ] **Step 3: 实现（全部为对 serializers 模式的对称复制）**

3a. `app/plugins/kernel.py`：

```python
KNOWN_COMPONENTS: Set[str] = {
    ...,
    "serializers",
    "gateways",     # ← 追加
}
COMPONENT_ORDER: tuple = (
    ...,
    "serializers",
    "gateways",     # ← 追加（重载顺序在最后：网关进程生命周期独立于会话组件）
)
```

3b. `app/plugins/loaders/runtime_component_loader.py`：仿 `_serializer_loader` / `_make_serializer_loader` / `_serializer_watcher` 三件套新增：

```python
_gateway_watcher: Optional[_RuntimeWatcher] = None


def _make_gateway_loader(registry=None):
    """gateways loader — 扫描 plugins/*/gateways/*.py 的 register(registry)。

    registry 缺省用 GatewayPlatformRegistry.get_instance()。
    proxy 的跨根覆盖（user > system）与 source 强制规则复用 _RegistryProxy
    （item_id 对应 platform_id，GatewayPlatformDef 携带 .id 属性时自动生效——
    实现 def 的 id property 别名：platform_id）。
    """
    global _gateway_watcher

    def _load_gateway_def(module, proxy):
        register_fn = getattr(module, "register", None)
        if register_fn is None:
            return False
        register_fn(proxy)
        return True

    if registry is None:
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        registry = GatewayPlatformRegistry.get_instance()
    loader = _ComponentLoader("gateways", _load_gateway_def, registry)
    _gateway_watcher = _RuntimeWatcher(loader, "gateways")
    _gateway_watcher.scan_now()
    _gateway_watcher.start()
    return _gateway_watcher
```

> ⚠️ 实现者注意：上面 `_ComponentLoader` 为示意名——**以文件内 serializers 的真实实现结构为准**（阅读 `_make_serializer_loader` 全文，逐点对称：loader 构造、proxy 注册、occupied 覆盖判定中 `getattr(item, "id", None)` 需要 `platform_id` 可被识别）。为此在 Task 1 的 `GatewayPlatformDef` 追加：

```python
    @property
    def id(self) -> str:
        """runtime loader 覆盖判定用（对齐 ModelAdapter.id 约定）"""
        return self.platform_id
```

（frozen dataclass 加 property 不影响构造。）并在 `warmup_runtime_components` 追加 `result["gateways"] = _make_gateway_loader().scan_roots()`。

3c. `app/plugins/builtin_reloaders.py`：

```python
def _reload_gateways(ctx: ReloadContext) -> Any:
    """gateways 分支：重扫该插件目录 + 清理后重注册；删除路径仅清理"""
    from app.plugins.loaders.runtime_component_loader import _make_gateway_loader
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    reg = GatewayPlatformRegistry.get_instance()
    if ctx.plugin is None:
        removed = reg.unregister_source(f"plugin:{ctx.plugin_name}")
        return len(removed) > 0
    reg.unregister_source(f"plugin:{ctx.plugin_name}")
    _make_gateway_loader().rescan_plugin(ctx.plugin_name)  # 方法名对齐 _RuntimeWatcher 实际 API
    return True
```

> `_RuntimeWatcher` 的单插件重扫方法名以实现为准（阅读后替换 `rescan_plugin`）。mapping 追加 `"gateways": _reload_gateways`。

3d. `plugins/system/.drifox-plugin/plugin.json`：`components` 追加 `"gateways": true`。

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `python -m pytest tests/plugins/ -x -q`
Expected: 全绿（若 system 插件尚无 gateways/ 目录，loader 空扫描不报错）

- [ ] **Step 5: 提交**

```bash
git add app/plugins/kernel.py app/plugins/loaders/runtime_component_loader.py app/plugins/builtin_reloaders.py plugins/system/.drifox-plugin/plugin.json tests/plugins/gateways/test_gateway_component_plumbing.py
git commit -m "feat(gateways): gateways 组件类型接入插件骨架（kernel/loader/reloader 对称 serializers，E2 Task3）"
```

---

### Task 4: 试点迁移 Telegram（并存过渡）

**Files:**
- Create: `plugins/system/gateways/telegram.py`（TelegramAdapter 类自 `app/gateway/adapters/telegram.py` 迁入 + `register()`）
- Modify: `app/gateway/manager.py`（`_load_adapters` 改为「registry 优先、未注册平台走旧 if 段」并存）
- Modify: `app/gateway/config.py`（`get_platform_config`/`set_platform_config` 对 TELEGRAM 先查 registry def）
- Delete: `app/gateway/adapters/telegram.py`（迁移完成后）
- Test: `tests/plugins/gateways/test_telegram_migration.py`

**Interfaces:**
- Consumes: Task 1/2/3 全部
- Produces: `plugins/system/gateways/telegram.py::register(registry)`（registry 为 loader proxy）；manager 的 registry 回退并存逻辑（Task 5 移除回退）

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/gateways/test_telegram_migration.py
# -*- coding: utf-8 -*-
"""Telegram 试点迁移：def 注册齐备 + manager/config 查表 + 行为等价。"""

import pytest

from app.plugins.contracts.gateway_platform import GatewayPlatformDef
from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry


@pytest.fixture()
def telegram_def_registered():
    """手动执行 system 插件 telegram 注册（集成环境由 loader 自动完成）"""
    import plugins.system.gateways.telegram as tg

    reg = GatewayPlatformRegistry.get_instance()
    tg.register(reg)
    yield reg
    reg.unregister_source("plugin:system")


class TestTelegramDef:
    def test_def_registered_with_full_callbacks(self, telegram_def_registered):
        d = telegram_def_registered.get("telegram")
        assert d is not None
        assert d.display_name == "Telegram"
        assert d.config_builder is not None
        assert d.config_writer is not None
        assert d.check_requirements is not None
        assert d.source in ("", "plugin:system")  # 手动注册时为空


class TestConfigEquivalent:
    def test_build_reads_settings(self, telegram_def_registered, monkeypatch, tmp_path):
        """config_builder 等价于旧 config.py TELEGRAM 段（读 Settings）"""
        from PyQt5.QtCore import QSettings  # noqa: F401  # 确认依赖在测试环境可用
        from app.gateway.config import GatewayConfigHelper
        from app.gateway.base import Platform, PlatformConfig

        monkeypatch.setattr(
            "app.utils.config.Settings.get_instance",
            lambda: _FakeSettings(gateway={"telegram_enabled": True, "telegram_token": "T123",
                                            "telegram_require_mention": False}),
        )
        cfg = GatewayConfigHelper.get_platform_config(Platform.TELEGRAM)
        assert isinstance(cfg, PlatformConfig)
        assert cfg.platform == Platform.TELEGRAM
        assert cfg.enabled is True
        assert cfg.token == "T123"
        assert cfg.extra["require_mention"] is False
```

> `_FakeSettings` 为本测试文件内定义的简单桩（属性访问 `.gateway_xxx.value`），实现如下，放测试文件顶部：

```python
class _FakeSettings:
    """Settings 桩：gateway_<platform>_<field> 键 → .value 访问"""

    def __init__(self, gateway: dict):
        self._gw = gateway

    def __getattr__(self, item: str):
        if item.startswith("gateway_"):
            rest = item[len("gateway_"):]  # telegram_token -> ("telegram","token")
            platform, _, field = rest.partition("_")
            val = self._gw.get(f"{platform}_{field}")
            return _Val(val)
        raise AttributeError(item)


class _Val:
    def __init__(self, v):
        self.value = v
```

> ⚠️ QSettings import 行仅在 Settings 真实实现需要 Qt 环境时保留；若 `app.utils.config` 导入无需 QApplication 即可 monkeypatch，删掉该行。以测试实际能跑为准。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/plugins/gateways/test_telegram_migration.py -v`
Expected: FAIL，`ModuleNotFoundError: plugins.system.gateways`

- [ ] **Step 3: 实现**

3a. `plugins/system/gateways/telegram.py`：**整文件迁移** `app/gateway/adapters/telegram.py` 的 `TelegramAdapter` 类与 `check_telegram_requirements`（保留原文件的延迟导入结构：SDK import 留在函数/方法内），文件头追加契约注释，尾部追加：

```python
# ── Phase E 插件注册 ────────────────────────────────────


def _build_config() -> "PlatformConfig":
    """读主程序 Settings 构造 Telegram 配置（存量用户配置零迁移）"""
    from app.gateway.base import Platform, PlatformConfig
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    return PlatformConfig(
        enabled=cfg.gateway_telegram_enabled.value,
        platform=Platform.TELEGRAM,
        token=cfg.gateway_telegram_token.value,
        extra={"require_mention": cfg.gateway_telegram_require_mention.value},
    )


def _write_config(config: "PlatformConfig") -> None:
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    cfg.set(cfg.gateway_telegram_enabled, config.enabled, save=True)
    if config.token is not None:
        cfg.set(cfg.gateway_telegram_token, config.token, save=True)


def _build_config_values(values: dict, old_config) -> "PlatformConfig":
    """设置卡保存回调：表单值 → PlatformConfig（对齐旧 _on_save TELEGRAM 分支）"""
    from app.gateway.base import Platform, PlatformConfig

    extra = dict(old_config.extra) if old_config and old_config.extra else {}
    if "require_mention" in values:
        extra["require_mention"] = bool(values["require_mention"])
    return PlatformConfig(
        enabled=bool(values.get("enabled", False)),
        platform=Platform.TELEGRAM,
        token=values.get("token") or "",
        extra=extra,
    )


def register(registry):
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    registry.register(
        GatewayPlatformDef(
            platform_id="telegram",
            display_name="Telegram",
            adapter_factory=lambda cfg: TelegramAdapter(cfg),
            check_requirements=check_telegram_requirements,
            config_builder=_build_config,
            config_writer=_write_config,
            build_config_values=_build_config_values,
            validate_config=lambda cfg: (bool(cfg.token), "Token 未配置"),
            ui_order=30,
        )
    )
```

3b. `app/gateway/adapters/telegram.py` 删除（`adapters/__init__.py` 的 `_try_import("Telegram", ...)` 返回 None——Task 5 统一清理）。

3c. `app/gateway/manager.py::_load_adapters` 头部插入 registry 路径（旧段保留作其余平台回退）：

```python
    def _load_adapters(self) -> None:
        """加载平台适配器（Phase E：registry 优先，未注册平台走内置回退段）"""
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        for d in GatewayPlatformRegistry.get_instance().list_platforms():
            try:
                if not d.check_requirements():
                    logger.info(f"[PlatformManager] {d.display_name} adapter skipped (missing dependencies)")
                    continue
                cfg = d.config_builder() if d.config_builder else PlatformConfig(platform=None)
                self._adapters[d.platform_id] = d.adapter_factory(cfg)
                logger.info(f"[PlatformManager] {d.display_name} adapter loaded (plugin)")
            except Exception as e:
                logger.warning(f"[PlatformManager] {d.display_name} 插件加载失败: {e}")

        # —— 以下旧内置段保留（尚未迁移平台的回退路径，Task 5 移除）——
        from app.gateway.adapters import (...)
        # （原有 6 段 if 中删除 TELEGRAM 段，其余保留；每段头部加：
        #   if Platform.WECOM in self._adapters 或 self._adapters.get("wecom") 已加载则跳过）
```

> 旧行为对齐细节：旧 TELEGRAM 段日志为 `"[PlatformManager] Telegram adapter loaded"`——registry 路径日志带 `(plugin)` 后缀属可接受差异（约束允许日志语义不变=级别/场景一致，文字后缀可辨来源）。

3d. `app/gateway/config.py`：`get_platform_config` / `set_platform_config` 头部插入：

```python
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        d = GatewayPlatformRegistry.get_instance().get(_platform_key(platform))
        if d is not None and d.config_builder is not None:
            return d.config_builder()   # get 路径
        if d is not None and d.config_writer is not None:
            d.config_writer(config)     # set 路径
            return
        # —— 未注册平台走下方旧 if-elif（Task 5 删除）——
```

`_platform_key(p)` = `p.value if isinstance(p, Platform) else str(p)`（放 `app/gateway/base.py`，Task 2 已建或此处补建 + 单测）。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest tests/plugins/gateways/ tests/test_gateway_adapters_lazy_import.py -v`
Expected: PASS（lazy import 测试若断言 adapters/__init__ 的 Telegram 符号——按其「缺包占位 None」断言语义，`_try_import` 失败返回 None 仍符合；如断言模块存在性则同步更新该用例为 registry 断言并注明）

- [ ] **Step 5: 提交**

```bash
git add plugins/system/gateways/ app/gateway/manager.py app/gateway/config.py tests/plugins/gateways/test_telegram_migration.py
git rm -f app/gateway/adapters/telegram.py
git commit -m "feat(gateways): Telegram 试点迁出主程序（registry 加载/配置读写全链路，行为等价，E2 Task4）"
```

---

### Task 5: 迁移其余 5 平台 + 删硬编码段（收官）

**Files:**
- Create: `plugins/system/gateways/{wecom,dingtalk,discord,feishu,slack}.py`
- Delete: `app/gateway/adapters/`（整目录：wecom.py/dingtalk.py/discord.py/feishu.py/extra.py/__init__.py/platforms/_http_client_limits.py——后者若被 feishu/dingtalk 引用则随迁至 `plugins/system/gateways/_http_client_limits.py`，下划线前缀不被 loader 扫描）
- Modify: `app/gateway/manager.py`（删旧 6 段回退与 `all_platforms` 硬编码 → registry 列表；`get_adapter`/`set_process_callback` 兼容 str/Platform 入参）
- Modify: `app/gateway/config.py`（删全部 7 段 if-elif，三方法纯 registry 分派）
- Modify: `app/gateway/__init__.py`（删除 adapters 相间接线；`create_platform_manager` 流程不变）
- Test: `tests/plugins/gateways/test_all_platforms_migration.py`

**Interfaces:**
- Consumes: Task 4 模式
- Produces: 主程序 `app/gateway/` 平台无关化完成（grep 断言）

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/gateways/test_all_platforms_migration.py
# -*- coding: utf-8 -*-
"""全部内置平台迁移收官：6 def 齐备 + 主程序零平台分支断言。"""

import inspect

BUILTIN_IDS = ["wecom", "dingtalk", "telegram", "discord", "feishu", "slack"]


def _load_all():
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    reg = GatewayPlatformRegistry.get_instance()
    import importlib

    for pid in BUILTIN_IDS:
        mod = importlib.import_module(f"plugins.system.gateways.{pid}")
        mod.register(reg)
    return reg


class TestAllDefsRegistered:
    def test_six_defs_with_full_callbacks(self):
        reg = _load_all()
        try:
            for pid in BUILTIN_IDS:
                d = reg.get(pid)
                assert d is not None, f"{pid} 未注册"
                assert d.adapter_factory is not None
                assert d.config_builder is not None
                assert d.config_writer is not None
                assert d.build_config_values is not None
        finally:
            from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry as R

            R.get_instance().unregister_source("plugin:system")


class TestNoHardcodedPlatformBranches:
    def test_manager_no_platform_if_chain(self):
        src = inspect.getsource(__import__("app.gateway.manager", fromlist=["x"]))
        for pid in BUILTIN_IDS:
            assert f"Platform.{pid.upper()}" not in src.upper().replace("PLATFORM.", "Platform."), (
                f"manager.py 仍硬编码 {pid}"
            )

    def test_config_no_platform_if_chain(self):
        src = inspect.getsource(__import__("app.gateway.config", fromlist=["x"]))
        assert "elif platform == Platform." not in src
        assert "if platform == Platform." not in src

    def test_adapters_package_deleted(self):
        import os

        assert not os.path.exists("app/gateway/adapters/__init__.py")

    def test_all_platforms_from_registry(self):
        """manager 启动列表 = registry（行为等价：6 内置平台全在）"""
        reg = _load_all()
        try:
            ids = {d.platform_id for d in reg.list_platforms()}
            assert set(BUILTIN_IDS) <= ids
        finally:
            from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry as R

            R.get_instance().unregister_source("plugin:system")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/plugins/gateways/test_all_platforms_migration.py -v`
Expected: FAIL（5 平台模块不存在 / 硬编码段仍在）

- [ ] **Step 3: 实现五平台迁移（每平台 = 类迁移 + register，模式与 Task 4 完全同构，差异点如下精确列出）**

各平台 register 的差异参数（`register` 函数体模板同 Task 4 的 telegram，逐平台替换以下值）：

| 平台 | platform_id | ui_order | config 字段来源（Settings 键 → PlatformConfig 字段） | validate |
|---|---|---|---|---|
| wecom | "wecom" | 10 | `gateway_wecom_enabled`→enabled；`gateway_wecom_bot_id`→bot_id；`gateway_wecom_secret`→secret；`gateway_wecom_websocket_url`→websocket_url | `(bool(cfg.bot_id and cfg.secret), "BotID/Secret 未配置")` |
| dingtalk | "dingtalk" | 20 | `gateway_dingtalk_enabled`→enabled；`gateway_dingtalk_client_id`→client_id；`gateway_dingtalk_client_secret`→client_secret | `(bool(cfg.client_id and cfg.client_secret), "ClientID/Secret 未配置")` |
| discord | "discord" | 40 | `gateway_discord_enabled`→enabled；`gateway_discord_token`→token；`gateway_discord_require_mention`→extra["require_mention"] | `(bool(cfg.token), "Token 未配置")` |
| feishu | "feishu" | 50 | `gateway_feishu_enabled`→enabled；`gateway_feishu_app_id`→extra["app_id"]；`gateway_feishu_app_secret`→extra["app_secret"] | `(bool(cfg.extra.get("app_id") and cfg.extra.get("app_secret")), "AppID/Secret 未配置")` |
| slack | "slack" | 60 | `gateway_slack_enabled`→enabled；`gateway_slack_bot_token`→extra["bot_token"]；`gateway_slack_app_token`→extra["app_token"] | `(bool(cfg.extra.get("bot_token")), "Bot Token 未配置")` |

各平台类迁移来源与注意事项：

- **wecom**：类 `WeComAdapter` + `check_wecom_requirements` 自 `adapters/wecom.py` 迁入；display_name "企业微信"
- **dingtalk**：`DingTalkAdapter` + `check_dingtalk_requirements` 自 `adapters/dingtalk.py`；**保留 dingtalk_stream SDK 的 monkey-patch 修复与延迟导入**；display_name "钉钉"
- **discord**：`DiscordAdapter` + `check_discord_requirements` 自 `adapters/discord.py`；display_name "Discord"
- **feishu**：`FeishuAdapter` + `check_feishu_requirements` 自 `adapters/feishu.py`；若引用 `platforms/_http_client_limits.py` 则该文件迁至 `plugins/system/gateways/_http_client_limits.py` 并改 import；display_name "飞书"
- **slack**：`SlackAdapter` 自 `adapters/extra.py` 迁出（extra.py 中被注释的 WhatsApp 代码**不迁移**，直接丢弃；如需 WhatsApp 由社区插件重新实现）；check_requirements 内联 `lambda: True`（现状无依赖检查）；display_name "Slack"

5b. `app/gateway/manager.py`：删除 `_load_adapters` 的全部旧 if 段；`_start_all_async` 的 `all_platforms` 硬编码列表改为：

```python
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        all_platforms = [
            d.platform_id for d in GatewayPlatformRegistry.get_instance().list_platforms()
        ]
```

`get_adapter` / `set_process_callback` / `_adapters` 的 key 统一 str（`_platform_key` 转换入参）。启动循环内 `Platform(platform)` 构造调用改为直接使用 str id（registry `d.platform_id`），涉及 `is_platform_enabled(platform)` 等调用的入参同步兼容。

5c. `app/gateway/config.py`：三方法删除全部 `if/elif platform == Platform.X` 段，统一：

```python
    @staticmethod
    def get_platform_config(platform) -> "PlatformConfig":
        from app.gateway.base import PlatformConfig, _platform_key
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        d = GatewayPlatformRegistry.get_instance().get(_platform_key(platform))
        if d is not None and d.config_builder is not None:
            return d.config_builder()
        return PlatformConfig(enabled=False, platform=platform)
```

`set_platform_config` / `is_platform_enabled` / `set_platform_enabled` 同构（writer 回调缺失时记 warning 返回）。**注意**：`is/set_platform_enabled` 的旧行为是读 Settings enabled 键——内置平台 def 的 builder/writer 已覆盖该路径；为行为等价，enabled 开关读写也走 `config_builder().enabled` / `config_writer(config with enabled=...)`（在 def 层补 enabled 单独读写实现：`_write_config` 已写 enabled 键，`is_platform_enabled` 实现 = `d.config_builder().enabled`）。

5d. 删除 `app/gateway/adapters/` 整目录；`app/gateway/__init__.py` 移除 `from app.gateway.adapters ...` 相关行（当前无直接 adapters 导出，确认后仅删 adapters/__init__.py）；grep 全仓 `app.gateway.adapters` 引用（测试/文档）同步更新为 `plugins.system.gateways`。

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run:
`python -m pytest tests/plugins/ -x -q`
`python -m pytest tests/ -q -k "gateway"`
`ruff check . && ruff format --check .`
Expected: 全绿；`grep -rn "app.gateway.adapters" --include="*.py" app/ tests/` 仅剩历史注释或零命中

- [ ] **Step 5: 提交**

```bash
git add plugins/system/gateways/ app/gateway/ tests/plugins/gateways/test_all_platforms_migration.py
git rm -rf app/gateway/adapters/
git commit -m "refactor(gateways): 其余 5 平台迁出主程序，manager/config 零平台分支（E2 Task5 收官）"
```

---

### Task 6: 设置卡 registry 驱动（UI 收口，独立可回退）

**Files:**
- Modify: `app/widgets/cards/settings/gateway_setting_card.py`（`PLATFORM_DEFS` 改遍历 registry 生成；`PlatformEditCard._on_save` 的 if-elif（L564-606）改调 `def.build_config_values(values, old_config)`；L254-305 平台硬编码校验改调 `def.validate_config`；保存路径 `GatewayConfigHelper.set_platform_config` 不变——内部已是 registry 分派）
- Test: `tests/plugins/gateways/test_setting_card_registry_driven.py`

**Interfaces:**
- Consumes: Task 1 `GatewayPlatformDef.{display_name, ui_order, build_config_values, validate_config, icon_hint}`；`GatewayConfigHelper.set_platform_config`
- Produces: 设置卡平台分区 = registry `list_platforms()`（ui_order 序）；新增第三方平台 def 后设置卡自动出现（无需改 UI 代码）

- [ ] **Step 1: 写失败测试**

```python
# tests/plugins/gateways/test_setting_card_registry_driven.py
# -*- coding: utf-8 -*-
"""设置卡 registry 驱动：第三方 def 自动渲染 + 保存走 build_config_values。"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def third_party_def():
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    from app.gateway.base import PlatformConfig

    def _build():
        return PlatformConfig(enabled=True, platform="teams-x", token="tt",
                              extra={"app_id": "a1"})

    def _build_values(values, old):
        return PlatformConfig(enabled=bool(values.get("enabled")),
                              platform="teams-x", token=values.get("token", ""))

    reg = GatewayPlatformRegistry.get_instance()
    reg.register(
        GatewayPlatformDef(
            platform_id="teams-x", display_name="Teams X",
            adapter_factory=lambda cfg: object(),
            config_builder=_build, config_writer=lambda c: None,
            build_config_values=_build_values,
            validate_config=lambda cfg: (bool(cfg.token), "token empty"),
            ui_order=5, source="plugin:test",
        )
    )
    yield reg
    reg.unregister_source("plugin:test")


def test_platform_defs_generated_from_registry(qapp, third_party_def, qtbot):  # qtbot 可选
    import app.widgets.cards.settings.gateway_setting_card as card_mod

    defs = card_mod._build_platform_defs_from_registry()
    ids = [d["platform_id"] for d in defs]
    assert "teams-x" in ids
    assert ids.index("teams-x") < ids.index("wecom")  # ui_order=5 排最前


def test_save_dispatches_to_def_callback(qapp, third_party_def, monkeypatch):
    """保存：值经 build_config_values → set_platform_config（不再 if-elif）"""
    import app.widgets.cards.settings.gateway_setting_card as card_mod

    captured = {}

    def _fake_set(platform, config):
        captured["platform"] = platform
        captured["config"] = config

    monkeypatch.setattr(card_mod, "GatewayConfigHelper", type("F", (), {"set_platform_config": staticmethod(_fake_set)}))
    # 构造编辑卡并触发保存（按类实际构造签名；本用例最小面：直接调模块级分发函数）
    cfg = card_mod._save_platform_values("teams-x", {"enabled": True, "token": "tok-9"}, None)
    assert cfg.token == "tok-9"
    assert captured["config"].token == "tok-9"


def test_validate_dispatches(qapp, third_party_def):
    import app.widgets.cards.settings.gateway_setting_card as card_mod
    from app.gateway.base import PlatformConfig

    ok, err = card_mod._validate_platform_config("teams-x", PlatformConfig(platform="teams-x", token=""))
    assert ok is False and "token" in err.lower() or err == "token empty"
```

> 模块级辅助 `_build_platform_defs_from_registry` / `_save_platform_values(platform_id, values, old_config)` / `_validate_platform_config(platform_id, cfg)` 为本 Task 在 gateway_setting_card.py 新增的三个纯函数（UI 类改为调用它们，测试直测它们——避免重型 QWidget 构造依赖）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/plugins/gateways/test_setting_card_registry_driven.py -v`
Expected: FAIL，`AttributeError: ... _build_platform_defs_from_registry`

- [ ] **Step 3: 实现**

3a. 三个模块级纯函数（gateway_setting_card.py 顶部，替换原 `PLATFORM_DEFS` 常量；保留常量名作别名 `PLATFORM_DEFS = _build_platform_defs_from_registry()` 以兼容文件内其他引用，若全文件仅一处消费则直接替换）：

```python
def _build_platform_defs_from_registry() -> list:
    """由 GatewayPlatformRegistry 生成平台定义清单（替代硬编码 PLATFORM_DEFS）。

    每项 dict：platform_id/display_name/fields（编辑卡表单字段）/icon（icon_hint 或默认）。
    fields 描述沿用现有编辑卡结构（key/label/type: text|password|bool）。
    内置平台字段映射与迁移前 PLATFORM_DEFS 一致：
      wecom: bot_id/secret/websocket_url(text)
      dingtalk: client_id/client_secret(text)
      telegram: token(text) + require_mention(bool)
      discord: token(text) + require_mention(bool)
      feishu: app_id/app_secret(text, 映射 extra)
      slack: bot_token/app_token(text, 映射 extra)
    第三方平台：E1 config_schema 存在 → fields 由 schema 生成；
    否则 fields=[]（仅显示开关行）。enabled 字段所有平台自动含。
    """
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    _BUILTIN_FIELDS = {
        "wecom": [
            {"key": "bot_id", "label": "Bot ID", "type": "text"},
            {"key": "secret", "label": "Secret", "type": "password"},
            {"key": "websocket_url", "label": "WebSocket URL", "type": "text"},
        ],
        "dingtalk": [
            {"key": "client_id", "label": "Client ID", "type": "text"},
            {"key": "client_secret", "label": "Client Secret", "type": "password"},
        ],
        "telegram": [
            {"key": "token", "label": "Bot Token", "type": "password"},
            {"key": "require_mention", "label": "需要 @机器人", "type": "bool"},
        ],
        "discord": [
            {"key": "token", "label": "Bot Token", "type": "password"},
            {"key": "require_mention", "label": "需要 @机器人", "type": "bool"},
        ],
        "feishu": [
            {"key": "app_id", "label": "App ID", "type": "text"},
            {"key": "app_secret", "label": "App Secret", "type": "password"},
        ],
        "slack": [
            {"key": "bot_token", "label": "Bot Token", "type": "password"},
            {"key": "app_token", "label": "App Token", "type": "password"},
        ],
    }
    from app.plugins.contracts.plugin_config import parse_config_schema  # E1（可选依赖）
    from app.plugins.registries.plugin_config_registry import PluginConfigRegistry  # E1

    defs = []
    for d in GatewayPlatformRegistry.get_instance().list_platforms():
        fields = list(_BUILTIN_FIELDS.get(d.platform_id, []))
        if not fields:
            schema = PluginConfigRegistry.get_instance().get(d.source.split(":", 1)[-1]) if d.source.startswith("plugin:") else None
            if schema:
                fields = [
                    {"key": f.key, "label": f.label, "type": f.type}
                    for f in schema.fields
                ]
        defs.append(
            {
                "platform_id": d.platform_id,
                "display_name": d.display_name,
                "icon": d.icon_hint or None,
                "fields": fields,
            }
        )
    return defs


def _save_platform_values(platform_id: str, values: dict, old_config):
    """保存分发：def.build_config_values 构造 → set_platform_config 落盘，返回新配置"""
    from app.gateway.config import GatewayConfigHelper
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    d = GatewayPlatformRegistry.get_instance().get(platform_id)
    if d is None or d.build_config_values is None:
        return None
    config = d.build_config_values(values, old_config)
    GatewayConfigHelper.set_platform_config(platform_id, config)
    return config


def _validate_platform_config(platform_id: str, config):
    """校验分发：def.validate_config 缺省恒真"""
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    d = GatewayPlatformRegistry.get_instance().get(platform_id)
    if d is None or d.validate_config is None:
        return True, ""
    return d.validate_config(config)
```

3b. `PlatformEditCard._on_save`（L564-606 的 if-elif 链）替换为：收集表单 values dict（按 fields key）→ `cfg = _save_platform_values(self._platform_id, values, self._config)` → 失败提示沿用现有 MessageBox 模式。L254-305 校验段替换为 `ok, err = _validate_platform_config(...)`。

> E1 未执行时的降级：`_build_platform_defs_from_registry` 中 E1 import 失败（ModuleNotFoundError）→ 捕获置 `schema=None`（第三方 fields 空）。用 try/except ImportError 包住两个 E1 import。

- [ ] **Step 4: 运行确认通过 + UI 回归**

Run: `python -m pytest tests/plugins/gateways/ tests/plugins/ -x -q`
Expected: 全绿。手动验收（推荐）：设置 → Gateway 卡 → 6 内置平台分区渲染与迁移前一致 → 编辑 Telegram token 保存 → 重启回显。

- [ ] **Step 5: 提交**

```bash
git add app/widgets/cards/settings/gateway_setting_card.py tests/plugins/gateways/test_setting_card_registry_driven.py
git commit -m "feat(gateways): 设置卡 registry 驱动（第三方平台自动渲染，删 UI 硬编码段，E2 Task6）"
```

---

### Task 7: E2E 验收 — 第三方平台纯插件接入

**Files:**
- Test: `tests/plugins/gateways/test_e2e_third_party_platform.py`

**Interfaces:**
- Consumes: Task 3 loader/watcher；Task 5 manager registry 分派
- Produces: E2E 证明（写临时插件目录 → loader 扫描 → registry 可见 → manager 可构造 adapter → config 走 def 回调）

- [ ] **Step 1: 写 E2E 测试（一次成型，跑绿即验收）**

```python
# tests/plugins/gateways/test_e2e_third_party_platform.py
# -*- coding: utf-8 -*-
"""E2E：第三方平台纯插件目录接入（零主程序改动证明）。

链路：临时 user 插件目录 gateways/xxx.py → loader 扫描（含 user>system 覆盖规则）
→ registry 注册（source=plugin:xxx）→ manager _load_adapters 拾取 →
config_builder 提供配置。
"""

import pytest


@pytest.fixture()
def third_party_plugin(tmp_path, monkeypatch):
    plug = tmp_path / "plugins" / "pt-awesome" / "gateways"
    plug.mkdir(parents=True)
    (plug / "pt_awesome.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "from app.plugins.contracts.gateway_platform import GatewayPlatformDef\n"
        "from app.gateway.base import PlatformConfig\n"
        "\n"
        "class PtAwesomeAdapter:\n"
        "    platform = 'pt-awesome'\n"
        "    def __init__(self, config):\n"
        "        self.config = config\n"
        "\n"
        "def register(registry):\n"
        "    registry.register(GatewayPlatformDef(\n"
        "        platform_id='pt-awesome', display_name='PT Awesome',\n"
        "        adapter_factory=lambda cfg: PtAwesomeAdapter(cfg),\n"
        "        config_builder=lambda: PlatformConfig(enabled=True, platform='pt-awesome',\n"
        "                                               token='e2e-token'),\n"
        "    ))\n",
        encoding="utf-8",
    )
    (tmp_path / "plugins" / "pt-awesome" / ".drifox-plugin").mkdir()
    (tmp_path / "plugins" / "pt-awesome" / ".drifox-plugin" / "plugin.json").write_text(
        '{"name": "pt-awesome", "version": "1.0.0", "components": {"gateways": true}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.plugins.loaders.runtime_component_loader._plugin_roots",
        lambda: [tmp_path / "plugins"],
    )
    yield tmp_path
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    GatewayPlatformRegistry.get_instance().unregister_source("plugin:pt-awesome")


def test_third_party_full_chain(third_party_plugin):
    from app.plugins.loaders.runtime_component_loader import _make_gateway_loader
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    reg = GatewayPlatformRegistry.get_instance()
    _make_gateway_loader(reg)
    d = reg.get("pt-awesome")
    assert d is not None and d.source == "plugin:pt-awesome"

    # manager 拾取（不真启动事件循环，仅加载适配器表）
    from app.gateway.config import GatewayConfigHelper
    from app.gateway.manager import PlatformManager

    mgr = PlatformManager.__new__(PlatformManager)  # 绕过 __init__ 的线程/loop 依赖
    mgr._adapters = {}
    mgr._config = GatewayConfigHelper
    PlatformManager._load_adapters(mgr)
    assert "pt-awesome" in mgr._adapters
    assert mgr._adapters["pt-awesome"].config.token == "e2e-token"


def test_user_overrides_system_same_id(third_party_plugin, tmp_path):
    """user 根同 platform_id 覆盖 system 根（对齐 provider/runtime loader 规则）"""
    from app.plugins.loaders.runtime_component_loader import _make_gateway_loader
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    reg = GatewayPlatformRegistry.get_instance()
    _make_gateway_loader(reg)  # user 根扫描（_plugin_roots 已 monkeypatch 为 user 模拟）
    # 同根二次注册同 id 的行为已在 Task 1 单测覆盖；跨根覆盖由 loader proxy 规则保证，
    # 此处仅断言扫描后 def 存在且 source 正确（跨根规则在 loader 层有独立单测）
    assert reg.get("pt-awesome") is not None
```

> `PlatformManager.__new__` 绕构造是测试技巧；若 `_load_adapters` 还依赖其他实例属性（读源码确认），在 fixture 内补齐（`mgr._config` 等已有）。`_make_gateway_loader` 的 watcher 全局单例重复创建：loader 内部幂等处理或测试用 `importlib.reload`——按 Task 3 实际实现调整，断言不变。

- [ ] **Step 2: 运行跑绿（E2E 不要求先红——依赖 Task 3/5 已交付的机制；若失败即机制缺陷，修复到绿）**

Run: `python -m pytest tests/plugins/gateways/test_e2e_third_party_platform.py -v`
Expected: PASS

- [ ] **Step 3: 全量回归 + 提交**

Run: `python -m pytest tests/ -q -k "gateway or plugin"`
Expected: 全绿

```bash
git add tests/plugins/gateways/test_e2e_third_party_platform.py
git commit -m "test(gateways): 第三方平台纯插件接入 E2E 验收（零主程序改动，E2 Task7）"
```

---

### Task 8: 文档同步

**Files:**
- Modify: `AGENTS.md`（运行时组件插件化约定段后追加 gateways 段）
- Modify: `plugins/system/gateways/README.md`（Create，说明 6 平台 def 与新增平台方法）

- [ ] **Step 1: AGENTS.md 追加**：

```markdown
> **Gateway 平台插件**（Phase E）：`plugins/<name>/gateways/*.py` 暴露
> `register(registry)`，注册 `GatewayPlatformDef`（platform_id/adapter 工厂/
> check_requirements/config_builder|config_writer/build_config_values/
> validate_config/ui_order）。主程序 `PlatformManager`、`GatewayConfigHelper`、
> Gateway 设置卡全部查 `GatewayPlatformRegistry`，零平台 if 分支。
> 内置 6 平台（wecom/dingtalk/telegram/discord/feishu/slack）位于
> `plugins/system/gateways/`，配置仍读写主程序 Settings（闭包桥接，存量零迁移）。
> 第三方平台：platform_id 为任意 str（`Platform` 已 str-mixin 化），配置建议经
> E1 `config_schema` 声明 + `PluginConfigStore` 读取，设置卡自动渲染字段。
> SDK 依赖必须函数内延迟导入（历史教训：顶层 eager import 曾致 gateway 包加载失败）。
```

- [ ] **Step 2: `plugins/system/gateways/README.md`**：6 平台清单表（id/display/依赖/配置字段）+ 「新增平台三步」：①建 `gateways/<id>.py` 实现 Adapter + register ②plugin.json 声明 `components.gateways`（+ 可选 E1 config_schema）③重启或热重载生效。

- [ ] **Step 3: 提交**

```bash
git add AGENTS.md plugins/system/gateways/README.md
git commit -m "docs: AGENTS.md + gateways README 同步 Gateway 平台插件化（E2）"
```

---

## 自审记录（Self-Review）

1. **Spec 覆盖**：硬编码清单 1/2/3/4/5/6（Task 4+5）、7（Task 4/5 迁移）、8/9（Task 6）全覆盖；第三方接入通道（Task 2 str-mixin + Task 7 E2E）覆盖；文档（Task 8）。E2 依赖 E1 处仅在 Task 6 第三方字段渲染（已给 ImportError 降级路径）。
2. **占位符扫描**：Task 3 的 `_ComponentLoader`/`rescan_plugin` 为「以文件内 serializers 真实结构为准」的执行对齐指令（附断言不变原则）；Task 5 平台迁移用差异参数表 + 逐平台来源/注意事项（非 "similar to Task N" 空引用；register 模板在 Task 4 给了全文）。无 TBD。
3. **类型一致性**：`GatewayPlatformDef` 字段集在 Task 1 定义、Task 3 加 `id` property、Task 4/5/6/7 使用一致；`_platform_key` Task 2 定义 Task 4/5 使用；registry 方法名 `register/get/list_platforms/unregister_source` 全计划一致；`_make_gateway_loader` Task 3 定义、Task 7 复用。
4. **风险与回退**：Task 4/5 存量配置零迁移（builder 闭包读 Settings）；Task 2 若 hash 语义不达预期有 `_platform_key` 保底方案；Task 6 独立 commit 可单独 revert。
