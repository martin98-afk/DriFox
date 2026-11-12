# -*- coding: utf-8 -*-
"""
主题管理器
- 支持两层扫描：内置主题 (app/themes/) + 用户主题 (~/.drifox/themes/)
- 用户主题优先级高，同名覆盖内置主题
- 每个主题一个文件夹，支持资源文件（图片等）
- 完全从文件读取，不硬编码主题数据
- 参考技能加载模式设计（多层搜索 + 合并）
"""

import hashlib
import logging
import os
import pickle
import re
import threading
import time
import weakref
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# 内置主题目录（打包在 exe 中，只读）
# 指向系统插件 themes 目录，不再依赖 app/themes/
_BUILTIN_THEMES_DIR = Path(__file__).parent.parent.parent / "plugins" / "system-themes" / "themes"

# 指纹全量扫描窗口（秒）：窗口内只要主题根目录 mtime 未变，就复用上次指纹
# 而不做逐文件扫描。插件/主题目录增删会改父目录 mtime，必被捕获；
# 仅「已存在 yaml 就地改内容」会延迟到本窗口之后（或显式 reload(force=True)）。
_FINGERPRINT_FULL_WINDOW_S = 10.0

# 动画背景帧率上限：素材按 12fps 制作，主题写再高也没有帧可用，
# 且解码成本随时长线性上升（640x360 单帧约 3ms），必须 clamp。
_ANIM_FPS_CAP = 15


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
        # [PERF] 指纹计算的根目录预检缓存：Windows + 杀软环境实测单次目录枚举
        # ~26ms、单次 stat ~6ms，旧实现两轮 rglob 全树扫（17 个主题 yaml）要
        # 550~1050ms 主线程阻塞。改为「根目录 mtime 预检 + 单层 scandir」：
        # 根目录未变且全量扫描窗口未过期时直接复用上次指纹（仅 6 次 stat）。
        self._fingerprint_value: Optional[str] = None
        self._fingerprint_root_sig: Optional[tuple] = None
        self._fingerprint_full_checked_at: float = 0.0
        # [PERF] 插件 themes 目录收集缓存（plugins 根目录 mtime 未变则复用）
        self._plugin_theme_dirs_cache: Optional[list] = None
        self._plugin_theme_dirs_sig: Optional[tuple] = None

    # ── 扫描加载 ──────────────────────────────────────────

    def _load_themes(self):
        """加载所有主题：系统 → 插件 → 用户（后加载覆盖前加载）

        插件主题通过两条路径加载：
        a) 直接扫描插件目录（不依赖 PluginManager，确保 setup_ui 前即可用）
        b) PluginManager 初始化后重新加载（仅已启用插件，覆盖 a)

        加载顺序即优先级（后加载覆盖同名的前加载）：
        系统主题 < 插件主题(直接扫描) < 插件主题(PluginManager) < 用户主题
        """

        # 1. 内置主题（打包在 exe 的 plugins/system-themes/themes/，优先级最低）
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
    def _parse_yaml_cached(path_str: str, mtime_ns: int, size: int) -> dict:
        """按 (路径, mtime_ns, size) 缓存 YAML 解析结果（文件变化自动失效）。

        缓存 key 含 mtime：主题文件被编辑保存后 mtime 变化 → 自动重新解析，
        reload() 无需手动清缓存即可拿到新内容。只缓存纯净的解析结果，
        元信息（_path/_dir/_is_builtin/_source）由调用方写入拷贝，不污染缓存。

        [PERF L2] 跨启动持久层：解析产物 pickle 到聚合文件
        <app_data>/cache/theme_yaml/aggregate.pickle，
        条目键 = sha1(路径|mtime_ns|size) 弱签名——文件变化即新签名 → miss →
        重解析（旧条目自然废弃，聚合表尾部无界增长极慢：单条 KB 级 × 变更频率，
        不做主动 GC，注释说明即可）。二次冷启动单次反序列化命中，免磁盘 IO +
        PyYAML 全量解析（70 主题 ~350-400ms → 实测 <100ms）。
        - pickle 安全边界：缓存文件仅本程序写入（app_data 内），读侧全量
          try/except（损坏/缺文件/任何异常 → 当空表 → 静默回退正常解析，
          缓存问题绝不阻塞/崩溃主题加载）
        - 写入：解析后标 dirty + 0.5s 去抖 Timer 后台合并写（临时文件 +
          os.replace 原子替换），不占启动路径、首启批量不重复写
        """
        sig = ThemeManager._yaml_cache_sig(path_str, mtime_ns, size)
        agg = ThemeManager._load_yaml_agg()
        cached = agg.get(sig)
        if cached is not None:
            return cached
        with open(path_str, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        if isinstance(parsed, dict):
            ThemeManager._store_yaml_agg_entry(sig, parsed)
        return parsed

    # 跨启动聚合缓存：进程内态（聚合表/去抖 Timer/dirty 集），锁内更新锁外 IO
    _yaml_agg_cache: Optional[dict] = None
    _yaml_agg_loaded = False
    _yaml_agg_dirty: set = set()
    _yaml_agg_timer: Optional[threading.Timer] = None
    _yaml_agg_lock = threading.Lock()

    @staticmethod
    def _yaml_cache_sig(path_str: str, mtime_ns: int, size: int) -> str:
        """条目弱签名：sha1(路径|mtime_ns|size)。文件变化即新签名 → 天然失效。"""
        return hashlib.sha1(f"{path_str}|{mtime_ns}|{size}".encode("utf-8")).hexdigest()

    @staticmethod
    def _yaml_agg_path() -> Path:
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "cache" / "theme_yaml" / "aggregate.pickle"

    @staticmethod
    def _load_yaml_agg() -> dict:
        """进程内懒加载聚合表（每进程只读盘一次）；损坏/缺文件 → 当空表。"""
        if ThemeManager._yaml_agg_loaded:
            return ThemeManager._yaml_agg_cache or {}
        with ThemeManager._yaml_agg_lock:
            if ThemeManager._yaml_agg_loaded:
                return ThemeManager._yaml_agg_cache or {}
            data: dict = {}
            try:
                p = ThemeManager._yaml_agg_path()
                if p.exists():
                    with open(p, "rb") as f:
                        loaded = pickle.load(f)
                    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                        data = loaded["entries"]
            except Exception:
                data = {}  # 损坏/任何异常 → 当空表（静默回退正常解析）
            ThemeManager._yaml_agg_cache = data
            ThemeManager._yaml_agg_loaded = True
            return data

    @staticmethod
    def _store_yaml_agg_entry(sig: str, parsed: dict) -> None:
        """内存表入条目 + 标 dirty + 0.5s 去抖后台合并写（首启 70 条只写一次）。"""
        with ThemeManager._yaml_agg_lock:
            ThemeManager._yaml_agg_loaded = True
            if ThemeManager._yaml_agg_cache is None:
                ThemeManager._yaml_agg_cache = {}
            ThemeManager._yaml_agg_cache[sig] = parsed
            ThemeManager._yaml_agg_dirty.add(sig)
            if ThemeManager._yaml_agg_timer is None:
                timer = threading.Timer(0.5, ThemeManager._flush_yaml_agg)
                timer.daemon = True
                ThemeManager._yaml_agg_timer = timer
                timer.start()

    @staticmethod
    def _flush_yaml_agg() -> None:
        """把 dirty 条目合并写聚合文件（临时文件 + os.replace 原子替换）；失败静默。"""
        with ThemeManager._yaml_agg_lock:
            dirty = set(ThemeManager._yaml_agg_dirty)
            ThemeManager._yaml_agg_dirty.clear()
            if ThemeManager._yaml_agg_timer is not None:
                ThemeManager._yaml_agg_timer.cancel()
                ThemeManager._yaml_agg_timer = None
            if not dirty:
                return
            snapshot = dict(ThemeManager._yaml_agg_cache or {})
        try:
            p = ThemeManager._yaml_agg_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump({"version": 1, "entries": snapshot}, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, p)
        except Exception:
            pass  # 缓存写失败静默（不占启动路径、不崩溃）

    def _load_yaml(self, yaml_path: Path, theme_dir: Optional[Path], is_builtin: bool):
        """加载单个主题 YAML（带 mtime 缓存，同文件未变化不重复解析）"""
        try:
            st = yaml_path.stat()
            parsed = self._parse_yaml_cached(str(yaml_path), st.st_mtime_ns, st.st_size)
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


    def get_theme_window(self, theme_id: str) -> dict:
        """获取窗口背景配置"""
        self._ensure_loaded()
        theme = self._themes.get(theme_id) or {}
        return theme.get("window", {})


    def get_theme_backgrounds(self, theme_id: str) -> dict:
        """获取主题的多区域背景配置（新字段，统一 5 个区域）

        新 schema（yaml 的 `backgrounds:` 块）：
          - window      : 整个 TabManagerWindow 最底层（颜色或图片）
          - sidebar     : 左侧 Tab 面板（独立背景）
          - chat_area   : 右侧 _chat_frame 纯色底（向下兼容旧主题；aurora 类新主题
                          直接全部配置在 scene 即可，chat_area 可省略）
          - scene       : 右侧 _chat_frame 图片/场景化背景（撑满整个右侧圆角矩形，
                          含 replace_tab_bar / 对话区 / 输入框 / LEFT/RIGHT/BOTTOM
                          停靠区 / UI 插件槽位等所有 _chat_frame 内的内容）
                          可嵌套 foreground 子对象放全画布前景层（壁纸拆出的人物等）：
                          {image, opacity, enabled}，与 image 同矩形同拉伸，逐像素对齐
          - decorations : 装饰件数组（多张 PNG，按 anchor 摆放；anchor 坐标系以
                          OpenAIChatToolWindow 为基准，定位在对话区/输入框附近）
          - overlay     : 动态光影视频覆盖层（循环视频帧压暗混合，scene 之上、
                          UI 之下；字段 video/target/opacity/brightness_boost/
                          position/enabled）
          - music       : 主题背景音乐播放列表（字段 tracks/volume/loop/
                          fade_ms/enabled）

        旧字段兼容（避免破坏现有 17 套内置主题）：
          - background.window_bg → backgrounds.window
          - background.chat_list → backgrounds.window（历史行为：作为 TabManagerWindow 全屏背景）
            注：旧字段命名"chat_list"在历史实现里实际作用于 TabManagerWindow 全屏背景，
            保持向后兼容不破坏现有主题外观。新主题应直接使用 `backgrounds.window`。

        每个区域的标准字段：{color, image, opacity, blur, dim, enabled}。
        返回结构：
          {
            "window": dict | None,
            "sidebar": dict | None,
            "chat_area": dict | None,
            "scene": dict | None,
            "decorations": list[dict],
            "overlay": dict | None,
            "music": dict | None,
          }
        """
        self._ensure_loaded()
        theme = self._themes.get(theme_id) or {}

        new_bg = theme.get("backgrounds") or {}
        legacy_bg = theme.get("background") or {}

        def _normalize_animation(d) -> Optional[dict]:
            """动画背景归一化（backgrounds.<region>.animation）

            动画素材为动画 WebP（也兼容 GIF），由 Qt 内置解码器逐帧读取，
            不依赖系统媒体栈。字段：
              - src      : str  主题目录相对路径或绝对路径
              - canvas   : shared | independent（默认 independent）
                           shared = 多区域共用同一张动画，各区域取自己的矩形，
                           接缝连续且只解码一次
              - fps      : int  1~15（上限 clamp，防止主题写出高帧率吃满 CPU）
              - blend    : normal | overlay（overlay=黑底+alpha 压暗，光影遮罩）
              - fit      : cover | contain | tile
              - position : cover 裁切对齐 top/center/bottom
              - opacity  : float 0.0~1.0
              - enabled  : bool
            """
            if not isinstance(d, dict):
                return None
            src = d.get("src")
            if not src:
                return None

            def _f(v, default, lo, hi):
                try:
                    return max(lo, min(hi, float(v)))
                except (TypeError, ValueError):
                    return default

            fps = int(_f(d.get("fps", 12), 12, 1, _ANIM_FPS_CAP))
            blend = str(d.get("blend") or "normal").lower()
            if blend not in ("normal", "overlay"):
                blend = "normal"
            fit = str(d.get("fit") or "cover").lower()
            if fit not in ("cover", "contain", "tile"):
                fit = "cover"
            position = str(d.get("position") or "center").lower()
            if position not in ("top", "center", "bottom"):
                position = "center"
            return {
                "src": str(src),
                "canvas": "shared" if str(d.get("canvas") or "").lower() == "shared" else "independent",
                "fps": fps,
                "blend": blend,
                "fit": fit,
                "position": position,
                "opacity": _f(d.get("opacity", 1.0), 1.0, 0.0, 1.0),
                "enabled": bool(d.get("enabled", True)),
            }

        def _normalize(d: Optional[dict]) -> Optional[dict]:
            """统一区域字段：补默认值 + 兼容旧字段"""
            if not d:
                return None
            return {
                "color": d.get("color"),
                "image": d.get("image"),
                "opacity": d.get("opacity", 1.0),
                "blur": d.get("blur", 0),
                "dim": d.get("dim"),
                "enabled": d.get("enabled", True),
                "animation": _normalize_animation(d.get("animation")),
                # overlay 区块专用：视频路径、亮度补偿色与挂载区域，其余区块恒 None
                "video": d.get("video"),
                "brightness_boost": d.get("brightness_boost"),
                "position": d.get("position"),
                "target": d.get("target"),
            }

        def _normalize_music(d: Optional[dict]) -> Optional[dict]:
            """music 区块字段集与区域背景不同，单独归一（不套 _normalize）"""
            if not d:
                return None
            tracks = d.get("tracks") or []
            if isinstance(tracks, str):
                tracks = [tracks]
            return {
                "tracks": [str(t) for t in tracks],
                "volume": float(d.get("volume", 0.6)),
                "loop": bool(d.get("loop", True)),
                "fade_ms": int(d.get("fade_ms", 300)),
                "enabled": d.get("enabled", True),
            }

        def _normalize_scene(d: Optional[dict]) -> Optional[dict]:
            """scene 专用归一化：在通用区域字段之外保留 foreground 子对象

            foreground 是壁纸拆出的全画布前景图层（如人物），字段集与 image/
            opacity 相同。它是嵌套 dict，不能被 _normalize 的白名单拍平。
            """
            normalized = _normalize(d)
            if normalized is None:
                return None
            fg = d.get("foreground")
            if isinstance(fg, dict):
                normalized["foreground"] = {
                    "image": fg.get("image"),
                    "opacity": fg.get("opacity", 1.0),
                    "enabled": fg.get("enabled", True),
                }
            else:
                normalized["foreground"] = None
            return normalized

        # scene 与 chat 是同一块（右侧整个 _chat_frame）：chat 为新区块名，
        # 与 scene 同时存在时逐字段覆盖，便于新主题只用 chat。
        scene_cfg = dict(new_bg.get("scene") or {})
        chat_cfg = new_bg.get("chat")
        if isinstance(chat_cfg, dict):
            scene_cfg.update(chat_cfg)
        scene_normalized = _normalize_scene(scene_cfg)

        return {
            "window": (
                _normalize(new_bg.get("window"))
                or _normalize(legacy_bg.get("window_bg"))
                or _normalize(legacy_bg.get("chat_list"))  # 历史 chat_list 也作 window（保持向后兼容）
            ),
            "sidebar": _normalize(new_bg.get("sidebar")),
            "chat_area": _normalize(new_bg.get("chat_area")),
            "scene": scene_normalized,
            "chat": scene_normalized,  # 新区块名，与 scene 同一对象
            "decorations": new_bg.get("decorations") or [],
            "overlay": _normalize(new_bg.get("overlay")),
            "music": _normalize_music(new_bg.get("music")),
        }

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
        """获取当前主题的 colors 部分（保证完整，供 UI 直接硬索引）

        启动期竞态：ui_theme_style 可能指向尚未加载（或版本门禁下永久不可用）
        的插件主题，此时 get_current_theme() 返回空 dict。旧实现静默返回 {}，
        current_theme() 的下游按完整字典硬索引（card_viewers._load_skeleton 的
        theme["card_bg_solid"]、chart_viewer_card 的 theme["accent"]）即抛
        KeyError。这里按生效深浅回退到内置默认主题，保证 colors 始终完整；
        插件主题刷新完成后自然切回真实主题，仅影响这一帧的取色。
        """
        theme = self.get_current_theme()
        colors = theme.get("colors", {})
        if colors:
            return colors
        try:
            from app.utils.config import DEFAULT_DARK_THEME, DEFAULT_LIGHT_THEME, Settings

            light = bool(Settings.get_instance().ui_light_mode.value)
            fallback_id = DEFAULT_LIGHT_THEME if light else DEFAULT_DARK_THEME
        except Exception:
            fallback_id = None
        if fallback_id:
            fallback = self.get_theme(fallback_id) or {}
            colors = fallback.get("colors", {})
            if colors:
                return colors
        # 极端：连默认主题文件都缺失，取任意已加载主题，仍不返回空集合
        first = next(iter(self._themes.values()), {})
        return first.get("colors", {}) or {}

    # ── 主题管理 ──────────────────────────────────────────

    _reload_callbacks: list = []

    def on_reload(self, callback):
        """注册主题重载完成后的回调（用于 UI 自动刷新等）"""
        if callback not in self._reload_callbacks:
            self._reload_callbacks.append(callback)


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

        另：推进一次外观纪元（``ThemeRefreshCoordinator.bump_appearance_epoch``）。
        这是"一次外观变更 = 一个纪元"的唯一定义点——batched 路径每次主题/字体/
        字号变更都无条件调本方法一次，插件页据此判定自己是否落后（隐藏期只置脏，
        显示时 ``ensure_theme_fresh`` 补刷）。放这里而不是 ``begin_refresh_cycle``：
        后者每窗口进一次，会在一轮内推进 N 次，先把可见页刷新再判它落后。
        """
        self._cached_light_check = (None, None)
        try:
            from app.utils.theme_refresh import ThemeRefreshCoordinator

            ThemeRefreshCoordinator.bump_appearance_epoch()
        except Exception:
            pass
        # 🐛 推进 ThemeRefreshCoordinator 版本号：使消息卡正文 CSS 变量注入
        # 的幂等短路失效。此前 should_skip / on_theme_changed 从未被调用，
        # 版本号恒为 0，导致 CodeWebViewer.refresh_theme 只在首次切换注入一次
        # CSS 变量，之后所有主题切换正文/思考块颜色不刷新。
        try:
            from app.utils.theme_refresh import ThemeRefreshCoordinator

            ThemeRefreshCoordinator.should_skip(self.get_current_theme_id())
        except Exception:
            pass
        # Phase E：发布主题切换事件（供插件订阅刷新自定义 UI）
        try:
            from app.core.infra.ui_event_bus import EV_THEME_CHANGED, UIEventBus

            theme_id = self.get_current_theme_id()
            theme_obj = self.get_current_theme() or {}
            is_dark = not self.is_light_theme(theme_id)
            UIEventBus.get_instance().publish(
                EV_THEME_CHANGED,
                theme_id=theme_id,
                theme_name=theme_obj.get("name", theme_id) if isinstance(theme_obj, dict) else theme_id,
                is_dark=is_dark,
            )
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

        # 🐛 先同步 qfluentwidgets 全局主题再分发（与 main_widget
        # _execute_batched_theme_refresh 全局段同款：清缓存 → setTheme → EV）。
        # 此前 setTheme 只存在于 OpenAIChatToolWindow._apply_runtime_ui_settings
        # 的 is_color 块内，而本方法按注册顺序分发 target：TabPanel /
        # TabManagerWindow / WorkbenchPanel 排在 main_widget 之前，刷新时全局
        # 主题还是旧值，其中的 FluentIcon 按旧主题渲染后无人补刷 → gitee 弹窗
        # 兜底切换后侧栏/标题区 icon 反色。
        # 清缓存必须在 setTheme 之前：reload() 热改主题 mode 时 theme_id 未变，
        # is_light_theme() 会命中旧缓存。
        self._cached_light_check = (None, None)
        try:
            from qfluentwidgets import Theme, setTheme

            setTheme(Theme.LIGHT if self.is_light_theme() else Theme.DARK, lazy=True)
        except Exception:
            pass

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
            # - 无 _theme_needs_refresh 属性的 target 保守不跳过
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
        fp = self._compute_fingerprint()
        if not force and self._themes and fp == self._themes_fingerprint:
            logger.debug("[ThemeManager] reload: 主题指纹未变化，跳过重扫与 UI 刷新")
            return

        # 重载前：保存当前主题的快照，用于后续比对
        old_id = self.get_current_theme_id()
        old_data = self.get_theme(old_id)

        self._themes.clear()
        self._load_themes()
        # 记录本次重载后的指纹，供下次 reload 比对。直接复用入口处算出的 fp：
        # 重扫本身不写文件，指纹不会因重扫而变化；旧实现在这里再算一遍，
        # 等于把全目录扫描成本翻倍（实测 550ms × 2）。
        self._themes_fingerprint = fp
        # 清除浅色检测缓存
        self._cached_light_check = (None, None)

        # 重载后：获取新数据，判断当前主题是否真实变化
        new_data = self.get_theme(old_id)
        if new_data != old_data:
            # 当前主题被修改/删除 → 全量刷新 UI
            # ⚠️ 同 id 内容变化必须显式推进 ThemeRefreshCoordinator 版本：
            # dispatch_refresh → on_theme_changed → should_skip(theme_id) 因
            # id 未变幂等短路、版本号不推进 → 消息卡 CodeWebViewer.refresh_theme
            # 同版本直接 return（实例级 _cached_streaming_html 不失效、
            # _needs_full_render 不置位），且 T16 lru 按版本分桶命中旧主题 HTML
            # → 卡片 HTML 正文/代码高亮不跟随刷新。QSS 部分不走版本号会正常
            # 刷新，形成"框架刷了、内容没刷"的错位观感。
            from app.utils.theme_refresh import ThemeRefreshCoordinator

            ThemeRefreshCoordinator.on_theme_changed(old_id)
            self.dispatch_refresh()

        # 兼容旧回调（用于设置面板更新主题下拉列表等，无论当前主题是否变化都需要）
        for cb in self._reload_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"[ThemeManager] reload callback error: {e}")

    def _plugin_theme_dirs(self) -> list:
        """扫描插件目录下所有 themes/ 子目录（按 plugins 根目录 mtime 缓存）

        [PERF] 收集本身要 iterdir 两个 plugins 根 + 每个 entry 两次 is_dir，
        实测 200~300ms（单目录枚举 26ms 起）。根目录 mtime 未变说明插件目录
        增删未发生，直接复用上次结果。
        """
        from app.utils.utils import get_app_data_dir

        roots = (Path(__file__).parent.parent.parent / "plugins", get_app_data_dir() / "plugins")
        sig_parts = []
        for root in roots:
            try:
                sig_parts.append(root.stat().st_mtime_ns)
            except OSError:
                sig_parts.append(None)
        sig = tuple(sig_parts)
        if self._plugin_theme_dirs_cache is not None and sig == self._plugin_theme_dirs_sig:
            return list(self._plugin_theme_dirs_cache)

        dirs: list = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for entry in sorted(root.iterdir()):
                    if entry.is_dir():
                        td = entry / "themes"
                        if td.is_dir():
                            dirs.append(td)
            except OSError:
                continue
        self._plugin_theme_dirs_cache = dirs
        self._plugin_theme_dirs_sig = sig
        return list(dirs)

    def _theme_source_dirs(self) -> list:
        """主题源根目录全集（与 _load_themes 的 4 个来源一一对应）"""
        from app.utils.utils import get_app_data_dir

        dirs = [_BUILTIN_THEMES_DIR]
        dirs.extend(self._plugin_theme_dirs())

        # 3. PluginManager 已启用插件主题路径（内存查询，无 I/O）
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                dirs.extend(Path(p) for p in pm.get_theme_paths())
        except Exception:
            pass

        # 4. 用户主题目录
        dirs.append(get_app_data_dir() / "themes")

        # 去重保序（同一目录可能同时来自直扫与 PluginManager）
        seen = set()
        out = []
        for d in dirs:
            key = str(d)
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out

    def _compute_fingerprint(self) -> str:
        """计算主题源目录指纹：所有主题 YAML 的 (路径, mtime_ns) 排序哈希

        与 _load_themes 的 4 个来源一一对应（见 _theme_source_dirs）。

        [PERF] 两级策略（旧实现单次 rglob 双后缀全树扫实测 550~1050ms）：
        1. 快路径：主题根目录 mtime 未变且距上次全量扫描不足
           _FINGERPRINT_FULL_WINDOW_S → 直接复用上次指纹（仅 6~8 次 stat，
           约 40ms）。插件/主题目录的增删都会改父目录 mtime，必被捕获。
        2. 全量路径：窗口过期或根目录 mtime 变化 → 单层 scandir 扫描
           （与 _load_from_dir 的目录约定一致：<themes>/<id>/<id>.yaml 或
           <themes>/<id>.yaml），用 DirEntry 自带 stat 数据，不再每文件单独
           stat，也不再同一棵树扫两遍（旧实现 .yaml 与 .yml 各一遍）。
        """
        import hashlib

        theme_dirs = self._theme_source_dirs()

        # ── 快路径：根目录 mtime 预检 ──
        root_sig_parts = []
        for base in theme_dirs:
            try:
                st = base.stat()
                root_sig_parts.append((str(base), st.st_mtime_ns))
            except OSError:
                root_sig_parts.append((str(base), None))
        root_sig = tuple(root_sig_parts)

        now = time.monotonic()
        if (
            self._fingerprint_value is not None
            and root_sig == self._fingerprint_root_sig
            and now - self._fingerprint_full_checked_at < _FINGERPRINT_FULL_WINDOW_S
        ):
            return self._fingerprint_value

        # ── 全量路径：单层扫描 ──
        h = hashlib.md5()
        for base in sorted(theme_dirs, key=str):
            try:
                entries = sorted(os.scandir(base), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir():
                        # 目录形式：只看同名 yaml（与 _load_from_dir 一致）
                        for suffix in (".yaml", ".yml"):
                            path = os.path.join(entry.path, entry.name + suffix)
                            try:
                                mtime_ns = os.stat(path).st_mtime_ns
                            except OSError:
                                continue
                            h.update(f"{path}:{mtime_ns}".encode("utf-8"))
                            break
                    elif entry.name.endswith((".yaml", ".yml")):
                        st = entry.stat()
                        h.update(f"{entry.path}:{st.st_mtime_ns}".encode("utf-8"))
                except OSError:
                    continue  # 文件被并发删除等竞态 → 跳过，重扫兜底

        value = h.hexdigest()
        self._fingerprint_value = value
        self._fingerprint_root_sig = root_sig
        self._fingerprint_full_checked_at = now
        return value



# 全局单例
theme_manager = ThemeManager()
