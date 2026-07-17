# -*- coding: utf-8 -*-
"""
主题管理器
- 支持两层扫描：内置主题 (app/themes/) + 用户主题 (~/.drifox/themes/)
- 用户主题优先级高，同名覆盖内置主题
- 每个主题一个文件夹，支持资源文件（图片等）
- 完全从文件读取，不硬编码主题数据
- 参考技能加载模式设计（多层搜索 + 合并）
"""

import logging
import re
import weakref
from pathlib import Path
from typing import Dict, Optional

import yaml

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
        """加载所有主题：系统 → 插件 → 用户（后加载覆盖前加载）

        插件主题通过两条路径加载：
        a) 直接扫描插件目录（不依赖 PluginManager，确保 setup_ui 前即可用）
        b) PluginManager 初始化后重新加载（仅已启用插件，覆盖 a)

        加载顺序即优先级（后加载覆盖同名的前加载）：
        系统主题 < 插件主题(直接扫描) < 插件主题(PluginManager) < 用户主题
        """

        # 1. 内置主题（打包在 exe 的 plugins/system/themes/，优先级最低）
        self._load_from_dir(_BUILTIN_THEMES_DIR, is_builtin=True)

        # 2. 插件主题 — 直接扫描插件目录（不依赖 PluginManager）
        #    在 setup_ui() 之前即可加载插件主题，避免被重置
        self._load_plugin_themes_directly()

        # 3. 插件主题（PluginManager 提供，仅已启用插件，可覆盖 #2）
        try:
            from app.core.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for theme_path in pm.get_theme_paths():
                    self._load_from_dir(theme_path, is_builtin=True)
        except ImportError, Exception:
            pass

        # 4. 用户主题（~/.drifox/themes/，可写，优先级最高，不可被内置覆盖）
        from app.utils.utils import get_app_data_dir

        user_dir = get_app_data_dir() / "themes"
        self._load_from_dir(user_dir, is_builtin=False)

        if not self._themes:
            logger.warning("[ThemeManager] 未加载到任何主题")

    def _load_plugin_themes_directly(self):
        """直接扫描所有插件目录下的 themes/ 文件夹

        不依赖 PluginManager，确保在 setup_ui() 之前插件主题就已可用。
        PluginManager 初始化后，_reload_themes_from_plugins() 会通过
        get_theme_paths() 再次加载（仅已启用插件），同名主题会覆盖此处结果。
        """
        # 系统插件目录: plugins/
        system_plugin_dir = Path(__file__).parent.parent.parent / "plugins"
        if system_plugin_dir.is_dir():
            for entry in sorted(system_plugin_dir.iterdir()):
                if entry.is_dir():
                    theme_dir = entry / "themes"
                    if theme_dir.exists():
                        self._load_from_dir(theme_dir, is_builtin=True)

        # 用户插件目录: ~/.drifox/plugins/
        try:
            from app.utils.utils import get_app_data_dir

            user_plugin_dir = get_app_data_dir() / "plugins"
            if user_plugin_dir.is_dir():
                for entry in sorted(user_plugin_dir.iterdir()):
                    if entry.is_dir():
                        theme_dir = entry / "themes"
                        if theme_dir.exists():
                            self._load_from_dir(theme_dir, is_builtin=True)
        except Exception:
            pass

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

    def get_theme_pet(self, theme_id: str) -> dict:
        """获取主题的 pet 配置。

        主题 YAML 可选声明 `pet:` 段：
            pet:
              image: ./pet.png    # 相对主题目录的路径

        Returns:
            dict: {"image": Path, "source": "theme"} 表示该主题声明了有效 pet
                  {} 表示该主题未声明 / 声明无效，调用方应 fallback 到内嵌默认
        """
        theme = self._themes.get(theme_id) or {}
        pet_cfg = theme.get("pet") or {}
        image_rel = pet_cfg.get("image")
        if not image_rel:
            return {}

        theme_dir = self.get_theme_dir(theme_id)
        if not theme_dir:
            return {}

        image_path = theme_dir / image_rel
        if image_path.exists():
            return {"image": image_path, "source": "theme"}

        logger.warning(f"[ThemeManager] 主题 {theme_id} 声明的 pet 不存在: {image_path}，fallback 到内嵌默认")
        return {}

    # ── 浅色/深色模式检测 ──────────────────────────────

    # 缓存：上次检测的 theme_id 和结果
    _cached_light_check: tuple = (None, None)

    def is_light_theme(self, theme_id: str = None) -> bool:
        """判断指定主题（或当前主题）是否为浅色模式。

        优先级：
        1. 主题 YAML 显式声明 `mode: light` → True
        2. 主题 YAML 显式声明 `mode: dark` → False
        3. 自动检测：text_primary 的亮度 > 128 → 浅色模式

        结果会被缓存（按 theme_id），主题切换时自动失效。
        """
        if theme_id is None:
            theme_id = self.get_current_theme_id()

        # 缓存命中
        if self._cached_light_check[0] == theme_id:
            return self._cached_light_check[1]

        theme = self.get_theme(theme_id) or {}

        # 1. 显式声明
        mode = theme.get("mode")
        if mode == "light":
            self._cached_light_check = (theme_id, True)
            return True
        if mode == "dark":
            self._cached_light_check = (theme_id, False)
            return False

        # 2. 自动检测：text_primary 亮度
        colors = theme.get("colors", {})
        text_primary = colors.get("text_primary", "#ffffff")

        m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", text_primary)
        if m:
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            # hex 格式
            text_primary_hex = text_primary.lstrip("#")
            if len(text_primary_hex) == 6:
                r = int(text_primary_hex[0:2], 16)
                g = int(text_primary_hex[2:4], 16)
                b = int(text_primary_hex[4:6], 16)
            else:
                # fallback
                r, g, b = 255, 255, 255

        # 相对亮度公式 (感知亮度)
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        result = luminance < 128  # 暗文字 = 浅色模式
        self._cached_light_check = (theme_id, result)
        return result

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

    _reload_callbacks: list = []

    def on_reload(self, callback):
        """注册主题重载完成后的回调（用于 UI 自动刷新等）"""
        if callback not in self._reload_callbacks:
            self._reload_callbacks.append(callback)

    def remove_reload_callback(self, callback):
        """移除已注册的回调"""
        if callback in self._reload_callbacks:
            self._reload_callbacks.remove(callback)

    # ── 统一刷新目标注册 ──────────────────────────────────
    # 使用弱引用防止阻止垃圾回收
    _refresh_targets: list = []

    def register_refresh_target(self, widget) -> None:
        """注册需要接收主题刷新的 widget（自动去重）

        Args:
            widget: 实现了 refresh_theme() 方法的 QWidget 实例
        """
        ref = weakref.ref(widget)
        # 去重
        for existing in self._refresh_targets:
            if existing() is widget:
                return
        self._refresh_targets.append(ref)

    def unregister_refresh_target(self, widget) -> None:
        """取消注册 widget"""
        self._refresh_targets = [ref for ref in self._refresh_targets if ref() is not widget]

    def on_theme_changed(self):
        """主题切换后调用：清除浅色检测缓存。

        图标适配由 _ThemeIconEngine 自动处理（QIconEngine.key 随主题变化），
        无需手动清图标缓存。
        """
        self._cached_light_check = (None, None)

    def dispatch_refresh(self) -> None:
        """向所有已注册的 widget 分发 refresh_theme() 调用

        由 reload() 或外部触发（配置变更）调用。
        已失效的弱引用会被自动清理。
        """
        from app.utils.design_tokens import Colors

        Colors.refresh()

        self.on_theme_changed()

        alive = []
        for ref in self._refresh_targets:
            widget = ref()
            if widget is None:
                continue  # 弱引用已失效，跳过
            alive.append(ref)
            try:
                if hasattr(widget, "refresh_theme"):
                    widget.refresh_theme()
            except Exception as e:
                logger.warning(f"[ThemeManager] dispatch_refresh error: {e}")
        self._refresh_targets = alive

    def reload(self):
        """重新加载所有主题（修改文件后调用）

        比对当前活动主题在重载前后是否变化，只有当前主题确实被修改/删除
        时才触发 UI 全量刷新（dispatch_refresh），避免热重载无关主题时
        不必要地重绘整个界面。
        """
        # 重载前：保存当前主题的快照，用于后续比对
        old_id = self.get_current_theme_id()
        old_data = self.get_theme(old_id)

        self._themes.clear()
        self._load_themes()
        # 清除浅色检测缓存
        self._cached_light_check = (None, None)

        # 重载后：获取新数据，判断当前主题是否真实变化
        new_data = self.get_theme(old_id)
        if new_data != old_data:
            # 当前主题被修改/删除 → 全量刷新 UI
            self.dispatch_refresh()

        # 兼容旧回调（用于设置面板更新主题下拉列表等，无论当前主题是否变化都需要）
        for cb in self._reload_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"[ThemeManager] reload callback error: {e}")

    def get_user_themes_dir(self) -> Path:
        """获取用户主题目录"""
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "themes"


# 全局单例
theme_manager = ThemeManager()
