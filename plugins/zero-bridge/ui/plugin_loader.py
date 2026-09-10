# -*- coding: utf-8 -*-
"""zero 插件装载器（适配 DriFox 插件架构）。

zero 是 DriFox 的一种**组件类型**（kernel.KNOWN_COMPONENTS 中的 "zero"）：
插件目录下的 ``zero/`` 子目录，每个 ``*.py`` 文件是一个 zero 插件：

    NAME = "example"              # 可选，缺省用文件名
    PROVIDES = ("db",)            # 可选，声明提供的服务（拓扑排序用）
    @inject(required=[...])       # 可选，声明依赖
    def apply(ctx):               # 必需：插件体
        ...

监听 / 变更识别 / 触发全部复用 DriFox 的 PluginHostService 现有链路；
本装载器只做两件事：
- 按插件目录装载 ``zero/`` 下的组件文件（登记进共享 registry）
- 提供 reloader 入口（由 zero-bridge 注册到 ComponentReloaderRegistry），
  把 DriFox 识别出的变更转换为 zero 的「回滚 → 重装」

坑位防范：
- ``sys.modules`` 缓存 → 重载前 purge
- pyc 字节码缓存（mtime 秒级 + size 校验，同秒同大小编辑会执行过期 bytecode）
  → 改用 read_text + compile 直载，绕开 pyc 全部机制
- 坏插件不拖垮其他插件 → 单文件失败只记日志
- 跨插件重名 → 登记名带插件名前缀（``<plugin>.<stem>``）
"""

from __future__ import annotations

import logging
import sys
import types
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

from zero.service import get_inject_spec

if TYPE_CHECKING:
    from zero.context import Context

logger = logging.getLogger(__name__)

__all__ = ["ZeroPluginLoader"]

_PLUGIN_FN = "apply"


@dataclass
class _LoadedItem:
    """一条已装载的 zero 组件文件。"""

    key: str  # registry 登记名：<plugin_name>.<stem>
    path: Path
    module_name: str


@dataclass
class _LoadedPlugin:
    """一个 DriFox 插件的 zero 组件集合。"""

    plugin_name: str
    plugin_dir: Path
    items: Dict[str, _LoadedItem] = field(default_factory=dict)


class ZeroPluginLoader:
    """按 DriFox 插件目录装载 zero/ 组件（登记进共享 zero registry）。"""

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx
        self._plugins: Dict[str, _LoadedPlugin] = {}

    def loaded_plugins(self) -> List[str]:
        return sorted(self._plugins)

    # ── 装载 / 重载 / 卸载（reloader 与启动共用）───────────────────────
    def load_plugin(self, plugin_name: str, plugin_dir: Path) -> List[str]:
        """装载一个 DriFox 插件的 zero/ 组件目录，返回成功加载的组件 key。"""
        zero_dir = Path(plugin_dir) / "zero"
        if not zero_dir.is_dir():
            return []

        # 同插件重复装载先卸干净（幂等）
        self.unload_plugin(plugin_name)

        loaded = _LoadedPlugin(plugin_name=plugin_name, plugin_dir=Path(plugin_dir))
        for path in sorted(zero_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue  # _ 前缀是内部模块
            item = self._load_file(loaded, path)
            if item:
                loaded.items[item.key] = item

        if not loaded.items:
            return []

        self._plugins[plugin_name] = loaded
        # 统一走 registry 拓扑排序加载；单组件失败只停用自身
        forks = self._ctx.registry.load_all()
        loaded_keys = {f.name for f in forks}
        ok = [key for key in loaded.items if key in loaded_keys]
        logger.info(f"[zero-bridge] 插件 {plugin_name!r} 的 zero 组件已装载: {sorted(loaded.items)}")
        return ok

    def reload_plugin(self, plugin_name: str, plugin_dir: Path) -> bool:
        """重载一个插件的全部 zero 组件（旧 fork 全部回滚后重装）。"""
        if plugin_name in self._plugins:
            self.unload_plugin(plugin_name)
        return bool(self.load_plugin(plugin_name, plugin_dir))

    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载一个插件的全部 zero 组件（副作用回滚、模块 purge）。"""
        loaded = self._plugins.pop(plugin_name, None)
        if loaded is None:
            return False
        for key in loaded.items:
            self._ctx.registry.delete(key)
            self._purge(loaded.items[key].module_name)
        logger.info(f"[zero-bridge] 插件 {plugin_name!r} 的 zero 组件已卸载")
        return True

    # ── DriFox reloader 入口（由 zero-bridge 注册到 kernel）────────────
    def handle_reload(self, reload_ctx: Any) -> bool:
        """ComponentReloaderRegistry 的 reloader 签名。

        ReloadContext: plugin 为 None 表示插件被删除（走卸载），否则精准重载。
        """
        plugin_name = reload_ctx.plugin_name
        plugin = getattr(reload_ctx, "plugin", None)
        if plugin is None:
            return self.unload_plugin(plugin_name)
        plugin_dir = getattr(plugin, "path", None)
        if plugin_dir is None:
            return False
        return self.reload_plugin(plugin_name, plugin_dir)

    def keys_of(self, plugin_name: str) -> Set[str]:
        loaded = self._plugins.get(plugin_name)
        return set(loaded.items) if loaded else set()

    # ── 内部 ──────────────────────────────────────────────────────────
    def _load_file(self, loaded: _LoadedPlugin, path: Path) -> Optional[_LoadedItem]:
        try:
            module = self._import(path)
            plugin, name, provides = self._extract(path, module)
            key = f"{loaded.plugin_name}.{name}"
            with suppress(AttributeError, TypeError):
                plugin.__name__ = key  # fork 名与依赖错误消息带插件前缀
            self._ctx.registry.add(plugin, name=key, provides=tuple(provides))
            item = _LoadedItem(key=key, path=path, module_name=module.__name__)
            return item
        except Exception as e:
            logger.error(f"[zero-bridge] 装载失败 {path.name}: {e!r}")
            return None

    def _extract(self, path: Path, module: Any) -> tuple:
        """从模块提取 (plugin, name, provides)；缺 apply 视为无效组件。"""
        plugin: Optional[Callable[..., Any]] = getattr(module, _PLUGIN_FN, None)
        if plugin is None or not callable(plugin):
            raise ValueError(f"缺少 {_PLUGIN_FN}(ctx) 插件体")
        name = getattr(module, "NAME", "") or path.stem
        provides = getattr(module, "PROVIDES", ()) or ()
        if get_inject_spec(plugin) is None:
            pass  # 无依赖声明也合法
        return plugin, name, provides

    def _import(self, path: Path):
        """读源码直接 compile 执行（绕开 pyc 字节码缓存）。

        坑：SourceFileLoader 的 pyc 校验基于 (mtime 秒级, size)。编辑器快速
        保存时两个版本可能同秒同长度，导致 exec 执行过期 bytecode——症状是
        「改了没生效」。compile 源码字符串则每次都忠实执行当前文件内容。
        """
        module_name = f"zero_plugin_{path.stem}"
        self._purge(module_name)
        source = path.read_text(encoding="utf-8")
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        sys.modules[module_name] = module
        exec(compile(source, str(path), "exec"), module.__dict__)
        return module

    @staticmethod
    def _purge(module_name: str) -> None:
        sys.modules.pop(module_name, None)
