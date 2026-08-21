# -*- coding: utf-8 -*-
"""对话引擎注册表 — 引擎插件按槽位注册工厂，backend 创建时经工厂替换内置引擎。

- register(factory, source)：同槽位后注册者覆盖（热更新换新版自然生效）
- get_factory(slot)：无注册返回 None，调用方回退内置引擎（安全网，引擎为必需品不抛错）
- 热重载经 unregister_source("plugin:<name>") 精准清理（watcher 分派）

与 LoopPolicyRegistry 差异：后者无注册抛 RuntimeError（引导启用 system 插件）；
引擎的"默认实现"就是内置类本身，零插件时零行为变化。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from app.plugins.contracts.dialogue_engine import EngineFactory


class EngineRegistry:
    def __init__(self) -> None:
        self._factories: Dict[str, Tuple[EngineFactory, str]] = {}
        self._lock = threading.Lock()

    def register(self, factory: EngineFactory, source: str = "") -> None:
        with self._lock:
            prev = self._factories.get(factory.id)
            self._factories[factory.id] = (factory, source)
        if prev is not None and prev[1] != source:
            logger.info(f"[EngineRegistry] 槽位 '{factory.id}' 由 {prev[1] or '内置'} 切换为 {source or '内置'}")

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s) in self._factories.items() if s == source]
            for k in dead:
                del self._factories[k]

    def get_factory(self, slot: str) -> Optional[EngineFactory]:
        with self._lock:
            item = self._factories.get(slot)
        return item[0] if item else None

    def factories(self) -> Dict[str, EngineFactory]:
        with self._lock:
            return {k: v[0] for k, v in self._factories.items()}

    @staticmethod
    def get_instance() -> "EngineRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = EngineRegistry()
            return _instance


def create_engine_for_slot(slot: str, fallback_cls: type, **kwargs: Any) -> Any:
    """按槽位创建引擎的唯一入口：工厂存在且产出兼容实例 → 返回；否则回退内置类。

    兼容性安全网（两条回退路径 + 一条直通）：
    1. 无工厂 → 直接 fallback_cls(**kwargs)（零插件时零行为变化）
    2. 工厂 create 抛异常 → 记录错误 + 回退
    3. 产出实例非 fallback_cls 子类 → 记录错误 + 回退
       （替换类必须继承内置引擎——main_widget 对引擎有大量属性/回调接线，
        鸭子类型不足以保证兼容，isinstance 是硬约束）
    """
    factory = EngineRegistry.get_instance().get_factory(slot)
    if factory is None:
        return fallback_cls(**kwargs)
    try:
        engine = factory.create(**kwargs)
    except Exception as e:
        logger.error(f"[EngineRegistry] 槽位 '{slot}' 工厂创建失败，回退内置 {fallback_cls.__name__}: {e}")
        return fallback_cls(**kwargs)
    if not isinstance(engine, fallback_cls):
        logger.error(
            f"[EngineRegistry] 槽位 '{slot}' 工厂产出 {type(engine).__name__} 不是 "
            f"{fallback_cls.__name__} 子类，回退内置（引擎替换类必须继承内置引擎）"
        )
        return fallback_cls(**kwargs)
    logger.info(f"[EngineRegistry] 槽位 '{slot}' 使用插件引擎 {type(engine).__name__}")
    return engine


_instance: Optional[EngineRegistry] = None
_instance_lock = threading.Lock()
