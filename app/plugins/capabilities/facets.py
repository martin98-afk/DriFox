# -*- coding: utf-8 -*-
"""能力面（facet）声明 —— 把「某能力暴露宿主的哪些方法」变成数据。

背景：`team.read` / `tool.read` 这类能力原先在两个 surface 的 provider 里
各手写一份 `{别名: 宿主方法}` 字典，键名必须逐字等于属性名。一次 typo 就
在取用瞬间废掉整个能力——`team.read` 曾引用不存在的
`TeamManager.get_member_agent_name`，导致 system-hooks 两个注入 hook 静默
空转、`team_list_members` 工具直接报 Execution error。

改为数据声明后：
- **白名单即能力边界**：不在名单上的方法插件根本看不见，写操作天然隔离
- 单处声明，tool / ui 两个 surface 共用
- 契约测试遍历 ``FACETS`` 对目标类断言每个名字存在，typo 在 CI 阶段就炸
- 目标对象临时缺方法（测试替身）时按 ``AttributeError`` 降级，调用方的
  ``getattr(..., None)`` 回退逻辑照常工作

provider 侧用法::

    def _team_read(host: Any):
        return build_facet("team.read")
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple


@dataclass(frozen=True)
class FacetSpec:
    """一条能力面声明。

    Attributes:
        target: ``"模块路径:单例类名"``，构造时调该类的 ``get_instance()``。
            每次取用都重新解析，测试 monkeypatch 单例时自动跟随。
        methods: 暴露的方法名白名单（必须是目标类真实存在的公开方法）。
    """

    target: str
    methods: Tuple[str, ...]

    def resolve(self) -> Any:
        """解析出目标单例实例（延迟 import，不缓存类引用）。"""
        module_name, _, cls_name = self.target.partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, cls_name).get_instance()


class ReadOnlyFacade:
    """白名单只读代理：名单外的属性访问一律 ``AttributeError``。

    不做属性缓存（目标对象的绑定方法随实例状态变化，缓存会拿到过期引用）。
    """

    __slots__ = ("_obj", "_methods", "_facet")

    def __init__(self, obj: Any, methods: Tuple[str, ...], facet: str) -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_methods", frozenset(methods))
        object.__setattr__(self, "_facet", facet)

    def __getattr__(self, name: str) -> Any:
        methods = object.__getattribute__(self, "_methods")
        if name not in methods:
            facet = object.__getattribute__(self, "_facet")
            raise AttributeError(f"能力面 {facet} 不暴露 {name}（白名单: {sorted(methods)}）")
        # 目标缺该方法时同样抛 AttributeError —— 调用方的 getattr 回退照常生效
        return getattr(object.__getattribute__(self, "_obj"), name)

    def __dir__(self) -> list:
        return sorted(object.__getattribute__(self, "_methods"))


# 团队只读面：成员表 / 模板 / 运行状态。
# 派任务、改状态等写操作刻意不在名单内（需要时另立写能力）。
_TEAM_READ = FacetSpec(
    target="app.core.team.team_manager:TeamManager",
    methods=(
        "get_members",
        "get_member_by_agent",
        "find_member",
        "is_team_member",
        "get_member_agent_name",
        "get_member_run_id",
        "get_member_capability",
        "get_member_busy_status",
        "get_running_tasks",
        "get_pending_tasks",
        "get_template",
        "get_template_for_run_id",
        "get_team_run_ids",
        "get_team_label",
        "get_team_label_by_run",
        "get_team_workdir",
    ),
)

FACETS: Dict[str, FacetSpec] = {
    "team.read": _TEAM_READ,
    # 团队写面：派发任务邮件（写对方邮箱）。与只读面分开授权，避免
    # 只读插件顺手拿到写入口。
    "team.send": FacetSpec(
        target="app.core.team.team_manager:TeamManager",
        methods=("send_task",),
    ),
}


def build_facet(facet_id: str) -> Callable[[], Any]:
    """构造能力的 ``api()`` 闭包（provider 直接返回它）。

    Raises:
        KeyError: 未在 ``FACETS`` 声明的能力 ID。
    """
    spec = FACETS[facet_id]

    def api() -> ReadOnlyFacade:
        return ReadOnlyFacade(spec.resolve(), spec.methods, facet_id)

    return api
