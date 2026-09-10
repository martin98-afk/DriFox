# -*- coding: utf-8 -*-
"""依赖图导出测试。"""

from zero import Service, create_context, inject


class Database(Service):
    name = "db"


class Cache(Service):
    name = "cache"


def _build_scenario():
    root = create_context()

    def database(ctx):
        ctx.provide()(Database)

    def cache(ctx):
        ctx.provide()(Cache)

    @inject(required=["db"])
    def feature_a(ctx):
        ctx.set("marker", "a")

    @inject(required=["db"], optional=["cache"])
    def feature_b(ctx):
        ctx.set("marker", "b")

    root.use(database)
    root.use(cache)
    root.use(feature_a)
    root.use(feature_b)
    return root


def test_graph_collects_services_and_edges():
    root = _build_scenario()
    graph = root.graph()

    assert sorted(graph.services()) == ["cache", "db"]

    provides = [e for e in graph.edges if e.kind == "provides"]
    assert len(provides) == 2

    depends = [e for e in graph.edges if e.kind == "depends"]
    assert len(depends) == 3  # a:db, b:db, b:cache(optional)
    assert len([e for e in depends if e.optional]) == 1


def test_graph_index_answers_who_depends_on():
    root = _build_scenario()
    graph = root.graph()

    assert len(graph.dependents_of("db")) == 2  # feature_a 与 feature_b
    assert len(graph.dependents_of("cache")) == 1  # 只有 optional 依赖
    assert graph.provider_of("db") is not None


def test_mermaid_output_shape():
    root = _build_scenario()
    text = root.to_mermaid()

    assert text.startswith("graph TD")
    assert "provides" in text
    assert "requires" in text
    assert "optional" in text


def test_dot_output_shape():
    root = _build_scenario()
    text = root.graph().to_dot()

    assert text.startswith("digraph zero {")
    assert "style=dashed" in text


def test_graph_reflects_disposal():
    root = _build_scenario()
    fork = [c for c in root.children if c.name == "feature_a"][0]
    fork.dispose()

    graph = root.graph()
    assert len(graph.dependents_of("db")) == 1  # 卸载后少一个依赖者
    root.dispose()
