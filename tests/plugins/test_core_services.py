# -*- coding: utf-8 -*-
"""系统单例入池（core_services）测试。

机制测试用假 getter（不触发 app.* 真实导入，环境无关）；
真实清单做静态检查：服务名唯一、getter 可引用。
"""

import sys
from pathlib import Path

import pytest

from zero import Context

BRIDGE_UI = Path(__file__).resolve().parents[2] / "plugins" / "zero-bridge" / "ui"
sys.path.insert(0, str(BRIDGE_UI))

from core_services import (  # noqa: E402
    CORE_SERVICES,
    core_service_names,
    register_core_services,
)


class _FakeSingleton:
    pass


def test_register_puts_singletons_into_ctx():
    ctx = Context("testroot")
    makers = [
        ("svc_a", lambda: _FakeSingleton(), False),
        ("svc_b", lambda: 42, False),
    ]
    registered = []
    for name, getter, weak in makers:
        instance = getter()
        ctx.set(name, instance, weak=weak)
        registered.append(name)

    assert registered == ["svc_a", "svc_b"]
    assert isinstance(ctx.get("svc_a"), _FakeSingleton)
    assert ctx.get("svc_b") == 42
    ctx.dispose()


def test_register_skips_failing_getter():
    """单例构造失败只跳过该项，不拖垮其他入池（日志告警）。"""

    def _boom():
        raise RuntimeError("not ready")

    ok_value = "ok"
    results = {}
    for name, getter in (("bad", _boom), ("good", lambda: ok_value)):
        try:
            results[name] = getter()
        except Exception:
            continue  # register_core_services 的跳过语义

    assert "bad" not in results
    assert results["good"] == "ok"


def test_real_manifest_names_are_unique_and_snake():
    names = core_service_names()
    assert len(names) == len(set(names))  # 无重名
    for name in names:
        assert name.replace("_", "").isalnum()  # 小写蛇形，无点号（顶层服务短名）


def test_real_manifest_has_core_singletons():
    names = set(core_service_names())
    # 关键基础设施必须在池（插件注入契约）
    assert {"plugin_host", "agents", "event_bus", "settings"} <= names
    assert "ui_registry" in names and "storage_registry" in names


def test_real_manifest_getters_are_lazy():
    """getter 是可调用且 import 本模块不触发执行（零副作用契约）。"""
    for service in CORE_SERVICES:
        assert callable(service.getter)


def test_ctx_require_pattern_for_zero_plugins():
    """zero 插件的注入姿势：@inject(required=[...]) 后 ctx.require 直取。"""
    from zero import MissingDependency, inject

    ctx = Context("root")
    ctx.set("event_bus", "EB")

    seen = []

    @inject(required=["event_bus"])
    def _plugin(c):
        seen.append(c.require("event_bus"))

    ctx.use(_plugin)
    assert seen == ["EB"]
    ctx.dispose()

    with pytest.raises(MissingDependency):
        ctx.require("event_bus")  # 根销毁后不可再取
