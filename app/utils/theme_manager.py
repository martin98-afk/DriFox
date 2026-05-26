# -*- coding: utf-8 -*-
"""
主题管理器
- 支持两层扫描：内置主题 (app/themes/) + 用户主题 (~/.drifox/themes/)
- 用户主题优先级高，同名覆盖内置主题
- 每个主题一个文件夹，支持资源文件（图片等）
- 完全从文件读取，不硬编码主题数据
- 参考技能加载模式设计（多层搜索 + 合并）
"""
import os
import yaml
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 内置主题目录（打包在 exe 中，只读）
# 指向系统插件 themes 目录，不再依赖 app/themes/
_BUILTIN_THEMES_DIR = Path(__file__).parent.parent.parent / "plugins" / "system" / "themes"


class ThemeManager:
    """主题管理器 - 单例，纯文件驱动"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._themes: Dict[str, dict] = {}
        self._load_themes()

    # ── 扫描加载 ──────────────────────────────────────────

    def _load_themes(self):
        """加载所有主题：插件 + 内置 + 用户"""

        # 1. 插件主题（PluginManager 提供的插件主题路径，优先级最高）
        try:
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for theme_path in pm.get_theme_paths():
                    self._load_from_dir(theme_path, is_builtin=True)
        except (ImportError, Exception):
            pass

        # 2. 内置主题（打包在 exe 的 app/themes/，只读）
        self._load_from_dir(_BUILTIN_THEMES_DIR, is_builtin=True)

        # 3. 用户主题（~/.drifox/themes/，可写，优先级最高）
        from app.utils.utils import get_app_data_dir
        user_dir = get_app_data_dir() / "themes"
        self._load_from_dir(user_dir, is_builtin=False)

        if not self._themes:
            logger.warning("[ThemeManager] 未加载到任何主题")

    def _load_from_dir(self, base_dir: Path, is_builtin: bool):
        """从指定目录扫描加载主题"""
        if not base_dir.is_dir():
            return

        for entry in sorted(base_dir.iterdir()):
            if entry.is_dir():
                # 文件夹形式：xxx/xxx.yaml
                yaml_file = entry / f"{entry.name}.yaml"
                if yaml_file.exists():
                    self._load_yaml(yaml_file, entry, is_builtin)
            elif entry.suffix in (".yaml", ".yml"):
                # 单文件形式：xxx.yaml
                self._load_yaml(entry, None, is_builtin)

    def _load_yaml(self, yaml_path: Path, theme_dir: Optional[Path], is_builtin: bool):
        """加载单个主题 YAML"""
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not data or not data.get("id"):
                return

            theme_id = data["id"]

            # 用户主题优先级高：如果已是用户主题，跳过内置覆盖
            if theme_id in self._themes:
                existing = self._themes[theme_id]
                # builtin=False 表示用户主题，允许覆盖
                # builtin=True 但已有用户主题，跳过
                if is_builtin and not existing.get("_is_builtin"):
                    return  # 用户主题优先，不覆盖

            data["_path"] = str(yaml_path)
            data["_dir"] = str(theme_dir) if theme_dir else ""
            data["_is_builtin"] = is_builtin
            data["_source"] = str(yaml_path.parent)
            self._themes[theme_id] = data
            logger.debug(f"[ThemeManager] 加载主题: {theme_id} <- {yaml_path}")

        except Exception as e:
            logger.warning(f"[ThemeManager] 加载主题失败 {yaml_path}: {e}")

    # ── 查询接口 ──────────────────────────────────────────

    def list_themes(self) -> Dict[str, str]:
        """列出所有可用主题 {id: name}"""
        return {tid: data.get("name", tid) for tid, data in self._themes.items()}

    def get_theme(self, theme_id: str) -> Optional[dict]:
        """获取指定主题数据"""
        return self._themes.get(theme_id)

    def get_theme_value(self, theme_id: str, key: str, default: str = None) -> str:
        """从主题的 colors 中获取颜色值"""
        theme = self._themes.get(theme_id)
        if not theme:
            return default
        colors = theme.get("colors", {})
        return colors.get(key, default)

    def get_theme_window(self, theme_id: str) -> dict:
        """获取窗口背景配置"""
        theme = self._themes.get(theme_id) or {}
        return theme.get("window", {})

    def get_theme_background(self, theme_id: str) -> dict:
        """获取背景图片配置"""
        theme = self._themes.get(theme_id) or {}
        return theme.get("background", {})

    def get_theme_dir(self, theme_id: str) -> Optional[Path]:
        """获取主题资源目录（主题文件夹路径）"""
        theme = self._themes.get(theme_id)
        if not theme:
            return None
        dir_path = theme.get("_dir", "")
        return Path(dir_path) if dir_path else None

    def get_theme_resource(self, theme_id: str, filename: str) -> Optional[Path]:
        """获取主题资源文件路径（如背景图片）"""
        theme_dir = self.get_theme_dir(theme_id)
        if not theme_dir:
            return None
        resource_path = theme_dir / filename
        return resource_path if resource_path.exists() else None

    def is_user_theme(self, theme_id: str) -> bool:
        """判断是否为用户自定义主题（非内置）"""
        theme = self._themes.get(theme_id)
        return bool(theme and not theme.get("_is_builtin", True))

    # ── 当前主题 ──────────────────────────────────────────

    def get_current_theme_id(self) -> str:
        """获取当前选中的主题 ID"""
        try:
            from app.utils.config import Settings
            return Settings.get_instance().ui_theme_style.value
        except Exception:
            return "midnight"

    def get_current_theme(self) -> dict:
        """获取当前主题的完整数据"""
        theme_id = self.get_current_theme_id()
        return self.get_theme(theme_id) or {}

    def get_current_colors(self) -> dict:
        """获取当前主题的 colors 部分"""
        theme = self.get_current_theme()
        return theme.get("colors", {})

    # ── 主题管理 ──────────────────────────────────────────

    def reload(self):
        """重新加载所有主题（修改文件后调用）"""
        self._load_themes()

    def get_user_themes_dir(self) -> Path:
        """获取用户主题目录"""
        from app.utils.utils import get_app_data_dir
        return get_app_data_dir() / "themes"


# 全局单例
theme_manager = ThemeManager()