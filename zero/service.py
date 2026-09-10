# -*- coding: utf-8 -*-
"""服务与依赖注入（M1）。

两条规则决定了 zero 的热重载可靠性：

1. **依赖未就绪则插件不加载**：不是加载后崩溃，是压根不进插件体，
   因此不会留下半截副作用（注册了一半的监听、建了一半的连接）。
2. **被依赖的服务变更时，依赖者自动回滚重启**：服务替换、提供者卸载，
   都会让依赖它的插件走一遍 dispose → 重新加载。

``inject`` 声明依赖，``Service`` 提供带生命周期的服务实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Iterable, Optional, Tuple, TypeVar, Union

T = TypeVar("T")


@dataclass(frozen=True)
class InjectSpec:
    """插件的依赖声明。

    required：缺失则插件不加载（生命周期依赖该服务）
    optional：缺失也能加载，取值为 None，但服务变更同样会触发重启
    """

    required: Tuple[str, ...] = ()
    optional: Tuple[str, ...] = ()

    @property
    def all_keys(self) -> Tuple[str, ...]:
        return tuple(self.required) + tuple(self.optional)


def inject(
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
) -> Callable[[T], T]:
    """声明插件依赖（装饰器）。

    @inject(required=["db"], optional=["cache"])
    def my_plugin(ctx):
        db = ctx["db"]      # 或 ctx.require("db")
        cache = ctx.get("cache")
    """
    spec = InjectSpec(required=tuple(required), optional=tuple(optional))

    def _decorate(target: T) -> T:
        setattr(target, "__zero_inject__", spec)
        return target

    return _decorate


def get_inject_spec(plugin: Any) -> Optional[InjectSpec]:
    """取插件的依赖声明（未声明返回 None）。"""
    spec = getattr(plugin, "__zero_inject__", None)
    return spec if isinstance(spec, InjectSpec) else None


class Service:
    """服务基类：由上下文托管的生命周期对象。

    子类可覆写 ``start`` / ``stop``：``ctx.provide`` 注册时调用 start，
    上下文销毁（或服务被替换）时调用 stop。

        class Database(Service):
            name = "db"

            def start(self):
                self.conn = connect()

            def stop(self):
                self.conn.close()
    """

    name: ClassVar[str] = ""

    def __init__(self, ctx: Any, name: str = "") -> None:
        self.ctx = ctx
        self.name = name or type(self).name or type(self).__name__

    def start(self) -> None:
        """服务启动（``provide`` 注册后立即调用）。"""

    def stop(self) -> None:
        """服务停止（上下文销毁 / 服务被替换时调用）。"""

    def __repr__(self) -> str:
        return f"<Service {self.name}>"


def resolve_name(target: Union[type, Service, Any], fallback: str) -> str:
    """推导服务名：Service 子类用 name，其余用类名。"""
    name = getattr(target, "name", "")
    if isinstance(name, str) and name:
        return name
    if isinstance(target, type):
        return target.__name__
    return type(target).__name__ or fallback
