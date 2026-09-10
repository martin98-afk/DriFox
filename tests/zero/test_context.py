# -*- coding: utf-8 -*-
"""Context 测试：依赖解析、插件可逆加载、生命周期、解析域隔离。"""

import pytest

from zero import (
    EV_DISPOSE,
    EV_FORK,
    EV_READY,
    Context,
    DisposedError,
    MissingDependency,
    PluginError,
    create_context,
)


@pytest.fixture
def root() -> Context:
    ctx = create_context()
    yield ctx
    ctx.dispose()


# ── 依赖解析 ──────────────────────────────────────────────────────────
def test_child_resolves_parent_dependency(root):
    root.set("db", "sqlite")
    child = root.fork("child")
    assert child.get("db") == "sqlite"


def test_local_value_shadows_parent(root):
    root.set("db", "sqlite")
    child = root.fork("child")
    child.set("db", "postgres")
    assert child.get("db") == "postgres"
    assert root.get("db") == "sqlite"  # 父不受影响


def test_dispose_restores_previous_value(root):
    root.set("db", "sqlite")
    child = root.fork("child")
    child.set("db", "postgres")
    child.dispose()
    assert root.get("db") == "sqlite"


def test_has_and_require(root):
    assert root.has("missing") is False
    root.set("present", None)  # 值为 None 也算存在
    assert root.has("present") is True

    with pytest.raises(MissingDependency):
        root.require("missing")


# ── 插件可逆加载 ──────────────────────────────────────────────────────
def test_use_loads_plugin_and_exposes_dependency(root):
    def _plugin(ctx):
        ctx.set("greeting", "hi")

    root.use(_plugin)
    assert root.get("greeting") is None  # 插件写在自己的子上下文
    assert root.children[0].get("greeting") == "hi"


def test_plugin_receives_config():
    root = create_context()
    seen = []

    def _plugin(ctx, config):
        seen.append(config)

    root.use(_plugin, {"key": "value"})
    assert seen == [{"key": "value"}]
    root.dispose()


def test_plugin_signature_without_config():
    root = create_context()
    calls = []
    root.use(lambda ctx: calls.append(ctx))
    assert len(calls) == 1
    root.dispose()


def test_failed_plugin_leaves_no_partial_effects(root):
    log = []

    def _broken(ctx):
        ctx.set("half", "registered")
        ctx.add_disposer(lambda: log.append("plugin-cleanup"))
        raise RuntimeError("plugin boom")

    with pytest.raises(PluginError):
        root.use(_broken)

    # 关键：失败即整棵子树回滚，父上下文看不到任何残留
    assert root.get("half") is None
    assert root.children == []
    assert log == ["plugin-cleanup"]


def test_plugin_dispose_rolls_back_everything(root):
    log = []

    def _plugin(ctx):
        ctx.set("service", "impl")
        ctx.add_disposer(lambda: log.append("undo"))

    fork = root.use(_plugin)
    assert fork.get("service") == "impl"

    fork.dispose()
    assert log == ["undo"]
    assert fork.get("service") is None
    assert fork not in root.children


def test_parent_dispose_cascades_to_children(root):
    root.use(lambda ctx: ctx.set("a", 1))
    root.use(lambda ctx: ctx.set("b", 2))
    assert len(root.children) == 2

    root.dispose()
    assert root.children == []
    assert root.disposed


# ── 事件与生命周期 ────────────────────────────────────────────────────
def test_lifecycle_event_order(root):
    seen = []

    def _plugin(ctx):
        ctx.on(EV_READY, lambda c: seen.append("ready"))
        ctx.on(EV_DISPOSE, lambda c: seen.append("dispose"))

    fork = root.use(_plugin)
    root.on(EV_FORK, lambda c: seen.append("fork"))

    assert seen == ["ready"]  # fork 事件在 use 内部已先触发
    fork.dispose()
    assert seen == ["ready", "dispose"]


def test_event_bubbles_up_to_ancestors(root):
    seen = []
    root.on("tick", lambda: seen.append("root"))
    fork = root.fork("child")
    fork.emit("tick")
    assert seen == ["root"]  # 子发出的事件祖先可见


def test_listener_auto_unsubscribed_on_dispose(root):
    calls = []
    fork = root.fork("child")
    fork.on("tick", lambda: calls.append(1))

    fork.emit("tick")
    assert calls == [1]

    fork.dispose()
    fork.emit("tick")
    assert calls == [1]  # 退订生效


def test_waterfall_rewrites_and_short_circuits(root):
    root.on("wrap", lambda v, nxt: nxt(v + 1))
    root.on("wrap", lambda v, nxt: nxt(v * 10))
    assert root.waterfall("wrap", 1) == 20

    root.on("stop", lambda v, nxt: None)  # 不调 next → 短路
    root.on("stop", lambda v, nxt: nxt("never"))
    assert root.waterfall("stop", "start") == "start"


# ── 解析域隔离 ────────────────────────────────────────────────────────
def test_isolate_resolves_different_implementations(root):
    root.set("storage", "global-storage")

    win1 = root.isolate("storage", "w1")
    win2 = root.isolate("storage", "w2")
    win1.set("storage", "w1-storage")

    assert win1.get("storage") == "w1-storage"
    assert win2.get("storage") == "global-storage"  # 回落到父级


# ── 边界 ──────────────────────────────────────────────────────────────
def test_operations_after_dispose_rejected(root):
    root.dispose()
    with pytest.raises(DisposedError):
        root.set("x", 1)
    with pytest.raises(DisposedError):
        root.fork("child")


def test_path_and_repr(root):
    def _my_plugin(ctx):
        ctx.set("a", 1)

    fork = root.use(_my_plugin)
    assert fork.path == "root/_my_plugin"
    assert "Context" in repr(root)
