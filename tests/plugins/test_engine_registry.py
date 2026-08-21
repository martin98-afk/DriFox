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