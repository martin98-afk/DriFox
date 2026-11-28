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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from loguru import logger

# 组件探测规则表（单一事实源在 kernel.KNOWN_COMPONENTS，此处只定义物理探测谓词）
from app.plugins.kernel import KNOWN_COMPONENTS

# 插件平台声明与 deps 统一加载（设计：docs/superpowers/specs/2026-08-27-plugin-platform-deps-design.md）
from app.plugins.deps_loader import check_platform, ensure_deps_on_path
from app.utils._startup_probe import mark as _probe
from app.utils.safe_mode import is_safe_mode as _safe_mode_active

# 插件名合法字符：首字符必须为字母或数字，后续允许字母数字与 _ . -
# 严禁路径分隔符与 `..`（plugin_name 会参与配置存储路径拼接，见 plugin_config_store）
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# [T26/H2] 插件发现缓存 schema 版本：缓存结构变化时递增（整体失效）
_DISCOVERY_CACHE_SCHEMA_VERSION = 2

# [T25] config_schema 解析结果缓存：key = (plugin_name, manifest_mtime_ns, size)，
# value = parse_config_schema() 的返回值（None 也缓存，表示"无合法 schema"）。
# parse_config_schema 只依赖 manifest 内容，而每次扫描/热重载都会对所有插件重跑
# 一遍（72 插件 × 每次），按 manifest mtime+size 缓存可跳过重复解析。
# 口径与 agent.py 的 _agent_file_cache 一致（mtime 寻址，非内容哈希）。
# 失效：_unregister_config_schema（所有插件移除/schema 删除路径的公共下沉点）
# 统一 pop；mtime/size 变化自然 miss，无需额外失效逻辑。
_CONFIG_SCHEMA_CACHE: Dict[tuple, Any] = {}


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

def _resolve_system_plugin_dir() -> Path:
    """系统插件根：单一事实源。

    打包形态为 onedir（Drifox.spec COLLECT，datas 把 plugins/ 放进 _internal），
    与项目 resource_path() 惯例一致：hasattr(sys, "_MEIPASS") 即分发目录
    （onedir 下 _MEIPASS == <安装目录>/_internal，非临时解压），系统插件根为
    _MEIPASS/plugins；开发环境为项目根 plugins/。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "plugins"
    return Path(__file__).resolve().parent.parent.parent.parent / "plugins"


def _assert_writable_plugin_target(path: Path) -> None:
    """P1-6：分发目录只读守卫——onedir 下 _internal（含系统插件根）不应被插件写入，
    打包形态（存在 _MEIPASS）下命中即拒；开发环境恒过。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    target = Path(path).resolve()
    if target.is_relative_to(Path(meipass).resolve()):
        logger.warning(f"[PluginManager] 打包版分发目录只读，拒绝写入: {target}")
        raise PermissionError(f"打包版分发目录只读: {target}")


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
    "hook_policies": lambda d: (d / "hook_policies").exists() and any((d / "hook_policies").glob("*.py")),
    "storages": lambda d: (d / "storages").exists() and any((d / "storages").glob("*.py")),
    "serializers": lambda d: (d / "serializers").exists() and any((d / "serializers").glob("*.py")),
    "gateways": lambda d: (d / "gateways").exists() and any((d / "gateways").glob("*.py")),
    "transports": lambda d: (d / "transports").exists() and any((d / "transports").glob("*.py")),
    "stream_sinks": lambda d: (d / "stream_sinks").exists() and any((d / "stream_sinks").glob("*.py")),
    "engines": lambda d: (d / "engines").exists() and any((d / "engines").glob("*.py")),
    "context_tiers": lambda d: (d / "context_tiers").exists() and any((d / "context_tiers").glob("*.py")),
    "budget_resolvers": lambda d: (
        (d / "budget_resolvers").exists() and any((d / "budget_resolvers").glob("*.py"))
    ),
}


# P1-2：插件清单文件大小上限（防超大 manifest 拖垮加载/解析）
_MANIFEST_MAX_BYTES = 256 * 1024


def _load_manifest_file(manifest_path: Path):
    """读取插件清单 JSON（P1-2：文件 ≤256KB，超限拒载 + 明确报错）。

    Returns:
        (manifest, schema_warnings)；文件不可读 / 超限 / 解析失败 / 根节点非对象
        → (None, [])。
    """
    try:
        size = manifest_path.stat().st_size
    except OSError as e:
        logger.error(f"[PluginManager] 清单不可读: {manifest_path} {e}")
        return None
    if size > _MANIFEST_MAX_BYTES:
        logger.error(
            f"[PluginManager] 清单文件 {size} 字节超过上限 {_MANIFEST_MAX_BYTES}（256KB），拒载: {manifest_path}"
        )
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[PluginManager] 清单解析失败: {manifest_path} {e}")
        return None
    if not isinstance(manifest, dict):
        logger.error(f"[PluginManager] 清单根节点不是对象，跳过: {manifest_path}")
        return None
    manifest = _enforce_manifest_limits(manifest, manifest_path)
    # 契约1：manifest schema 宽容校验（类型不符 → warning + 缺省处理；未知字段忽略）
    from app.plugins.contracts.manifest_schema import validate_manifest

    manifest, schema_warnings = validate_manifest(
        manifest, source=manifest_path.parent.parent.name, fallback_name=manifest_path.parent.parent.name
    )
    # 契约2b：capabilities 声明语义校验。未知 ID 在 expand 时被静默保留、
    # 运行期表现为"拿不到能力却看不到原因"，这里在加载期挑出来进
    # manifest_warnings（两条扫描路径都会 warning + 存 PluginInfo）。
    from app.plugins.capabilities.catalog import unknown_declared

    for bad in unknown_declared(manifest.get("capabilities")):
        schema_warnings.append(
            f"[CapabilityManifest] 未知能力 ID {bad!r}（catalog 无此项，运行期不可用，检查拼写或改用组通配）"
        )
    return manifest, schema_warnings


def _enforce_manifest_limits(manifest: dict, manifest_path: Path) -> dict:
    """P1-2：manifest 资源上限（超限截断/丢弃 + warning，不拒载）。"""
    icon = manifest.get("icon")
    if isinstance(icon, str) and len(icon) > 512:
        logger.warning(
            f"[PluginManager] manifest icon 字段 {len(icon)} 字符超过上限 512，已丢弃: {manifest_path}"
        )
        manifest.pop("icon", None)
    elif isinstance(icon, dict):
        cleaned = {k: v for k, v in icon.items() if isinstance(v, str) and len(v) <= 512}
        if len(cleaned) != len(icon):
            logger.warning(
                f"[PluginManager] manifest icon 子项超长(>512 字符)，已丢弃超限项: {manifest_path}"
            )
            manifest["icon"] = cleaned
    pip = manifest.get("pip")
    if isinstance(pip, list) and len(pip) > 20:
        logger.warning(
            f"[PluginManager] manifest pip 声明 {len(pip)} 条超过上限 20，已截断: {manifest_path}"
        )
        manifest["pip"] = pip[:20]
    return manifest


def _detect_components(plugin_dir: Path) -> Dict[str, bool]:
    """按 kernel.KNOWN_COMPONENTS 优先级探测插件目录实际组件（物理目录为准）

    返回 {component_name: True} 子集。新增组件类型只需在 kernel.KNOWN_COMPONENTS 登记
    并在 _COMPONENT_PROBES 加一条谓词即可，无需改动扫描逻辑。
    """
    return {name: True for name in KNOWN_COMPONENTS if _COMPONENT_PROBES[name](plugin_dir)}


# ============================================================
# 插件信息数据类
# ============================================================


