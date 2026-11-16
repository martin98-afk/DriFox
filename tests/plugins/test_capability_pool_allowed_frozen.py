# -*- coding: utf-8 -*-
"""S4 回归：CapabilityPool._allowed() 预算 frozenset 后行为逐值不变、不再分配新集合。

沿用 #13「改前快照 + 内嵌 sha256 双断言」：
- 行为锚点：改前实测 names/iter/len/contains/group/legacy 的 repr 哈希 =
  44f77731bbce0f3b3210a51bae9adb15eff432431aa993aadd35233738b9f08d（并集去重后 4 个 ID）
- 分配锚点：_allowed() 连续调用必须返回同一对象（is），且为 frozenset
"""
from __future__ import annotations

import hashlib

from app.plugins.capabilities.pool import CapabilityPool

# 改前基线（2026-11-15，HEAD 21b77723e 之前）：sorted(_allowed()) 的 sha256
ALLOWED_SHA_BEFORE = "44f77731bbce0f3b3210a51bae9adb15eff432431aa993aadd35233738b9f08d"
NAMES_BEFORE = ["a.one", "b.two", "c.three", "u.free"]


def _make_pool() -> CapabilityPool:
    prov = {"a.one": lambda h: 1, "b.two": lambda h: 2, "c.three": lambda h: 3}
    # u.free 为 ungated 独有；a.one 与 providers 交集，验证并集去重
    ung = {"u.free", "a.one"}
    return CapabilityPool(
        plugin="x", surface="s", host=object(), declared=set(prov),
        providers=prov, ungated=ung, legacy_providers={"old": lambda h: 9},
    )


def test_allowed_sha256_unchanged():
    """内嵌 sha256 双断言①：并集内容逐值不变。"""
    p = _make_pool()
    allowed = p._allowed()
    sha = hashlib.sha256(repr(sorted(allowed)).encode()).hexdigest()
    assert sha == ALLOWED_SHA_BEFORE, f"放行集合内容变化：{sha}"
    assert p.names() == NAMES_BEFORE


def test_pool_protocol_outputs_unchanged():
    """内嵌 sha256 双断言②：Mapping 协议与查询辅助输出逐值不变。"""
    p = _make_pool()
    assert p.names() == NAMES_BEFORE
    assert sorted(iter(p)) == NAMES_BEFORE
    assert len(p) == 4
    assert [c in p for c in ["a.one", "b.two", "u.free", "nope"]] == [True, True, True, False]
    assert p["a.one"] == 1
    assert p["u.free"] is None  # ungated 无 provider，返回 None
    assert sorted(p.group("").keys()) == []  # catalog 无这些测试 ID，组过滤为空
    assert p.legacy("old", "a.one") == 9
    # CapabilityDenied 类型与消息不动：未授权访问仍抛
    from app.plugins.capabilities.pool import CapabilityDenied

    try:
        p["nope"]
    except CapabilityDenied as e:
        assert "未获授权能力 nope" in str(e)
    else:
        raise AssertionError("未授权访问未抛 CapabilityDenied")


def test_allowed_no_new_set_allocation():
    """S4 核心：_allowed() 返回构造期预算对象，连续调用不新建集合。"""
    p = _make_pool()
    a, b, c = p._allowed(), p._allowed(), p._allowed()
    assert isinstance(a, frozenset), f"应为 frozenset，实际 {type(a).__name__}"
    assert a is b and b is c, "_allowed() 每次返回了新对象（仍在重分配）"
    assert a is p._allowed_set, "未复用构造期预算集合"


def test_frozen_allowed_survives_spec_reuse():
    """S4×S3 交互：同一 spec 复用出的两个池，frozenset 内容一致且互不混写。"""
    from app.plugins.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry()
    try:
        reg.register("session.read", lambda h: "x", surface="ui")
        # 同 (plugin, surface, declared) 两个 host：spec 命中复用，池实例各自现建
        p1 = reg.build_pool(surface="ui", host="h1", plugin="p", declared={"session.read"})
        p2 = reg.build_pool(surface="ui", host="h2", plugin="p", declared={"session.read"})
        assert p1 is not p2
        a1, a2 = p1._allowed_set, p2._allowed_set
        assert isinstance(a1, frozenset) and isinstance(a2, frozenset)
        assert a1 == a2 == frozenset({"session.read"})  # 内容一致
        assert p1.names() == p2.names() == ["session.read"]
        # 各自首触互不串值（host 惰性值隔离，防跨池污染）
        assert p1["session.read"] == "x" and p2["session.read"] == "x"
    finally:
        reg.clear()
