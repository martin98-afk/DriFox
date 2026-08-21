# -*- coding: utf-8 -*-
"""对话引擎工厂契约 — 插件用工厂替换内置 UIEngine 的接口。

引擎是必需品（LoopPolicy 类比引导插件；引擎的"默认实现"就是内置 UIEngine 本身），
因此注册表无注册时直接回退内置类，零插件时零行为变化。

替换类必须继承内置 UIEngine（见 registries/engine_registry.create_engine_for_slot 的
isinstance 安全网），保证 main_widget 全量回调接线不炸。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

ENGINE_SLOT_UI = "ui"

# gateway = 消息平台引擎（GatewayEngine，全局单例语义）
ENGINE_SLOT_GATEWAY = "gateway"


@runtime_checkable
class EngineFactory(Protocol):
    """引擎工厂协议 — 插件实现并经 ``register(registry)`` 注册到 ``EngineRegistry``。

    ``id`` 为槽位名（首版仅 ``ENGINE_SLOT_UI``）；``create(**kwargs)`` 返回内置
    ``UIEngine`` 子类实例，``kwargs`` 与内置 ``UIEngine.__init__`` 完全一致。
    """

    id: str  # 目标槽位（ENGINE_SLOT_UI = "ui"）

    def create(self, **kwargs: Any) -> Any:
        """创建引擎实例（kwargs 与内置 UIEngine.__init__ 完全一致）"""
        ...


class ClassEngineFactory:
    """便捷工厂 — 直接包装引擎类：create() = cls(**kwargs)

    用法（插件 engines/my_engine.py）::

        from app.core.engines.ui import UIEngine
        from app.plugins.contracts.dialogue_engine import ENGINE_SLOT_UI, ClassEngineFactory

        class MyEngine(UIEngine):
            ...

        def register(registry):
            registry.register(ClassEngineFactory(ENGINE_SLOT_UI, MyEngine))
    """

    def __init__(self, slot: str, engine_class: type):
        self.id = slot
        self._engine_class = engine_class

    def create(self, **kwargs: Any) -> Any:
        return self._engine_class(**kwargs)