# icon_config 解析哨兵：区分「未解析」与「解析结果为 None」，None 也必须缓存
# （无图标插件每次访问都不再重复付 FS 扫描）
_ICON_CONFIG_UNRESOLVED = object()


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
    api_compatible: bool = True  # 契约2：api_version 与宿主插件 API 版本比对（缺省=兼容）
    api_reason: str = ""  # api_version 不兼容原因（供 UI 展示）
    deps_conflict_reason: str = ""  # L2：跨插件依赖版本不可调和的原因（非空=拦截）
    manifest_warnings: List[str] = field(default_factory=list)  # 契约1：schema 校验 warning 清单（UI 角标后续）
    overridden_by: str = ""  # 同名覆盖标记：被覆盖方 plugin_type（"system"/"claude"），空=未被覆盖
    # icon_config 解析缓存（实例级）：重扫/热重载/安装/更新走 _scan_one_plugin_dir
    # 或 _plugin_info_from_cached 重建实例，缓存随旧实例丢弃，无需主动失效钩子
    _icon_config_cache: Any = field(default=_ICON_CONFIG_UNRESOLVED, repr=False, compare=False)

    @property
    def description(self) -> str:
        return self.manifest.get("description", "")

    @property
    def version(self) -> str:
        return self.manifest.get("version", "0.0.0")

    @property
    def load_blocked(self) -> bool:
        """是否被门禁拦截（平台/版本/api_version/依赖冲突任一不满足），拦截时宿主不得加载其任何组件"""
        if not (self.platform_compatible and self.version_compatible and self.api_compatible):
            return True
        return bool(self.deps_conflict_reason)

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
        """返回 {"light": Path, "dark": Path} 或 None（解析结果缓存于实例）

        ★ 性能（2026-11-03）：解析走 Path.resolve/exists 文件系统调用（Windows 上
        realpath 冷态单次可达数十 ms），本 property 是图标渲染热路径：tab_panel
        插件标签页、设置卡 MCP 行/插件配置卡每次刷新都访问，不缓存时一次设置卡
        构造要重复付几十次 FS 调用（watchdog 实测打开设置阻塞 519ms 栈正停在
        icon_config 内的 os.path.realpath）。失效策略见 _icon_config_cache 字段注释。
        """
        if self._icon_config_cache is not _ICON_CONFIG_UNRESOLVED:
            return self._icon_config_cache
        self._icon_config_cache = self._resolve_icon_config()
        return self._icon_config_cache

    def _resolve_icon_config(self) -> Optional[dict]:
        """从 manifest 的 "icon" 字段解析插件图标路径（真实 FS 解析，由 icon_config 缓存）。

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
            p = (self.path / raw).resolve()
            # P1-1：icon 路径逃逸校验——解析后必须仍在插件根内（str/dict 两形态同规则）
            if not p.is_relative_to(self.path.resolve()):
                logger.warning(
                    f"[PluginManager] 插件 '{self.name}' icon 路径越界，已丢弃: {raw!r}"
                )
                return None
            if p.exists():
                return {"light": p, "dark": p}
            return None
        if isinstance(raw, dict):
            result: dict = {}
            root_resolved = self.path.resolve()
            for theme in ("light", "dark"):
                path_str = raw.get(theme)
                if path_str:
                    p = (self.path / path_str).resolve()
                    # P1-1：icon 路径逃逸校验（越界条目丢弃 + warning）
                    if not p.is_relative_to(root_resolved):
                        logger.warning(
                            f"[PluginManager] 插件 '{self.name}' icon 路径越界，已丢弃: {path_str!r}"
                        )
                        continue
                    if p.exists():
                        result[theme] = p
            # 单主题补齐：只有一个主题时补齐另一个
            if "light" not in result and "dark" in result:
                result["light"] = result["dark"]
            if "dark" not in result and "light" in result:
                result["dark"] = result["light"]
            return result if result else None


def _hard_gate_blocked(p: PluginInfo) -> bool:
    """平台/版本/api 三维硬门禁（不含依赖冲突，避免判定自引用）。"""
    return not (p.platform_compatible and p.version_compatible and p.api_compatible)


def _pick_deps_winner(plugins: Dict[str, PluginInfo], a: str, b: str) -> tuple:
    """冲突对裁决：返回 (保留方, 让出方)。

    优先级 system > claude > user：内置插件是 app 功能的一部分，用户插件是扩展。
    同类型按名字字典序，保证每次启动裁决结果一致（不随扫描顺序漂移）。
    """
    rank = {"system": 0, "claude": 1, "user": 2}

    def key(name: str) -> tuple:
        p = plugins.get(name)
        return (rank.get(getattr(p, "plugin_type", "user"), 2), name)

    return (a, b) if key(a) <= key(b) else (b, a)


# ============================================================
# 插件管理器（单例）
# ============================================================


class PluginManager:
    """
    插件管理器（全局单例）

    负责扫描、加载、查询插件。各子系统通过 PluginManager 获取插件资源路径。
    """

    _instance: Optional["PluginManager"] = None

    # 安全模式（--safe-mode）：本进程只加载系统插件。类属性给 __new__ 构造的
    # 测试实例提供默认值，真实实例在 __init__ 中按命令行开关覆盖。
    _safe_mode: bool = False

    # 插件搜索路径（按优先级）
    # 系统插件：项目根目录 plugins/（打包在 exe 中；P1-6：onedir 下解析 _MEIPASS/plugins）
    _SYSTEM_PLUGIN_DIR = _resolve_system_plugin_dir()
    # 不可禁用核心插件名单（黑名单制）：禁用会断核心链路（组件宿主/插件市场自身）。
    # system 插件已按组件类型拆分为 system-* 系列内置插件，其中承载核心链路的
    # 子集（工具/序列化/存储/模型适配/服务商/Hooks/两类策略/命令/智能体
    # /协议传输/流式接收器）不可禁用；
    # 外围插件（主题/技能/MCP/团队模板/UI 页/上下文策略）可整插件禁用。
    # plugin-marketplace/ui/installer.py 状态分类与本名单保持单一数据源。
    _NON_DISABLEABLE = frozenset(
        {
            "system-interact",
            "system-core",
            "system-question",
            "system-serializers",
            "system-storages",
            "system-model-adapters",
            "system-providers",
            "system-hooks",
            "system-loop-policies",
            "system-hook-policies",
            "system-commands",
            "system-agents",
            "system-transports",
            "system-stream-sinks",
            "system-title-generator",
            "plugin-marketplace",
        }
    )
    # 用户插件：~/.drifox/plugins/（相对于 app_data_dir）
    _USER_PLUGIN_DIR_NAME = "plugins"
    # Claude Code 插件目录（同时支持两种生态）
    _CLAUDE_USER_SKILLS_DIR = Path.home() / ".claude" / "skills"
    _CLAUDE_PLUGIN_CACHE_DIR = Path.home() / ".claude" / "plugins" / "cache"

    def __init__(self):
        self._plugins: Dict[str, PluginInfo] = {}
        self._discovery_candidates: dict[str, dict[str, PluginInfo]] = {}
        self._initialized = False
        self._app_data_dir: Optional[Path] = None
        # 安全模式：托盘「安全模式重启」注入 --safe-mode，只加载系统插件
        self._safe_mode = _safe_mode_active()
        # 组件/细项禁用集缓存（None = 未加载）。Settings 里该项变更极低频，
        # 但 hooks 触发等热路径会高频查询，故缓存到进程内，写操作同步更新。
        self._disabled_components_cache: Optional[frozenset] = None
        # 插件目录签名缓存（rescan 短路用，None = 尚未扫描过）
        self._last_scan_signature: Optional[tuple] = None
        # [PERF 2026-10-24] 待回放的自动设置卡：扫描期只登记 schema，不 import UI 链。
        # 原实现 _register_config_schema 在扫描循环里直接 import
        # app.plugins.registries.ui_plugin_registry + app.widgets.cards.settings.
        # plugin_config_card（Qt/qfluentwidgets 级联），把 UI 依赖拖进
        # PluginManager.initialize 的同步段（实测 ~166ms）。改为登记 + 由
        # main_widget._after_plugins_loaded 调 replay_config_cards() 统一回放。
        self._pending_config_cards: Dict[str, Any] = {}

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

        # 0. 一次性迁移：system 单体插件拆分为 system-* 系列后的旧状态键改写
        self._migrate_split_system_states()

        # 安全模式：只扫系统插件，跳过发现缓存与 Claude/用户插件目录。
        # 缓存不能读也不能写：缓存 key 只含磁盘目录签名，与加载范围无关，
        # 读写会让正常启动命中「仅 system」的缓存并丢掉用户插件。
        if self._safe_mode:
            logger.warning("[PluginManager] 安全模式：仅加载系统插件（跳过 Claude/用户插件）")
            self._discover_system_plugins()
            logger.info(f"[PluginManager] Safe mode loaded {len(self._plugins)} system plugins")
            self._initialized = True
            self._restore_enabled_from_settings()
            try:
                self._last_scan_signature = self._plugins_dir_signature(include_mtime=False)
            except Exception:
                self._last_scan_signature = None
            self._schedule_preload_plugin_tools()
            return

        # [T26/H2] 插件发现缓存命中 → 跳过三连全扫（热态 -0.8~2.5s）。
        # 命中路径复用 _merge_discovered 合并语义 + 重放幂等副作用
        # （ensure_deps_on_path / _register_config_schema）；异常回退全扫。
        _probe("p3.cache_probe_start")
        hit = self._try_load_from_discovery_cache()
        _probe(f"p3.cache_probe_end(hit={hit})")
        if hit:
            self._initialized = True
            self._restore_enabled_from_settings()
            # perf: (#31) 命中路径不再同步算目录签名（67 插件 ≈134 stat，估
            # 7-40ms 白占启动关键路径）：置 None 后延迟 2s 后台补算；签名唯一
            # 用途是启动 3 秒后的 rescan 短路，补算前 rescan 入口现场算兜底
            # （最坏打穿一次全扫，检测语义不变）
            self._last_scan_signature = None
            try:
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(2000, self._backfill_scan_signature)
            except Exception:
                self._backfill_scan_signature()
            # perf: 优化点（#5a）：工具预载后台化——同步段实测 161ms（T3c 弹着点），
            # 照 _backfill_scan_signature 同款 QTimer 2s 延迟，启动段直减
            self._schedule_preload_plugin_tools()
            return

        selected, _ = self._scan_selected_plugins()
        self._plugins.update(selected)
        self._apply_deps_conflict_gate(self._plugins)
        for plugin in self._plugins.values():
            self._activate_discovery(plugin)

        logger.info(f"[PluginManager] Loaded {len(self._plugins)} plugins: {', '.join(self._plugins.keys())}")
        self._initialized = True

        # 自动从 Settings 恢复已启用状态
        self._restore_enabled_from_settings()

        # 记录首次扫描的目录签名：让启动期那次 rescan（ConfigSync 合并重载兜底）
        # 能直接短路，省掉一次全量重扫（实测 ~450ms）。口径与 ：919 rescan 短路
        # 统一用弱签名（include_mtime=False），否则强签名≠弱签名永不命中（#F17）
        try:
            self._last_scan_signature = self._plugins_dir_signature(include_mtime=False)
        except Exception:
            self._last_scan_signature = None

        # [T26] 全扫成功 → 回写发现缓存（下次启动命中；含损坏自修复）
        self._save_discovery_cache()
        # perf: 优化点（#5a）：同命中路径，预载后台化
        self._schedule_preload_plugin_tools()

    def _schedule_preload_plugin_tools(self):
        """[#5a] 工具预载后台化调度：QTimer 2s 后主线程执行（照 _backfill_scan_signature 模式）。

        perf: T3c 实测同步预载 161ms 挡在 plugin_scan 段；延迟后启动段直减。
        选主线程 QTimer 而非后台线程：插件工具 import 含 bs4 等重依赖，与
        main_widget 级联 import 争 Python import 锁有负收益前科（实测拖慢主线程
        26%），2s 时点级联早已完成。幂等互斥语义不变：2s 内保险丝先行触发则
        本调度 0ms 短路；预载失败时保险丝仍可按 60s 节流重试兜底。
        """
        try:
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(2000, self._preload_plugin_tools)
        except Exception:
            self._preload_plugin_tools()

    def _preload_plugin_tools(self):
        """[S1-C] 插件工具加载归位：initialize 末尾预调一次（幂等）。

        perf: 原 ToolNameMapper.is_known 的 _ensure_plugin_tools_loaded 保险丝
        会在首个 agent md 解析时隐式同步加载全部插件工具模块（实测冷 188ms），
        把这笔成本藏进 reload_agents 段（#35 归因）。预调后成本显形归位到
        plugin_scan 段，agent 解析回归毫秒级；重复调用零开销。
        """
        try:
            from app.tools import _ensure_plugin_tools_loaded

            _ensure_plugin_tools_loaded()
        except Exception as e:
            logger.debug(f"[PluginManager] 插件工具预载失败（跳过，后续保险丝兑底）: {e}")

    def _backfill_scan_signature(self) -> None:
        """延迟补算目录签名（#31 命中路径专用）。

        rescan 若已先行写回（非 None）则跳过，幂等；异常静默置 None（rescan
        入口现场算兜底，与命中路径改前行为一致）。
        """
        if self._last_scan_signature is not None:
            return
        try:
            self._last_scan_signature = self._plugins_dir_signature(include_mtime=False)
        except Exception:
            self._last_scan_signature = None

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
        except Exception as e:
            logger.warning(f"[PluginManager] 从 Settings 恢复启用状态失败（吞异常保留语义）: {e}")

    # ============================================================
    # system 单体插件拆分迁移（v0.5.9 → 拆分版，一次性 + 幂等）
    # ============================================================

    # 组件目录 → 拆分后承载该组件的系统插件名（与 plugins/ 目录一一对应）
    _SPLIT_COMPONENT_TO_PLUGIN = {
        "commands": "system-commands",
        "agents": "system-agents",
        "skills": "system-skills",
        "themes": "system-themes",
        "hooks": "system-hooks",
        "loop_policies": "system-loop-policies",
        "hook_policies": "system-hook-policies",
        "model_adapters": "system-model-adapters",
        "providers": "system-providers",
        "storages": "system-storages",
        "serializers": "system-serializers",
        "transports": "system-transports",
        "stream_sinks": "system-stream-sinks",
        "tools": "system-interact",  # 2026-11-12 由 system-tools 更名
        "context_tiers": "system-context",
        "budget_resolvers": "system-context",
        # 「ui」组件的系统插件锚点：v0.5.9 单体拆分迁移的一次性承继映射。
        # ⚠ 不可删除：老版本用户升级时按此把旧 ui 组件归入 artifacts-manager；
        # 该插件现已改为卡片成果栏（2026-11-17），映射对已迁移用户无副作用。
        "ui": "artifacts-manager",
        "team_templates": "system-team-templates",
        "mcp": "system-mcp",
    }
    # 旧单体插件 config_schema（网页搜索 API Key）的承继插件。
    # 保持历史名 "system-tools"：system-web 的 _ensure_migrated 也按该历史路径读，
    # 与现插件名 system-interact 无关，改名不能动这个锚点。
    _SPLIT_CONFIG_HEIR = "system-tools"

    # 工具级禁用键迁移：`system-tools:tools:<tool>` → `<拆分后插件>:tools:<tool>`
    # （拆分后工具分散到多个插件，旧禁用键不改写会让用户已禁用的工具重新出现）
    _SPLIT_TOOL_PLUGINS = (
        "system-core",
        "system-web",
        "system-question",
        "system-team",
        "system-diagnostics",
    )
    _SPLIT_TOOL_TO_PLUGIN = {
        "read": "system-core",
        "write": "system-core",
        "edit": "system-core",
        "multi_edit": "system-core",
        "grep": "system-core",
        "list": "system-core",
        "glob": "system-core",
        "scan_repo": "system-core",
        "stage_files": "system-core",
        "bash": "system-core",
        "background_task": "system-core",
        "todowrite": "system-core",
        "todoread": "system-core",
        "websearch": "system-web",
        "webfetch": "system-web",
        "question": "system-question",
        "team_send_message": "system-team",
        "team_manage": "system-team",
        "get_diagnostics": "system-diagnostics",
        "lsp": "system-diagnostics",
        # 拆分时留守在 system-tools（2026-11-12 更名 system-interact）的工具：
        # 旧禁用键不改写会在更名后指向不存在的插件，已禁用工具回魂
        "skill": "system-interact",
        "manage_skill": "system-interact",
        "upload_file": "system-interact",
        "mcp_manage": "system-interact",
        "mcp_list_servers": "system-interact",  # mcp_manage 更名前的旧工具名
    }

    def _migrate_split_system_states(self) -> None:
        """system 插件按类型拆分后的一次性状态迁移（幂等，无旧键时零写入）。

        1. disabled_plugin_components：`system:<comp>[:<item>]` → `<split>:<comp>[:<item>]`
        2. enabled_plugins / disabled_plugins：移除已不存在的 "system" 条目
        3. plugin_data/system/config.json → plugin_data/system-tools/config.json
           （旧 monolith 的网页搜索 API Key 由 system-tools 承继）
        """
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            changed = False

            # 1. 组件/细项禁用键改写
            disabled = list(cfg.disabled_plugin_components.value or [])
            mapped: List[str] = []
            for key in disabled:
                if isinstance(key, str) and key.startswith("system:"):
                    rest = key[len("system:") :]
                    comp = rest.split(":", 1)[0]
                    new_name = self._SPLIT_COMPONENT_TO_PLUGIN.get(comp)
                    if new_name:
                        mapped.append(f"{new_name}:{rest}")
                        changed = True
                        continue
                mapped.append(key)
            # 1b. 工具级禁用键改写：system-tools:tools:<tool> → <拆分后插件>:tools:<tool>
            remapped: List[str] = []
            for key in mapped:
                if isinstance(key, str) and key.startswith("system-tools:tools:"):
                    tool = key[len("system-tools:tools:") :]
                    owner = self._SPLIT_TOOL_TO_PLUGIN.get(tool)
                    if owner:
                        remapped.append(f"{owner}:tools:{tool}")
                        changed = True
                        continue
                remapped.append(key)
            mapped = remapped

            if changed:
                cfg.set(cfg.disabled_plugin_components, mapped, save=True)
                logger.info(f"[PluginManager] 迁移 system 拆分禁用键 {len(mapped)} 条")

            # 2. 启用/禁用插件列表：清理旧单体名 + 补录拆分插件名
            #    补录必须发生在迁移期（早于 Settings 期 provider warmup 的白名单检查），
            #    否则启动早期 load_providers 会把 system-providers 等整体跳过，
            #    导致 x-opencode-session 等插件声明能力丢失（400 MissingSessionID）。
            for setting in (cfg.enabled_plugins, cfg.disabled_plugins):
                names = list(setting.value or [])
                if "system" in names:
                    names.remove("system")
                    cfg.set(setting, names, save=True)
                    changed = True
            enabled = list(cfg.enabled_plugins.value or [])
            missing = [n for n in self._SPLIT_COMPONENT_TO_PLUGIN.values() if n not in enabled]
            missing += [n for n in self._SPLIT_TOOL_PLUGINS if n not in enabled]
            if missing:
                cfg.set(cfg.enabled_plugins, enabled + missing, save=True)
                changed = True
                logger.info(f"[PluginManager] 补录拆分插件到启用白名单: {missing}")

            if changed:
                logger.info("[PluginManager] system 拆分状态迁移完成")
        except Exception as e:
            logger.warning(f"[PluginManager] system 拆分状态迁移失败（吞异常不影响启动）: {e}")

        # 3. 插件配置文件承继（独立于 Settings，失败静默）
        self._migrate_split_system_config_file()

    def _migrate_split_system_config_file(self) -> None:
        """plugin_data/system/config.json → plugin_data/system-tools/config.json（幂等）"""
        try:
            from app.utils.utils import get_app_data_dir

            old_path = Path(get_app_data_dir()) / "plugin_data" / "system" / "config.json"
            new_path = Path(get_app_data_dir()) / "plugin_data" / self._SPLIT_CONFIG_HEIR / "config.json"
            if old_path.exists() and not new_path.exists():
                new_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.write_bytes(old_path.read_bytes())
                logger.info(f"[PluginManager] 已承继旧 system 插件配置 → {new_path}")
        except Exception as e:
            logger.warning(f"[PluginManager] system 插件配置承继失败（吞异常）: {e}")


    def reset(self):
        """重置（主要用于测试）"""
        self._plugins.clear()
        self._discovery_candidates = {}
        self._initialized = False

    def is_initialized(self) -> bool:
        return self._initialized

    def is_safe_mode(self) -> bool:
        """本进程是否为安全模式（只加载系统插件，由 --safe-mode 决定）"""
        return bool(self._safe_mode)

    # ============================================================
    # 运行时重扫
    # ============================================================

    def _plugins_dir_signature(self, include_mtime: bool = True) -> tuple:
        """插件目录级签名：各插件根的存在性 + 其下每个插件目录名与 mtime_ns

        用于 rescan 短路：签名未变 = 目录层面的新增/删除状态与上次扫描一致
        （rescan 语义本就只追踪目录级别变化，不追踪插件内部文件变更），可跳过
        数十个插件目录的全量重扫。实测全量重扫约 680ms/次，而启动期 ConfigSync
        的 3s 兜底与热重载合并路径都会调用 rescan。

        include_mtime=False（rescan 短路专用）：剔除目录 mtime 成分，只保留
        存在性 + 目录名单 + manifest 元数据。目录内部文件增删/覆盖会刷父目录
        mtime（如 ConfigSync 云端恢复 user-custom 解压覆盖），但 rescan 的
        diff 只比 manifest，扫完注定空 diff（实测 67 插件全扫空转）；组件级
        文件变更由各组件 watcher 热重载链负责，与 rescan 职责不重叠。
        目录改名/增删由名单捕获，manifest 内容变更由 _manifest_meta 捕获，
        弱签名对 rescan 结果严格等价。
        """
        sig: list = []
        roots = [self._SYSTEM_PLUGIN_DIR]
        if self._app_data_dir:
            roots.append(self._app_data_dir / self._USER_PLUGIN_DIR_NAME)
        roots.extend((self._CLAUDE_USER_SKILLS_DIR, self._CLAUDE_PLUGIN_CACHE_DIR))
        for root in roots:
            root_path = Path(root)
            if not root_path.exists():
                sig.append((str(root_path), False, 0, ()))
                continue
            try:
                children = sorted(p.name for p in root_path.iterdir())
            except OSError:
                children = []
            entries = []
            for name in children:
                if not include_mtime:
                    entries.append((name, 0))
                    continue
                try:
                    entries.append((name, (root_path / name).stat().st_mtime_ns))
                except OSError:
                    entries.append((name, -1))
            if include_mtime:
                try:
                    root_mtime = root_path.stat().st_mtime_ns
                except OSError:
                    root_mtime = 0
            else:
                root_mtime = 0
            sig.append((str(root_path), True, root_mtime, tuple(entries)))
        return tuple(sig) + self._manifest_meta()

    def _manifest_meta(self) -> tuple:
        """每个 manifest 的 (path, size, mtime_ns) 元组（两路 manifest 路径）。

        目录签名只覆盖插件目录级变化（新增/删除/目录 mtime）；manifest 文件**内容**
        编辑（version/api_version/config_schema 等）不改变父目录 mtime（NTFS 父目录
        mtime 陷阱：只有直接子项的增删才会刷新父目录 mtime），故 rescan 签名必须
        合并本元数据才能感知 manifest 内容变更。

        历史缺陷：_plugins_dir_signature 曾不含本项，导致改插件 manifest 后 rescan
        返回空 diff、agents/commands 等组件变更被吞（#17 修复）。
        """
        manifest_meta: list = []
        roots = [self._SYSTEM_PLUGIN_DIR]
        if self._app_data_dir:
            roots.append(self._app_data_dir / self._USER_PLUGIN_DIR_NAME)
        roots.extend((self._CLAUDE_USER_SKILLS_DIR, self._CLAUDE_PLUGIN_CACHE_DIR))
        for root in roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            try:
                children = sorted(p.name for p in root_path.iterdir())
            except OSError:
                continue
            for name in children:
                for mf in (
                    root_path / name / ".drifox-plugin" / "plugin.json",
                    root_path / name / ".claude-plugin" / "plugin.json",
                ):
                    try:
                        st = mf.stat()
                    except OSError:
                        continue
                    manifest_meta.append((str(mf), st.st_size, st.st_mtime_ns))
        return (tuple(manifest_meta),)

    # ============================================================
    # 插件发现持久化缓存（[T26/H2]：命中跳过全量插件扫描）
    # ============================================================

    def _discovery_cache_path(self) -> Path:
        """发现缓存落盘路径：<data_dir>/cache/plugin_discovery.json"""
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir() / "cache" / "plugin_discovery.json"

    def _discovery_signature(self) -> tuple:
        """目录签名（_plugins_dir_signature，已含 manifest 元数据）。

        #29 问题 2：此前重复追加 _manifest_meta()，多一轮全插件 manifest stat
        （_plugins_dir_signature 末尾已合并同一元数据）。
        """
        return self._plugins_dir_signature()

    def _discovery_cache_key(self) -> list:
        """缓存键四元组：schema 版本 + 宿主版本 + 插件 API 契约版本 + 目录/清单签名。"""
        from app.plugins.version_gate import HOST_PLUGIN_API_VERSION, host_version

        return [
            _DISCOVERY_CACHE_SCHEMA_VERSION,
            host_version(),
            HOST_PLUGIN_API_VERSION,
            self._discovery_signature(),
        ]

    @staticmethod
    def _serialize_plugin_info(p: PluginInfo) -> dict:
        return {
            "name": p.name,
            "manifest": p.manifest,
            "path": str(p.path),
            "plugin_type": p.plugin_type,
            "platform_compatible": p.platform_compatible,
            "version_compatible": p.version_compatible,
            "version_reason": p.version_reason,
            "api_compatible": p.api_compatible,
            "api_reason": p.api_reason,
            "manifest_warnings": p.manifest_warnings,
            "overridden_by": p.overridden_by,
        }

    @staticmethod
    def _plugin_info_from_cached(d: dict) -> Optional[PluginInfo]:
        """缓存条目 → PluginInfo（str→Path）；缺失/类型异常返回 None（全扫兜底）。"""
        try:
            return PluginInfo(
                name=d["name"],
                manifest=d["manifest"],
                path=Path(d["path"]),
                plugin_type=d.get("plugin_type", "user"),
                platform_compatible=d.get("platform_compatible", True),
                version_compatible=d.get("version_compatible", True),
                version_reason=d.get("version_reason", ""),
                api_compatible=d.get("api_compatible", True),
                api_reason=d.get("api_reason", ""),
                manifest_warnings=list(d.get("manifest_warnings", [])),
                overridden_by=d.get("overridden_by", ""),
            )
        except Exception:
            return None

    def _save_discovery_cache(self) -> None:
        """原子回写发现缓存（tmp + replace）；写失败吞异常（缓存永不致命）。

        [T10g] 空插件守卫：_plugins 为空时（如测试隔离目录实例）直接不写，
        避免空缓存文件落盘污染——真实路径靠 key 不匹配兜底，但空缓存文件
        本身不应存在。

        安全模式不写：缓存 key 与加载范围无关，落盘后正常启动会命中
        「仅 system」缓存，用户插件在正常运行中消失。
        """
        if self._safe_mode:
            return
        if not self._plugins:
            return
        try:
            groups: Dict[str, list] = {"system": [], "claude": [], "user": []}
            for group in self._discovery_candidates.values():
                for plugin in group.values():
                    groups[plugin.plugin_type].append(self._serialize_plugin_info(plugin))
            payload = {"key": self._discovery_cache_key(), "groups": groups}
            cache_file = self._discovery_cache_path()
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_name(cache_file.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache_file)
            logger.debug(f"[PluginManager] 发现缓存已回写 full scan 结果（{len(self._plugins)} 个插件）")
        except Exception as e:
            logger.warning(f"[PluginManager] 发现缓存写入失败（忽略）: {e}")

    def _try_load_from_discovery_cache(self) -> bool:
        """尝试命中发现缓存：重建 _plugins 并重放幂等副作用。

        任何一步异常 / 键不等 / 条目损坏 → 返回 False 走三连全扫兜底。
        验收日志：命中打 "from cache"、全扫不打（键不等打 "full scan"）。
        """
        try:
            if self._safe_mode:
                # 缓存里含 Claude/用户插件，安全模式必须走全扫（只扫系统）
                return False
            cache_file = self._discovery_cache_path()
            if not cache_file.exists():
                return False
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            # [T26] 键比较用规范化 JSON 串：signature 含 tuple，JSON roundtrip 后
            # tuple→list，直接 != 会恒不等导致缓存永不命中
            if json.dumps(payload.get("key"), sort_keys=True) != json.dumps(
                self._discovery_cache_key(), sort_keys=True
            ):
                logger.info("[PluginManager] 插件发现缓存键不匹配，full scan")
                return False
            groups = payload.get("groups", {})
            rebuilt: Dict[str, Dict[str, PluginInfo]] = {}
            for gtype in ("system", "claude", "user"):
                infos = []
                for d in groups.get(gtype, []):
                    pi = self._plugin_info_from_cached(d)
                    if pi is None:
                        logger.info("[PluginManager] 发现缓存条目损坏，full scan")
                        return False
                    infos.append(pi)
                rebuilt[gtype] = {p.name: p for p in infos}
            if not rebuilt["system"]:
                return False
            # 合并重跑（与 rescan 同源语义，[T26]）
            self._discovery_candidates = rebuilt
            new_plugins, _changed = self._merge_discovered(
                rebuilt["system"], rebuilt["claude"], rebuilt["user"], self._allow_user_override()
            )
            self._plugins.update(new_plugins)
            # L2：先跑依赖冲突裁决，replay 注入循环会自然跳过被裁为让出方的插件
            self._apply_deps_conflict_gate(new_plugins)
            # 副作用重放（幂等）：deps 注入仅门禁通过插件（G3 对齐）；config_schema 注册
            _probe("p2.replay.start")
            for p in self._plugins.values():
                self._activate_discovery(p)
            _probe("p2.replay.end")
            logger.info(
                f"[PluginManager] 插件发现命中缓存 from cache: {len(self._plugins)} 个插件（跳过全扫）"
            )
            return True
        except Exception as e:
            logger.warning(f"[PluginManager] 发现缓存读取异常，回退全扫: {e}")
            return False

    def _invalidate_discovery_cache(self) -> None:
        """删除发现缓存文件（热扫描/新插件注册后，内存态与缓存可能不一致）。"""
        try:
            cache_file = self._discovery_cache_path()
            if cache_file.exists():
                cache_file.unlink()
                logger.debug("[PluginManager] 发现缓存已作废（文件已删除）")
        except Exception as e:
            logger.warning(f"[PluginManager] 发现缓存删除失败（忽略）: {e}")

    @staticmethod
    def _allow_user_override() -> bool:
        try:
            from app.utils.config import Settings

            return bool(Settings.get_instance().allow_user_override.value)
        except Exception:
            return True

    def _discovery_roots(self):
        yield "system", self._SYSTEM_PLUGIN_DIR
        if not self._safe_mode:
            yield "claude", self._CLAUDE_USER_SKILLS_DIR
            yield "claude", self._CLAUDE_PLUGIN_CACHE_DIR
            if self._app_data_dir:
                yield "user", self._app_data_dir / self._USER_PLUGIN_DIR_NAME

    def _scan_selected_plugins(self):
        groups: dict[str, dict[str, PluginInfo]] = {"system": {}, "claude": {}, "user": {}}
        for kind, root in self._discovery_roots():
            groups[kind].update((p.name, p) for p in self._scan_plugins(root, kind))
        self._discovery_candidates = groups
        return self._merge_discovered(groups["system"], groups["claude"], groups["user"], self._allow_user_override())

    def _activate_discovery(self, plugin: PluginInfo):
        if not plugin.load_blocked:
            ensure_deps_on_path(plugin.path)
        self._register_config_schema(plugin.name, plugin.manifest, self._manifest_file_for(plugin.path))

    def _merge_discovered(
        self,
        current_system: Dict[str, PluginInfo],
        current_claude: Dict[str, PluginInfo],
        current_user: Dict[str, PluginInfo],
        allow_user_override: bool,
    ) -> tuple:
        """按优先级合并三路发现结果：系统 → Claude → 用户（最高）。

        [T26] 从 rescan 抽出的公共合并语义：rescan 与发现缓存命中路径共用，
        保证覆盖关系单源（overridden_by 标记 + changed 收集 + _plugins 顺手写回）。

        Args:
            allow_user_override: False 时用户目录同名插件跳过，保留现有版本

        Returns:
            (new_plugins, changed)：合并后映射 + 因覆盖而变化的插件列表
        """
        new_plugins: Dict[str, PluginInfo] = {}
        changed: List[PluginInfo] = []
        # 先加系统插件
        for name, p in current_system.items():
            p.overridden_by = ""
            new_plugins[name] = p
        # Claude 插件同名覆盖系统（同名覆盖显性化：warning + overridden_by 标记）
        for name, p in current_claude.items():
            p.overridden_by = ""
            if name in new_plugins:
                overridden = new_plugins[name]
                logger.warning(
                    f"[PluginManager] Rescan: '{name}' 被 Claude 插件覆盖 "
                    f"({overridden.plugin_type}: {overridden.path} → claude: {p.path})"
                )
                p.overridden_by = overridden.plugin_type
                changed.append(p)
            new_plugins[name] = p
        # 用户插件同名覆盖前两者（最高优先级）
        for name, p in current_user.items():
            p.overridden_by = ""
            if name in new_plugins:
                if not allow_user_override:
                    logger.warning(
                        f"[PluginManager] Rescan: 用户插件 '{name}' 因 allow_user_override=false 跳过，"
                        f"保留现有版本: {new_plugins[name].path}"
                    )
                    continue
                overridden = new_plugins[name]
                logger.warning(
                    f"[PluginManager] Rescan: '{name}' 被用户插件覆盖 "
                    f"({overridden.plugin_type}: {overridden.path} → user: {p.path})"
                )
                p.overridden_by = overridden.plugin_type
                changed.append(p)
            new_plugins[name] = p
        return new_plugins, changed

    def _apply_deps_conflict_gate(self, plugins: Dict[str, PluginInfo]) -> None:
        """L2：跨插件依赖冲突门禁（合并后统一判定，让出方不加载任何组件）

        冲突判定需要全局视图：deps 注入是逐插件在扫描阶段完成的，而"两个插件
        要了不可调和的版本"要等全部扫完才知道。此处执行时尚未 import 任何插件
        组件，sys.modules 里没有第三方包，因此撤销注入是安全的（不会把已加载
        模块扣在半路）。

        让出方的 deps 路径从 sys.path 摘除并把原因写进 load_blocked，三个组件
        loader（tool/provider/runtime）与 UI 挂载会一并跳过它，避免它静默跑在
        对方版本的包上。

        Args:
            plugins: 合并后的完整插件集合（rescan 传 new_plugins，缓存路径传重建结果）
        """
        from app.plugins.deps_loader import (
            find_deps_conflicts,
            remove_deps_from_path,
            resolve_pip_deps,
        )

        # 先清旧判定：热重载/卸载后冲突可能已消失，不清会永久拦截
        live_map = getattr(self, "_plugins", None) or {}
        for p in plugins.values():
            p.deps_conflict_reason = ""
            live = live_map.get(p.name)
            # rescan 只把新增插件写回 _plugins，存量插件仍是旧实例：
            # 不镜像的话，热重载路径上被裁为让出方的存量插件 load_blocked 不生效
            if live is not None and live is not p:
                live.deps_conflict_reason = ""

        declarations: Dict[str, List[str]] = {}
        for p in plugins.values():
            if _hard_gate_blocked(p):
                continue
            specs = resolve_pip_deps(p.manifest)
            if specs:
                declarations[p.name] = specs
        if len(declarations) < 2:
            return

        losers: Dict[str, List[str]] = {}
        for pkg, a, b in find_deps_conflicts(declarations):
            keep, drop = _pick_deps_winner(plugins, a, b)
            losers.setdefault(drop, []).append(f"{pkg}（与 {keep} 要求不可调和）")
        for name, reasons in losers.items():
            p = plugins.get(name)
            if p is None:
                continue
            p.deps_conflict_reason = "；".join(reasons)
            live = live_map.get(name)
            if live is not None and live is not p:
                live.deps_conflict_reason = p.deps_conflict_reason
            removed = remove_deps_from_path(p.path)
            logger.warning(
                f"[PluginManager] {name} 因依赖冲突被拦截（组件不加载）: {p.deps_conflict_reason}"
                + (f"；已摘除 deps 注入 {removed}" if removed else "")
            )

    def rescan(self, force: bool = False) -> dict:
        """运行时重新扫描插件目录，检测新增/移除的插件

        仅扫描目录级别变化（新增/删除插件目录），不追踪插件内部文件变更。

        Args:
            force: True 时忽略目录签名缓存，强制全量重扫（显式「重载插件」场景）

        Returns:
            {"added": [PluginInfo], "removed": [PluginInfo], "changed": [PluginInfo]}
            - added: 新发现的插件列表
            - removed: 不再存在的插件列表（已从 _plugins 中移除）
            - changed: 因用户插件覆盖系统插件而变化的所有插件
        """
        if not self._initialized:
            logger.warning("[PluginManager] PluginManager not initialized, cannot rescan")
            return {"added": [], "removed": [], "changed": []}

        # 目录签名短路：目录层面无变化时直接返回空 diff（与全量重扫结果等价，
        # 但省掉三次 _scan_plugins 的全目录遍历与 manifest 解析）。
        # 用弱签名（不含目录 mtime）：内部文件变更不改变 added/removed/changed
        # 结果（diff 只比 manifest），避免解压覆盖类触碰打穿短路空转全扫。
        sig = self._plugins_dir_signature(include_mtime=False)
        if not force and self._plugins and sig == getattr(self, "_last_scan_signature", None):
            logger.debug("[PluginManager] 插件目录签名未变，rescan 短路（跳过全量重扫）")
            return {"added": [], "removed": [], "changed": []}
        self._last_scan_signature = sig

        result: Dict[str, list] = {"added": [], "removed": [], "changed": []}
        old_names = set(self._plugins.keys())
        old_plugins = self._plugins.copy()
        new_plugins, _ = self._scan_selected_plugins()
        changed_list = [
            p for name, p in new_plugins.items()
            if name in old_plugins and (p.path != old_plugins[name].path or p.manifest != old_plugins[name].manifest)
        ]
        # L2：全量扫完后裁决跨插件依赖冲突（注入已在扫描阶段完成，此处撤销让出方）
        self._apply_deps_conflict_gate(new_plugins)
        result["changed"] = changed_list

        new_names = set(new_plugins.keys())

        # 4. 检测新增和移除
        added_names = new_names - old_names
        removed_names = old_names - new_names

        for plugin in new_plugins.values():
            self._plugins[plugin.name] = plugin
            self._activate_discovery(plugin)

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
        # S7：diff 非空时失效能力池声明缓存（S1 去 stat 后的 manifest 变更感知
        # 通道之一；热重载主通道在 plugin_host_service.reload_plugin_subsystems）
        if added_names or removed_names or changed_list:
            changed_names = {p.name for p in changed_list}
            for _name in set(added_names) | set(removed_names) | changed_names:
                self.invalidate_capability_declared(_name)
        # [T26] 全量重扫成功 → 回写发现缓存（短路路径不回写，缓存仍有效）
        self._save_discovery_cache()
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
        selected = self._discover_and_register(name)
        if old is not None and selected is None:
            self._unload_plugin_ui(name)
            self._unregister_config_schema(name)
            del self._plugins[name]
        self.invalidate_mcp_cache()
        self._invalidate_discovery_cache()

    def _discover_and_register(self, name: str) -> Optional[PluginInfo]:
        """对目标名称的各来源复用全量扫描的覆盖规则。"""
        if not _normalize_plugin_name(name, ""):
            return None
        groups: dict[str, dict[str, PluginInfo]] = {"system": {}, "claude": {}, "user": {}}
        old = self._plugins.get(name)
        known = getattr(self, "_discovery_candidates", {})
        paths = {}
        for kind, root in self._discovery_roots():
            previous = known.get(kind, {}).get(name)
            if previous and previous.path.parent == root:
                paths[previous.path] = kind
            paths[root / name] = kind
        if old and old.plugin_type in groups and (not self._safe_mode or old.plugin_type == "system"):
            paths.setdefault(old.path, old.plugin_type)
        for path, kind in paths.items():
            if path.is_dir():
                plugin = self._scan_one_plugin_dir(path, kind, activate=False)
                if plugin is not None and plugin.name == name:
                    groups[kind][name] = plugin
        selected, _ = self._merge_discovered(
            groups["system"], groups["claude"], groups["user"], self._allow_user_override()
        )
        plugin = selected.get(name)
        if known:
            for kind, group in groups.items():
                known[kind].pop(name, None)
                known[kind].update(group)
        if old is not None and (plugin is None or old.path != plugin.path):
            from app.plugins.deps_loader import remove_deps_from_path

            remove_deps_from_path(old.path)
        if plugin is not None:
            from app.plugins.deps_loader import resolve_pip_deps

            self._plugins[name] = plugin
            old_specs = resolve_pip_deps(old.manifest) if old else []
            new_specs = resolve_pip_deps(plugin.manifest)
            if (old_specs or new_specs) and (
                old_specs != new_specs or old is None or old.path != plugin.path
                or _hard_gate_blocked(old) != _hard_gate_blocked(plugin) or old.version != plugin.version
            ):
                self._apply_deps_conflict_gate(self._plugins)
            elif old is not None:
                plugin.deps_conflict_reason = old.deps_conflict_reason
            self._activate_discovery(plugin)
            if old is None:
                self._restore_enabled_from_settings()
        return plugin

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

    def enable_plugin(self, name: str, load_components: bool = True):
        """启用插件（配置持久化，调用方需触发各子系统 reload）

        Args:
            load_components: True（默认）= 联动加载全部组件并触发 plugin_changed
                hook（完整语义，user-custom 自动创建等路径依赖）；False = 只写
                配置，组件加载与 hook 由主线程补载链完成（plugin-marketplace
                cards._on_manage_done）——worker 线程完整加载与主线程补载曾形成
                双通道（UI 组件被完整 exec 两轮），False 口径消除之。
        """
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
            # S7：启用后组件重载，该插件声明可见性变化，能力池声明缓存同步失效
            self.invalidate_capability_declared(name)
            logger.info(f"[PluginManager] Enabled plugin: {name}")
            state_changed = True
        # 联动加载全部组件（与禁用路径对称）：只补 UI + 工具会让 providers /
        # model_adapters / lsp / hooks 在「禁用→启用」后不再生效，直到重启。
        # 🔴 历史 bug 防回归：严禁只补 UI 单组件，必须全组件口径。
        # [双通道拆除] load_components=False：组件加载由主线程补载链完成。
        if load_components:
            self._load_plugin_components(name)
            # 状态实际变化才触发（幂等跳过重复启用）；置于末尾保证 diff 拿到注册后状态
            if state_changed:
                self._trigger_plugin_changed_hook("enabled", name)

    def disable_plugin(self, name: str, load_components: bool = True):
        """禁用插件（配置持久化，调用方需触发各子系统 reload）

        系统插件保护改为名单制：仅 _NON_DISABLEABLE（system / plugin-marketplace）
        拒绝禁用——禁用它们会断核心链路（组件宿主/插件市场自身）且用户无法恢复。
        其余内置插件（包括 manifest type=system 的 shortcut-manager、agent_trace、
        assistant_hub、welcome_changelog 等）允许通过 Settings.disabled_plugins
        禁用，与 installer 的 _set_managed_enabled 判定对齐。

        判定依据是插件名名单而非 manifest type / PluginInfo.is_system（plugin_type）：
        项目根 plugins/ 下所有插件在扫描时 plugin_type 均为 "system"（目录位置
        判定），用 is_system 会把内置插件全部拒绝；type 字段回归纯元数据语义。

        Args:
            load_components: 语义同 enable_plugin（False = 只写配置，组件卸载与
                hook 由主线程补载链完成，消除 worker/主线程双通道）。
        """
        p = self._plugins.get(name)
        if p is None:
            logger.warning(f"[PluginManager] Plugin not found: {name}")
            return
        if name in self._NON_DISABLEABLE:
            logger.warning(f"[PluginManager] 拒绝禁用核心插件: {name}")
            return
        # 回收该插件通过 process.start 启动的后台进程（防禁用后残留孤儿进程）
        try:
            from app.plugins.capabilities.process_registry import ProcessRegistry

            ProcessRegistry.get_instance().kill_plugin(name)
        except Exception as e:
            logger.warning(f"[PluginManager] 回收插件后台进程失败: {e}")
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
            # S7：禁用后组件卸载，该插件声明可见性变化，能力池声明缓存同步失效
            self.invalidate_capability_declared(name)
            logger.info(f"[PluginManager] Disabled plugin: {name}")
            state_changed = True
        # 联动卸载全部组件（不只是 UI/tools）：禁用语义 = 该插件的一切都不再生效。
        # 历史实现只清 UI + tools + 贡献项，providers / model_adapters / storages /
        # serializers / gateways / lsp / hooks / agents / commands 全部留在注册表里
        # 继续生效——禁用后仍会被调用，且这类残留没有自愈轮询，只能重启进程。
        # 🔴 历史 bug 防回归：严禁只清 UI 单组件，必须全组件口径。
        # [双通道拆除] load_components=False：组件卸载由主线程补载链完成。
        if load_components:
            self._unload_plugin_components(name)
            # 状态实际变化才触发（幂等跳过重复禁用）；置于末尾保证 diff 拿到注销后状态
            if state_changed:
                self._trigger_plugin_changed_hook("disabled", name)

    def _load_plugin_components(self, name: str) -> None:
        """启用路径：按 manifest 全组件加载（与 _unload_plugin_components 对称）。"""

        def _run() -> None:
            from app.core.services.plugin_host_service import PluginHostService

            PluginHostService.get_instance()._reload_single_plugin(name, "__manifest__")

        try:
            import threading

            from app.core.services.plugin_host_service import run_on_main_thread

            # 同禁用路径：加载会碰 Qt（UI 注册 / 主题重载 / 命令快捷键重建）。
            if threading.current_thread() is threading.main_thread():
                _run()
                return
            if run_on_main_thread(_run):
                return
            logger.warning(f"[PluginManager] 启用插件组件加载未能投递主线程 ({name})，回退 UI+工具加载")
        except Exception as e:
            logger.warning(f"[PluginManager] 启用插件时全组件加载失败，回退 UI+工具加载 ({name}): {e}")
        # 兜底：保持历史加载面
        self._load_plugin_ui(name)
        self._rescan_plugin_tools(name, enabled=True)

    def _unload_plugin_components(self, name: str) -> None:
        """禁用路径：按「插件已删除」语义卸载该插件全部组件 + 宿主级资源（幂等）。

        复用 PluginHostService 的删除段（kernel reloader 分派），保证「禁用」与
        「卸载」两条路径的清理面完全一致，不再各自维护一份子集。
        """
        plugin = self._plugins.get(name)
        components = dict(plugin.components) if plugin is not None and isinstance(plugin.components, dict) else {}
        manifest = (
            dict(plugin.manifest)
            if plugin is not None and isinstance(getattr(plugin, "manifest", None), dict)
            else None
        )

        def _run() -> None:
            from app.core.services.plugin_host_service import PluginHostService
            from app.plugins.kernel import KNOWN_COMPONENTS

            result = {k: (0 if k == "agents" else False) for k in KNOWN_COMPONENTS}
            PluginHostService.get_instance()._cleanup_removed_plugin_components(
                name, components, result, tuple(KNOWN_COMPONENTS), manifest=manifest
            )

        try:
            import threading

            from app.core.services.plugin_host_service import run_on_main_thread

            # 组件卸载会碰 Qt：主题全量重载、命令快捷键重建、UI 实例销毁。
            # 插件市场的启停在后台 worker 线程调用本方法（_set_managed_enabled），
            # 直接执行就是跨线程操作 QShortcut/QWidget → 0xC0000005。
            if threading.current_thread() is threading.main_thread():
                _run()
                return
            if run_on_main_thread(_run):
                return
            logger.warning(
                f"[PluginManager] 禁用插件组件卸载未能投递主线程 ({name})，回退 UI+工具清理"
            )
        except Exception as e:
            logger.warning(f"[PluginManager] 禁用插件时全组件卸载失败，回退 UI+工具清理 ({name}): {e}")
        # 兜底：至少保持历史清理面（UI + 工具），不让禁用整体失败
        self._unload_plugin_ui(name)
        self._rescan_plugin_tools(name, enabled=False)

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
        UIPluginRegistry.get_instance().ensure_plugin_loaded(name, plugin.path)

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
        if base_dir.is_dir():
            for directory in base_dir.iterdir():
                if directory.name.startswith((".", "_")):
                    continue
                plugin = self._scan_one_plugin_dir(directory, plugin_type, activate=False)
                if plugin is not None:
                    discovered.append(plugin)
        return discovered

    def _scan_one_plugin_dir(self, plugin_dir: Path, plugin_type: str, *, activate: bool = True) -> Optional[PluginInfo]:
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
            parsed = _load_manifest_file(manifest_path)
            if parsed is None:
                return None
            manifest, schema_warnings = parsed
            for _w in schema_warnings:
                logger.warning(f"[PluginManager] {_w}")
            plugin_name = _normalize_plugin_name(manifest.get("name"), plugin_dir.name)
            if plugin_name is None:
                return None

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

            # —— 平台兼容检查（G3：门禁全部通过前不注入 deps）——
            compatible, reason = check_platform(manifest)
            if not compatible:
                logger.warning(f"[PluginManager] {plugin_name} 平台不兼容: {reason}")

            # —— P1 版本契约：min_host_version 与宿主版本比对 ——
            from app.plugins.version_gate import check_api_version, check_host_version

            ver_ok, ver_reason = check_host_version(manifest, plugin_name)
            api_ok, api_reason = check_api_version(manifest, plugin_name)

            info = PluginInfo(
                name=plugin_name,
                manifest=manifest,
                path=plugin_dir,
                plugin_type=plugin_type,
                platform_compatible=compatible,
                version_compatible=ver_ok,
                version_reason=ver_reason,
                api_compatible=api_ok,
                api_reason=api_reason,
                manifest_warnings=schema_warnings,
            )
            if activate:
                self._activate_discovery(info)
            return info
        except Exception as e:
            logger.error(f"[PluginManager] Failed to rescan plugin at {plugin_dir}: {e}")
            return None

    @staticmethod
    def _manifest_file_for(plugin_dir: Path) -> Optional[Path]:
        """定位插件目录下的 manifest 文件（两路：.drifox-plugin / .claude-plugin）。

        [T25] 为 config_schema 缓存提供 mtime/size 寻址材料。与 _scan_one_plugin_dir
        （:1453-1458）的探测顺序保持一致，避免两处行为分叉。
        """
        for candidate in (
            plugin_dir / ".drifox-plugin" / "plugin.json",
            plugin_dir / ".claude-plugin" / "plugin.json",
        ):
            if candidate.exists():
                return candidate
        return None

    def _config_schema_cache_key(
        self, plugin_name: str, manifest_path: Optional[Path]
    ) -> Optional[tuple]:
        """config_schema 缓存 key；manifest_path 为空时返回 None（调用方按 miss 处理）。

        取不到路径时不猜、不用 name 兜底：同一插件名在不同根（system/user/claude）
        可能对应不同目录，用 name 做 key 会跨根串味。宁可不缓存也不缓存错。
        """
        if manifest_path is None:
            return None
        try:
            st = manifest_path.stat()
        except OSError:
            return None
        return (plugin_name, manifest_path, st.st_mtime_ns, st.st_size)

    def _register_config_schema(
        self, plugin_name: str, manifest: dict, manifest_path: Optional[Path] = None
    ) -> None:
        """E1：解析 plugin.json config_schema 并注册到 PluginConfigRegistry + 自动设置卡。

        幂等：同名插件重复注册由 PluginConfigRegistry 与 UIPluginRegistry 内部
        覆盖（PluginManager 全量扫描天然幂等；热重载 rescan 同样无害）。
        任一异常均降级为 warning，不影响插件本体的扫描加载。
        """
        # 解析阶段：非法 schema 走 parse_config_schema 内部 warning 兜底。
        # [T25] 结果只依赖 manifest 内容，按 manifest mtime+size 缓存解析结果：
        # 命中则跳过 parse（72 插件 × 每次扫描/热重载都重跑一遍）；miss 才解析并回写。
        # ⚠️ 只缓存 parse 结果，注册动作（PluginConfigRegistry.register /
        # _pending_config_cards 登记）仍每次执行——注册表语义不变。
        cache_key = self._config_schema_cache_key(plugin_name, manifest_path)
        if cache_key is not None and cache_key in _CONFIG_SCHEMA_CACHE:
            config_schema = _CONFIG_SCHEMA_CACHE[cache_key]
        else:
            from app.plugins.contracts.plugin_config import parse_config_schema

            raw_schema = manifest.get("config_schema")
            config_schema = parse_config_schema(plugin_name, raw_schema)
            if cache_key is not None:
                _CONFIG_SCHEMA_CACHE[cache_key] = config_schema
        if config_schema is None:
            # manifest 已无合法 schema：显式清掉历史注册（含自动设置卡），避免残留旧卡。
            # unload_plugin 已改为保留 auto_config_card，清理只能走这里（manifest 层职责）。
            self._unregister_config_schema(plugin_name)
            return

        # 注册表（必需）
        try:
            from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

            PluginConfigRegistry.get_instance().register(config_schema)
        except Exception as e:
            logger.warning(f"[PluginManager] config_schema 注册失败({plugin_name}): {e}")
            return

        # 自动设置卡：扫描期只登记，不 import UI 链（见 __init__ 的 _pending_config_cards
        # 注释）。真正的 UIPluginRegistry.register_settings_card 由 replay_config_cards()
        # 在 main_widget._after_plugins_loaded 统一回放（UI 栈已就绪、插件卡分区构建前）。
        # getattr 兜底：测试常用 PluginManager.__new__ 绕过 __init__（本方法不依赖实例状态）
        pending = getattr(self, "_pending_config_cards", None)
        if pending is None:
            pending = self._pending_config_cards = {}
        pending[plugin_name] = config_schema

    def replay_config_cards(self) -> int:
        """回放待注册的自动设置卡（UI 栈就绪后调用，幂等），返回成功注册数。

        与 ``_register_config_schema`` 的分工：扫描期只登记 schema（零 UI import），
        本方法才真正 import 卡类并注册。两步分离把 Qt/qfluentwidgets 级联从
        ``PluginManager.initialize`` 同步段摘出去（实测 ~166ms）。

        幂等：重复调用对同名 card_id 是覆盖注册（UIPluginRegistry 内部语义），
        但成功后即从 pending 摘除，正常情况下二次调用直接返回 0。
        """
        pending = getattr(self, "_pending_config_cards", None)
        if not pending:
            return 0
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
            from app.widgets.cards.settings.plugin_config_card import make_card_class
        except ImportError as e:
            # UI 栈不可用（headless/纯后端进程）：保留 pending，等真正需要时再回放
            logger.debug(f"[PluginManager] 设置卡回放跳过（UI 栈不可用）: {e}")
            return 0

        registry = UIPluginRegistry.get_instance()
        count = 0
        for plugin_name, config_schema in list(pending.items()):
            try:
                registry.register_settings_card(
                    plugin_name,
                    f"{plugin_name}-config",
                    config_schema.title,
                    make_card_class(plugin_name),
                    metadata={"auto_config_card": True},
                )
                count += 1
            except Exception as e:
                logger.warning(f"[PluginManager] config_schema 设置卡注册失败({plugin_name}): {e}")
                continue
            # 仅在注册成功后摘除：失败保留 pending，下次 replay 重试
            pending.pop(plugin_name, None)
        if count:
            logger.debug(f"[PluginManager] 设置卡回放 {count} 张")
        return count

    def _unregister_config_schema(self, plugin_name: str) -> None:
        """E1：插件移除/Schema 删除时清理 config_schema 注册 + 自动设置卡。

        设置卡的清理在此显式触发（manifest 层职责）。UIPluginRegistry.unload_plugin
        已改为保留 auto_config_card——ui 组件热重载（unload→load）不能误杀
        rescan 刚注册的卡，清理收敛到本函数单一入口。
        """
        # [T25] 清掉本插件所有 config_schema 解析缓存条目（key 含 mtime，可能多条）。
        # 放在本函数而非各移除点：_unregister_config_schema 是「插件移除 / schema 删除」
        # 两条路径的公共下沉点（rescan removed、manifest gone、schema 置空）。
        if _CONFIG_SCHEMA_CACHE:
            for _k in [k for k in _CONFIG_SCHEMA_CACHE if k[0] == plugin_name]:
                _CONFIG_SCHEMA_CACHE.pop(_k, None)
        # 待回放队列必须先摘：否则「注册 → 卸载 → replay」会把已删插件的卡复活
        # （僵尸卡，扫描期失败后 pending 残留同样会这样）
        pending = getattr(self, "_pending_config_cards", None)
        if pending is not None:
            pending.pop(plugin_name, None)
        try:
            from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

            PluginConfigRegistry.get_instance().unregister_plugin(plugin_name)
        except Exception as e:
            logger.warning(f"[PluginManager] config_schema 清理失败({plugin_name}): {e}")
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().unregister_auto_config_cards(plugin_name)
        except Exception as e:
            logger.warning(f"[PluginManager] config_schema 设置卡清理失败({plugin_name}): {e}")

    def _discover_system_plugins(self):
        """扫描系统插件目录 app/plugins/"""
        plugins = self._scan_plugins(self._SYSTEM_PLUGIN_DIR, "system")
        for p in plugins:
            self._plugins[p.name] = p
            self._activate_discovery(p)

    def _discover_user_plugins(self, app_data_dir: Path):
        """扫描用户插件目录 ~/.drifox/plugins/（安全模式下跳过）"""
        if self._safe_mode:
            return
        user_plugin_dir = app_data_dir / self._USER_PLUGIN_DIR_NAME
        for plugin in self._scan_plugins(user_plugin_dir, "user"):
            previous = self._plugins.get(plugin.name)
            if previous is not None and previous.plugin_type != "user" and not self._allow_user_override():
                continue
            if previous is not None:
                plugin.overridden_by = previous.overridden_by if previous.plugin_type == "user" else previous.plugin_type
            self._plugins[plugin.name] = plugin
            self._activate_discovery(plugin)

    def _discover_claude_plugins(self):
        """扫描 Claude Code 插件目录

        同时兼容两种路径：
        - ~/.claude/skills/       （个人 skills-directory 插件）
        - ~/.claude/plugins/cache/（从市场安装的缓存插件）

        优先级介于系统插件和 DriFox 用户插件之间，即：
        系统 → Claude → DriFox 用户（最高）

        安全模式下跳过（Claude 插件也是用户安装的外部插件）。
        """
        if self._safe_mode:
            return
        for base_dir in (self._CLAUDE_USER_SKILLS_DIR, self._CLAUDE_PLUGIN_CACHE_DIR):
            plugins = self._scan_plugins(base_dir, "claude")
            for p in plugins:
                if p.name in self._plugins:
                    existing = self._plugins[p.name]
                    if existing.is_system:
                        logger.info(f"[PluginManager] Claude plugin '{p.name}' overrides system plugin")
                self._plugins[p.name] = p
                self._activate_discovery(p)

    # ============================================================
    # 插件查询
    # ============================================================

    def invalidate_capability_declared(self, name: str) -> None:
        """S7：启停/卸载路径的能力池声明缓存失效（委托 CapabilityRegistry）。

        禁用后组件卸载、启用后重载，该插件的声明可见性变化，声明缓存同步失效。
        """
        try:
            from app.plugins.capabilities.registry import CapabilityRegistry

            CapabilityRegistry.get_instance().invalidate_declared(name)
        except Exception as e:
            logger.warning(f"[PluginManager] 能力池声明缓存失效失败 ({name}): {e}")

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
        # 安全模式只加载系统插件，user-custom 也是用户侧内容
        if not self._safe_mode:
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

    def get_command_files_for_plugin(self, plugin_name: str) -> List[Path]:
        """只获取指定插件的命令文件（精准重载用，不走全量遍历）。

        与 get_command_files 的区别：不遍历全部已启用插件、不做跨插件同名去重
        —— 同名去重需要全局视野，而精准重载的场景是「只重新注册这一个插件的
        命令」，跨插件优先级由调用方（CommandManager.unregister_source + 重注册）
        按注册顺序自然维持。

        ``plugin_name == "user-custom"`` 时返回用户自定义命令目录（与
        get_command_files 的特殊处理对齐，该目录无 manifest 也在扫）。
        """
        result: List[Path] = []

        if plugin_name == "user-custom":
            if self._safe_mode:
                return result
            from app.utils.utils import get_app_data_dir

            cmd_dir = get_app_data_dir() / "plugins" / "user-custom" / "commands"
            if cmd_dir.exists():
                result.extend(sorted(cmd_dir.glob("*.md")))
            return result

        plugin = self._plugins.get(plugin_name)
        if plugin is None or not plugin.has_component("commands"):
            return result
        cmd_dir = plugin.path / "commands"
        if cmd_dir.exists():
            result.extend(sorted(cmd_dir.glob("*.md")))
        return result

    def get_agent_files(self) -> List[Path]:
        """获取所有插件的智能体文件"""
        return self._get_md_files("agents")


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
                        # source：启动安全门禁（mcp_lsp_safety）靠它判定内置源。
                        # 不传会导致系统插件自带的 LSP 也被要求确认，而 LSP 侧无确认入口
                        # → server 永远无法启动（EU-G22）
                        configs.append(
                            {"plugin": plugin.name, "source": str(lsp_file), "config": json.load(f)}
                        )
                except Exception as e:
                    logger.warning(f"[PluginManager] 解析 {lsp_file} 失败: {e}")

        # 2. 额外扫描：用户插件目录下没有 manifest 但有 .lsp.json 的目录
        # 安全模式只加载系统插件，这里也一并跳过
        if self._app_data_dir and not self._safe_mode:
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
                            configs.append(
                                {"plugin": item.name, "source": str(lsp_file), "config": json.load(f)}
                            )
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
                # 热重载路径同样需要 source（否则重载后 server 又启不来）
                return {"plugin": plugin_name, "source": str(lsp_file), "config": json.load(f)}
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
        # 安全模式只加载系统插件，跳过用户自定义内容
        if self._safe_mode:
            return files
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
                content = json.loads(mcp_file.read_text(encoding="utf-8-sig"))

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
            # OAuth 等认证声明。缺省空 dict，非 OAuth 服务器行为不变。
            # 仅 http/sse 型有意义；stdio 型忽略此字段。
            "auth": cfg.get("auth", {}),
            # API Key 凭据声明（label/getUrl/inject）。连接层据此把用户
            # 在设置界面填的 Key 注入 url 或 headers。缺省空 dict。
            "credential": cfg.get("credential", {}),
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
        # auth 不含路径占位符，但需持久化，否则用户从 UI 保存会丢 OAuth 声明
        # credential 同理：丢了就没法在设置界面填/读 API Key
        _MCP_PERSIST_FIELDS = ("command", "args", "env", "url", "headers", "auth", "credential")
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
            # P1-6：分发目录只读守卫（来源插件在 _internal 下时拒写）
            _assert_writable_plugin_target(source_path)
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
            from app.core.hooks.hook_manager import trigger_plugin_changed_hook

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

        只有 user-custom（界面添加的）才真删。插件自带的配置一律只禁用：
        删掉后插件仍在、配置消失，且同一 .mcp.json 常被多个 server 共用，
        删一个会连累其他；插件更新时被删的条目还会重新出现，徒增困惑。
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

        # P1-6：分发目录只读守卫（来源插件在 _internal 下时拒写；用户插件不受影响）
        _assert_writable_plugin_target(source)

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

            if source.parent.name == "user-custom":
                # 界面添加的：真删
                if name in mcp_servers:
                    del mcp_servers[name]
                    if is_claude_format and not mcp_servers:
                        # .claude-plugin 格式：删除后为空，写回空对象
                        source.write_text("{}", encoding="utf-8")
                    else:
                        source.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[PluginManager] Removed user MCP server '{name}'")
            else:
                # 插件自带（system / 市场插件 / Claude 插件）：只禁用，不删文件条目。
                # 同插件多 server 共用一份 .mcp.json，删单个会误伤其他条目。
                if name in mcp_servers:
                    mcp_servers[name]["enabled"] = False
                    source.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
                    logger.info(f"[PluginManager] Disabled plugin-owned MCP server '{name}'")

            self.invalidate_mcp_cache()
        except Exception as e:
            logger.error(f"[PluginManager] Failed to remove MCP server '{name}': {e}")
        else:
            # 系统插件禁用 / 用户插件删除，对活跃列表均为移除
            self._trigger_plugin_changed_hook("mcp_removed", name)
