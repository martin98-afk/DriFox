# -*- coding: utf-8 -*-
"""
服务商插件加载器 — 扫描 plugins/*/providers/*.py 的 register(registry) 入口

约定（与 tools 插件完全对称）：
- 插件目录：`plugins/<name>/providers/<provider>.py`
- 每个文件暴露 `register(registry)` 函数（入参 ProviderRegistry 代理），
  内部调用 registry.register(ProviderDef(...)) 或 registry.define(name=..., ...)
- 服务商定义（icon/url/模型/能力/余额/用量 fetcher）完全由插件内联声明

扫描范围（含系统插件）：
- 工作树 `plugins/`（含 `plugins/system/providers/` 系统内置服务商插件）
- 用户插件目录 `<app_data>/plugins/`

热重载：
- 后台线程轮询签名（path, mtime, size），变更触发全量重扫
- 幂等：重扫按插件名清理已不存在文件的注册
- 优先级：user 插件可覆盖 system 插件的同名服务商（user > system）
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

from loguru import logger

from app.plugins.registries.provider_registry import ProviderRegistry

# 插件根等级：system < user。
_ROOT_KIND_SYSTEM = "system"
_ROOT_KIND_USER = "user"
_ROOT_KIND_PRIORITY: Dict[str, int] = {_ROOT_KIND_SYSTEM: 0, _ROOT_KIND_USER: 1}


def _plugin_roots() -> List[Path]:
    """服务商插件扫描根：项目 plugins/（含 system）+ 用户插件目录"""
    roots = [Path(__file__).resolve().parent.parent.parent.parent / "plugins"]
    try:
        from app.utils.utils import get_app_data_dir

        roots.append(get_app_data_dir() / "plugins")
    except Exception:
        pass
    return roots


def _root_kind(root: Optional[Path]) -> str:
    """识别 root 等级：项目工作树 → system；app_data/plugins → user"""
    if root is None:
        return _ROOT_KIND_SYSTEM
    try:
        from app.utils.utils import get_app_data_dir

        user_root = get_app_data_dir() / "plugins"
        if root == user_root:
            return _ROOT_KIND_USER
    except Exception:
        pass
    return _ROOT_KIND_SYSTEM


_PLUGIN_ROOTS: List[Path] = _plugin_roots()


def _is_plugin_load_blocked(plugin_name: str) -> bool:
    """P1/P2：检查插件是否被版本/平台门禁拦截（load_blocked）。

    仅在 PluginManager 已初始化且能查到插件时检查；否则视为不拦截（放行）。
    拦截的插件其服务商不进 registry——已注册过的会在后续重扫中被清理。
    """
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if not pm.is_initialized():
            return False
        plugin = pm.get_plugin(plugin_name)
        if plugin is None:
            return False
        return bool(getattr(plugin, "load_blocked", False))
    except Exception as e:
        logger.warning(f"[ProviderLoader] 门禁检查失败，默认放行 {plugin_name}: {e}")
        return False


def _is_plugin_enabled(plugin_name: str) -> bool:
    """按插件启用状态过滤服务商加载（对齐 PluginToolLoader._is_plugin_enabled）。

    统一以 Settings.enabled_plugins 为准。pm 未初始化时（真实启动链中
    app.plugins 可能在 backend.initialize 之前被导入）直接读 Settings。
    """
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if pm.is_initialized():
            if not pm.has_plugin(plugin_name):
                return True
            return pm.is_enabled(plugin_name)
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = cfg.enabled_plugins.value or []
        disabled = cfg.disabled_plugins.value or []
        if not saved and not disabled:
            return True
        return plugin_name in saved
    except Exception as e:
        logger.warning(f"[ProviderLoader] 插件启用状态检查失败，默认加载 {plugin_name}: {e}")
        return True


def _is_component_enabled(plugin_name: str, component: str = "providers") -> bool:
    """按组件级禁用集过滤服务商加载（D9：插件内部 providers 子项开关）

    与 _is_plugin_enabled 同源策略：pm 已初始化时走 pm（带进程内缓存），
    否则直接读 Settings（导入期可用）。读取失败默认加载。
    """
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if pm.is_initialized():
            if not pm.has_plugin(plugin_name):
                return True
            return pm.is_component_enabled(plugin_name, component)
        from app.utils.config import Settings

        disabled = Settings.get_instance().disabled_plugin_components.value or []
        return f"{plugin_name}:{component}" not in set(disabled)
    except Exception as e:
        logger.warning(f"[ProviderLoader] 组件启停检查失败，默认加载 {plugin_name}: {e}")
        return True


def _iter_provider_modules(plugin_root: Path):
    """遍历插件目录下所有 providers/*.py"""
    if not plugin_root.is_dir():
        return
    for plugin_dir in sorted(plugin_root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        providers_dir = plugin_dir / "providers"
        if providers_dir.is_dir():
            for py in sorted(providers_dir.glob("*.py")):
                if py.name.startswith("_"):
                    continue
                yield plugin_dir.name, py


class _ProviderRegistryProxy:
    """服务商注册代理 — 强制注入 source 与同名覆盖规则。

    插件代码的 `register(registry)` 收到的是本代理而非裸 ProviderRegistry：
    - source 强制为 `plugin:<plugin_name>`（热重载清理依赖）
    - 同名覆盖规则（root_kind 优先级）：
        * 同根内：已被其他插件占用时不覆盖（先注册者优先；同插件重扫由 watcher 卸载兜底）
        * 跨根：user 插件（高优先级）可覆盖 system 插件（低优先级）的同名服务商
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        plugin_name: str,
        root: Optional[Path] = None,
        root_tracker: Optional[dict] = None,
    ):
        self._registry = registry
        self._plugin_name = plugin_name
        self._root = root
        self._root_kind = _root_kind(root)
        self._root_tracker = root_tracker if root_tracker is not None else {}
        self.registered_names: Set[str] = set()

    def register(self, provider, source: str = "") -> bool:
        """注册一个服务商定义（ProviderDef 或任意带 source 属性的定义对象）

        返回 True 表示注册成功；False 表示被同名保护拒绝或 ProviderDef 非法。
        """
        from app.plugins.registries.provider_registry import ProviderDef

        if isinstance(provider, ProviderDef):
            name = provider.name
        elif hasattr(provider, "name"):
            name = provider.name
        else:
            logger.warning(f"[ProviderLoader] 非法服务商定义（缺少 name）: {provider}")
            return False

        # 插件自带图标目录：<plugin>/providers/icons/（深色）+ providers/icons_light/
        # （浅色，icon 自包含，与 tools 机制对称）
        if isinstance(provider, ProviderDef):
            if self._root is not None:
                _icons = Path(self._root) / self._plugin_name / "providers" / "icons"
                if _icons.is_dir():
                    provider.icon_dir = str(_icons)
                _icons_light = Path(self._root) / self._plugin_name / "providers" / "icons_light"
                if _icons_light.is_dir():
                    provider.icon_dir_light = str(_icons_light)

        # 同名覆盖判定（root_kind 优先级）
        src_root = self._root_tracker.get(name) if self._root is not None else None
        existing = self._registry.get(name)
        is_cross_root = src_root is not None and src_root != self._root
        if is_cross_root:
            # 跨根：仅允许高等级覆盖低等级
            if _ROOT_KIND_PRIORITY.get(self._root_kind, 0) <= _ROOT_KIND_PRIORITY.get(_root_kind(src_root), 0):
                return False
        else:
            # 同根/无 root/首次注册：已被其他插件占用时不覆盖
            if existing is not None and existing.source != f"plugin:{self._plugin_name}":
                return False

        # 覆盖场景：先卸载旧来源（跨根 user 覆盖 system，或无 root 追踪时的同插件重载）
        if existing is not None and existing.source != f"plugin:{self._plugin_name}":
            self._registry.clear_source(existing.source)

        source = source or f"plugin:{self._plugin_name}"
        ok = self._registry.register(provider, source=source)
        if ok:
            self.registered_names.add(name)
            if self._root is not None:
                self._root_tracker[name] = self._root
        return ok


def _load_module(plugin_name: str, path: Path):
    """加载服务商插件模块（唯一模块名，避免命名冲突；显式 compile 绕过 pyc 缓存）"""
    mod_name = f"_plugin_provider_{plugin_name}_{path.stem}"
    sys.modules.pop(mod_name, None)
    # P5/P1b：exec 前 AST 聚合门（parse-once）——sys.modules 声明式放行判定
    # + register 入口检查 + 危险 import 审计，共享单次 ast.parse（原为 2 次）。
    # 服务商约定与 tool 一致：必须 register(registry)。
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"[ProviderLoader] 读取 {path} 失败: {e}")
        return None
    from app.plugins.loaders._ast_guard import guard_plugin_module_once

    guard = guard_plugin_module_once(
        source, path, require_register=True, component="ProviderLoader", plugin_dir=path.parent.parent
    )
    if guard.syntax_error or guard.rejected_writes or not guard.has_register:
        sys.modules.pop(mod_name, None)
        # 拒载原因已由聚合门输出 warning
        return None
    if guard.dangerous_imports:
        # A4：危险 import 审计（仅日志告警，不拒载）——对齐 tool/runtime loader 审计口径。
        _audit_detail = "; ".join(f"line {ln}: {sym}" for ln, sym in guard.dangerous_imports)
        logger.warning(
            f"[ProviderLoader] [AST审计] 插件 {plugin_name} 服务商模块含模块级危险 import"
            f"（已放行，仅告警）: {_audit_detail} ({path})"
        )
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        logger.warning(f"[ProviderLoader] 无法加载 {path}")
        return None
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__builtins__"] = __builtins__
    sys.modules[mod_name] = module
    try:
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except Exception as e:
        logger.warning(f"[ProviderLoader] 加载 {path} 失败: {e}")
        sys.modules.pop(mod_name, None)
        return None
    return module


def _run_register(
    registry: ProviderRegistry, plugin_name: str, py_path: Path, root: Path, root_tracker: dict
) -> Set[str]:
    """执行单个 providers/*.py 的 register(registry)，返回注册成功的服务商名集合"""
    module = _load_module(plugin_name, py_path)
    if module is None:
        return set()
    register_fn = getattr(module, "register", None)
    if not callable(register_fn):
        logger.warning(f"[ProviderLoader] {py_path} 缺少 register(registry) 入口")
        return set()

    proxy = _ProviderRegistryProxy(registry, plugin_name, root, root_tracker)
    try:
        register_fn(proxy)
    except Exception:
        # 部分成功回滚：失败插件不留下半套服务商定义
        for name in proxy.registered_names:
            p = registry.get(name)
            if p is not None and p.source == f"plugin:{plugin_name}":
                registry.clear_source(f"plugin:{plugin_name}")
                logger.warning(f"[ProviderLoader] 回滚部分注册服务商: {name} ({plugin_name})")
        raise
    names = proxy.registered_names
    if names:
        logger.info(f"[ProviderLoader] 插件 {plugin_name} 注册服务商: {sorted(names)}")
    return names


def load_providers(
    registry: Optional[ProviderRegistry] = None,
    plugin_roots: Optional[List[Path]] = None,
    root_tracker: Optional[Dict[str, Path]] = None,
) -> Dict[str, Set[str]]:
    """扫描并注册全部服务商插件（含系统插件）。

    Returns:
        {plugin_name: {provider_name, ...}} — 本次加载的插件服务商映射
    """
    registry = registry or ProviderRegistry.get_instance()
    roots = plugin_roots if plugin_roots is not None else _PLUGIN_ROOTS
    loaded: Dict[str, Set[str]] = {}
    if root_tracker is None:
        root_tracker = {}

    for root in roots:
        for plugin_name, py_path in _iter_provider_modules(Path(root)):
            # 对齐插件工具：插件被 Settings 禁用后其服务商不再注册
            if not _is_plugin_enabled(plugin_name):
                logger.info(f"[ProviderLoader] 跳过已禁用插件的服务商: {plugin_name}")
                continue
            # D9：providers 组件整类停用时跳过
            if not _is_component_enabled(plugin_name):
                logger.info(f"[ProviderLoader] 跳过 providers 组件已停用的插件: {plugin_name}")
                continue
            # P1/P2：版本/平台门禁——load_blocked 插件不加载其服务商
            if _is_plugin_load_blocked(plugin_name):
                logger.warning(f"[ProviderLoader] 跳过被门禁拦截插件的服务商: {plugin_name}")
                continue
            try:
                new_names = _run_register(registry, plugin_name, py_path, Path(root), root_tracker)
                loaded.setdefault(plugin_name, set()).update(new_names)
            except Exception as e:
                logger.warning(f"[ProviderLoader] 插件 {plugin_name} register 失败: {e}")
    return loaded


class ProviderWatcher:
    """服务商插件热重载 watcher：轮询签名检测变更，变更时全量重扫（幂等）。

    与 PluginToolWatcher 同构：签名 (path, mtime, size) 变更 → 全量重扫。
    """

    def __init__(self, registry: Optional[ProviderRegistry] = None, roots: Optional[List[Path]] = None):
        self._registry = registry or ProviderRegistry.get_instance()
        self._roots = roots if roots is not None else _PLUGIN_ROOTS
        self._root_tracker: Dict[str, Path] = {}
        self._scan_lock = threading.Lock()

    def scan_now(self) -> None:
        """全量重扫：先注销注册表中全部插件来源服务商，再全量重新注册（幂等）。

        卸载以注册表实际内容为准（而非 watcher 自身的加载记忆）：
        启动链 warmup_providers() 直接注册不经 watcher，且重扫时同名保护
        会拒绝重复注册使记忆失真（永远为空），按记忆卸载会漏清已删除
        文件对应的服务商（残留 bug 回归点，见 tests/core/test_provider_watcher.py）。
        """
        with self._scan_lock:
            # 1) 按注册表实际内容注销全部插件来源
            for source in self._registry.provider_sources():
                self._registry.clear_source(source)
            self._root_tracker = {}
            # 2) 全量重扫注册
            try:
                load_providers(
                    registry=self._registry,
                    plugin_roots=self._roots,
                    root_tracker=self._root_tracker,
                )
            except Exception as e:
                logger.warning(f"[ProviderWatcher] 全量重扫失败: {e}")

    def _plugin_provider_names(self, plugin_name: str) -> Set[str]:
        """按注册表实际内容反查某插件注册的服务商名（对齐 scan_now 的"以注册表为准"）"""
        source = f"plugin:{plugin_name}"
        return {n for n in self._registry.names() if self._registry.get(n).source == source}

    def _root_of_plugin(self, plugin_name: str) -> Optional[Path]:
        """从 root_tracker 反推某插件名所属扫描根（磁盘目录可能已删除）"""
        for name in self._plugin_provider_names(plugin_name):
            r = self._root_tracker.get(name)
            if r is not None:
                return Path(r)
        return None

    def _restore_lower_roots(self, removed_rank: int) -> None:
        """跨根覆盖恢复：重扫比目标根更低等级的所有根（被覆盖方），补注册被覆盖服务商"""
        for root in self._roots:
            if _ROOT_KIND_PRIORITY.get(_root_kind(root), 0) >= removed_rank:
                continue
            load_providers(
                registry=self._registry,
                plugin_roots=[Path(root)],
                root_tracker=self._root_tracker,
            )

    def unload_plugin(self, plugin_name: str) -> None:
        """精准卸载单个插件的服务商（删除/禁用路径），不波及他插件

        与 scan_now 的区别：只注销目标插件来源的服务商，并对更低等级根重扫
        一次恢复被其覆盖的同名服务商（跨根覆盖恢复），不触发全量重扫。
        """
        with self._scan_lock:
            names = self._plugin_provider_names(plugin_name)
            removed_root = self._root_of_plugin(plugin_name)
            removed_rank = _ROOT_KIND_PRIORITY.get(_root_kind(removed_root), 0)
            if names:
                self._registry.clear_source(f"plugin:{plugin_name}")
                for n in names:
                    self._root_tracker.pop(n, None)
                logger.info(f"[ProviderWatcher] 注销插件服务商: {sorted(names)} ({plugin_name})")
            # 跨根覆盖恢复：仅重扫比目标根更低等级的根（被覆盖方）
            self._restore_lower_roots(removed_rank)

    def reload_plugin(self, plugin_name: str) -> None:
        """精准重载单个插件的服务商（安装/更新/启用路径），不波及他插件

        语义：注销该插件旧服务商 → 恢复被其覆盖的低等级根同名服务商 →
        重新注册该插件当前模块（高等级根再次覆盖）→ 最终状态与全量重扫一致。
        """
        with self._scan_lock:
            names = self._plugin_provider_names(plugin_name)
            removed_root = self._root_of_plugin(plugin_name)
            removed_rank = _ROOT_KIND_PRIORITY.get(_root_kind(removed_root), 0)
            if names:
                self._registry.clear_source(f"plugin:{plugin_name}")
                for n in names:
                    self._root_tracker.pop(n, None)
                logger.info(f"[ProviderWatcher] 重载前注销插件服务商: {sorted(names)} ({plugin_name})")
            # 跨根覆盖恢复：仅重扫比目标根更低等级的根（被覆盖方）
            self._restore_lower_roots(removed_rank)
            # 重注册该插件当前模块（enabled 过滤与 load_providers 一致）
            if not _is_plugin_enabled(plugin_name):
                logger.info(f"[ProviderLoader] 跳过已禁用插件的服务商: {plugin_name}")
                return
            # D9：providers 组件整类被停用 → 只注销不重注册（与全量扫描一致）
            if not _is_component_enabled(plugin_name):
                logger.info(f"[ProviderLoader] 跳过 providers 组件已停用的重载: {plugin_name}")
                return
            # P1/P2：版本/平台门禁——load_blocked 插件不重注册其服务商
            if _is_plugin_load_blocked(plugin_name):
                logger.warning(f"[ProviderLoader] 跳过被门禁拦截插件的服务商重载: {plugin_name}")
                return
            for root in self._roots:
                root_path = Path(root)
                for pname, py in _iter_provider_modules(root_path):
                    if pname != plugin_name:
                        continue
                    _run_register(self._registry, plugin_name, py, root_path, self._root_tracker)

    def start(self, poll_interval: float = 2.0) -> None:
        """[已退役] 轮询线程不再启动。

        providers 组件变更改由 backend watchfiles 主链驱动：kernel
        KNOWN_COMPONENTS 已含 providers → _identify_all_components_from_changes
        识别 → builtin_reloaders._reload_providers 调 scan_now。本方法
        保留是为向后兼容旧调用点，空转即可。scan_now() 语义不变。
        """
        return


# ========== 进程级惰性启动 ==========

_provider_watcher: Optional[ProviderWatcher] = None
_provider_watcher_lock = threading.Lock()


def ensure_provider_watcher() -> Optional[ProviderWatcher]:
    """确保服务商插件热重载 watcher 已启动（进程级一次，幂等）"""
    global _provider_watcher
    if _provider_watcher is not None:
        return _provider_watcher
    with _provider_watcher_lock:
        if _provider_watcher is not None:
            return _provider_watcher
        try:
            _provider_watcher = ProviderWatcher()
            _provider_watcher.start()
            logger.info("[ProviderWatcher] 服务商插件热重载 watcher 已启动")
        except Exception as e:
            logger.warning(f"[ProviderWatcher] watcher 启动失败: {e}")
    return _provider_watcher


def warmup_providers() -> Dict[str, Set[str]]:
    """启动期一次性加载全部服务商插件（拉齐 system + user）。

    在 ProviderWatcher.start() 之前调用，保证主程序启动即有完整服务商表。
    """
    registry = ProviderRegistry.get_instance()
    return load_providers(registry=registry)
