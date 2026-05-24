# -*- coding: utf-8 -*-
"""
主题管理器
- 从 app/themes/ 目录扫描加载主题（文件夹或单文件）
- 支持主题文件夹内的资源文件（图片等）
- 完全从文件读取，不硬编码主题数据
"""
import os
import yaml
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 主题目录：app/themes/
_THEMES_DIR = Path(__file__).parent.parent / "themes"


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

    def _load_themes(self):
        """扫描 app/themes/ 加载所有主题"""
        self._themes = {}
        themes_dir = _THEMES_DIR

        if not themes_dir.is_dir():
            logger.warning(f"[ThemeManager] 主题目录不存在: {themes_dir}")
            return

        for entry in themes_dir.iterdir():
            if entry.is_dir():
                # 文件夹形式：xxx/xxx.yaml
                yaml_file = entry / f"{entry.name}.yaml"
                if yaml_file.exists():
                    self._load_yaml(yaml_file, entry)
            elif entry.suffix in (".yaml", ".yml"):
                # 单文件形式：xxx.yaml
                self._load_yaml(entry, None)

    def _load_yaml(self, yaml_path: Path, theme_dir: Optional[Path]):
        """加载单个主题 YAML"""
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not data or not data.get("id"):
                return

            theme_id = data["id"]
            data["_path"] = str(yaml_path)
            if theme_dir:
                data["_dir"] = str(theme_dir)
            self._themes[theme_id] = data
            logger.debug(f"[ThemeManager] 加载主题: {theme_id} <- {yaml_path}")

        except Exception as e:
            logger.warning(f"[ThemeManager] 加载主题失败 {yaml_path}: {e}")

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
        """获取主题资源目录（如果有）"""
        theme = self._themes.get(theme_id)
        if not theme:
            return None
        dir_path = theme.get("_dir")
        return Path(dir_path) if dir_path else None

    def get_theme_resource(self, theme_id: str, filename: str) -> Optional[Path]:
        """获取主题资源文件路径"""
        theme_dir = self.get_theme_dir(theme_id)
        if not theme_dir:
            return None
        resource_path = theme_dir / filename
        return resource_path if resource_path.exists() else None

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

    def reload(self):
        """重新加载所有主题"""
        self._load_themes()


# 全局单例
theme_manager = ThemeManager()