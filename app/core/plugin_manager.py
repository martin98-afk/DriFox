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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from loguru import logger

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

    @property
    def description(self) -> str:
        return self.manifest.get("description", "")

    @property
    def version(self) -> str:
        return self.manifest.get("version", "0.0.0")

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
    _SYSTEM_PLUGIN_DIR = Path(__file__).parent.parent.parent / "plugins"
    # 用户插件：~/.drifox/plugins/（相对于 app_data_dir）
    _USER_PLUGIN_DIR_NAME = "plugins"
    # Claude Code 插件目录（同时支持两种生态）
    _CLAUDE_USER_SKILLS_DIR = Path.home() / ".claude" / "skills"
    _CLAUDE_PLUGIN_CACHE_DIR = Path.home() / ".claude" / "plugins" / "cache"

    def __init__(self):
        self._plugins: Dict[str, PluginInfo] = {}
        self._initialized = False
        self._app_data_dir: Optional[Path] = None

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
        """从 Settings 恢复已启用插件状态，新发现的插件默认启用"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            saved = cfg.enabled_plugins.value or []
            saved_set = set(saved)
            for name in self._plugins:
                if name not in saved_set:
                    saved.append(name)
            cfg.set(cfg.enabled_plugins, saved, save=True)
        except ImportError, Exception:
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
        if name not in enabled:
            enabled.add(name)
            self._save_enabled_set(enabled)
            self.invalidate_mcp_cache()  # 启用插件可能带入新 MCP 配置
            logger.info(f"[PluginManager] Enabled plugin: {name}")
        # 联动加载 UI 组件
        self._load_plugin_ui(name)

    def disable_plugin(self, name: str):
        """禁用插件（配置持久化，调用方需触发各子系统 reload）"""
        if name not in self._plugins:
            logger.warning(f"[PluginManager] Plugin not found: {name}")
            return
        enabled = self._get_enabled_set()
        if name in enabled:
            enabled.discard(name)
            self._save_enabled_set(enabled)
            self.invalidate_mcp_cache()  # 禁用插件需剔除其 MCP 配置
            logger.info(f"[PluginManager] Disabled plugin: {name}")
        # 联动卸载 UI 组件
        self._unload_plugin_ui(name)

    def _load_plugin_ui(self, name: str):
        """加载指定插件的 UI 组件"""
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry
        except ImportError:
            return
        plugin = self._plugins.get(name)
        if plugin is None or not plugin.has_component("ui"):
            return
        UIPluginRegistry.get_instance().load_plugin(name, plugin.path)

    def _unload_plugin_ui(self, name: str):
        """卸载指定插件的 UI 组件"""
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry
        except ImportError:
            return
        UIPluginRegistry.get_instance().unload_plugin(name)

    def _get_enabled_set(self) -> set:
        """从 Settings 读取已启用的插件名集合"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            return set(cfg.enabled_plugins.value or [])
        except ImportError, Exception:
            return set(self._plugins.keys())

    def _save_enabled_set(self, enabled: set):
        """保存已启用集合到 Settings"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            cfg.set(cfg.enabled_plugins, list(enabled), save=True)
        except ImportError, Exception:
            pass

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
                plugin_name = manifest.get("name", item.name)

                # .claude-plugin 格式：自动补全缺少的字段
                if manifest_format == "claude":
                    manifest.setdefault("type", plugin_type)
                    manifest.setdefault("version", manifest.get("version", "0.0.0"))

                # 自动检测组件：扫描目录结构（两种格式都做，保证新增目录能被识别）
                # hooks 组件需同时存在 hooks/ 目录和 hooks/hooks.json 文件
                components = {}
                for comp_name in ("commands", "agents", "skills", "themes"):
                    if (item / comp_name).exists():
                        components[comp_name] = True
                # 团队模板：检测 team_templates/ 目录（含 .yaml 文件才标记）
                if (item / "team_templates").exists():
                    has_yaml = any((item / "team_templates").glob("*.yaml"))
                    if has_yaml:
                        components["team_templates"] = True
                if (item / "hooks").exists() and (item / "hooks" / "hooks.json").exists():
                    components["hooks"] = True
                if (item / ".mcp.json").exists():
                    components["mcp"] = True
                if (item / ".lsp.json").exists():
                    components["lsp"] = True
                # UI 组件需同时存在 ui/ 目录和 ui/__init__.py
                if (item / "ui").exists() and (item / "ui" / "__init__.py").exists():
                    components["ui"] = True
                # 组件以物理目录检测结果为准（覆盖 manifest 声明）：
                # 防止 manifest 声明了实际不存在的组件（如 browser 声明 commands
                # 但无 commands/ 目录）导致热更新触发全量命令重载
                manifest["components"] = components

                discovered.append(
                    PluginInfo(
                        name=plugin_name,
                        manifest=manifest,
                        path=item,
                        plugin_type=plugin_type,
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
            # hooks 组件需同时存在 hooks/ 目录和 hooks/hooks.json 文件
            detected_components = {}
            for comp_name in ("commands", "agents", "skills", "themes"):
                if (plugin_dir / comp_name).exists():
                    detected_components[comp_name] = True
            # 团队模板：检测 team_templates/ 目录（含 .yaml 文件才标记）
            if (plugin_dir / "team_templates").exists():
                has_yaml = any((plugin_dir / "team_templates").glob("*.yaml"))
                if has_yaml:
                    detected_components["team_templates"] = True
            if (plugin_dir / "hooks").exists() and (plugin_dir / "hooks" / "hooks.json").exists():
                detected_components["hooks"] = True
            if (plugin_dir / ".mcp.json").exists():
                detected_components["mcp"] = True
            if (plugin_dir / ".lsp.json").exists():
                detected_components["lsp"] = True
            # UI 组件需同时存在 ui/ 目录和 ui/__init__.py
            if (plugin_dir / "ui").exists() and (plugin_dir / "ui" / "__init__.py").exists():
                detected_components["ui"] = True

            # 组件以物理目录检测结果为准（覆盖 manifest 声明）：
            # 防止 manifest 声明了实际不存在的组件（如 browser 声明 commands
            # 但无 commands/ 目录）导致热更新触发全量命令重载
            manifest["components"] = detected_components

            info = PluginInfo(
                name=plugin_name,
                manifest=manifest,
                path=plugin_dir,
                plugin_type=plugin_type,
            )
            logger.debug(
                f"[PluginManager] Rescanned plugin: {plugin_name} (type={plugin_type}, format={manifest_format})"
            )
            return info
        except Exception as e:
            logger.error(f"[PluginManager] Failed to rescan plugin at {plugin_dir}: {e}")
            return None

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

    def _iter_enabled_plugins(self):
        """迭代所有已启用插件"""
        enabled_names = self._get_enabled_set()
        for plugin in self._plugins.values():
            if plugin.name in enabled_names:
                yield plugin

    def get_plugin_dirs(self, item_type: str, include_user: bool = True) -> List[Path]:
        """获取所有已启用插件中某一类型资源的目录列表

        Args:
            item_type: "commands", "agents", "skills", "themes"
            include_user: 是否包含用户插件（默认 True）
        """
        dirs = []

        for plugin in self._iter_enabled_plugins():
            if not include_user and plugin.is_system is False:
                continue
            if not plugin.has_component(item_type):
                continue
            p = plugin.path / item_type
            if p.exists():
                dirs.append(p)

        return dirs

    def get_command_files(self) -> List[Path]:
        """获取所有已启用插件的命令文件，同名去重（系统→用户，用户覆盖系统）

        始终包含 user-custom 插件命令目录（即使插件清单不存在），
        解决 ShortcutManager 写入自定义快捷键后 user-custom 插件未注册
        导致自定义文件不被加载的问题。
        """
        result = []
        name_order: Dict[str, int] = {}

        for plugin in self._iter_enabled_plugins():
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
        for plugin in self._iter_enabled_plugins():
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
        for plugin in self._iter_enabled_plugins():
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

        # 1. 扫描已启用插件
        for plugin in self._iter_enabled_plugins():
            seen_plugins.add(plugin.name)
            lsp_file = plugin.path / ".lsp.json"
            if lsp_file.exists():
                try:
                    with open(lsp_file, "r", encoding="utf-8") as f:
                        configs.append({"plugin": plugin.name, "config": json.load(f)})
                except Exception as e:
                    logger.warning(f"[PluginManager] 解析 {lsp_file} 失败: {e}")

        # 2. 额外扫描：用户插件目录下没有 manifest 但有 .lsp.json 的目录
        if self._app_data_dir:
            user_plugins_dir = self._app_data_dir / self._USER_PLUGIN_DIR_NAME
            if user_plugins_dir.exists():
                for item in user_plugins_dir.iterdir():
                    if not item.is_dir():
                        continue
                    if item.name in seen_plugins:
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

        for plugin in self._iter_enabled_plugins():
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
