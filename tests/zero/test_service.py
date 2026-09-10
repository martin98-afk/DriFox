# -*- coding: utf-8 -*-
"""服务与依赖注入测试（M1）。

三条硬规则的验证：
1. 依赖未就绪 → 插件不加载，不留半截副作用
2. 被依赖的服务变更 → 依赖者自动回滚重启
3. 服务消失 → 依赖者停用（不崩、不残留）
"""

import pytest

from zero import Context, MissingDependency, Service, create_context, inject


class Database(Service):
    name = "db"

    def __init__(self, ctx: Context, name: str = "") -> None:
        super().__init__(ctx, name)
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture
def root() -> Context:
    ctx = create_context()
    yield ctx
    ctx.dispose()


# ── 服务注册与生命周期 ────────────────────────────────────────────────
def test_provide_registers_service(root):
    db = Database(root)
    root.provide("db", db)
    assert root.get("db") is db
    assert db.started == 1


def test_service_stopped_on_dispose(root):
    db = Database(root)
    root.provide("db", db)
    root.dispose()
    assert db.stopped == 1
    assert db.started == 1


def test_provide_as_decorator(root):
    root.provide()(Database)
    service = root.get("db")
    assert isinstance(service, Database)
    assert service.started == 1


def test_provide_derives_name_from_class(root):
    class Cache(Service):
        name = "cache"

    root.provide()(Cache)
    assert root.get("cache") is not None


# ── 依赖声明 ──────────────────────────────────────────────────────────
def test_missing_required_dependency_blocks_load(root):
    log = []

    @inject(required=["db"])
    def _plugin(ctx):
        log.append("entered")

    with pytest.raises(MissingDependency):
        root.use(_plugin)

    assert log == []  # 压根没进插件体
    assert root.children == []  # 没有留下空壳上下文


def test_required_dependency_satisfied(root):
    db = Database(root)
    root.provide("db", db)

    seen = []

    @inject(required=["db"])
    def _plugin(ctx):
        seen.append(ctx.require("db"))

    root.use(_plugin)
    assert seen == [db]


def test_optional_dependency_missing_still_loads(root):
    seen = []

    @inject(optional=["cache"])
    def _plugin(ctx):
        seen.append(ctx.get("cache"))

    root.use(_plugin)
    assert seen == [None]


# ── 服务变更触发回滚重启 ──────────────────────────────────────────────
def test_service_replacement_restarts_dependent(root):
    first = Database(root)
    root.provide("db", first)

    instances = []

    @inject(required=["db"])
    def _plugin(ctx):
        instances.append(ctx.require("db"))

    root.use(_plugin)
    assert instances == [first]

    second = Database(root)
    root.provide("db", second)  # 服务替换

    assert instances == [first, second]  # 依赖者被重载，拿到新实例
    assert first.stopped == 1  # 旧服务被 stop


def test_restart_does_not_leak_old_fork(root):
    root.provide("db", Database(root))

    @inject(required=["db"])
    def _plugin(ctx):
        ctx.set("marker", "alive")

    root.use(_plugin)
    root.provide("db", Database(root))

    forks = [c for c in root.children if c.name == "_plugin"]
    assert len(forks) == 1  # 旧 fork 已销毁，没有堆积
    assert forks[0].get("marker") == "alive"


def test_service_disappearance_stops_dependent(root):
    """提供方 fork 销毁 → 依赖它的插件一起停用，服务被 stop。"""
    provider = root.fork("provider")
    db = Database(provider)
    provider.provide("db", db)

    loads = []

    @inject(required=["db"])
    def _plugin(ctx):
        loads.append(ctx.require("db"))

    provider.use(_plugin)
    assert len(loads) == 1
    assert db.started == 1

    provider.dispose()
    assert db.stopped == 1
    assert provider.children == []  # 依赖者随提供方一起卸载


def test_sibling_cannot_see_local_values(root):
    """普通 set 值兄弟不可见；服务是全局能力，provide 后人人可见。"""
    provider = root.fork("provider")
    provider.set("db", "local-value")  # set 是本地值，不是服务
    assert root.get("db") is None


def test_dependent_stops_when_provider_dies(root):
    """提供方 fork 销毁 → 服务撤销 → 依赖者停用（不崩、不残留）。"""
    provider = root.fork("provider")
    db = Database(provider)
    provider.provide("db", db)

    loads = []

    @inject(required=["db"])
    def _plugin(ctx):
        loads.append(ctx.require("db"))

    root.use(_plugin)
    assert len(loads) == 1
    assert root.get("db") is db  # 服务注册在根上，人人可用

    provider.dispose()  # 提供方销毁：disposer 撤销服务，依赖者重载失败即停用
    assert root.get("db") is None
    assert db.stopped == 1
    assert len(loads) == 1
    assert [c for c in root.children if c.name == "_plugin"] == []


def test_dependents_tracking_cleaned_on_dispose(root):
    root.provide("db", Database(root))

    @inject(required=["db"])
    def _plugin(ctx):
        ctx.set("x", 1)

    fork = root.use(_plugin)
    assert root._dependents[("", "db")] == [fork]

    fork.dispose()
    assert root._dependents[("", "db")] == []


def test_restart_is_reentrancy_safe(root):
    """插件在加载过程中又 set 服务，不应递归重启自身。"""
    root.provide("db", Database(root))
    loads = []

    @inject(required=["db"])
    def _plugin(ctx):
        loads.append(1)
        ctx.set("derived", len(loads))  # 触发 set → 重启检查

    root.use(_plugin)
    assert loads == [1]  # 未被递归重载
