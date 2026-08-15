# -*- coding: utf-8 -*-
"""
插件工具加载器 — 扫描 plugins/*/tools/*.py 的 register(registry) 入口

约定：
- 插件目录：`plugins/<name>/tools/<tool>.py`
- 每个工具文件暴露 `register(registry)` 函数（入参 ToolRegistry 代理），
  内部调用 registry.register(name, schema, impl=..., danger=..., icon=...,
  cn_name=..., group=..., description=..., aliases=...)
- 危险级别强制校验：register 必须显式声明 danger（registry 层拒绝未声明插件工具）

扫描范围（含系统插件）：
- 工作树 `plugins/`（含 `plugins/system/tools/` 系统内置工具插件）
- 用户插件目录 `<app_data>/plugins/`

热重载：
- 后台线程轮询签名（path, mtime, size），变更触发全量重扫
- 幂等：重扫按插件名清理已不存在文件的注册
- 并发安全：registry 版本号 + 执行快照（正在执行的工具调用不中断）

优先级：
- 多根遍历按顺序；同名工具「先注册者优先」（工作树 plugins/ 先扫 → system 内置优先）
- 同根内：已被其他插件占用的同名工具不覆盖
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from loguru import logger

from app.tools.registry import ToolRegistry


def _plugin_roots() -> List[Path]:
    """插件工具扫描根：项目 plugins/（含 system）+ 用户插件目录"""
    roots = [Path(__file__).resolve().parent.parent.parent / "plugins"]
    try:
        from app.utils.utils import get_app_data_dir

        roots.append(get_app_data_dir() / "plugins")
    except Exception:
        pass
    return roots


_PLUGIN_ROOTS: List[Path] = _plugin_roots()


def _iter_tool_modules(plugin_root: Path):
    """遍历插件目录下所有 tools/*.py（含 <name>/tools/ 与 <name>/<name>.py 结构）"""
    if not plugin_root.is_dir():
        return
    for plugin_dir in sorted(plugin_root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        tools_dir = plugin_dir / "tools"
        if tools_dir.is_dir():
            for py in sorted(tools_dir.glob("*.py")):
                if py.name.startswith("_"):
                    continue
                yield plugin_dir.name, py
        else:
            # 单文件插件：<name>/<name>.py 带 register 入口
            single = plugin_dir / f"{plugin_dir.name}.py"
            if single.exists():
                yield plugin_dir.name, single


class _PluginRegistryProxy:
    """插件注册代理 — 强制注入 source 与 danger 校验。

    插件代码的 `register(registry)` 收到的是本代理而非裸 ToolRegistry：
    - source 强制为 `plugin:<plugin_name>`（忽略插件自报，防伪造 builtin 绕过 danger 校验）
    - danger 仍必须显式声明（裸 registry 的校验继续生效）
    - 同名保护：已被其他源注册的同名工具不覆盖（先注册者优先）
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        plugin_name: str,
        root: Optional[Path] = None,
        root_tracker: Optional[dict] = None,
    ):
        self._registry = registry
        self._plugin_name = plugin_name
        self._root = root
        # root_tracker: {tool_name: 来源根路径}（跨根优先级判定）
        self._root_tracker = root_tracker if root_tracker is not None else {}

    def register(self, name, schema, impl=None, danger=None, source=None, metadata=None, **meta) -> bool:
        """注册（透传全部元数据：icon/cn_name/group/description/aliases）"""
        # 跨根同名保护（先注册者优先）：同名工具已被**其他根**注册时不覆盖
        if self._root is not None:
            src_root = self._root_tracker.get(name)
            if src_root is not None and src_root != self._root:
                return False
        # 同根内：已被其他插件/内置占用时不覆盖
        existing = self._registry.get(name)
        if existing is not None and existing.source != f"plugin:{self._plugin_name}":
            return False
        # 插件自带图标目录：<plugin>/tools/icons/（深色）+ tools/icons_light/（浅色，icon 自包含）
        icon_dir = ""
        icon_dir_light = ""
        if self._root is not None:
            _icons = Path(self._root) / self._plugin_name / "tools" / "icons"
            if _icons.is_dir():
                icon_dir = str(_icons)
            _icons_light = Path(self._root) / self._plugin_name / "tools" / "icons_light"
            if _icons_light.is_dir():
                icon_dir_light = str(_icons_light)
        ok = self._registry.register(
            name,
            schema,
            impl=impl,
            danger=danger,
            icon=meta.get("icon", ""),
            icon_dir=icon_dir,
            icon_dir_light=icon_dir_light,
            cn_name=meta.get("cn_name", ""),
            group=meta.get("group", ""),
            description=meta.get("description", ""),
            aliases=meta.get("aliases"),
            source=f"plugin:{self._plugin_name}",
            metadata=metadata,
        )
        if ok and self._root is not None:
            self._root_tracker[name] = self._root
        return ok


def _load_module(plugin_name: str, path: Path):
    """加载插件工具模块（唯一模块名，避免命名冲突）。

    显式 compile 绕过 SourceFileLoader 的 __pycache__ 缓存：
    Windows 文件系统 mtime 精度低，快速写入后 exec 可能误复用旧 pyc
    （热重载内容不更新）——直接读文件编译最可靠。
    """
    mod_name = f"_plugin_tool_{plugin_name}_{path.stem}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        logger.warning(f"[PluginToolLoader] 无法加载 {path}")
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        source = path.read_text(encoding="utf-8")
        code = compile(source, str(path), "exec")
    except (OSError, SyntaxError) as e:
        logger.warning(f"[PluginToolLoader] 读取/编译失败 {path}: {e}")
        return None
    exec(code, module.__dict__)
    return module


def _run_register(
    registry: ToolRegistry,
    plugin_name: str,
    py_path: Path,
    root: Path,
    root_tracker: Dict[str, Path],
) -> Set[str]:
    """执行单个工具文件的 register，返回新注册的工具名集合"""
    module = _load_module(plugin_name, py_path)
    register_fn = getattr(module, "register", None) if module else None
    if not callable(register_fn):
        return set()
    before = set(registry.names())
    register_fn(_PluginRegistryProxy(registry, plugin_name, root=root, root_tracker=root_tracker))
    after = set(registry.names())
    new_tools = after - before
    if new_tools:
        logger.info(f"[PluginToolLoader] 插件 {plugin_name} 注册工具: {sorted(new_tools)}")
    return new_tools


def load_plugin_tools(
    registry: Optional[ToolRegistry] = None,
    plugin_roots: Optional[List[Path]] = None,
) -> Dict[str, Set[str]]:
    """扫描并注册全部插件工具（含系统插件）。

    Returns:
        {plugin_name: {tool_name, ...}} — 本次加载的插件工具映射
    """
    registry = registry or ToolRegistry.get_instance()
    roots = plugin_roots if plugin_roots is not None else _PLUGIN_ROOTS
    loaded: Dict[str, Set[str]] = {}
    root_tracker: Dict[str, Path] = {}  # tool_name -> 来源根（跨根优先级）

    for root in roots:
        for plugin_name, py_path in _iter_tool_modules(Path(root)):
            try:
                new_tools = _run_register(registry, plugin_name, py_path, Path(root), root_tracker)
                loaded.setdefault(plugin_name, set()).update(new_tools)
            except Exception as e:
                logger.warning(f"[PluginToolLoader] 插件 {plugin_name} register 失败: {e}")
    return loaded


def unload_plugin_tools(plugin_name: str, tool_names: Set[str], registry: Optional[ToolRegistry] = None) -> None:
    """注销指定插件的工具（热重载/插件卸载时调用，幂等）"""
    registry = registry or ToolRegistry.get_instance()
    for name in tool_names:
        reg = registry.get(name)
        if reg is not None and reg.source == f"plugin:{plugin_name}":
            registry.unregister(name)
            logger.info(f"[PluginToolLoader] 注销插件工具: {name} ({plugin_name})")


class PluginToolWatcher:
    """插件工具热重载 watcher：轮询签名检测变更，变更时全量重扫（幂等）。

    用法：:
        watcher = PluginToolWatcher()
        watcher.start()          # 后台线程轮询
        watcher.stop()
        watcher.scan_now()       # 手动触发一次全量重扫
    """

    def __init__(self, registry: Optional[ToolRegistry] = None, roots: Optional[List[Path]] = None):
        self._registry = registry or ToolRegistry.get_instance()
        self._roots = roots if roots is not None else _PLUGIN_ROOTS
        self._loaded: Dict[str, Set[str]] = {}  # plugin_name -> tool_names（当前生效）
        self._root_tracker: Dict[str, Path] = {}  # tool_name -> 来源根（跨根优先级）
        self._thread = None
        self._stop = False

    def scan_now(self) -> None:
        """全量重扫：先注销已加载插件的全部工具，再全量重新注册（幂等）。

        ⚠️ 修复：旧实现用「注册前后 diff（after-before）」记录 _loaded，
        热更新场景（工具已注册、文件内容变更）diff 为空集，导致 _loaded
        记录丢失 → 后续删除文件时无法注销残留工具。改为先注销再全量重扫，
        _loaded 始终等于 registry 中该插件的实际工具集。
        """
        # 1) 注销已加载插件的全部工具（幂等；执行中的调用不受影响——快照机制）
        for plugin_name, old_names in self._loaded.items():
            unload_plugin_tools(plugin_name, old_names, self._registry)
        # 2) 全量重扫注册（含跨根优先级保护，load_plugin_tools 内部处理）
        try:
            self._loaded = load_plugin_tools(registry=self._registry, plugin_roots=self._roots)
        except Exception as e:
            logger.warning(f"[PluginToolWatcher] 全量重扫失败: {e}")
            # 重扫失败：registry 可能已被部分修改，下次 scan 会重新注销+重扫

    def start(self, poll_interval: float = 2.0) -> None:
        """后台线程轮询监听（轻量轮询，避免线程模型冲突）"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False

        def _loop():
            last_sig = self._signature()
            while not self._stop:
                time.sleep(poll_interval)
                sig = self._signature()
                if sig != last_sig:
                    logger.info("[PluginToolWatcher] 检测到插件工具目录变更，重扫")
                    try:
                        self.scan_now()
                    except Exception as e:
                        logger.warning(f"[PluginToolWatcher] 重扫失败: {e}")
                    last_sig = self._signature()

        self._thread = threading.Thread(target=_loop, daemon=True, name="plugin-tool-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _signature(self) -> Tuple:
        """目录变更指纹：(path, mtime, size) 列表（多根聚合）"""
        sig = []
        for root in self._roots:
            for plugin_name, py in _iter_tool_modules(Path(root)):
                try:
                    st = py.stat()
                    sig.append((str(py), st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        return tuple(sig)


# ========== 进程级惰性启动 ==========

_plugin_watcher: Optional[PluginToolWatcher] = None
_plugin_watcher_lock = threading.Lock()


def ensure_plugin_tool_watcher() -> Optional[PluginToolWatcher]:
    """确保插件工具热重载 watcher 已启动（进程级一次，幂等）"""
    global _plugin_watcher
    if _plugin_watcher is not None:
        return _plugin_watcher
    with _plugin_watcher_lock:
        if _plugin_watcher is not None:
            return _plugin_watcher
        try:
            _plugin_watcher = PluginToolWatcher()
            _plugin_watcher.start()
            logger.info("[PluginToolWatcher] 插件工具热重载 watcher 已启动")
        except Exception as e:
            logger.warning(f"[PluginToolWatcher] watcher 启动失败: {e}")
    return _plugin_watcher
