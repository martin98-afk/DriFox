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
from functools import lru_cache
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
        # 延迟加载：首次访问主题数据时再扫描，避免 import 时触发文件 I/O
        self._themes_loaded = False
        # 🚀 P5c：主题源目录 mtime 指纹缓存（reload 短路用，None=未计算）
        self._themes_fingerprint: Optional[str] = None

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
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for theme_path in pm.get_theme_paths():
                    self._load_from_dir(theme_path, is_builtin=True)
        except (ImportError, Exception):
            pass

        # 4. 用户主题（~/.drifox/themes/，可写，优先级最高，不可被内置覆盖）
        from app.utils.utils import get_app_data_dir

        user_dir = get_app_data_dir() / "themes"
        self._load_from_dir(user_dir, is_builtin=False)

        if not self._themes:
            logger.warning("[ThemeManager] 未加载到任何主题")

    def _ensure_loaded(self):
        """确保主题数据已加载（惰性加载守卫）"""
        if self._themes_loaded:
            return
        self._themes_loaded = True
        self._load_themes()
        # 记录首次加载后的指纹，供 reload() 短路比对（否则 reload 首次必重扫）
        self._themes_fingerprint = self._compute_fingerprint()

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

    @staticmethod
    @lru_cache(maxsize=256)
    def _parse_yaml_cached(path_str: str, mtime_ns: int) -> dict:
        """按 (路径, mtime_ns) 缓存 YAML 解析结果（文件变化自动失效）。

        缓存 key 含 mtime：主题文件被编辑保存后 mtime 变化 → 自动重新解析，
        reload() 无需手动清缓存即可拿到新内容。只缓存纯净的解析结果，
        元信息（_path/_dir/_is_builtin/_source）由调用方写入拷贝，不污染缓存。
        """
        with open(path_str, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _load_yaml(self, yaml_path: Path, theme_dir: Optional[Path], is_builtin: bool):
        """加载单个主题 YAML（带 mtime 缓存，同文件未变化不重复解析）"""
        try:
            mtime_ns = yaml_path.stat().st_mtime_ns
            parsed = self._parse_yaml_cached(str(yaml_path), mtime_ns)
            if not parsed or not parsed.get("id"):
                return
            # 浅拷贝：下方写入 _path/_dir/_is_builtin/_source 元信息及
            # _themes 存储均基于拷贝，缓存只保留纯净的 YAML 解析结果
            data = dict(parsed)

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
        self._ensure_loaded()
        return {tid: data.get("name", tid) for tid, data in self._themes.items()}

    def get_theme(self, theme_id: str) -> Optional[dict]:
        """获取指定主题数据"""
        self._ensure_loaded()
        return self._themes.get(theme_id)

    def get_theme_value(self, theme_id: str, key: str, default: str = None) -> str:
        """从主题的 colors 中获取颜色值"""
        self._ensure_loaded()
        theme = self._themes.get(theme_id)
        if not theme:
            return default
        colors = theme.get("colors", {})
        return colors.get(key, default)

    def get_theme_window(self, theme_id: str) -> dict:
        """获取窗口背景配置"""
        self._ensure_loaded()
        theme = self._themes.get(theme_id) or {}
        return theme.get("window", {})

    def get_theme_background(self, theme_id: str) -> dict:
        """获取背景图片配置"""
        self._ensure_loaded()
        theme = self._themes.get(theme_id) or {}
        return theme.get("background", {})

    def get_theme_dir(self, theme_id: str) -> Optional[Path]:
        """获取主题资源目录（主题文件夹路径）"""
        self._ensure_loaded()
        theme = self._themes.get(theme_id)
        if not theme:
            return None
        dir_path = theme.get("_dir", "")
        return Path(dir_path) if dir_path else None

    def get_theme_resource(self, theme_id: str, filename: str) -> Optional[Path]:
        """获取主题资源文件路径（如背景图片）"""
        self._ensure_loaded()
        theme_dir = self.get_theme_dir(theme_id)
        if not theme_dir:
            return None
        resource_path = theme_dir / filename
        return resource_path if resource_path.exists() else None

    def get_theme_pet(self, theme_id: str) -> dict:
        """获取主题的 pet 配置。"""
        self._ensure_loaded()
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
        self._ensure_loaded()
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
        # 🐛 推进 ThemeRefreshCoordinator 版本号：使消息卡正文 CSS 变量注入
        # 的幂等短路失效。此前 should_skip / on_theme_changed 从未被调用，
        # 版本号恒为 0，导致 CodeWebViewer.refresh_theme 只在首次切换注入一次
        # CSS 变量，之后所有主题切换正文/思考块颜色不刷新。
        try:
            from app.utils.theme_refresh import ThemeRefreshCoordinator

            ThemeRefreshCoordinator.should_skip(self.get_current_theme_id())
        except Exception:
            pass

    def dispatch_refresh(self) -> None:
        """向所有已注册的 widget 分发 refresh_theme() 调用

        由 reload() 或外部触发（配置变更）调用。
        已失效的弱引用会被自动清理。

        🛡️ 可见性过滤（T2-R7）：tab 内非 active 页窗口不执行全量 refresh_theme
        （N 窗 × M 卡 findChildren + runJavaScript 成本），仅标记
        _theme_needs_refresh 待刷；切回可见时由窗口自身补刷链路
        （main_widget._on_tab_selected 检查 _theme_needs_refresh 后调用
        _apply_runtime_ui_settings，见 main_widget.py:8064-8077 同机制）自动
        补刷，不丢主题更新。可见窗口行为与原先完全一致（立即 refresh_theme）；
        无补刷链路的 target 保守不跳过（全量刷新），避免主题更新永久丢失。
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
            # 🛡️ 可见性过滤：仅跳过"tab 内非 active 页"窗口——自身隐藏 + 顶层
            # 窗口可见 + 有 _theme_needs_refresh 补刷链路（main_widget 系窗口），
            # 标记待刷；切回 tab 时由 _on_tab_selected 检查标志后调
            # _apply_runtime_ui_settings（见 main_widget.py:8064-8077 同机制）
            # 自动补刷，不丢主题更新。
            #
            # 边界对齐（devil-advocate 审查确认）：
            # - 不用朴素 isVisible——TabManager 整体隐藏（最小化到托盘）时子窗口
            #   isVisible 全为 False，朴素跳过无补刷链路导致主题不更新；故要求
            #   顶层可见才跳过，与 _execute_batched_theme_refresh 的 _tab_active_win
            #   语义对齐（顶层不可见时仍全量刷新）。
            # - 无 _theme_needs_refresh 属性的 target（PixelPetWidget 等）保守不跳过
            #   ——避免隐藏期间主题更新永久丢失（无补刷链路时跳过=不可恢复）。
            try:
                if (
                    hasattr(widget, "isVisible")
                    and not widget.isVisible()
                    and hasattr(widget, "_theme_needs_refresh")
                ):
                    top_visible = False
                    try:
                        top = widget.window()
                        top_visible = bool(top.isVisible()) if hasattr(top, "isVisible") else False
                    except Exception:
                        top_visible = False
                    if top_visible:
                        widget._theme_needs_refresh = True
                        # P5b：dispatch_refresh 语义恒为纯主题变化，记录 scope="theme"
                        #（切回 tab 补刷时精确只刷颜色，避免漏刷/过度刷新）
                        widget._theme_needs_refresh_scope = "theme"
                        logger.debug("[ThemeManager] dispatch_refresh: hidden tab target marked _theme_needs_refresh")
                        continue
            except Exception:
                pass  # isVisible 异常（如已销毁的 C++ 对象）→ 按可见处理走原刷新路径
            try:
                if hasattr(widget, "refresh_theme"):
                    widget.refresh_theme()
                    # 刷新后清除待刷标志（与 batched 路径 main_widget.py:8148 对齐，
                    # 避免残留 True 导致下次切回重复全量刷新）
                    if hasattr(widget, "_theme_needs_refresh"):
                        try:
                            widget._theme_needs_refresh = False
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"[ThemeManager] dispatch_refresh error: {e}")
        self._refresh_targets = alive

    def reload(self, force: bool = False):
        """重新加载所有主题（修改文件后调用）

        比对当前活动主题在重载前后是否变化，只有当前主题确实被修改/删除
        时才触发 UI 全量刷新（dispatch_refresh），避免热重载无关主题时
        不必要地重绘整个界面。

        🚀 P5c：mtime 指纹缓存——重扫 4 来源（内置/插件直扫/PluginManager/
        用户主题）前先计算目录指纹（主题文件路径+mtime_ns 哈希），与上次
        重载结果比对：
        - 指纹未变（gitee 同步等场景触发 reload，但主题文件未被改动）
          → 直接短路返回，复用已有 _themes，跳过重扫与 UI 全量刷新
          （reload 100-300ms → ~1ms）
        - 指纹变化（主题文件新增/修改/删除）→ 正常重扫 + 刷新
        - force=True（手动切主题等需要强制重扫的场景）→ 绕过指纹缓存

        边界：mtime_ns 纳秒级校验，主题文件真实变化必然引起指纹变化；
        首次加载（_themes 为空）不短路，保证数据就绪。
        """
        # 指纹未变化且已有主题数据 → 直接复用，跳过重扫与 UI 刷新
        if not force and self._themes and self._compute_fingerprint() == self._themes_fingerprint:
            logger.debug("[ThemeManager] reload: 主题指纹未变化，跳过重扫与 UI 刷新")
            return

        # 重载前：保存当前主题的快照，用于后续比对
        old_id = self.get_current_theme_id()
        old_data = self.get_theme(old_id)

        self._themes.clear()
        self._load_themes()
        # 记录本次重载后的指纹，供下次 reload 比对
        self._themes_fingerprint = self._compute_fingerprint()
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

    def _compute_fingerprint(self) -> str:
        """计算主题源目录指纹：所有主题 YAML 的 (路径, mtime_ns) 排序哈希

        与 _load_themes 的 4 个来源一一对应：
        1. 内置主题目录（plugins/system/themes）
        2. 插件主题直扫（plugins/*/themes，系统 + 用户插件目录）
        3. PluginManager 提供的已启用插件主题路径
        4. 用户主题目录（~/.drifox/themes）

        mtime_ns 纳秒精度：主题文件任何真实改动（增/删/改）都会改变指纹；
        仅重扫 stat 不读文件内容，比 YAML 解析快两个数量级。
        """
        import hashlib

        from app.utils.utils import get_app_data_dir

        h = hashlib.md5()
        theme_dirs = {_BUILTIN_THEMES_DIR}

        # 2. 插件主题直扫：系统 + 用户插件目录下所有插件子目录的 themes/
        for base in (Path(__file__).parent.parent.parent / "plugins", get_app_data_dir() / "plugins"):
            if base.is_dir():
                for entry in sorted(base.iterdir()):
                    if entry.is_dir():
                        td = entry / "themes"
                        if td.is_dir():
                            theme_dirs.add(td)

        # 3. PluginManager 已启用插件主题路径
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                theme_dirs.update(Path(p) for p in pm.get_theme_paths())
        except Exception:
            pass

        # 4. 用户主题目录
        theme_dirs.add(get_app_data_dir() / "themes")

        for base in sorted(theme_dirs, key=str):
            if not base.is_dir():
                continue
            for yaml_file in sorted(base.rglob("*.yaml")) + sorted(base.rglob("*.yml")):
                try:
                    h.update(f"{yaml_file}:{yaml_file.stat().st_mtime_ns}".encode("utf-8"))
                except OSError:
                    pass  # 文件被并发删除等竞态 → 跳过，指纹可能含噪，重扫兜底
        return h.hexdigest()

    def get_user_themes_dir(self) -> Path:
        """获取用户主题目录"""
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "themes"


# 全局单例
theme_manager = ThemeManager()
