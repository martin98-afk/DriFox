# -*- coding: utf-8 -*-
"""
插件管理器（PluginManager）

核心职责：
1. 扫描插件目录（系统 + 用户），发现插件
2. 解析插件清单 (.drifox-plugin/plugin.json)
3. 向各子系统提供插件资源路径查询接口
4. 管理 MCP 配置合并

插件目录结构：
    project-root/
    ├── plugins/
    │   └── system/              # 系统内置插件（打包在 exe）
    │       ├── .drifox-plugin/
    │       │   └── plugin.json  # 插件清单
    │       ├── commands/        # 系统命令
    │       ├── agents/          # 系统智能体
    │       ├── skills/          # 系统技能
    │       ├── themes/          # 系统主题
    │       └── .mcp.json        # 默认 MCP 配置
    └── app/
        └── core/
            └── plugin_manager.py

    ~/.drifox/
    └── plugins/                 # 用户安装的插件
        └── {plugin-name}/
            └── ...

类型约定：
    - "system": 系统内置插件（project-root/plugins/）
    - "user": 用户安装插件（~/.drifox/plugins/）

命名空间约定：
    - system 插件：命令/智能体直接用短名称（/new, /explore）
    - user 插件：命令/智能体添加命名空间前缀（/my-plugin:command）
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from loguru import logger

# 组件探测规则表（单一事实源在 kernel.KNOWN_COMPONENTS，此处只定义物理探测谓词）
from app.plugins.kernel import KNOWN_COMPONENTS

# 插件平台声明与 deps 统一加载（设计：docs/superpowers/specs/2026-08-27-plugin-platform-deps-design.md）
from app.plugins.deps_loader import check_platform, ensure_deps_on_path

# 插件名合法字符：首字符必须为字母或数字，后续允许字母数字与 _ . -
# 严禁路径分隔符与 `..`（plugin_name 会参与配置存储路径拼接，见 plugin_config_store）
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _normalize_plugin_name(raw: object, fallback: str) -> Optional[str]:
    """把清单里的 name 归一为安全插件名；非法返回 None（调用方跳过该插件）。

    防护点：
    ① 类型污染 —— `{"name": 123}` 会让 int 成为 _plugins 的 key，后续
       ui_plugin_registry 的 str 方法（.lower()/.replace()）抛 AttributeError；
    ② 路径穿越 —— name 参与 `<app_data>/plugin_data/<name>/config.json` 拼接，
       `../../` 可写到宿主任意位置。
    """
    if isinstance(raw, str) and _PLUGIN_NAME_RE.match(raw):
        return raw
    if _PLUGIN_NAME_RE.match(fallback):
        if raw is not None:
            # 清单 name 非法但目录名合法 —— 回退到目录名，保证存量插件不被误杀
            logger.warning(f"[PluginManager] 插件清单 name 非法 {raw!r}，回退为目录名 {fallback!r}")
        return fallback
    return None

# 组件物理探测谓词：按 kernel.KNOWN_COMPONENTS 顺序遍历，物理目录/根文件命中即标记
# 探测规则差异：hooks 需 hooks.json、ui 需 __init__.py、tools/providers 需 *.py、
# team_templates 需 *.yaml、其余只看子目录是否存在
_COMPONENT_PROBES: Dict[str, Callable[[Path], bool]] = {
    "commands": lambda d: (d / "commands").exists(),
    "agents": lambda d: (d / "agents").exists(),
    "skills": lambda d: (d / "skills").exists(),
    "themes": lambda d: (d / "themes").exists(),
    "hooks": lambda d: (d / "hooks").exists() and (d / "hooks" / "hooks.json").exists(),
    "mcp": lambda d: (d / ".mcp.json").exists(),
    "lsp": lambda d: (d / ".lsp.json").exists(),
    "ui": lambda d: (d / "ui").exists() and (d / "ui" / "__init__.py").exists(),
    "tools": lambda d: (d / "tools").exists() and any((d / "tools").glob("*.py")),
    "providers": lambda d: (d / "providers").exists() and any((d / "providers").glob("*.py")),
    "team_templates": lambda d: (d / "team_templates").exists() and any((d / "team_templates").glob("*.yaml")),
    "model_adapters": lambda d: (d / "model_adapters").exists() and any((d / "model_adapters").glob("*.py")),
    "loop_policies": lambda d: (d / "loop_policies").exists() and any((d / "loop_policies").glob("*.py")),
    "storages": lambda d: (d / "storages").exists() and any((d / "storages").glob("*.py")),
    "serializers": lambda d: (d / "serializers").exists() and any((d / "serializers").glob("*.py")),
    "gateways": lambda d: (d / "gateways").exists() and any((d / "gateways").glob("*.py")),
    "engines": lambda d: (d / "engines").exists() and any((d / "engines").glob("*.py")),
}


def _detect_components(plugin_dir: Path) -> Dict[str, bool]:
    """按 kernel.KNOWN_COMPONENTS 优先级探测插件目录实际组件（物理目录为准）

    返回 {component_name: True} 子集。新增组件类型只需在 kernel.KNOWN_COMPONENTS 登记
    并在 _COMPONENT_PROBES 加一条谓词即可，无需改动扫描逻辑。
    """
    return {name: True for name in KNOWN_COMPONENTS if _COMPONENT_PROBES[name](plugin_dir)}


# ============================================================
# 插件信息数据类
# ============================================================


@dataclass
class PluginInfo:
    """插件信息"""

    name: str  # 插件唯一标识（目录名）
    manifest: dict  # 原始清单数据
    path: Path  # 插件根目录
    plugin_type: str = "user"  # "system" | "user"
    platform_compatible: bool = True  # platforms 声明与当前系统是否兼容（缺省声明=兼容）
    version_compatible: bool = True  # min_host_version 与宿主版本比对（缺省声明=兼容）
    version_reason: str = ""  # 版本不兼容原因（供 UI 展示）

    @property
    def description(self) -> str:
        return self.manifest.get("description", "")

    @property
    def version(self) -> str:
        return self.manifest.get("version", "0.0.0")

    @property
    def load_blocked(self) -> bool:
        """是否被门禁拦截（平台不兼容或版本不满足），拦截时宿主不得加载其任何组件"""
        return not (self.platform_compatible and self.version_compatible)

    @property
    def is_system(self) -> bool:
        return self.plugin_type == "system"

    @property
    def components(self) -> dict:
        """插件包含的组件声明"""
        return self.manifest.get("components", {})

    def has_component(self, name: str) -> bool:
        """检查插件是否声明了某组件"""
        return self.components.get(name, False)

    @property
    def icon_config(self) -> Optional[dict]:
        """返回 {"light": Path, "dark": Path} 或 None

        从 manifest 的 "icon" 字段解析插件图标路径。
        支持三种形式：
        1. 字符串 "icon.svg" → 同一文件用于深浅主题
        2. 字典 {"light": "a.svg", "dark": "b.svg"} → 分别指定
        3. 无 "icon" 字段 → 检查插件根目录 icon.svg 兜底（文件需实际存在）
        """
        raw = self.manifest.get("icon")
        if not raw:
            default = self.path / "icon.svg"
            if default.exists():
                return {"light": default, "dark": default}
            return None
        if isinstance(raw, str):
            p = self.path / raw
            if p.exists():
                return {"light": p, "dark": p}
            return None
        if isinstance(raw, dict):
            result: dict = {}
            for theme in ("light", "dark"):
                path_str = raw.get(theme)
                if path_str:
                    p = (self.path / path_str).resolve()
                    if p.exists():
                        result[theme] = p
            # 单主题补齐：只有一个主题时补齐另一个
            if "light" not in result and "dark" in result:
                result["light"] = result["dark"]
            if "dark" not in result and "light" in result:
                result["dark"] = result["light"]
            return result if result else None


# ============================================================
# 插件管理器（单例）
# ============================================================


class PluginManager:
    """
    插件管理器（全局单例）

    负责扫描、加载、查询插件。各子系统通过 PluginManager 获取插件资源路径。
    """

    _instance: Optional["PluginManager"] = None

    # 插件搜索路径（按优先级）
    # 系统插件：项目根目录 plugins/（打包在 exe 中）
    _SYSTEM_PLUGIN_DIR = Path(__file__).parent.parent.parent.parent / "plugins"
    # 不可禁用核心插件名单（黑名单制）：禁用会断核心链路（组件宿主/插件市场自身）。
    # 其余插件（含 manifest type=system 的内置插件）均可禁用；
    # plugin-marketplace/ui/installer.py 状态分类与本名单保持单一数据源。
    _NON_DISABLEABLE = frozenset({"system", "plugin-marketplace"})
    # 用户插件：~/.drifox/plugins/（相对于 app_data_dir）
    _USER_PLUGIN_DIR_NAME = "plugins"
    # Claude Code 插件目录（同时支持两种生态）
    _CLAUDE_USER_SKILLS_DIR = Path.home() / ".claude" / "skills"
    _CLAUDE_PLUGIN_CACHE_DIR = Path.home() / ".claude" / "plugins" / "cache"

    def __init__(self):
        self._plugins: Dict[str, PluginInfo] = {}
        self._initialized = False
        self._app_data_dir: Optional[Path] = None
        # 组件/细项禁用集缓存（None = 未加载）。Settings 里该项变更极低频，
        # 但 hooks 触发等热路径会高频查询，故缓存到进程内，写操作同步更新。
        self._disabled_components_cache: Optional[frozenset] = None

    @classmethod
    def get_instance(cls) -> "PluginManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ============================================================
    # 初始化
    # ============================================================

    def initialize(self, app_data_dir: Optional[Path] = None):
        """初始化插件管理器，扫描并加载所有插件

        Args:
            app_data_dir: 应用数据目录（如 .drifox/），用于定位用户插件
        """
        if self._initialized:
            return

        self._app_data_dir = app_data_dir

        # 1. 扫描系统插件
        self._discover_system_plugins()

        # 2. 扫描 Claude Code 插件（优先级介于系统和用户之间）
        self._discover_claude_plugins()

        # 3. 扫描用户插件（最高优先级，可覆盖前两者）
        if app_data_dir:
            self._discover_user_plugins(app_data_dir)

        logger.info(f"[PluginManager] Loaded {len(self._plugins)} plugins: {', '.join(self._plugins.keys())}")
        self._initialized = True

        # 自动从 Settings 恢复已启用状态
        self._restore_enabled_from_settings()

    def _restore_enabled_from_settings(self):
        """从 Settings 恢复已启用插件状态，新发现的插件默认启用（D8：跳过禁用集）"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            saved = cfg.enabled_plugins.value or []
            saved_set = set(saved)
            disabled_set = set(cfg.disabled_plugins.value or [])
            for name in self._plugins:
                if name not in saved_set and name not in disabled_set:
                    saved.append(name)
            cfg.set(cfg.enabled_plugins, saved, save=True)
        except (ImportError, Exception):
            pass

    def reset(self):
        """重置（主要用于测试）"""
        self._plugins.clear()
        self._initialized = False

    def is_initialized(self) -> bool:
        return self._initialized

    # ============================================================
    # 运行时重扫
    # ============================================================

    def rescan(self) -> dict:
        """运行时重新扫描插件目录，检测新增/移除的插件

        仅扫描目录级别变化（新增/删除插件目录），不追踪插件内部文件变更。

        Returns:
            {"added": [PluginInfo], "removed": [PluginInfo], "changed": [PluginInfo]}
            - added: 新发现的插件列表
            - removed: 不再存在的插件列表（已从 _plugins 中移除）
            - changed: 因用户插件覆盖系统插件而变化的所有插件
        """
        if not self._initialized:
            logger.warning("[PluginManager] PluginManager not initialized, cannot rescan")
            return {"added": [], "removed": [], "changed": []}

        result: Dict[str, list] = {"added": [], "removed": [], "changed": []}
        old_names = set(self._plugins.keys())

        # 1. 重新扫描系统插件
        system_plugins = self._scan_plugins(self._SYSTEM_PLUGIN_DIR, "system")
        current_system = {p.name: p for p in system_plugins}

        # 2. 重新扫描用户插件
        user_plugins = []
        if self._app_data_dir:
            user_plugin_dir = self._app_data_dir / self._USER_PLUGIN_DIR_NAME
            user_plugins = self._scan_plugins(user_plugin_dir, "user")
        current_user = {p.name: p for p in user_plugins}

        # 2.5 重新扫描 Claude Code 插件
        claude_plugins = []
        for claude_dir in (self._CLAUDE_USER_SKILLS_DIR, self._CLAUDE_PLUGIN_CACHE_DIR):
            claude_plugins.extend(self._scan_plugins(claude_dir, "claude"))
        current_claude = {p.name: p for p in claude_plugins}

        # 3. 构建新插件映射（优先级: 系统 → Claude → 用户）
        new_plugins: Dict[str, PluginInfo] = {}
        # 先加系统插件
        for name, p in current_system.items():
            new_plugins[name] = p
        # Claude 插件同名覆盖系统
        for name, p in current_claude.items():
            if name in new_plugins:
                if new_plugins[name].is_system:
                    logger.info(f"[PluginManager] Rescan: Claude plugin '{name}' overrides system plugin")
                    result["changed"].append(p)
            new_plugins[name] = p
        # 用户插件同名覆盖前两者（最高优先级）
        for name, p in current_user.items():
            if name in new_plugins:
                if new_plugins[name].is_system:
                    logger.info(f"[PluginManager] Rescan: user plugin '{name}' overrides system plugin")
                    result["changed"].append(p)
            new_plugins[name] = p

        new_names = set(new_plugins.keys())

        # 4. 检测新增和移除
        added_names = new_names - old_names
        removed_names = old_names - new_names

        for name in added_names:
            result["added"].append(new_plugins[name])
            self._plugins[name] = new_plugins[name]
            logger.info(f"[PluginManager] Rescan: new plugin '{name}' detected")
            # 自动加载新插件的 UI 组件
            self._load_plugin_ui(name)

        for name in removed_names:
            result["removed"].append(self._plugins[name])
            self._unload_plugin_ui(name)
            self._unregister_config_schema(name)
            del self._plugins[name]
            logger.info(f"[PluginManager] Rescan: plugin '{name}' removed")

        # 5. 确保新增插件自动启用
        if added_names:
            self._restore_enabled_from_settings()

        logger.info(f"[PluginManager] Rescan done: added={len(added_names)}, removed={len(removed_names)}")
        # MCP 配置可能因插件增删而变更，失效缓存（列表刷新/连接清理依赖最新数据）
        self.invalidate_mcp_cache()
        return result

    def rescan_plugin(self, name: str):
        """只重新扫描指定插件目录，更新其 PluginInfo

        用于 watchfiles 热更新：已知变更属于某个插件时，
        只刷新该插件而不扫描全量插件。

        对于 _plugins 中尚不存在的新插件，自动在所有插件目录中
        搜索匹配后扫描注册（避免调用方需要先全量 rescan）。

        Args:
            name: 插件名称。如果插件不存在且目录被删除，则静默返回。
        """
        old = self._plugins.get(name)
        if not old:
            # 新插件：在已知插件目录中搜索并注册
            self._discover_and_register(name)
            # 新插件可能带入 MCP 配置，失效缓存（否则列表/断连逻辑读到旧数据）
            self.invalidate_mcp_cache()
            return

        plugin_dir = old.path
        if not plugin_dir.exists():
            self._unload_plugin_ui(name)
            self._unregister_config_schema(name)
            del self._plugins[name]
            logger.info(f"[PluginManager] Plugin removed during rescan: {name}")
            # 插件被移除，其 MCP 配置需从缓存中剔除
            self.invalidate_mcp_cache()
            return

        # 只扫描这一个插件目录，不走全量遍历
        new_info = self._scan_one_plugin_dir(plugin_dir, old.plugin_type)
        if new_info:
            self._plugins[name] = new_info
            logger.debug(f"[PluginManager] Rescanned plugin: {name}")
        else:
            # manifest 已不存在
            self._unload_plugin_ui(name)
            self._unregister_config_schema(name)
            del self._plugins[name]
            logger.info(f"[PluginManager] Plugin removed during rescan (manifest gone): {name}")
        # 无论更新还是移除，MCP 配置都可能变化，失效缓存
        self.invalidate_mcp_cache()

    def _discover_and_register(self, name: str) -> Optional[PluginInfo]:
        """在已知插件目录中按名称搜索新插件，找到后扫描并注册到 _plugins

        搜索顺序：系统插件 → Claude 插件 → 用户插件（优先级同 rescan）。
        找到第一个匹配目录即停止。

        Args:
            name: 插件名称

        Returns:
            注册后的 PluginInfo，未找到返回 None
        """
        # 定义搜索目录及对应类型（按优先级从低到高）
        search_dirs: List[tuple] = [
            (self._SYSTEM_PLUGIN_DIR, "system"),
        ]
        for d in (self._CLAUDE_USER_SKILLS_DIR, self._CLAUDE_PLUGIN_CACHE_DIR):
            search_dirs.append((d, "claude"))
        if self._app_data_dir:
            search_dirs.append((self._app_data_dir / self._USER_PLUGIN_DIR_NAME, "user"))

        for base_dir, plugin_type in search_dirs:
            if not base_dir.exists():
                continue
            target = base_dir / name
            if not target.is_dir():
                continue
            info = self._scan_one_plugin_dir(target, plugin_type)
            if info is not None:
                self._plugins[name] = info
                # 同步 Settings 启用状态（新插件默认启用）
                self._restore_enabled_from_settings()
                logger.info(f"[PluginManager] 发现并注册新插件 '{name}' ({plugin_type})")
                return info
            # 目录存在但没有 manifest（.drifox-plugin/plugin.json）
            # 此时目录存在但插件格式不对，继续搜索其他目录
            continue

        # 循环结束未找到 → 注册未发生，无需同步 Settings
        return None

    # ============================================================
    # 启用/禁用
    # ============================================================

    def get_enabled_plugins(self) -> List[PluginInfo]:
        """获取所有已启用的插件"""
        enabled_names = self._get_enabled_set()
        return [p for p in self._plugins.values() if p.name in enabled_names]

    def is_enabled(self, name: str) -> bool:
        """检查插件是否启用"""
        return name in self._get_enabled_set()

    def enable_plugin(self, name: str):
        """启用插件（配置持久化，调用方需触发各子系统 reload）"""
        if name not in self._plugins:
            logger.warning(f"[PluginManager] Plugin not found: {name}")
            return
        enabled = self._get_enabled_set()
        state_changed = False
        if name not in enabled:
            enabled.add(name)
            self._save_enabled_set(enabled)
            # 对称双写：从禁用集移除（D8：启停持久化）
            disabled = self._get_disabled_set()
            if name in disabled:
                disabled.discard(name)
                self._save_disabled_set(disabled)
            self.invalidate_mcp_cache()  # 启用插件可能带入新 MCP 配置
            logger.info(f"[PluginManager] Enabled plugin: {name}")
            state_changed = True
        # 联动加载 UI 组件
        self._load_plugin_ui(name)
        # 对齐工具注册（该插件工具注册；watcher 未启动时跳过）
        self._rescan_plugin_tools(name, enabled=True)
        # 状态实际变化才触发（幂等跳过重复启用）；置于末尾保证 diff 拿到注册后状态
        if state_changed:
            self._trigger_plugin_changed_hook("enabled", name)

    def disable_plugin(self, name: str):
        """禁用插件（配置持久化，调用方需触发各子系统 reload）

        系统插件保护改为名单制：仅 _NON_DISABLEABLE（system / plugin-marketplace）
        拒绝禁用——禁用它们会断核心链路（组件宿主/插件市场自身）且用户无法恢复。
        其余内置插件（包括 manifest type=system 的 shortcut-manager、agent_trace、
        assistant_hub、welcome_changelog 等）允许通过 Settings.disabled_plugins
        禁用，与 installer 的 _set_managed_enabled 判定对齐。

        判定依据是插件名名单而非 manifest type / PluginInfo.is_system（plugin_type）：
        项目根 plugins/ 下所有插件在扫描时 plugin_type 均为 "system"（目录位置
        判定），用 is_system 会把内置插件全部拒绝；type 字段回归纯元数据语义。
        """
        p = self._plugins.get(name)
        if p is None:
            logger.warning(f"[PluginManager] Plugin not found: {name}")
            return
        if name in self._NON_DISABLEABLE:
            logger.warning(f"[PluginManager] 拒绝禁用核心插件: {name}")
            return
        enabled = self._get_enabled_set()
        state_changed = False
        if name in enabled:
            enabled.discard(name)
            self._save_enabled_set(enabled)
            # 对称双写：加入禁用集（D8：启停持久化）
            disabled = self._get_disabled_set()
            disabled.add(name)
            self._save_disabled_set(disabled)
            self.invalidate_mcp_cache()  # 禁用插件需剔除其 MCP 配置
            logger.info(f"[PluginManager] Disabled plugin: {name}")
            state_changed = True
        # 联动卸载 UI 组件
        self._unload_plugin_ui(name)
        # 对齐工具注册（该插件工具注销；watcher 未启动时跳过）
        self._rescan_plugin_tools(name, enabled=False)
        # 状态实际变化才触发（幂等跳过重复禁用）；置于末尾保证 diff 拿到注销后状态
        if state_changed:
            self._trigger_plugin_changed_hook("disabled", name)

    def _rescan_plugin_tools(self, name: str, enabled: bool) -> None:
        """插件启停后精准对齐工具注册（工具插件随启停热生效）。

        - enabled=True（启用）→ watcher.reload_plugin(name)：只重载该插件工具
        - enabled=False（禁用）→ watcher.unload_plugin(name)：只注销该插件工具
        均不触发 scan_now 全量重扫（旧实现会把全部插件工具注销再重注册）。
        """
        try:
            from app.plugins.loaders.plugin_tool_loader import ensure_plugin_tool_watcher

            watcher = ensure_plugin_tool_watcher()
            if watcher is not None:
                if enabled:
                    watcher.reload_plugin(name)
                else:
                    watcher.unload_plugin(name)
        except Exception as e:
            logger.warning(f"[PluginManager] 插件工具重扫失败: {e}")

    def _load_plugin_ui(self, name: str):
        """加载指定插件的 UI 组件"""
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        except ImportError:
            return
        plugin = self._plugins.get(name)
        if plugin is None or not plugin.has_component("ui"):
            return
        # D9：ui 组件被整类停用时不再挂载（插件的浮动卡/侧边栏等槽位随之消失）
        if not self.is_component_enabled(name, "ui"):
            logger.debug(f"[PluginManager] ui 组件已停用，跳过加载: {name}")
            return
        UIPluginRegistry.get_instance().load_plugin(name, plugin.path)

    def _unload_plugin_ui(self, name: str):
        """卸载指定插件的 UI 组件"""
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        except ImportError:
            return
        UIPluginRegistry.get_instance().unload_plugin(name)

    def _get_enabled_set(self) -> set:
        """从 Settings 读取已启用的插件名集合"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            return set(cfg.enabled_plugins.value or [])
        except (ImportError, Exception):
            return set(self._plugins.keys())

    def _save_enabled_set(self, enabled: set):
        """保存已启用集合到 Settings"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            cfg.set(cfg.enabled_plugins, list(enabled), save=True)
        except (ImportError, Exception):
            pass

    def _get_disabled_set(self) -> set:
        """从 Settings 读取已禁用的插件名集合（D8：启停持久化）"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            return set(cfg.disabled_plugins.value or [])
        except (ImportError, Exception):
            return set()

    def _save_disabled_set(self, disabled: set):
        """保存已禁用集合到 Settings（D8：启停持久化）"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            cfg.set(cfg.disabled_plugins, list(disabled), save=True)
        except (ImportError, Exception):
            pass

    # ============================================================
    # 组件级禁用（D9：插件内部子项开关，如关闭某插件的 hooks/lsp）
    # 细项级禁用（D10：单个 tool / 单条 hook / 单个模板）
    #
    # key 约定（":" 分隔，整类优先于细项）：
    #   "<plugin>:<component>"            → 整类停用（D9 语义，向后兼容旧配置）
    #   "<plugin>:<component>:<item_id>"  → 单个条目停用（D10 细项粒度）
    # 判定规则：整类停用 ⇒ 其下所有细项均停用；整类启用时再看细项自身。
    # ============================================================

    def _get_disabled_components(self) -> frozenset:
        """读取组件/细项禁用集合（进程内缓存，写操作或 invalidate 时刷新）"""
        cached = self._disabled_components_cache
        if cached is not None:
            return cached
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            self._disabled_components_cache = frozenset(cfg.disabled_plugin_components.value or [])
        except (ImportError, Exception):
            self._disabled_components_cache = frozenset()
        return self._disabled_components_cache

    def _save_disabled_components(self, disabled: set):
        """保存禁用集合到 Settings，并同步刷新进程内缓存"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            cfg.set(cfg.disabled_plugin_components, sorted(disabled), save=True)
            self._disabled_components_cache = frozenset(disabled)
        except (ImportError, Exception):
            pass

    def invalidate_component_cache(self):
        """丢弃禁用集缓存（外部绕过本类直接改写 Settings 后调用）"""
        self._disabled_components_cache = None

    def disabled_keys(self) -> frozenset:
        """返回完整的禁用 key 集合（只读视图，供热路径批量判断）

        hook 触发等热路径若逐条调 is_item_enabled，会反复构造 key 字符串；
        这里一次取走整个集合，由调用方自己做 in 判断。
        注意：拿到的是不可变集合，**不要**就地修改——改动请走
        set_component_enabled / set_item_enabled，否则缓存会与磁盘失同步。
        """
        return self._get_disabled_components()

    @staticmethod
    def component_key(plugin_name: str, component: str) -> str:
        """整类禁用 key"""
        return f"{plugin_name}:{component}"

    @staticmethod
    def item_key(plugin_name: str, component: str, item_id: str) -> str:
        """细项禁用 key"""
        return f"{plugin_name}:{component}:{item_id}"

    def is_component_enabled(self, plugin_name: str, component: str) -> bool:
        """检查插件的某组件是否启用（未被组件级禁用即为启用）

        插件整体禁用不在本方法职责内（调用方已用 _iter_enabled_plugins 过滤）。
        """
        return f"{plugin_name}:{component}" not in self._get_disabled_components()

    def is_item_enabled(self, plugin_name: str, component: str, item_id: str) -> bool:
        """检查插件某组件下的单个条目是否启用

        整类被停用 ⇒ 返回 False；否则取决于该条目自身是否被停用。
        插件整体禁用同样不在本方法职责内。
        """
        disabled = self._get_disabled_components()
        if f"{plugin_name}:{component}" in disabled:
            return False
        return f"{plugin_name}:{component}:{item_id}" not in disabled

    def set_component_enabled(self, plugin_name: str, component: str, enabled: bool):
        """设置插件组件启停（仅持久化；热重载由调用方触发 PluginHostService）

        - mcp 组件即时失效缓存（get_mcp_servers 30s TTL 需主动失效）
        - 其余组件由 reloader 在重载路径消费资源查询时自然过滤
        """
        disabled = set(self._get_disabled_components())
        key = f"{plugin_name}:{component}"
        if enabled:
            if key not in disabled:
                return  # 幂等
            disabled.discard(key)
        else:
            if key in disabled:
                return  # 幂等
            disabled.add(key)
        self._save_disabled_components(disabled)
        if component == "mcp":
            self.invalidate_mcp_cache()
        logger.info(f"[PluginManager] Component '{component}' of plugin '{plugin_name}' → {'enabled' if enabled else 'disabled'}")

    def set_item_enabled(self, plugin_name: str, component: str, item_id: str, enabled: bool):
        """设置插件组件下单个条目的启停（D10 细项粒度）

        与 set_component_enabled 同构：只做持久化，热生效由调用方触发
        PluginHostService.on_plugin_item_toggled。
        """
        disabled = set(self._get_disabled_components())
        key = f"{plugin_name}:{component}:{item_id}"
        if enabled:
            if key not in disabled:
                return  # 幂等
            disabled.discard(key)
        else:
            if key in disabled:
                return  # 幂等
            disabled.add(key)
        self._save_disabled_components(disabled)
        if component == "mcp":
            self.invalidate_mcp_cache()
        logger.info(f"[PluginManager] Item '{item_id}' of {plugin_name}:{component} → {'enabled' if enabled else 'disabled'}")

    def disabled_items(self, plugin_name: str, component: str) -> List[str]:
        """列出该插件该组件下被单独停用的条目 id"""
        prefix = f"{plugin_name}:{component}:"
        return [k[len(prefix) :] for k in self._get_disabled_components() if k.startswith(prefix)]

    # ============================================================
    # 插件发现
    # ============================================================

    def _scan_plugins(self, base_dir: Path, plugin_type: str) -> List[PluginInfo]:
        """扫描 base_dir 下的插件

        每个子目录如果包含 .drifox-plugin/plugin.json，即视为一个插件。
        """
        discovered = []

        if not base_dir.exists():
            return discovered

        for item in base_dir.iterdir():
            if not item.is_dir():
                continue
            if item.name.startswith(".") or item.name.startswith("_"):
                continue

            # 支持两种清单格式：.drifox-plugin/plugin.json（优先）和 .claude-plugin/plugin.json
            manifest_path = item / ".drifox-plugin" / "plugin.json"
            manifest_format = "drifox"
            if not manifest_path.exists():
                manifest_path = item / ".claude-plugin" / "plugin.json"
                manifest_format = "claude"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    logger.error(f"[PluginManager] 清单根节点不是对象，跳过: {manifest_path}")
                    continue
                plugin_name = _normalize_plugin_name(manifest.get("name"), item.name)
                if plugin_name is None:
                    logger.error(
                        f"[PluginManager] 插件名非法（须匹配 [A-Za-z0-9][A-Za-z0-9_.-]* 且"
                        f"不含路径分隔符），跳过: {manifest_path} -> {manifest.get('name')!r}"
                    )
                    continue

                # .claude-plugin 格式：自动补全缺少的字段
                if manifest_format == "claude":
                    manifest.setdefault("type", plugin_type)
                    manifest.setdefault("version", manifest.get("version", "0.0.0"))

                # 自动检测组件：扫描目录结构（两种格式都做，保证新增目录能被识别）
                # 探测规则见 _COMPONENT_PROBES，按 kernel.KNOWN_COMPONENTS 顺序遍历
                # 组件以物理目录检测结果为准（覆盖 manifest 声明）：
                # 防止 manifest 声明了实际不存在的组件（如 browser 声明 commands
                # 但无 commands/ 目录）导致热更新触发全量命令重载
                manifest["components"] = _detect_components(item)

                # —— 平台兼容检查 + deps 统一注入（幂等，热重载 rescan 同样覆盖）——
                compatible, reason = check_platform(manifest)
                if not compatible:
                    logger.warning(f"[PluginManager] {plugin_name} 平台不兼容: {reason}")

                # —— P1 版本契约：min_host_version 与宿主版本比对 ——
                from app.plugins.version_gate import check_host_version

                ver_ok, ver_reason = check_host_version(manifest, plugin_name)

                # —— 安全：deps 注入延后到门禁之后 ——
                # deps 目录被 insert 到 sys.path[0]（优先于 stdlib），注入即等于
                # 赋予该插件劫持全进程导入的能力。故平台不兼容 / 版本契约未通过的
                # 插件一律不注入，避免"未启用的插件仍能污染宿主"。
                if compatible and ver_ok:
                    ensure_deps_on_path(item)

                # —— E1 声明式插件配置：解析 config_schema 并注册（含自动设置卡）——
                self._register_config_schema(plugin_name, manifest)

                discovered.append(
                    PluginInfo(
                        name=plugin_name,
                        manifest=manifest,
                        path=item,
                        plugin_type=plugin_type,
                        platform_compatible=compatible,
                        version_compatible=ver_ok,
                        version_reason=ver_reason,
                    )
                )
                logger.debug(
                    f"[PluginManager] Discovered plugin: {plugin_name} "
                    f"(type={plugin_type}, format={manifest_format}) at {item}"
                )
            except Exception as e:
                logger.error(f"[PluginManager] Failed to load plugin at {item}: {e}")

        return discovered

    def _scan_one_plugin_dir(self, plugin_dir: Path, plugin_type: str) -> Optional[PluginInfo]:
        """扫描单个插件目录，返回 PluginInfo

        与 _scan_plugins 的单目录版本，复用相同逻辑但不遍历兄弟目录。
        """
        if not plugin_dir.exists() or not plugin_dir.is_dir():
            return None

        # 支持两种清单格式
        manifest_path = plugin_dir / ".drifox-plugin" / "plugin.json"
        manifest_format = "drifox"
        if not manifest_path.exists():
            manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
            manifest_format = "claude"
        if not manifest_path.exists():
            return None

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            plugin_name = manifest.get("name", plugin_dir.name)

            if manifest_format == "claude":
                manifest.setdefault("type", plugin_type)
                manifest.setdefault("version", manifest.get("version", "0.0.0"))

            # 自动检测插件目录中实际存在的组件子目录，补充到 manifest 的 components 中
            # 两种格式都做 auto-detect，确保新增目录（如 themes/）能被热更新识别
            # 探测规则见 _COMPONENT_PROBES，按 kernel.KNOWN_COMPONENTS 顺序遍历
            # 组件以物理目录检测结果为准（覆盖 manifest 声明）：
            # 防止 manifest 声明了实际不存在的组件（如 browser 声明 commands
            # 但无 commands/ 目录）导致热更新触发全量命令重载
            manifest["components"] = _detect_components(plugin_dir)

            # —— 平台兼容检查 + deps 统一注入（幂等，热重载 rescan 同样覆盖）——
            compatible, reason = check_platform(manifest)
            if not compatible:
                logger.warning(f"[PluginManager] {plugin_name} 平台不兼容: {reason}")
            ensure_deps_on_path(plugin_dir)

            # —— P1 版本契约：min_host_version 与宿主版本比对 ——
            from app.plugins.version_gate import check_host_version

            ver_ok, ver_reason = check_host_version(manifest, plugin_name)

            # —— E1 声明式插件配置：解析 config_schema 并注册（含自动设置卡）——
            self._register_config_schema(plugin_name, manifest)

            info = PluginInfo(
                name=plugin_name,
                manifest=manifest,
                path=plugin_dir,
                plugin_type=plugin_type,
                platform_compatible=compatible,
                version_compatible=ver_ok,
                version_reason=ver_reason,
            )
            logger.debug(
                f"[PluginManager] Rescanned plugin: {plugin_name} (type={plugin_type}, format={manifest_format})"
            )
            return info
        except Exception as e:
            logger.error(f"[PluginManager] Failed to rescan plugin at {plugin_dir}: {e}")
            return None

    def _register_config_schema(self, plugin_name: str, manifest: dict) -> None:
        """E1：解析 plugin.json config_schema 并注册到 PluginConfigRegistry + 自动设置卡。

        幂等：同名插件重复注册由 PluginConfigRegistry 与 UIPluginRegistry 内部
        覆盖（PluginManager 全量扫描天然幂等；热重载 rescan 同样无害）。
        任一异常均降级为 warning，不影响插件本体的扫描加载。
        """
        # 解析阶段：非法 schema 走 parse_config_schema 内部 warning 兜底
        from app.plugins.contracts.plugin_config import parse_config_schema

        raw_schema = manifest.get("config_schema")
        config_schema = parse_config_schema(plugin_name, raw_schema)
        if config_schema is None:
            return

        # 注册表（必需）
        try:
            from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

            PluginConfigRegistry.get_instance().register(config_schema)
        except Exception as e:
            logger.warning(f"[PluginManager] config_schema 注册失败({plugin_name}): {e}")
            return

        # 自动设置卡（可选，挂 Phase D 扩展点）
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
            from app.widgets.cards.settings.plugin_config_card import make_card_class

            UIPluginRegistry.get_instance().register_settings_card(
                plugin_name,
                f"{plugin_name}-config",
                config_schema.title,
                make_card_class(plugin_name),
            )
        except Exception as e:
            logger.warning(f"[PluginManager] config_schema 设置卡注册失败({plugin_name}): {e}")

    def _unregister_config_schema(self, plugin_name: str) -> None:
        """E1：插件移除时清理 config_schema 注册（设置卡由 UIPluginRegistry.unload_plugin 清理）。"""
        try:
            from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

            PluginConfigRegistry.get_instance().unregister_plugin(plugin_name)
        except Exception as e:
            logger.warning(f"[PluginManager] config_schema 清理失败({plugin_name}): {e}")

    def _discover_system_plugins(self):
        """扫描系统插件目录 app/plugins/"""
        plugins = self._scan_plugins(self._SYSTEM_PLUGIN_DIR, "system")
        for p in plugins:
            self._plugins[p.name] = p

    def _discover_user_plugins(self, app_data_dir: Path):
        """扫描用户插件目录 ~/.drifox/plugins/"""
        user_plugin_dir = app_data_dir / self._USER_PLUGIN_DIR_NAME
        plugins = self._scan_plugins(user_plugin_dir, "user")
        for p in plugins:
            # 用户插件同名覆盖系统插件（优先级高）
            if p.name in self._plugins:
                existing = self._plugins[p.name]
                if existing.is_system:
                    logger.info(f"[PluginManager] User plugin '{p.name}' overrides system plugin")
            self._plugins[p.name] = p

    def _discover_claude_plugins(self):
        """扫描 Claude Code 插件目录

        同时兼容两种路径：
        - ~/.claude/skills/       （个人 skills-directory 插件）
        - ~/.claude/plugins/cache/（从市场安装的缓存插件）

        优先级介于系统插件和 DriFox 用户插件之间，即：
        系统 → Claude → DriFox 用户（最高）
        """
        for base_dir in (self._CLAUDE_USER_SKILLS_DIR, self._CLAUDE_PLUGIN_CACHE_DIR):
            plugins = self._scan_plugins(base_dir, "claude")
            for p in plugins:
                if p.name in self._plugins:
                    existing = self._plugins[p.name]
                    if existing.is_system:
                        logger.info(f"[PluginManager] Claude plugin '{p.name}' overrides system plugin")
                self._plugins[p.name] = p

    # ============================================================
    # 插件查询
    # ============================================================

    def get_plugin(self, name: str) -> Optional[PluginInfo]:
        """获取指定插件"""
        return self._plugins.get(name)

    def list_plugins(self) -> List[PluginInfo]:
        """获取所有插件列表"""
        return list(self._plugins.values())

    def has_plugin(self, name: str) -> bool:
        return name in self._plugins

    # ============================================================
    # 资源路径查询（供各子系统使用）
    # ============================================================

    def _iter_enabled_plugins(self, component: str = ""):
        """迭代所有已启用插件

        Args:
            component: 可选组件名；传入时额外过滤掉组件被禁用的插件（D9），
                如 _iter_enabled_plugins("hooks") 跳过 hooks 组件被关的插件。
        """
        enabled_names = self._get_enabled_set()
        disabled_components = self._get_disabled_components() if component else set()
        for plugin in self._plugins.values():
            if plugin.name not in enabled_names:
                continue
            if component and f"{plugin.name}:{component}" in disabled_components:
                continue
            yield plugin

    def get_plugin_dirs(self, item_type: str, include_user: bool = True) -> List[Path]:
        """获取所有已启用插件中某一类型资源的目录列表

        Args:
            item_type: "commands", "agents", "skills", "themes"
            include_user: 是否包含用户插件（默认 True）
        """
        dirs = []

        for plugin in self._iter_enabled_plugins(item_type):
            if not include_user and plugin.is_system is False:
                continue
            if not plugin.has_component(item_type):
                continue
            p = plugin.path / item_type
            if p.exists():
                dirs.append(p)

        return dirs

    def get_plugin_dirs_named(self, item_type: str, include_user: bool = True) -> List[tuple]:
        """同 get_plugin_dirs，但保留插件名：[(plugin_name, dir), ...]

        细项级过滤需要知道目录归属哪个插件（否则无法拼 `plugin:component:item`
        这样的 key），而纯路径列表会丢失这个信息。
        """
        result: List[tuple] = []
        for plugin in self._iter_enabled_plugins(item_type):
            if not include_user and plugin.is_system is False:
                continue
            if not plugin.has_component(item_type):
                continue
            p = plugin.path / item_type
            if p.exists():
                result.append((plugin.name, p))
        return result

    def get_command_files(self) -> List[Path]:
        """获取所有已启用插件的命令文件，同名去重（系统→用户，用户覆盖系统）

        始终包含 user-custom 插件命令目录（即使插件清单不存在），
        解决 ShortcutManager 写入自定义快捷键后 user-custom 插件未注册
        导致自定义文件不被加载的问题。
        """
        result = []
        name_order: Dict[str, int] = {}

        for plugin in self._iter_enabled_plugins("commands"):
            cmd_dir = plugin.path / "commands"
            if not cmd_dir.exists():
                continue
            for md_file in sorted(cmd_dir.glob("*.md")):
                if md_file.stem in name_order:
                    idx = name_order[md_file.stem]
                    result[idx] = md_file
                else:
                    name_order[md_file.stem] = len(result)
                    result.append(md_file)

        # 始终包含 user-custom 命令目录（最高优先级，覆盖所有其他插件）
        from app.utils.utils import get_app_data_dir

        user_custom_cmd = get_app_data_dir() / "plugins" / "user-custom" / "commands"
        if user_custom_cmd.exists():
            for md_file in sorted(user_custom_cmd.glob("*.md")):
                if md_file.stem in name_order:
                    idx = name_order[md_file.stem]
                    result[idx] = md_file
                else:
                    name_order[md_file.stem] = len(result)
                    result.append(md_file)

        return result

    def get_agent_files(self) -> List[Path]:
        """获取所有插件的智能体文件"""
        return self._get_md_files("agents")

    def get_skill_paths(self) -> List[Path]:
        """获取所有插件的技能目录路径"""
        return self.get_plugin_dirs("skills")

    def get_skills_with_plugin(self) -> List[dict]:
        """获取所有已启用插件的技能信息，包含所属插件名称和类型

        Returns:
            [{"path": Path, "plugin_name": str, "is_system": bool}, ...]

        用于 get_local_skills() 给用户插件技能添加命名空间前缀。
        """
        result: List[dict] = []
        for plugin in self._iter_enabled_plugins("skills"):
            if not plugin.has_component("skills"):
                continue
            d = plugin.path / "skills"
            if not d.exists():
                continue
            result.append(
                {
                    "path": d,
                    "plugin_name": plugin.name,
                    "is_system": plugin.is_system,
                }
            )
        return result

    def get_theme_paths(self) -> List[Path]:
        """获取所有插件的主题目录路径"""
        return self.get_plugin_dirs("themes")

    def get_hooks_dirs(self) -> List[Path]:
        """获取所有已启用插件的 hooks 目录路径"""
        return self.get_plugin_dirs("hooks")

    def get_global_hooks_file(self) -> Path:
        """获取全局 hooks 文件路径（user-custom 插件的 hooks/hooks.json）

        用户自定义 hooks 存放位置。如果文件不存在则返回路径但不创建。
        """
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "plugins" / "user-custom" / "hooks" / "hooks.json"

    def get_mcp_configs(self) -> List[Path]:
        """获取所有已启用插件的 .mcp.json 文件路径"""
        configs = []
        for plugin in self._iter_enabled_plugins("mcp"):
            if not plugin.has_component("mcp"):
                continue
            mcp_file = plugin.path / ".mcp.json"
            if mcp_file.exists():
                configs.append(mcp_file)
        return configs

    # ============================================================
    # 内部辅助
    # ============================================================

    def get_lsp_configs(self) -> List[dict]:
        """获取所有插件的 .lsp.json LSP 配置

        扫描已启用插件 + 额外扫描没有 manifest 但有 .lsp.json 的目录。

        Returns:
            [{"plugin": "pyright-lsp", "config": {"pyright": {...}}}, ...]
        """
        import json

        configs = []
        seen_plugins = set()

        # 1. 扫描已启用插件（lsp 组件被禁用的插件跳过，但仍记入 seen_plugins 防兜底扫描捡回）
        for plugin in self._iter_enabled_plugins():
            seen_plugins.add(plugin.name)
            if not self.is_component_enabled(plugin.name, "lsp"):
                continue
            lsp_file = plugin.path / ".lsp.json"
            if lsp_file.exists():
                try:
                    with open(lsp_file, "r", encoding="utf-8") as f:
                        configs.append({"plugin": plugin.name, "config": json.load(f)})
                except Exception as e:
                    logger.warning(f"[PluginManager] 解析 {lsp_file} 失败: {e}")

        # 2. 额外扫描：用户插件目录下没有 manifest 但有 .lsp.json 的目录
        if self._app_data_dir:
            disabled_components = self._get_disabled_components()
            user_plugins_dir = self._app_data_dir / self._USER_PLUGIN_DIR_NAME
            if user_plugins_dir.exists():
                for item in user_plugins_dir.iterdir():
                    if not item.is_dir():
                        continue
                    if item.name in seen_plugins:
                        continue
                    # 兜底目录同样受组件级禁用约束（D9）：该目录 lsp 组件被禁时跳过，
                    # 防止「无 manifest 兜底」路径绕过第 1 段的组件过滤
                    if f"{item.name}:lsp" in disabled_components:
                        continue
                    lsp_file = item / ".lsp.json"
                    if not lsp_file.exists():
                        continue
                    try:
                        with open(lsp_file, "r", encoding="utf-8") as f:
                            configs.append({"plugin": item.name, "config": json.load(f)})
                        logger.debug(f"[PluginManager] 发现独立 LSP 配置: {item.name}")
                    except Exception as e:
                        logger.warning(f"[PluginManager] 解析 {lsp_file} 失败: {e}")

        return configs

    def get_plugin_lsp_config(self, plugin_name: str) -> Optional[dict]:
        """获取指定单个插件的 LSP 配置（增量重载用）

        只读取该插件的 .lsp.json，不遍历其他插件目录。

        Returns:
            {"plugin": name, "config": {...}} 或 None（无 .lsp.json 或读取失败）
        """
        import json

        plugin = self._plugins.get(plugin_name)
        if not plugin:
            return None
        # lsp 组件被禁用时返回 None（增量重载路径不会注册该插件 LSP，D9）
        if not self.is_component_enabled(plugin_name, "lsp"):
            return None
        lsp_file = plugin.path / ".lsp.json"
        if not lsp_file.exists():
            return None
        try:
            with open(lsp_file, "r", encoding="utf-8") as f:
                return {"plugin": plugin_name, "config": json.load(f)}
        except Exception as e:
            logger.warning(f"[PluginManager] 解析 {lsp_file} 失败: {e}")
            return None

    def _get_md_files(self, subdir: str) -> List[Path]:
        """从所有已启用插件获取某子目录下的 .md 文件

        始终包含 user-custom 插件对应子目录（即使插件清单不存在）。
        """
        files: List[Path] = []
        seen: Set[str] = set()

        for plugin in self._iter_enabled_plugins(subdir):
            d = plugin.path / subdir
            if not d.exists():
                continue
            for md_file in sorted(d.glob("*.md")):
                if md_file.stem not in seen:
                    seen.add(md_file.stem)
                    files.append(md_file)
                else:
                    idx = next(i for i, f in enumerate(files) if f.stem == md_file.stem)
                    files[idx] = md_file

        # 始终包含 user-custom 对应子目录（最高优先级，覆盖所有其他插件）
        from app.utils.utils import get_app_data_dir

        user_custom_sub = get_app_data_dir() / "plugins" / "user-custom" / subdir
        if user_custom_sub.exists():
            for md_file in sorted(user_custom_sub.glob("*.md")):
                if md_file.stem not in seen:
                    seen.add(md_file.stem)
                    files.append(md_file)
                else:
                    idx = next(i for i, f in enumerate(files) if f.stem == md_file.stem)
                    files[idx] = md_file

        return files

    # ============================================================
    # MCP 配置合并
    # ============================================================

    def get_mcp_servers(self) -> list:
        """获取所有已启用插件的合并 MCP 服务器列表

        返回格式：
        [{"name": "...", "type": "stdio", "command": "...", "args": [], "env": {}, "enabled": True}, ...]

        支持三种 .mcp.json 格式：
        1. DriFoxx 格式：{"mcpServers": {"ServerName": {"command": "...", ...}}}
        2. 旧格式：{"mcpServers": {"Servers": [{"name": "...", ...}]}}
        3. .claude-plugin 格式：{"ServerName": {"type": "http", "url": "..."}}

        同名策略：后加载的覆盖先加载的（user plugin 覆盖 system plugin）

        性能：结果带 30s TTL 缓存。此方法涉及逐插件 stat + read_text + json 解析，
        曾被设置卡片 3s 定时器在主线程反复触发（采样器实测单次 os.path.exists
        在高 I/O 压力下阻塞 2s+）。配置变更走 invalidate_mcp_cache() 主动失效。
        """
        import time as _time

        now = _time.monotonic()
        if self._mcp_servers_cache is not None and now - self._mcp_servers_cache_time < self._MCP_CACHE_TTL:
            return list(self._mcp_servers_cache)  # 浅拷贝，防调用方增删元素污染缓存

        servers: dict = {}  # name → entry dict（同名时后加载的覆盖先加载的）

        for mcp_file in self.get_mcp_configs():
            try:
                content = json.loads(mcp_file.read_text(encoding="utf-8"))

                # 判断格式：有 mcpServers 键 → DriFoxx 格式；否则 → .claude-plugin 格式
                if "mcpServers" in content:
                    mcp_servers = content["mcpServers"]
                else:
                    # .claude-plugin 格式：服务器直接放在根级别
                    mcp_servers = content

                if not isinstance(mcp_servers, dict):
                    continue

                # 兼容旧格式：{"mcpServers": {"Servers": [...]}}
                if "Servers" in mcp_servers and isinstance(mcp_servers["Servers"], list):
                    for server_data in mcp_servers["Servers"]:
                        name = server_data.get("name", "")
                        if not name:
                            continue
                        servers[name] = self._build_mcp_entry(name, server_data, mcp_file)
                    continue

                # 标准格式：{"ServerName": {...}}
                for name, server_cfg in mcp_servers.items():
                    if not isinstance(server_cfg, dict):
                        continue
                    servers[name] = self._build_mcp_entry(name, server_cfg, mcp_file)

            except Exception as e:
                logger.error(f"[PluginManager] Failed to load MCP config from {mcp_file}: {e}")

        result = list(servers.values())
        self._mcp_servers_cache = result
        self._mcp_servers_cache_time = now
        return result

    # MCP 服务器列表缓存（类属性声明默认值，实例首次访问即可用）
    _MCP_CACHE_TTL = 30.0
    _mcp_servers_cache: Optional[list] = None
    _mcp_servers_cache_time: float = 0.0

    def invalidate_mcp_cache(self) -> None:
        """失效 MCP 服务器列表缓存（配置增删改/插件启停后调用）"""
        self._mcp_servers_cache = None
        self._mcp_servers_cache_time = 0.0

    @staticmethod
    def _expand_mcp_vars(value: "Any", plugin_root: Path) -> "Any":
        """递归展开 MCP 配置中的变量占位符。

        ${CLAUDE_PLUGIN_ROOT} → plugin_root
        ${CLAUDE_PLUGIN_DATA} → plugin_root / "data"
        """
        if isinstance(value, str):
            root_str = plugin_root.as_posix()
            data_str = (plugin_root / "data").as_posix()
            # 归一化反斜杠，兼容 Windows 用户手动编辑的路径
            normalized = value.replace("\\", "/")
            # 先替换长的（DATA 包含 ROOT 路径前缀），避免 `${CLAUDE_PLUGIN_ROOT}/data` 被部分替换
            return normalized.replace("${CLAUDE_PLUGIN_DATA}", data_str).replace("${CLAUDE_PLUGIN_ROOT}", root_str)
        if isinstance(value, dict):
            return {k: PluginManager._expand_mcp_vars(v, plugin_root) for k, v in value.items()}
        if isinstance(value, list):
            return [PluginManager._expand_mcp_vars(v, plugin_root) for v in value]
        return value

    @staticmethod
    def _unexpand_mcp_vars(value: "Any", plugin_root: Path) -> "Any":
        """递归逆展开：将运行时绝对路径还原为变量占位符。

        plugin_root / "data" → ${CLAUDE_PLUGIN_DATA}
        plugin_root → ${CLAUDE_PLUGIN_ROOT}
        """
        if isinstance(value, str):
            root_str = plugin_root.as_posix()
            data_str = (plugin_root / "data").as_posix()
            # 归一化反斜杠，确保 Windows 用户手动编辑的路径也能匹配
            normalized = value.replace("\\", "/")
            # 先替换长的（data），再替换短的（root），避免部分匹配
            result = normalized.replace(data_str, "${CLAUDE_PLUGIN_DATA}")
            return result.replace(root_str, "${CLAUDE_PLUGIN_ROOT}")
        if isinstance(value, dict):
            return {k: PluginManager._unexpand_mcp_vars(v, plugin_root) for k, v in value.items()}
        if isinstance(value, list):
            return [PluginManager._unexpand_mcp_vars(v, plugin_root) for v in value]
        return value

    def _build_mcp_entry(self, name: str, cfg: dict, source_file: Path) -> dict:
        """构建统一格式的 MCP 服务器条目（展开 ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA} 变量）"""
        plugin_root = source_file.parent
        return {
            "name": name,
            "type": cfg.get("type", "stdio"),
            "enabled": cfg.get("enabled", True),
            "command": PluginManager._expand_mcp_vars(cfg.get("command", ""), plugin_root),
            "args": PluginManager._expand_mcp_vars(cfg.get("args", []), plugin_root),
            "env": PluginManager._expand_mcp_vars(cfg.get("env", {}), plugin_root),
            "url": PluginManager._expand_mcp_vars(cfg.get("url", ""), plugin_root),
            "headers": PluginManager._expand_mcp_vars(cfg.get("headers", {}), plugin_root),
            "_source": str(source_file),
        }

    def update_mcp_server(self, name: str, server_data: dict):
        """更新指定 MCP 服务器配置（写入来源插件的 .mcp.json）

        Args:
            name: 服务器名
            server_data: 完整的服务器配置 {"command": ..., "args": ..., "enabled": ..., ...}

        _source 可能是：
        1. 文件路径（如 D:\\work\\DriFoxx\\plugins\\system\\.mcp.json）
        2. 服务器名称字符串（编辑时传递的旧名称，需要查表找真实来源）
        """
        source = server_data.get("_source", "")
        if not source:
            logger.warning(f"[PluginManager] MCP server '{name}' has no _source, cannot update")
            return

        # 如果 _source 是名称而非路径，查表获取真实文件路径
        source_path = Path(source) if len(source) > 50 or source.endswith(".json") else None
        if source_path is None or not source_path.exists():
            # 查找该服务器的真实来源文件
            servers = self.get_mcp_servers()
            match = next((s for s in servers if s.get("name") == name), None)
            if match and match.get("_source"):
                source_path = Path(match["_source"])
            else:
                logger.warning(f"[PluginManager] MCP server '{name}' has no _source, cannot update")
                return

        if not source_path.exists():
            logger.warning(f"[PluginManager] MCP source file not found: {source_path}")
            return

        # 逆展开：将运行时绝对路径还原为 ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA}
        # 只处理持久化字段，避免污染 _source 等元数据
        _MCP_PERSIST_FIELDS = ("command", "args", "env", "url", "headers")
        for key in _MCP_PERSIST_FIELDS:
            if key in server_data:
                server_data[key] = PluginManager._unexpand_mcp_vars(server_data[key], source_path.parent)

        try:
            content = json.loads(source_path.read_text(encoding="utf-8"))

            # 判断格式：有 mcpServers 键 → DriFoxx 格式；否则 → .claude-plugin 格式
            if "mcpServers" in content:
                mcp_servers = content["mcpServers"]
            else:
                mcp_servers = content

            if not isinstance(mcp_servers, dict):
                logger.warning(f"[PluginManager] Invalid MCP config format in {source_path}")
                return

            # 兼容旧格式：如果顶级是 {"Servers": [...]} 结构
            if "Servers" in mcp_servers and isinstance(mcp_servers["Servers"], list):
                found = False
                for entry in mcp_servers["Servers"]:
                    if entry.get("name") == name:
                        entry.update({k: v for k, v in server_data.items() if k not in ("name", "_source", "_builtin")})
                        found = True
                        break
                if not found:
                    mcp_servers["Servers"].append(
                        {k: v for k, v in server_data.items() if k not in ("name", "_source", "_builtin")}
                    )
            elif "mcpServers" in content:
                # DriFoxx 格式：{"mcpServers": {"ServerName": {...}}}
                if name in mcp_servers:
                    mcp_servers[name].update(
                        {k: v for k, v in server_data.items() if k not in ("name", "_source", "_builtin")}
                    )
                else:
                    mcp_servers[name] = {
                        k: v for k, v in server_data.items() if k not in ("name", "_source", "_builtin")
                    }
                content["mcpServers"] = mcp_servers
            else:
                # .claude-plugin 格式：{"ServerName": {...}}
                # 服务器直接在根级，content 本身就是 mcp_servers
                if name in content:
                    content[name].update(
                        {k: v for k, v in server_data.items() if k not in ("name", "_source", "_builtin")}
                    )
                else:
                    content[name] = {k: v for k, v in server_data.items() if k not in ("name", "_source", "_builtin")}
            source_path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
            self.invalidate_mcp_cache()
            logger.info(f"[PluginManager] Updated MCP server '{name}' in {source_path}")
        except Exception as e:
            logger.error(f"[PluginManager] Failed to update MCP server '{name}': {e}")
        else:
            self._trigger_plugin_changed_hook("mcp_updated", name, server_data)

    def add_mcp_server(self, name: str, server_data: dict):
        """添加 MCP 服务器到用户自定义插件（user-custom）的 .mcp.json

        如果 user-custom 插件不存在，自动创建。
        """
        from app.utils.utils import get_app_data_dir

        custom_dir = get_app_data_dir() / "plugins" / "user-custom"
        custom_dir.mkdir(parents=True, exist_ok=True)

        # 确保 user-custom 插件有 plugin.json（仅首次创建时添加 hooks 组件声明）
        manifest_dir = custom_dir / ".drifox-plugin"
        manifest_dir.mkdir(exist_ok=True)
        manifest_path = manifest_dir / "plugin.json"

        manifest = {"type": "user", "components": {}}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {"type": "user", "components": {}}
        else:
            manifest = {
                "name": "user-custom",
                "description": "用户自定义配置（MCP、Hooks 等）",
                "version": "1.0.0",
                "type": "user",
            }

        # 合并组件声明（不覆盖已有）
        components = manifest.get("components", {})
        components["mcp"] = True
        components["hooks"] = True
        components["team_templates"] = True
        # 如果用户已在 user-custom 中创建了 commands 目录（如 ShortcutManager），
        # 则同步标记 commands 组件，确保重扫插件时能被识别
        if (custom_dir / "commands").exists():
            components["commands"] = True
        manifest["components"] = components
        if "name" not in manifest:
            manifest["name"] = "user-custom"
        if "description" not in manifest:
            manifest["description"] = "用户自定义配置（MCP、Hooks 等）"
        if "version" not in manifest:
            manifest["version"] = "1.0.0"
        if "type" not in manifest:
            manifest["type"] = "user"

        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        # 更新 .mcp.json
        mcp_path = custom_dir / ".mcp.json"
        content = {"mcpServers": {}}
        if mcp_path.exists():
            try:
                content = json.loads(mcp_path.read_text(encoding="utf-8"))
            except Exception:
                content = {"mcpServers": {}}

        # 写入服务器配置（去掉 UI 内部字段）
        mcp_entry = {k: v for k, v in server_data.items() if k not in ("name", "_source", "_builtin")}
        content["mcpServers"][name] = mcp_entry

        mcp_path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")

        # 重新发现插件（新创建的 user-custom 需要注册并自动启用）
        if not self.has_plugin("user-custom"):
            self._discover_user_plugins(get_app_data_dir())
            self.enable_plugin("user-custom")

        self.invalidate_mcp_cache()
        logger.info(f"[PluginManager] Added MCP server '{name}' to user-custom plugin")
        self._trigger_plugin_changed_hook("mcp_added", name, server_data)

    def _trigger_plugin_changed_hook(
        self, action: str, target_name: str, extra: Optional[dict] = None
    ) -> None:
        """触发 PluginChanged hook（PluginManager 内部变化统一出口）

        覆盖：MCP 配置级（mcp_added/mcp_removed/mcp_updated）与插件启停
        （enabled/disabled）。懒导入避免循环依赖（backend → plugin_manager）；
        任意线程安全（trigger_plugin_changed_hook 内部投递线程池并附带 diff）。

        Args:
            action: 子事件动作
            target_name: MCP 服务器名（mcp_* 动作）或插件名（enabled/disabled）
            extra: 附加字段（如 server_config）
        """
        try:
            from app.core.hook_manager import trigger_plugin_changed_hook

            is_mcp = action.startswith("mcp_")
            context: dict = {
                "action": action,
                ("server_name" if is_mcp else "plugin_name"): target_name,
            }
            if extra:
                # 只保留可序列化的展示字段，去掉内部标记
                key = "server_config" if is_mcp else "detail"
                context[key] = {k: v for k, v in extra.items() if not str(k).startswith("_")}
            trigger_plugin_changed_hook(context)
        except Exception as e:
            logger.debug(f"[PluginManager] trigger plugin changed hook failed: {e}")

    def remove_mcp_server(self, name: str):
        """从来源插件的 .mcp.json 中移除指定 MCP 服务器

        如果来源是 system 插件则只禁用（不删除），来源是 user-mcp 则删除。
        """
        # 找到该服务器属于哪个 .mcp.json
        servers = self.get_mcp_servers()
        target = next((s for s in servers if s.get("name") == name), None)
        if not target:
            logger.warning(f"[PluginManager] MCP server '{name}' not found")
            return

        source = Path(target.get("_source", ""))
        if not source.exists():
            logger.warning(f"[PluginManager] MCP source file not found: {source}")
            return

        try:
            content = json.loads(source.read_text(encoding="utf-8"))

            # 判断格式：有 mcpServers 键 → DriFoxx 格式；否则 → .claude-plugin 格式
            if "mcpServers" in content:
                mcp_servers = content["mcpServers"]
                is_claude_format = False
            else:
                mcp_servers = content
                is_claude_format = True

            if not isinstance(mcp_servers, dict):
                return

            if source.parent.name == "system":
                # 系统插件：只禁用，不删除
                if name in mcp_servers:
                    mcp_servers[name]["enabled"] = False
                    source.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[PluginManager] Disabled system MCP server '{name}'")
            else:
                # 用户插件（user-custom、.claude-plugin 等）：直接删除
                if name in mcp_servers:
                    del mcp_servers[name]
                    if is_claude_format and not mcp_servers:
                        # .claude-plugin 格式：删除后为空，写回空对象
                        source.write_text("{}", encoding="utf-8")
                    else:
                        source.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[PluginManager] Removed MCP server '{name}'")

            self.invalidate_mcp_cache()
        except Exception as e:
            logger.error(f"[PluginManager] Failed to remove MCP server '{name}': {e}")
        else:
            # 系统插件禁用 / 用户插件删除，对活跃列表均为移除
            self._trigger_plugin_changed_hook("mcp_removed", name)
