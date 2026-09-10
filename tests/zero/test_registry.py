# -*- coding: utf-8 -*-
"""插件注册表测试（M2）：拓扑排序、整体卸载、可重用插件。"""

import pytest

from zero import CircularDependency, Service, create_context, inject


class Database(Service):
    name = "db"

    def __init__(self, ctx, name: str = "") -> None:
        super().__init__(ctx, name)
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture
def root():
    ctx = create_context()
    yield ctx
    ctx.dispose()


def test_load_all_orders_by_service_dependency(root):
    order = []

    @root.registry.add(name="database", provides=("db",))
    def database(ctx):
        order.append("database")
        ctx.provide()(Database)

    @root.registry.add(name="feature")
    @inject(required=["db"])
    def feature(ctx):
        order.append("feature")

    root.registry.load_all()
    assert order == ["database", "feature"]  # 提供者先于消费者


def test_load_all_survives_external_service(root):
    """依赖无人提供的服务（root 手工提供）不影响排序。"""
    root.set("ext", "value")

    @root.registry.add(name="user")
    @inject(required=["ext"])
    def user(ctx):
        ctx.set("done", True)

    forks = root.registry.load_all()
    assert len(forks) == 1
    assert forks[0].get("done") is True


def test_circular_dependency_detected(root):
    @root.registry.add(name="a", provides=("sa",))
    @inject(required=["sb"])
    def plugin_a(ctx):
        pass

    @root.registry.add(name="b", provides=("sb",))
    @inject(required=["sa"])
    def plugin_b(ctx):
        pass

    with pytest.raises(CircularDependency):
        root.registry.load_all()


def test_delete_unloads_all_forks(root):
    @root.registry.add(name="reusable")
    def reusable(ctx):
        ctx.set("tag", ctx.path)

    reg = root.registry
    reg.load("reusable")
    reg.load("reusable")  # 可重用：两次加载两个 fork
    assert len(reg.forks_of("reusable")) == 2

    assert reg.delete("reusable") == 2
    assert reg.forks_of("reusable") == []
    assert root.children == []


def test_delete_stops_provided_services_and_dependents(root):
    holder = {}

    @root.registry.add(name="database", provides=("db",))
    def database(ctx):
        db = Database(ctx)
        ctx.provide("db", db)
        holder["db"] = db

    @root.registry.add(name="feature")
    @inject(required=["db"])
    def feature(ctx):
        ctx.set("alive", True)

    root.registry.load_all()

    db = holder["db"]
    assert db.started == 1

    root.registry.delete("database")
    assert db.stopped == 1  # 服务撤销
    assert root.get("db") is None
    # 依赖者 feature 重载失败 → 自动停用
    assert [c for c in root.children if c.name == "feature"] == []


def test_delete_unknown_plugin_is_noop(root):
    assert root.registry.delete("ghost") == 0


def test_duplicate_registration_rejected(root):
    root.registry.add(lambda ctx: None, name="same")
    with pytest.raises(Exception):
        root.registry.add(lambda ctx: None, name="same")


def test_registry_is_lazy_singleton_per_context(root):
    assert root.registry is root.registry
    fork = root.fork("child")
    assert fork.registry is not root.registry


def test_load_all_returns_in_order(root):
    @root.registry.add(name="database", provides=("db",))
    def database(ctx):
        ctx.provide()(Database)

    @root.registry.add(name="feature")
    @inject(required=["db"])
    def feature(ctx):
        ctx.set("marker", 1)

    forks = root.registry.load_all()
    assert [f.name for f in forks] == ["database", "feature"]
