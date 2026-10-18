# -*- coding: utf-8 -*-
"""能力池视图：裁剪 / CapabilityDenied / 惰性求值 / 首次触达审计 / 分组。"""
import pytest
from loguru import logger


def _pool(providers, declared=None, ungated=()):
    from app.plugins.capabilities.pool import CapabilityPool

    return CapabilityPool(
        plugin="demo",
        surface="ui",
        host=object(),
        declared=declared,
        providers=providers,
        ungated=set(ungated),
    )


def test_contains_filters_by_providers():
    p = _pool({"session.read": lambda host: {"x": 1}}, declared={"session.read"}, ungated={"state.kv"})
    assert "session.read" in p
    assert "state.kv" in p
    assert "session.write" not in p


def test_getitem_denied_with_fix_hint():
    from app.plugins.capabilities.pool import CapabilityDenied

    p = _pool({"session.read": lambda host: 1}, declared={"session.read"})
    with pytest.raises(CapabilityDenied) as ei:
        p["session.write"]
    msg = str(ei.value)
    assert "session.write" in msg
    assert "capabilities" in msg  # 修复指引
    assert ei.value.cap_id == "session.write"
    assert ei.value.plugin == "demo"


def test_lazy_single_evaluation():
    calls = []

    def provider(host):
        calls.append(1)
        return {"ok": True}

    p = _pool({"session.read": provider}, declared={"session.read"})
    assert calls == []
    assert p["session.read"] == {"ok": True}
    assert p["session.read"] == {"ok": True}
    assert len(calls) == 1


def test_two_arg_provider_receives_context():
    from app.plugins.capabilities.pool import CapabilityPool

    seen = {}

    def provider(host, ctx):
        seen["plugin"] = ctx.plugin
        seen["surface"] = ctx.surface
        return "v"

    p = CapabilityPool(
        plugin="demo",
        surface="tool",
        host=object(),
        declared={"plugin.log"},
        providers={"plugin.log": provider},
        ungated=set(),
    )
    assert p["plugin.log"] == "v"
    assert seen == {"plugin": "demo", "surface": "tool"}


def test_audit_once_and_levels():
    # log_capture fixture 只捕 WARNING+；medium/low 是 INFO，需自定义 sink
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        from app.plugins.capabilities.pool import CapabilityPool

        p = CapabilityPool(
            plugin="demo",
            surface="ui",
            host=object(),
            declared={"model.config", "session.read"},
            providers={"model.config": lambda h: "cfg", "session.read": lambda h: "s"},
            ungated=set(),
        )
        p["session.read"], p["session.read"]
        p["model.config"]
    finally:
        logger.remove(sink_id)
    audits = [r for r in records if "[CapabilityAudit]" in r]
    assert len(audits) == 2
    assert any("cap=model.config" in r and "level=high" in r for r in audits)
    assert any("cap=session.read" in r and "level=medium" in r for r in audits)


def test_names_and_group():
    p = _pool(
        {"session.read": lambda h: 1, "session.write": lambda h: 2, "theme.read": lambda h: 3},
        declared={"session.read", "session.write", "theme.read"},
    )
    assert p.names() == ["session.read", "session.write", "theme.read"]
    assert set(p.group("session")) == {"session.read", "session.write"}
    assert p.group("nope") == {}


def test_mapping_protocol_len_and_iter():
    p = _pool({"session.read": lambda h: 1}, declared={"session.read"}, ungated={"state.kv"})
    assert len(p) == 2
    assert set(iter(p)) == {"session.read", "state.kv"}
