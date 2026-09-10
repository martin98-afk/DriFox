# -*- coding: utf-8 -*-
"""zero 异常体系。"""


class ZeroError(Exception):
    """zero 所有异常的基类。"""


class DisposedError(ZeroError):
    """对已销毁的上下文 / 作用域继续操作。"""


class MissingDependency(ZeroError):
    """插件声明的必需依赖未就绪。

    插件在依赖缺失时不会被加载，因此不会留下半截副作用。
    """

    def __init__(self, plugin: str, missing: list) -> None:
        self.plugin = plugin
        self.missing = list(missing)
        super().__init__(f"插件 {plugin!r} 缺少必需依赖: {', '.join(self.missing)}")


class PluginError(ZeroError):
    """插件加载 / 卸载过程中抛出的异常包装。"""

    def __init__(self, plugin: str, cause: BaseException) -> None:
        self.plugin = plugin
        self.cause = cause
        super().__init__(f"插件 {plugin!r} 执行失败: {cause!r}")


class CircularDependency(ZeroError):
    """插件注册表中存在循环依赖，无法推导加载顺序。"""

    def __init__(self, cycle: list) -> None:
        self.cycle = list(cycle)
        super().__init__(f"循环依赖: {' -> '.join(self.cycle)}")
