# -*- coding: utf-8 -*-
"""EffectScope（副作用原语）测试。"""

import pytest

from zero import DisposedError, EffectScope, current_scope


def test_effect_registers_returned_disposer():
    scope = EffectScope("s")
    log = []
    scope.effect(lambda: log.append("run") or (lambda: log.append("undo")))

    assert log == ["run"]
    assert scope.pending == 1

    scope.dispose()
    assert log == ["run", "undo"]


def test_effect_without_disposer_is_fine():
    scope = EffectScope("s")
    scope.effect(lambda: 42)  # 返回非 callable，不登记
    assert scope.pending == 0
    scope.dispose()
    assert scope.disposed


def test_dispose_runs_in_reverse_order():
    scope = EffectScope("s")
    log = []
    for i in range(3):
        scope.add(lambda i=i: log.append(i))

    scope.dispose()
    assert log == [2, 1, 0]  # 逆序


def test_dispose_is_idempotent():
    scope = EffectScope("s")
    calls = []
    scope.add(lambda: calls.append(1))

    scope.dispose()
    scope.dispose()
    assert calls == [1]


def test_disposer_exception_does_not_block_others():
    scope = EffectScope("s")
    log = []

    def _boom():
        raise RuntimeError("boom")

    scope.add(_boom)
    scope.add(lambda: log.append("cleanup"))

    scope.dispose()  # 异常被吞掉并记日志
    assert log == ["cleanup"]
    assert scope.disposed


def test_child_disposed_before_parent_effects():
    parent = EffectScope("parent")
    child = parent.child("child")
    log = []
    parent.add(lambda: log.append("parent"))
    child.add(lambda: log.append("child"))

    parent.dispose()
    assert log == ["child", "parent"]  # 子先回滚


def test_operations_on_disposed_scope_rejected():
    scope = EffectScope("s")
    scope.dispose()

    with pytest.raises(DisposedError):
        scope.add(lambda: None)
    with pytest.raises(DisposedError):
        scope.effect(lambda: None)
    with pytest.raises(DisposedError):
        scope.child("x")


def test_add_requires_callable():
    scope = EffectScope("s")
    with pytest.raises(TypeError):
        scope.add("not-callable")


def test_current_scope_bound_during_effect():
    scope = EffectScope("s")
    seen = []
    scope.effect(lambda: seen.append(current_scope()))

    assert seen == [scope]
    assert current_scope() is None  # 执行结束后复位


def test_context_manager_binds_current_scope():
    scope = EffectScope("s")
    with scope:
        assert current_scope() is scope
    assert current_scope() is None


def test_repr():
    scope = EffectScope("s")
    assert "s" in repr(scope)
