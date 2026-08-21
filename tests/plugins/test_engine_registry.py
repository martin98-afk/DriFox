# -*- coding: utf-8 -*-
"""EngineRegistry：引擎工厂契约 + 注册表 + 创建入口安全网"""

import pytest


def test_engine_factory_protocol_runtime_checkable():
    """ClassEngineFactory 满足 EngineFactory Protocol（id + create）"""
    from app.plugins.contracts.dialogue_engine import ClassEngineFactory, EngineFactory

    class _Engine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    factory = ClassEngineFactory("ui", _Engine)
    assert isinstance(factory, EngineFactory)
    assert factory.id == "ui"
    engine = factory.create(session_manager="s")
    assert isinstance(engine, _Engine)
    assert engine.kwargs == {"session_manager": "s"}


def test_engine_slot_ui_constant():
    from app.plugins.contracts.dialogue_engine import ENGINE_SLOT_UI

    assert ENGINE_SLOT_UI == "ui"


class _BaseEngine:
    """测试用兜底基类（替身 UIEngine，避免 Qt/Session 依赖）"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _DerivedEngine(_BaseEngine):
    """合法替换类：继承内置引擎"""


class _ForeignEngine:
    """非法替换类：不是 _BaseEngine 子类"""


class _CrashFactory:
    """create 抛异常的工厂"""

    id = "ui"

    def create(self, **kwargs):
        raise RuntimeError("boom")


@pytest.fixture()
def fresh_engine_registry(monkeypatch):
    from app.plugins.registries.engine_registry import EngineRegistry

    reg = EngineRegistry()
    monkeypatch.setattr(EngineRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def test_registry_register_and_get(fresh_engine_registry):
    from app.plugins.contracts.dialogue_engine import ClassEngineFactory

    assert fresh_engine_registry.get_factory("ui") is None  # 未注册 → None（调用方回退）

    fresh_engine_registry.register(ClassEngineFactory("ui", _DerivedEngine), source="plugin:demo")
    factory = fresh_engine_registry.get_factory("ui")
    assert factory is not None and factory.id == "ui"

    # 同槽位后注册者覆盖（插件热更新换新版自然生效）
    fresh_engine_registry.register(ClassEngineFactory("ui", _DerivedEngine), source="plugin:demo2")
    assert fresh_engine_registry.get_factory("ui").id == "ui"


def test_registry_unregister_source(fresh_engine_registry):
    from app.plugins.contracts.dialogue_engine import ClassEngineFactory

    fresh_engine_registry.register(ClassEngineFactory("ui", _DerivedEngine), source="plugin:demo")
    fresh_engine_registry.unregister_source("plugin:demo")
    assert fresh_engine_registry.get_factory("ui") is None  # 卸载后回退 None


def test_create_engine_no_factory_falls_back(fresh_engine_registry):
    from app.plugins.registries.engine_registry import create_engine_for_slot

    engine = create_engine_for_slot("ui", _BaseEngine, session_manager="s")
    assert isinstance(engine, _BaseEngine) and type(engine) is _BaseEngine


def test_create_engine_factory_returns_derived(fresh_engine_registry):
    from app.plugins.contracts.dialogue_engine import ClassEngineFactory
    from app.plugins.registries.engine_registry import create_engine_for_slot

    fresh_engine_registry.register(ClassEngineFactory("ui", _DerivedEngine), source="plugin:demo")
    engine = create_engine_for_slot("ui", _BaseEngine, session_manager="s")
    assert isinstance(engine, _DerivedEngine)


def test_create_engine_incompatible_falls_back(fresh_engine_registry):
    """产出实例不是 fallback_cls 子类 → 回退内置（安全网）"""
    from app.plugins.contracts.dialogue_engine import ClassEngineFactory
    from app.plugins.registries.engine_registry import create_engine_for_slot

    fresh_engine_registry.register(ClassEngineFactory("ui", _ForeignEngine), source="plugin:demo")
    engine = create_engine_for_slot("ui", _BaseEngine)
    assert type(engine) is _BaseEngine


def test_create_engine_crash_factory_falls_back(fresh_engine_registry):
    """工厂抛异常 → 回退内置（安全网）"""
    from app.plugins.registries.engine_registry import create_engine_for_slot

    fresh_engine_registry.register(_CrashFactory(), source="plugin:demo")
    engine = create_engine_for_slot("ui", _BaseEngine)
    assert type(engine) is _BaseEngine
