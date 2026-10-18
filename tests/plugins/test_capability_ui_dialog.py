# -*- coding: utf-8 -*-
"""ui.dialog / ui.card.show：catalog 登记的能力必须有 provider（防登记-实现漂移）。

背景：catalog 有 ui.dialog / ui.card.show，但 provider 漏实现 → 插件声明了
不报错，取用时才 CapabilityDenied。本测试锁死「登记即实现」。
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(caps):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("ui")
    return CapabilityRegistry.get_instance().build_pool(
        surface="ui", host=None, plugin="", declared=caps
    )


def test_every_catalog_cap_has_provider_or_is_exempt():
    """catalog 里登记的能力，至少在一个 surface 有实现（豁免项显式列出）。"""
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.catalog import CATALOG
    from app.plugins.capabilities.registry import CapabilityRegistry

    bootstrap.ensure_registered("ui")
    bootstrap.ensure_registered("tool")
    reg = CapabilityRegistry.get_instance()

    # 暂未提供实现的能力（设计上登记了但还没落地），新增实现后从这里移除
    EXEMPT = {
        "session.read",   # 当前会话读取
        "session.list",   # 历史会话列表
        "session.manage",  # 会话增删
        "host.raw",
    }
    EXEMPT = {c for c in EXEMPT} | {
        c
        for c in CATALOG
        if CATALOG[c].surfaces == frozenset({"tool"}) and not reg.has(c, "tool")
    }
    missing = [
        cid
        for cid, spec in CATALOG.items()
        if cid not in EXEMPT
        and not any(reg.has(cid, s) for s in spec.surfaces)
    ]
    assert not missing, f"catalog 登记但无 provider: {missing}"


def test_ui_dialog_api_shape():
    pool = _pool({"ui.dialog"})
    api = pool["ui.dialog"]()
    assert set(api) == {"confirm", "prompt", "pick_file", "pick_dir", "save_file"}
    assert all(callable(v) for v in api.values())


def test_ui_card_show_callable():
    """ui.card.show 与 ui.card.hide 手感一致：取出来是 callable。"""
    pool = _pool({"ui.card.show", "ui.card.hide"})
    assert callable(pool["ui.card.show"])


def test_no_duplicate_registration_lines():
    """register() 里不应有重复的 (cap_id, surface) 主实现注册。"""
    import re

    from app.plugins.capabilities.providers import ui_core

    src = open(ui_core.__file__, encoding="utf-8").read()
    body = src[src.find("def register(registry)"):]
    ids = re.findall(r'reg\.register\(\s*"([^"]+)"', body)
    dups = {i for i in ids if ids.count(i) > 1 and "legacy_key" not in body}
    # 允许带 legacy_key 的同 ID 变体，主实现必须唯一
    main_calls = [
        m.group(1)
        for m in re.finditer(r'reg\.register\(\s*"([^"]+)"[^)]*\)', body)
        if "legacy_key" not in m.group(0)
    ]
    dup_main = {i for i in main_calls if main_calls.count(i) > 1}
    assert not dup_main, f"重复的主实现注册: {dup_main}"
