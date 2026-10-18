# -*- coding: utf-8 -*-
"""工具注册表访问门面（插件经能力池取，不直连 app.tools.registry）。

实现放在主程序侧的 capabilities 包里，插件从 sdk 拿到的是同一个函数。
"""

from __future__ import annotations

from typing import Any, Optional


class RegistryProxy:
    """registry 实例的惰性只读代理（缺方法时该键返回 None）。

    测试常用 ``monkeypatch.setattr(模块, "ToolRegistry", Fake)`` 注入替身，
    而替身往往只实现 list/get。一次性读全部属性会 AttributeError，所以改成
    属性访问时才取，并由调用方按"能力不存在"降级。
    """

    __slots__ = ("_reg", "_kind")

    _READ_ATTRS = (
        "names",
        "list",
        "schemas",
        "get",
        "has",
        "group_map",
        "is_no_prune",
        "is_no_offload",
        "get_danger",
        "is_protected",
    )
    _INJECT_ATTRS = ("register_schema_filter", "unregister_schema_filter", "apply_schema_filters")
    _ALIAS = {"groups": "group_map", "is_danger": "get_danger"}

    def __init__(self, reg: Any, kind: str = "read") -> None:
        self._reg = reg
        self._kind = kind

    def _attrs(self):
        return self._INJECT_ATTRS if self._kind == "inject" else self._READ_ATTRS

    def __contains__(self, key: str) -> bool:
        return self._ALIAS.get(key, key) in self._attrs()

    def __getitem__(self, key: str):
        name = self._ALIAS.get(key, key)
        if name not in self._attrs():
            return None
        return getattr(self._reg, name, None)


def tool_registry_api(
    kind: str = "read", registry: Any = None, module_globals: Optional[dict] = None
):
    """取工具注册表只读面 / 注入面。

    Args:
        kind: "read"（查询）或 "inject"（schema 过滤器注册）
        registry: 显式传入的 registry 实例（测试注入用）
        module_globals: 调用方模块的 globals()，用于识别测试注入的同名符号

    Returns:
        RegistryProxy / 能力池返回的 dict；都不可用时 None。

    优先序：显式 registry > 模块内 ToolRegistry 符号 > 能力池。
    """
    cap = "tool.inject" if kind == "inject" else "tool.read"

    if registry is None and module_globals:
        reg_cls = module_globals.get("ToolRegistry")
        if reg_cls is not None:
            try:
                registry = reg_cls.get_instance()
            except Exception:
                registry = None
    if registry is not None:
        return RegistryProxy(registry, kind)

    try:
        from app.plugins.capabilities.bootstrap import ensure_registered
        from app.plugins.capabilities.registry import CapabilityRegistry

        ensure_registered("tool")
        pool = CapabilityRegistry.get_instance().build_pool(
            surface="tool", host=None, plugin="", declared={cap, "state.kv", "plugin.log"}
        )
        if cap not in pool:
            return None
        return pool[cap]()
    except Exception:
        return None
