# -*- coding: utf-8 -*-
"""M3：isolate 解析域（多窗口隔离）测试。

核心语义：
- isolate 后，该域内 provide 的服务落在域槽位，域外不可见
- 域内插件 @inject 解析到本域实例
- 关闭窗口只撤销本域服务、停用本域插件，其他窗口不受影响
- isolate 映射被后代继承
"""

import pytest

from zero import Service, create_context, inject


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


class Cache(Service):
    name = "cache"


@pytest.fixture
def root():
    ctx = create_context()
    yield ctx
    ctx.dispose()


def _make_windows(root):
    """两个窗口域，各自 provide 一份 db；全局一份 cache。"""
    dbs = {}
    caches = {}

    def _database_for(tag):
        def database(ctx):
            db = Database(ctx)
            ctx.provide("db", db)
            dbs[tag] = db

        return database

    def _cache_for(tag):
        def cache(ctx):
            instance = Cache()
            ctx.provide("cache", instance)
            caches[tag] = instance

        return cache

    win1 = root.isolate("db", "w1")
    win2 = root.isolate("db", "w2")

    win1.use(_database_for("w1"))
    win2.use(_database_for("w2"))
    return win1, win2, dbs, caches


def test_realm_services_are_isolated(root):
    win1, win2, dbs, _ = _make_windows(root)

    assert dbs["w1"] is not dbs["w2"]
    assert win1.get("db") is dbs["w1"]
    assert win2.get("db") is dbs["w2"]
    assert root.get("db") is None  # 域外不可见
    assert dbs["w1"].started == 1 and dbs["w2"].started == 1


def test_plugin_in_window_resolves_window_service(root):
    win1, win2, dbs, _ = _make_windows(root)
    seen = []

    @inject(required=["db"])
    def viewer(ctx):
        seen.append(ctx.require("db"))

    win1.use(viewer)
    win2.use(viewer)
    assert seen == [dbs["w1"], dbs["w2"]]  # 各拿各的实例


def test_unisolated_keys_stay_shared(root):
    """未 isolate 的 key 全局共享：两个窗口看到同一个实例。"""
    root.provide("cache", Cache(root))

    win1 = root.isolate("db", "w1")
    win2 = root.isolate("db", "w2")

    assert win1.get("cache") is win2.get("cache")
    assert win1.get("cache") is root.get("cache")


def test_isolate_inherited_by_descendants(root):
    win1, _, dbs, _ = _make_windows(root)
    panel = win1.fork("panel")
    assert panel.get("db") is dbs["w1"]  # 后代继承域映射


def test_closing_window_stops_only_its_services(root):
    win1, win2, dbs, _ = _make_windows(root)
    loads = []

    @inject(required=["db"])
    def viewer(ctx):
        loads.append(ctx.require("db"))

    win1.use(viewer)
    win2.use(viewer)
    assert loads == [dbs["w1"], dbs["w2"]]

    win1.dispose()  # 关闭窗口 1

    assert dbs["w1"].stopped == 1  # 只撤销 w1 的服务
    assert dbs["w2"].stopped == 0
    assert loads == [dbs["w1"], dbs["w2"]]  # w2 插件未被重启（实例不变）
    assert win2.get("db") is dbs["w2"]  # w2 服务完好


def test_replacing_window_service_restarts_only_that_window(root):
    win1, win2, dbs, _ = _make_windows(root)
    instances = []

    @inject(required=["db"])
    def viewer(ctx):
        instances.append(ctx.require("db"))

    win1.use(viewer)
    win2.use(viewer)
    assert instances == [dbs["w1"], dbs["w2"]]

    replacement = Database(win1)
    win1.provide("db", replacement)  # 只替换 w1 的服务

    assert instances == [dbs["w1"], dbs["w2"], replacement]  # 仅 w1 插件重载
    assert dbs["w1"].stopped == 1
    assert dbs["w2"].started == 1 and dbs["w2"].stopped == 0  # w2 无感
    assert win2.get("db") is dbs["w2"]


def test_global_service_visible_from_windows(root):
    """域内未覆盖的 key 回落全局（先测过共享，这里验证 inject 也能解析）。"""
    root.provide("cache", Cache(root))
    seen = []

    @inject(optional=["cache"])
    def viewer(ctx):
        seen.append(ctx.get("cache"))

    win = root.isolate("db", "w1")
    win.use(viewer)
    assert seen == [root.get("cache")]


def test_graph_labels_realm_services(root):
    win1, win2, _, _ = _make_windows(root)
    labels = set(root.graph().services())

    assert labels == {"db@w1", "db@w2"}
