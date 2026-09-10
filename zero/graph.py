# -*- coding: utf-8 -*-
"""依赖图导出（M2 前置）。

把上下文树的三种关系导成结构化数据，便于诊断与可视化：

- ``child``：父子挂载（谁挂在哪）
- ``provides``：上下文 → 服务（谁提供的）
- ``depends``：上下文 → 服务（谁依赖的，required 实线 / optional 虚线）

导出为 mermaid / dot 文本，可直接贴进文档或渲染。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Set

if TYPE_CHECKING:  # 避免循环导入
    from zero.context import Context

__all__ = ["GraphEdge", "GraphNode", "DependencyGraph", "build_graph"]

_SERVICE_PREFIX = "svc:"


@dataclass
class GraphNode:
    """图节点：上下文或服务。"""

    id: str
    label: str
    kind: str  # "context" | "service"

    def mermaid(self) -> str:
        if self.kind == "service":
            return f'  {self.id}(("{self.label}"))'
        return f'  {self.id}["{self.label}"]'


@dataclass
class GraphEdge:
    """图边：挂载 / 提供 / 依赖。"""

    source: str
    target: str
    kind: str  # "child" | "provides" | "depends"
    optional: bool = False

    def mermaid(self) -> str:
        if self.kind == "child":
            return f"  {self.source} --> {self.target}"
        if self.kind == "provides":
            return f"  {self.source} -.->|provides| {self.target}"
        style = "-.->" if self.optional else "-->"
        tag = "optional" if self.optional else "requires"
        return f"  {self.source} {style}|{tag}| {self.target}"


@dataclass
class DependencyGraph:
    """依赖图：节点 + 边 + 文本导出。"""

    nodes: List[GraphNode] = field(default_factory=list)
    edges: List[GraphEdge] = field(default_factory=list)

    def to_mermaid(self, title: str = "") -> str:
        """导出 mermaid flowchart（TD 方向）。"""
        lines = ["graph TD"]
        for node in self.nodes:
            lines.append(node.mermaid())
        for edge in self.edges:
            lines.append(edge.mermaid())
        return "\n".join(lines)

    def to_dot(self) -> str:
        """导出 Graphviz dot。"""
        lines = ["digraph zero {"]
        for node in self.nodes:
            shape = "ellipse" if node.kind == "service" else "box"
            lines.append(f'  "{node.id}" [label="{node.label}", shape={shape}];')
        for edge in self.edges:
            style = "dashed" if (edge.optional or edge.kind == "provides") else "solid"
            lines.append(f'  "{edge.source}" -> "{edge.target}" [label="{edge.kind}", style={style}];')
        lines.append("}")
        return "\n".join(lines)

    def services(self) -> List[str]:
        return [n.label for n in self.nodes if n.kind == "service"]

    def dependents_of(self, service: str) -> List[str]:
        """依赖某服务的上下文路径（匹配该服务的所有 realm 实例）。"""
        ids = {n.id for n in self.nodes if n.kind == "service" and n.label.split("@")[0] == service}
        return [e.source for e in self.edges if e.target in ids and e.kind == "depends"]

    def provider_of(self, service: str) -> Optional[str]:
        """提供某服务的上下文（多个 realm 时返回第一个）。"""
        ids = {n.id for n in self.nodes if n.kind == "service" and n.label.split("@")[0] == service}
        for edge in self.edges:
            if edge.target in ids and edge.kind == "provides":
                return edge.source
        return None


def _node_id(raw: str) -> str:
    """mermaid / dot 节点 id 合法化。"""
    return re.sub(r"[^0-9a-zA-Z_]", "_", raw)


def build_graph(root: "Context") -> DependencyGraph:
    """从上下文树构建依赖图。"""
    graph = DependencyGraph()
    seen_services: Set[str] = set()
    seen_nodes: Set[str] = set()

    def _visit(ctx: "Context") -> str:
        ctx_id = _node_id(f"ctx_{ctx.path}")
        if ctx_id not in seen_nodes:
            seen_nodes.add(ctx_id)
            graph.nodes.append(GraphNode(id=ctx_id, label=ctx.name, kind="context"))

        for realm, key in ctx._provided:
            label = f"{key}@{realm}" if realm else key
            svc_id = _node_id(f"svc:{label}")
            if svc_id not in seen_services:
                seen_services.add(svc_id)
                graph.nodes.append(GraphNode(id=svc_id, label=label, kind="service"))
            graph.edges.append(GraphEdge(source=ctx_id, target=svc_id, kind="provides"))

        spec = ctx._inject_spec
        if spec is not None:
            for key in spec.required:
                target_label = f"{key}@{ctx._realm_for(key)}" if ctx._realm_for(key) else key
                graph.edges.append(GraphEdge(source=ctx_id, target=_node_id(f"svc:{target_label}"), kind="depends"))
            for key in spec.optional:
                realm = ctx._realm_for(key)
                target_label = f"{key}@{realm}" if realm else key
                graph.edges.append(
                    GraphEdge(source=ctx_id, target=_node_id(f"svc:{target_label}"), kind="depends", optional=True)
                )

        for child in ctx.children:
            child_id = _visit(child)
            graph.edges.append(GraphEdge(source=ctx_id, target=child_id, kind="child"))
        return ctx_id

    _visit(root)
    return graph


def build_index(graph: DependencyGraph) -> Dict[str, List[str]]:
    """服务 → 依赖者列表（诊断快捷方式）。"""
    index: Dict[str, List[str]] = {}
    for node in graph.nodes:
        if node.kind == "service":
            index[node.label] = graph.dependents_of(node.label)
    return index
