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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from loguru import logger


# ============================================================
# 插件信息数据类
# ============================================================

@dataclass
class PluginInfo:
    """插件信息"""
    name: str                  # 插件唯一标识（目录名）
    manifest: dict             # 原始清单数据
    path: Path                 # 插件根目录
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

    def __init__(self):
        self._plugins: Dict[str, PluginInfo] = {}
        self._initialized = False

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

        # 1. 扫描系统插件
        self._discover_system_plugins()

        # 2. 扫描用户插件
        if app_data_dir:
            self._discover_user_plugins(app_data_dir)

        logger.info(f"[PluginManager] Loaded {len(self._plugins)} plugins: "
                     f"{', '.join(self._plugins.keys())}")
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
        except (ImportError, Exception):
            pass

    def reset(self):
        """重置（主要用于测试）"""
        self._plugins.clear()
        self._initialized = False

    def is_initialized(self) -> bool:
        return self._initialized

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
            logger.info(f"[PluginManager] Enabled plugin: {name}")

    def disable_plugin(self, name: str):
        """禁用插件（配置持久化，调用方需触发各子系统 reload）"""
        if name not in self._plugins:
            logger.warning(f"[PluginManager] Plugin not found: {name}")
            return
        enabled = self._get_enabled_set()
        if name in enabled:
            enabled.discard(name)
            self._save_enabled_set(enabled)
            logger.info(f"[PluginManager] Disabled plugin: {name}")

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

            manifest_path = item / ".drifox-plugin" / "plugin.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                plugin_name = manifest.get("name", item.name)
                discovered.append(PluginInfo(
                    name=plugin_name,
                    manifest=manifest,
                    path=item,
                    plugin_type=plugin_type,
                ))
                logger.debug(f"[PluginManager] Discovered plugin: {plugin_name} "
                            f"(type={plugin_type}) at {item}")
            except Exception as e:
                logger.error(f"[PluginManager] Failed to load plugin at {item}: {e}")

        return discovered

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
                    logger.info(f"[PluginManager] User plugin '{p.name}' "
                               f"overrides system plugin")
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
        """获取所有已启用插件的命令文件，同名去重（系统→用户，用户覆盖系统）"""
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

        return result

    def get_agent_files(self) -> List[Path]:
        """获取所有插件的智能体文件"""
        return self._get_md_files("agents")

    def get_skill_paths(self) -> List[Path]:
        """获取所有插件的技能目录路径"""
        return self.get_plugin_dirs("skills")

    def get_theme_paths(self) -> List[Path]:
        """获取所有插件的主题目录路径"""
        return self.get_plugin_dirs("themes")

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

    def _get_md_files(self, subdir: str) -> List[Path]:
        """从所有已启用插件获取某子目录下的 .md 文件"""
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

        return files

    # ============================================================
    # MCP 配置合并
    # ============================================================

    def get_mcp_servers(self) -> list:
        """获取所有已启用插件的合并 MCP 服务器列表

        返回格式与 Settings.mcp_servers 兼容：
        [{"name": "...", "type": "stdio", "command": "...", "args": [], "env": {}, "enabled": True}, ...]
        """
        servers = []
        seen_names = set()

        for mcp_file in self.get_mcp_configs():
            try:
                content = json.loads(mcp_file.read_text(encoding="utf-8"))
                mcp_servers = content.get("mcpServers", {})
                for name, server_cfg in mcp_servers.items():
                    if name in seen_names:
                        continue
                    seen_names.add(name)
                    servers.append({
                        "name": name,
                        "type": server_cfg.get("type", "stdio"),
                        "enabled": server_cfg.get("enabled", True),
                        "command": server_cfg.get("command", ""),
                        "args": server_cfg.get("args", []),
                        "env": server_cfg.get("env", {}),
                        "_source": str(mcp_file),
                    })
            except Exception as e:
                logger.error(f"[PluginManager] Failed to load MCP config from {mcp_file}: {e}")

        return servers

    def update_mcp_server(self, name: str, server_data: dict):
        """更新指定 MCP 服务器配置（写入来源插件的 .mcp.json）

        Args:
            name: 服务器名
            server_data: 完整的服务器配置 {"command": ..., "args": ..., "enabled": ..., ...}
        """
        source = server_data.get("_source", "")
        if not source:
            logger.warning(f"[PluginManager] MCP server '{name}' has no _source, cannot update")
            return

        source_path = Path(source)
        if not source_path.exists():
            logger.warning(f"[PluginManager] MCP source file not found: {source}")
            return

        try:
            content = json.loads(source_path.read_text(encoding="utf-8"))
            mcp_servers = content.get("mcpServers", {})

            if name in mcp_servers:
                # 更新已有条目（保留非 UI 字段）
                mcp_servers[name].update({
                    k: v for k, v in server_data.items()
                    if k not in ("name", "_source", "_builtin")
                })
            else:
                mcp_servers[name] = {
                    k: v for k, v in server_data.items()
                    if k not in ("name", "_source", "_builtin")
                }

            content["mcpServers"] = mcp_servers
            source_path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"[PluginManager] Updated MCP server '{name}' in {source}")
        except Exception as e:
            logger.error(f"[PluginManager] Failed to update MCP server '{name}': {e}")

    def add_mcp_server(self, name: str, server_data: dict):
        """添加 MCP 服务器到用户插件（user-mcp）的 .mcp.json

        如果 user-mcp 插件不存在，自动创建。
        """
        from app.utils.utils import get_app_data_dir

        user_mcp_dir = get_app_data_dir() / "plugins" / "user-mcp"
        user_mcp_dir.mkdir(parents=True, exist_ok=True)

        # 确保 user-mcp 插件有 plugin.json
        manifest_dir = user_mcp_dir / ".drifox-plugin"
        manifest_dir.mkdir(exist_ok=True)
        manifest_path = manifest_dir / "plugin.json"

        if not manifest_path.exists():
            manifest = {
                "name": "user-mcp",
                "description": "用户自定义 MCP 服务器",
                "version": "1.0.0",
                "type": "user",
                "components": {"mcp": True},
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        # 更新 .mcp.json
        mcp_path = user_mcp_dir / ".mcp.json"
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

        # 重新发现插件（新创建的 user-mcp 需要注册）
        if not self.has_plugin("user-mcp"):
            self._scan_plugins(get_app_data_dir() / "plugins", "user")  # type: ignore
            self._discover_user_plugins(get_app_data_dir())

        logger.info(f"[PluginManager] Added MCP server '{name}' to user-mcp plugin")

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
            mcp_servers = content.get("mcpServers", {})

            if source.parent.name == "system":
                # 系统插件：只禁用，不删除
                if name in mcp_servers:
                    mcp_servers[name]["enabled"] = False
                    source.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[PluginManager] Disabled system MCP server '{name}'")
            else:
                # 用户插件：直接删除
                if name in mcp_servers:
                    del mcp_servers[name]
                    source.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[PluginManager] Removed MCP server '{name}'")

        except Exception as e:
            logger.error(f"[PluginManager] Failed to remove MCP server '{name}': {e}")
