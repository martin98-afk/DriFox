# -*- coding: utf-8 -*-
"""
服务商注册表（ProviderRegistry）— 服务商插件化的数据与逻辑中枢。

「万物为插件」理念下，所有服务商信息（icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力 / 用量查询额外字段 / 余额查询 / 套餐用量查询）
全部由 providers 插件声明，主程序不再硬编码任何服务商。

插件约定（与 tools 插件对称）：
- 文件位置：`plugins/<name>/providers/<provider>.py`
- 每个文件暴露 `register(registry)` 函数，内部调用 registry.register(ProviderDef(...))
  或 registry.define(name=..., icon=..., api_url=..., ...)
- 由 app/core/provider_loader.py 负责扫描 + 热重载（本模块只持有注册数据）

聚合视图（替代原 constants.py 各硬编码常量）：
- provider_models()            ← PROVIDER_MODELS
- icon_map()                   ← PROVIDER_ICONS
- default_config(name)         ← FREE_PROVIDERS[name]
- quota_exclude_keys()         ← QUOTA_EXCLUDE_KEYS
- models_dev_map()             ← MODELS_DEV_PROVIDER_MAP
- family_capabilities(family)  ← PROVIDER_CAPABILITIES[family]
- get_merged_provider_models() ← constants.get_merged_provider_models（含 models.dev 合并）
- balance_fetch(name, config)  ← BALANCE_APIS 请求+解析逻辑
- coding_plan_fetch(name, ...) ← coding_plan_fetcher 注册表
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

# ============================================================
# 数据类
# ============================================================


@dataclass
class QuotaField:
    """套餐用量查询额外配置字段（仅用于用量查询，不进入模型参数/API 请求）。

    对应服务商配置中除 API_KEY 外的附加凭据，
    如 OpenCode 的 server_id/cookie/workspace_id、火山的 csrf_token 等。
    """

    key: str  # 存储到服务商配置的字段名（如 "server_id"）
    label: str  # UI 显示名（如 "Server ID:"）
    placeholder: str = ""  # 输入框占位提示


@dataclass
class ProviderDef:
    """一个服务商插件的完整定义（数据 + 可选查询逻辑）"""

    name: str  # 服务商唯一名（历史 FREE_PROVIDERS key，如 "DeepSeek"）
    icon: str = ""  # 图标 key（历史 PROVIDER_ICONS 值；亦为插件 icons/ 目录下的文件名）
    icon_dir: str = ""  # 插件自带深色图标目录（绝对路径；空 → 渲染回退主程序 qrc 资源）
    icon_dir_light: str = ""  # 插件自带浅色图标目录（主题感知；空 → 回退深色/qrc）
    api_url: str = ""  # 默认 API URL
    auth_type: str = "bearer"  # 认证方式：bearer / bce / none / anthropic
    default_model: str = ""  # 默认模型名（"模型名称"）
    default_params: Dict[str, Any] = field(default_factory=dict)  # 其他默认参数（温度/最大Token/思考模式…）
    register_url: str = ""  # 获取 API Key 的地址（"获取地址"）
    models: List[str] = field(default_factory=list)  # 模型列表（PROVIDER_MODELS）
    models_dev_id: str = ""  # models.dev provider id（MODELS_DEV_PROVIDER_MAP 值）
    family: str = ""  # 能力族（detect_provider_family 返回值，如 "deepseek"）
    capabilities: Dict[str, Any] = field(default_factory=dict)  # family 能力覆盖（PROVIDER_CAPABILITIES 条目）
    extra_quota_fields: List[QuotaField] = field(default_factory=list)  # 用量查询额外字段
    # 余额查询 fetcher：签名 (config: dict) -> {"balance":float,"currency":str,"provider":str}
    #   或 {"hide":True,"provider":str,"tooltip":str|省略} | None
    balance_fetcher: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None
    # 套餐用量 fetcher：签名 (config: dict) -> {"rolling":...,"weekly":...,"monthly":...} | None
    coding_plan_fetcher: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None

    # ── 便捷属性 ────────────────────────────────────────────

    @property
    def source(self) -> str:
        """注册来源标记（loader 注入，用于热重载清理）"""
        return self._source or ""

    @source.setter
    def source(self, value: str):
        self._source = value

    def to_default_config(self) -> Dict[str, Any]:
        """转成历史 FREE_PROVIDERS[name] 风格的默认配置 dict。

        free 配置注入（config.py _ensure_default_opencode_provider）等
        依赖此形态，保证兼容。
        """
        cfg: Dict[str, Any] = {
            "API_URL": self.api_url,
            "API_KEY": "",
            "模型名称": self.default_model,
            "认证方式": self.auth_type,
        }
        cfg.update(self.default_params)
        if self.register_url:
            cfg["获取地址"] = self.register_url
        return cfg


# ============================================================
# 通用 fetcher 工厂
# ============================================================

# 余额响应解析的常见路径模板："auto" 表示按层级自动尝试
_AUTO_KEYS = ("balance", "total_balance", "totalBalance", "balance_amount", "usage")


def make_bearer_balance_fetcher(
    url: str,
    balance_key: str,
    currency: str = "¥",
) -> Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """生成"Authorization: Bearer <API_KEY>" 型余额查询 fetcher。

    Args:
        url: 余额查询接口
        balance_key: 余额字段名（优先层：data.balance_infos[0][key] → data.data[key] → data[key]）
        currency: 货币符号
    """

    def _fetch(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        import requests

        api_key = (config.get("API_KEY", "") or "").strip()
        if not api_key:
            return None
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        except Exception:
            return {"hide": True}
        if resp.status_code != 200:
            return {"hide": True, "tooltip": f"余额查询失败 (HTTP {resp.status_code})"}
        try:
            data = resp.json()
        except Exception:
            return {"hide": True, "tooltip": "余额查询异常: 响应不是 JSON"}
        result = _extract_balance_value(data, balance_key)
        if result is None:
            return {"hide": True}
        return {"balance": float(result), "currency": currency}

    return _fetch


def _extract_balance_value(data: Any, key: str) -> Optional[Any]:
    """按常见响应层级提取余额字段值。

    依次尝试：
    1. data.balance_infos[0][key]   （DeepSeek 形态）
    2. data.data[key]               （SiliconFlow 形态）
    3. data[key]
    """
    if isinstance(data, dict):
        infos = data.get("balance_infos")
        if isinstance(infos, list) and infos and isinstance(infos[0], dict):
            v = infos[0].get(key)
            if v not in (None, ""):
                return v
        sub = data.get("data")
        if isinstance(sub, dict):
            v = sub.get(key)
            if v not in (None, ""):
                return v
        v = data.get(key)
        if v not in (None, ""):
            return v
    return None


# ============================================================
# 注册表（单例）
# ============================================================


class ProviderRegistry:
    """服务商注册表（全局单例，风格对齐 PluginManager.get_instance）"""

    _instance: Optional["ProviderRegistry"] = None

    @classmethod
    def get_instance(cls) -> "ProviderRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._providers: Dict[str, ProviderDef] = {}
        self._lock = threading.RLock()
        self._warmup_done = False
        # 内置 family 兜底（仅用于没有任何插件声明该 family 时，如自定义服务商）。
        # custom 不是"服务商"，而是通用默认能力，不属于可迁移的服务商硬编码。
        self._builtin_family_capabilities: Dict[str, Dict[str, Any]] = {
            "custom": {
                "context_limit": 200000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": False,
                "supports_thinking": False,
                "thinking_param": None,
            }
        }

    # ── 懒加载预热 ─────────────────────────────────────────

    def ensure_loaded(self) -> None:
        """确保服务商插件已加载（幂等）。

        启动早期（Settings.get_instance → _ensure_default_opencode_provider）
        先于 backend 的 warmup_providers 调用，故任何读取入口都需先走本方法
        触发一次插件扫描；backend 的显式 warmup 也会被本标志去重。
        """
        if self._warmup_done:
            return
        with self._lock:
            if self._warmup_done:
                return
            self._warmup_done = True
        try:
            from app.plugins.loaders.provider_loader import warmup_providers

            warmup_providers()
        except Exception as e:
            logger.warning(f"[ProviderRegistry] 服务商插件懒加载失败: {e}")
            with self._lock:
                self._warmup_done = False  # 失败允许下次重试

    # ── 注册 / 查询 ─────────────────────────────────────────

    def register(self, provider: ProviderDef, source: str = "") -> bool:
        """注册一个服务商定义，返回是否注册成功。

        同名服务商已注册时不覆盖（先注册者优先，与 tools 的同名保护一致）。
        热重载场景由 loader 先 clear_source 卸载旧来源再重新注册。
        """
        with self._lock:
            if provider.name in self._providers:
                logger.warning(
                    f"[ProviderRegistry] 服务商「{provider.name}」已注册，跳过重复注册 (source={source})"
                )
                return False
            provider.source = source
            self._providers[provider.name] = provider
            logger.debug(f"[ProviderRegistry] 已注册服务商: {provider.name} (source={source})")
            return True

    def clear_source(self, source: str) -> None:
        """卸载指定来源注册的全部服务商（热重载时清理旧版本）"""
        with self._lock:
            for name in [n for n, p in self._providers.items() if p.source == source]:
                del self._providers[name]
                logger.debug(f"[ProviderRegistry] 已卸载服务商: {name} (source={source})")

    def provider_sources(self) -> List[str]:
        """全部插件来源标记（去重排序，热重载全量重建时逐个清理用）"""
        with self._lock:
            return sorted({p.source for p in self._providers.values() if p.source.startswith("plugin:")})

    def get(self, name: str) -> Optional[ProviderDef]:
        self.ensure_loaded()
        with self._lock:
            return self._providers.get(name)

    def names(self) -> List[str]:
        self.ensure_loaded()
        with self._lock:
            return sorted(self._providers.keys())

    def all(self) -> List[ProviderDef]:
        self.ensure_loaded()
        with self._lock:
            return list(self._providers.values())

    # ── 聚合视图（替代 constants 硬编码）────────────────────

    def provider_models(self) -> Dict[str, List[str]]:
        """所有服务商的模型列表（PROVIDER_MODELS 替代）"""
        with self._lock:
            return {name: list(p.models) for name, p in self._providers.items() if p.models}

    def icon_map(self) -> Dict[str, str]:
        """服务商 → 图标 key（PROVIDER_ICONS 替代）"""
        with self._lock:
            return {name: p.icon for name, p in self._providers.items() if p.icon}

    def get_icon_dir(self, name: str) -> str:
        """服务商插件的深色图标目录（绝对路径），无则返回空串"""
        p = self.get(name)
        return p.icon_dir if p else ""

    def get_icon_dir_light(self, name: str) -> str:
        """服务商插件的浅色图标目录（绝对路径），无则返回空串"""
        p = self.get(name)
        return p.icon_dir_light if p else ""

    def default_config(self, name: str) -> Optional[Dict[str, Any]]:
        """服务商默认配置 dict（FREE_PROVIDERS[name] 替代），不存在返回 None"""
        p = self.get(name)
        return p.to_default_config() if p else None

    def quota_exclude_keys(self) -> "frozenset[str]":
        """全部服务商用量查询额外字段 key 集合（QUOTA_EXCLUDE_KEYS 替代）"""
        keys = set()
        with self._lock:
            for p in self._providers.values():
                for f in p.extra_quota_fields:
                    keys.add(f.key)
        return frozenset(keys)

    def models_dev_map(self) -> Dict[str, str]:
        """服务商 → models.dev provider id（MODELS_DEV_PROVIDER_MAP 替代）"""
        with self._lock:
            return {name: p.models_dev_id for name, p in self._providers.items() if p.models_dev_id}

    def family_capabilities(self, family: str) -> Dict[str, Any]:
        """family 能力（PROVIDER_CAPABILITIES 替代）。

        聚合所有声明该 family 的服务商能力的并集（后注册覆盖同名字段）；
        无任何插件声明时回退内置 custom 兜底。
        """
        merged: Dict[str, Any] = {}
        with self._lock:
            for p in self._providers.values():
                if p.family == family and p.capabilities:
                    merged.update(p.capabilities)
        if merged:
            return merged
        return dict(self._builtin_family_capabilities.get(family, self._builtin_family_capabilities["custom"]))

    def get_merged_provider_models(self) -> Dict[str, List[str]]:
        """PROVIDER_MODELS 与 models.dev 动态数据的合并结果（constants 同名函数替代）。

        合并规则与原 constants.get_merged_provider_models 一致：
        - 插件模型始终保留，且排在前面。
        - 动态模型按服务商追加，去重（不区分大小写）。
        - models.dev 未覆盖的服务商保持原样。
        - 同步失败或禁用时，完全回退到插件模型。
        """
        static = self.provider_models()
        try:
            from app.core.models_dev_sync import get_dynamic_models

            dynamic = get_dynamic_models()
            dynamic_providers = dynamic.provider_models
        except Exception:
            # 同步模块异常时不影响主程序，直接回退到插件模型
            return static

        merged: Dict[str, List[str]] = {}
        for provider_name, static_models in static.items():
            merged_models: List[str] = list(static_models)
            seen_lower = {m.strip().lower() for m in merged_models}
            dynamic_models = dynamic_providers.get(provider_name, [])
            for model in dynamic_models:
                key = model.strip().lower()
                if key and key not in seen_lower:
                    merged_models.append(model)
                    seen_lower.add(key)
            merged[provider_name] = merged_models
        return merged

    # ── 查询执行（余额 / 套餐用量）─────────────────────────

    def has_balance_support(self, name: str) -> bool:
        p = self.get(name)
        return bool(p and p.balance_fetcher)

    def balance_fetch(self, name: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """执行余额查询（fetcher 由插件提供），无支持返回 None"""
        p = self.get(name)
        if not p or not p.balance_fetcher:
            return None
        return p.balance_fetcher(dict(config or {}))

    def has_coding_plan_support(self, provider_name: str) -> bool:
        """该服务商（或其同 source 插件）是否注册了套餐用量获取器"""
        return self._resolve_coding_plan_fetcher(provider_name) is not None

    def _resolve_coding_plan_fetcher(self, provider_name: str) -> Optional[Callable]:
        """按服务商名精确匹配，回退按 family 前缀匹配"""
        p = self.get(provider_name)
        if p and p.coding_plan_fetcher:
            return p.coding_plan_fetcher
        with self._lock:
            for defn in self._providers.values():
                if defn.coding_plan_fetcher and defn.family and defn.family in provider_name:
                    return defn.coding_plan_fetcher
        return None

    def coding_plan_fetch(self, provider_name: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """执行套餐用量查询（同步，可能阻塞网络），无获取器返回 None"""
        fetcher = self._resolve_coding_plan_fetcher(provider_name)
        if fetcher is None:
            return None
        return fetcher(dict(config or {}))


ProviderRegistryInstance = ProviderRegistry.get_instance


def get_registry() -> ProviderRegistry:
    """获取全局注册表实例（延迟初始化，供消费方统一入口）"""
    return ProviderRegistry.get_instance()