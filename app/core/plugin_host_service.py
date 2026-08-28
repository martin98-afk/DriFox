# -*- coding: utf-8 -*-
"""
PluginHostService — 应用级插件宿主服务（一个应用一个实例）

历史问题：插件系统寄生在 ChatBackend（每 tab 一个）——watcher 线程用类级
标志/引用计数伪装单例、闭包持有首个 backend、广播需遍历活跃 backend 打补丁
（T3/P1/P5e 系列修复皆源于此）。

修复：插件基础设施整体上移本服务（QObject 应用级单例，TabManagerWindow 持有）：
- PluginManager 初始化（系统+用户插件发现、AgentManager 重载）
- watchfiles 文件监听（watcher 线程、变更识别、去重、抑制窗口合并）
- 热重载调度（debounce / 精准重载 / 新插件注册 / 卸载清理）
- MCP 自动发现 + 连接
- PluginChanged hook 触发 + UI 广播（plugin_changed 信号，各窗口直连）

生命周期由 TabManagerWindow 驱动：__init__ → ensure_started()；cleanup() → stop()
"""

import os
import time
from typing import Any, Dict, Optional

from loguru import logger
from PySide6.QtCore import QObject, QTimer, Signal


class PluginHostService(QObject):
    """插件宿主服务（应用级单例，主线程创建）"""

    _instance: Optional["PluginHostService"] = None

    # UI 广播：各窗口（OpenAIChatToolWindow）直连本信号刷新插件相关视图
    plugin_changed = Signal(dict)
    # watcher 线程 → 主线程（内部投递）
    _hot_reload_requested = Signal(str, str)  # (插件名, 组件), ""=全量/空组件=全部组件

    _NEW_PLUGIN_SENTINEL = "__NEW__"

    @classmethod
    def get_instance(cls) -> "PluginHostService":
        if cls._instance is None:
            svc = cls()
            cls._instance = svc
        return cls._instance

    def __init__(self, parent=None):
        if PluginHostService._instance is not None:
            raise RuntimeError("PluginHostService is singleton, use get_instance()")
        super().__init__(parent)

        # 全局组件（不绑任何窗口）
        from app.core.agent import AgentManager
        from app.core.hook_manager import HookManager

        # 插件 hooks 的注册目标。HookManager._hooks 类级共享（任一实例等效），
        # 注册到本专属实例后各 tab 均可触发；AgentManager 单例由此首次创建。
        self._host_hook_manager = HookManager()
        self._agent_manager = AgentManager.get_instance(None, self._host_hook_manager)

        self._hot_reload_requested.connect(self._on_hot_reload_requested)

        # 生命周期标志（原 backend 引用计数体系随寄生一起废除——服务与 app 同寿）
        logger.info("[PluginHost] 已创建（应用级单例）")

    def ensure_started(self) -> None:
        """幂等启动：PluginManager 扫描 + 插件工具 watcher + 主题/LSP/MCP 延迟链。

        原 ChatBackend._init_plugin_system 整体语义；TabManagerWindow.__init__
        先于任何 tab backend 创建，同步扫描结果由全局单例供所有窗口复用。
        """
        if getattr(self, "_started", False):
            return
        self._started = True
        self._init_plugin_system()

    def stop(self) -> None:
        """停止 watcher 线程（应用退出时由 TabManagerWindow.cleanup 调用）"""
        try:
            self._stop_plugin_watcher()
        except Exception as e:
            logger.warning(f"[PluginHost] stop failed: {e}")


    def _init_plugin_system(self):
        """初始化 PluginManager，加载所有插件

        [PERF] 拆分为关键路径和非关键路径：
        - 关键路径：PluginManager 扫描 + AgentManager 重载（必须同步，智能体/命令需要）
        - 非关键路径：主题刷新 + 热更新监听 + LSP 初始化 → 延迟到窗口就绪后执行
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager
            from app.utils.utils import get_app_data_dir

            pm = PluginManager.get_instance()
            app_data_dir = get_app_data_dir()

            # 记录初始化前的状态，用于判断是否为首次初始化
            was_initialized = pm.is_initialized()
            pm.initialize(app_data_dir)

            # 首次初始化时才需要全量重载智能体
            # 后续窗口复用已有的 PluginManager/AgentManager 单例数据
            if not was_initialized:
                # AgentManager 重新从已启用插件加载智能体（关键路径）
                if self._agent_manager:
                    self._agent_manager.reload_agents()

                # ── 非关键路径：延迟到窗口就绪后执行 ──
                # 主题刷新、插件热更新监听、LSP 初始化不需要阻塞首帧显示
                # 使用 QTimer 推迟执行（backend 本身不依赖 Qt，由调用方确保）
                self._defer_non_critical_plugin_init(pm)
            else:
                # ── 窗口重开场景：补启动插件热更新监听 ──
                # 应用托盘驻留（setQuitOnLastWindowClosed(False)），关闭全部窗口后进程
                # 仍存活，但 backend.cleanup() 已把 watcher 引用计数归零并停止监听线程
                # （_stop_plugin_watcher 会复位 _plugin_watcher_started=False）。
                # 而 PluginManager 单例仍保持已初始化状态，上方首次初始化分支被跳过，
                # 导致 watcher 永不重启 → 此后新增 agent/命令/技能文件均不触发热重载，
                # /命令列表看不到新增子智能体。
                # _start_plugin_watcher 内部有引用计数 + started 标志双重保护：
                # watcher 存活时调用只 +1 引用计数，不会重复创建线程。
                try:
                    self._start_plugin_watcher()
                except Exception as e:
                    logger.error(f"[PluginHost] 窗口重开重启插件监听失败: {e}")

            logger.info(
                f"[PluginHost] PluginManager 初始化完成，"
                f"已加载 {len(pm.list_plugins())} 个插件，"
                f"智能体 {len(self._agent_manager.list_agents())} 个"
            )

        except Exception as e:
            logger.error(f"[PluginHost] PluginManager 初始化失败: {e}")

    def _defer_non_critical_plugin_init(self, pm):
        """非关键插件初始化：主题/LSP/热更新，延迟执行不阻塞 UI"""
        # 使用 QTimer 延迟执行（backend 提供 _deferred_timer 供调用方关联到 Qt 事件循环）
        from PySide6.QtCore import QTimer

        def _do_deferred():
            # 内置组件 reloader 注册（幂等，进程一次 — chat_backend.py 顶层注册表
            # 可能在 ChatBackend 之前已被其他模块 import kernel 注册过，幂等保护）
            try:
                from app.plugins.builtin_reloaders import bind_runtime, register_builtin_reloaders
                from app.plugins.kernel import get_reloader_registry

                bind_runtime(self._agent_manager)
                register_builtin_reloaders(get_reloader_registry())
            except Exception as e:
                logger.error(f"[PluginHost] 内置 reloader 注册失败: {e}")

            # 刷新主题
            try:
                self._reload_themes_from_plugins()
            except Exception as e:
                logger.error(f"[PluginHost] 延迟主题刷新失败: {e}")

            # 启动插件文件变更监听（热更新，仅启动一次）
            try:
                self._start_plugin_watcher()
            except Exception as e:
                logger.error(f"[PluginHost] 延迟启动插件监听失败: {e}")

            # 插件工具按启用状态对齐重扫：工具加载发生在 import 期（早于 pm.initialize），
            # 彼时新插件尚未被 _restore_enabled_from_settings 补齐到 enabled 列表 →
            # 新装插件工具被过滤；pm.initialize 已在此前完成，重扫后
            # 新安装插件工具注册、被禁用插件工具注销，两边同时正确。
            try:
                from app.plugins.loaders.plugin_tool_loader import ensure_plugin_tool_watcher

                watcher = ensure_plugin_tool_watcher()
                if watcher is not None:
                    watcher.scan_now()
            except Exception as e:
                logger.error(f"[PluginHost] 插件工具启用状态对齐重扫失败: {e}")

            # 服务商插件（providers）：延迟初始化加载 + 热重载 watcher
            # （与工具插件并列；服务商核心数据在 UI 初始化前就绪）
            try:
                from app.plugins.loaders.provider_loader import ensure_provider_watcher
                from app.plugins.registries.provider_registry import ProviderRegistry

                ProviderRegistry.get_instance().ensure_loaded()
                pwatcher = ensure_provider_watcher()
                if pwatcher is not None:
                    pwatcher.scan_now()
            except Exception as e:
                logger.error(f"[PluginHost] 服务商插件初始化失败: {e}")

            # 运行时组件（model_adapters / loop_policies / storages）：
            # 内置实现先注册，插件目录可覆盖内置
            try:
                from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

                warmup_runtime_components()
            except Exception as e:
                logger.error(f"[PluginHost] 运行时组件 warmup 失败: {e}")

            # gateway 平台此时才注册进 registry（warmup 晚于 GatewayService
            # ensure_started 的 start_all_async → 启动期平台漏启）。注册完成后
            # 补一次同步：对"已注册 + 已启用 + 未连接"的平台补启连接（幂等）。
            # 修复：初始化时已启用的 gateway 插件必须手动关闭/打开才连接。
            try:
                from app.core.gateway_service import GatewayService

                GatewayService.get_instance().sync_platforms()
            except Exception as e:
                logger.error(f"[PluginHost] gateway 平台补启失败: {e}")

            # 初始化 LSP 管理器（仅首次，多窗口共享单例）
            try:
                from app.core.lsp.lsp_manager import get_lsp_manager

                lsp_mgr = get_lsp_manager()
                lsp_configs = pm.get_lsp_configs()
                workdir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                lsp_mgr.initialize(workdir, lsp_configs)
                logger.info(f"[PluginHost] LspManager 延迟初始化完成，已注册 {len(lsp_mgr._clients)} 个 LSP 服务器")
                lsp_mgr.start_all_background()
            except Exception as e:
                logger.error(f"[PluginHost] LSP 延迟初始化失败: {e}")

            # MCP 自动发现（仅首次）+ 建立连接（后台异步，不阻塞 UI）。
            # 修复：aa8f7a6b 重构把 _discover_mcp_servers/_init_mcp_connections 迁入
            # PluginHostService 时调用点丢失（原 ChatBackend 延迟创建尾部两段 try），
            # 启动后已启用的 MCP 服务器不会自动连接（须手动关闭+开启才启动）。
            # 与上方 gateway sync_platforms() 补启同构。
            try:
                self._discover_mcp_servers()
            except Exception as e:
                logger.error(f"[PluginHost] MCP 自动发现失败: {e}")
            try:
                self._init_mcp_connections()
            except Exception as e:
                logger.error(f"[PluginHost] MCP 连接初始化失败: {e}")

        # 延迟 2 秒执行，让窗口首帧 + 用户交互先就绪
        QTimer.singleShot(2000, _do_deferred)

    def _reload_themes_from_plugins(self):
        """插件系统初始化后，重新加载插件主题"""
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.reload()
            # 同时更新 Settings 中的主题选项
            from app.utils.config import update_theme_options

            update_theme_options()

            # 安全网：插件主题全部加载后，确保配置中保存的主题被正确恢复。
            # 读取配置文件中的保存值，与当前值比对。若被重置（启动过程中因列表不含
            # 插件主题而被回退到系统主题），在此修正。即使当前值恰好是列表中的某个
            # 系统主题，只要与保存值不同就恢复，保证用户选择不受中间状态影响。
            from app.utils.config import Settings

            settings = Settings.get_instance()
            themes = theme_manager.list_themes()
            current = settings.ui_theme_style.value
            from app.utils.utils import get_app_data_dir
            import orjson as json

            config_file = get_app_data_dir() / "app.config"
            saved_theme = None
            if config_file.exists():
                try:
                    raw = config_file.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    saved_theme = data.get("UI", {}).get("ThemeStyle")
                except Exception:
                    pass

            if saved_theme and saved_theme in themes:
                if current != saved_theme:
                    settings.ui_theme_style.value = saved_theme
                    logger.info(f"[PluginHost] 从配置文件恢复保存的主题: {saved_theme} (当前: {current})")
            elif saved_theme and saved_theme not in themes:
                # 保存的主题不可用（如插件已卸载），当前值如果也不在列表中才回退
                if current not in themes and themes:
                    fallback = next(iter(themes))
                    settings.ui_theme_style.value = fallback
                    logger.info(
                        f"[PluginHost] 保存的主题 {saved_theme} 不可用，当前值 {current} 也不可用，回退至: {fallback}"
                    )

            logger.info(
                f"[PluginHost] 插件主题刷新完成，共 {len(themes)} 个主题, 当前主题: {settings.ui_theme_style.value}"
            )
        except Exception as e:
            logger.error(f"[PluginHost] 刷新插件主题失败: {e}")

    # ========== 插件热更新（watchfiles） ==========

    _plugin_watcher_started = False  # 类级别标志，确保全局只启动一次
    # 🚀 P5e：gitee 同步抑制窗口——config_sync 下载解压 user-custom 期间抑制
    # 本 watcher 热重载链（backend 独立 watchfiles 线程），避免与 config_sync
    # 应用链（Settings 写回 + 主题刷新）两条主线程重活链同时爆发叠加阻塞。
    # _suppress_watcher_until: 抑制截止时间戳（0=不抑制）；
    # _watcher_pending_reload: 抑制窗口内被跳过的 user-custom 变更标志，
    # 由 config_sync 下载完成后兜底合并触发一次 reload_plugin_subsystems。
    _suppress_watcher_until = 0.0
    _watcher_pending_reload = False
    # ★ 泄漏修复（P1）：watcher 闭包持有首个 backend 实例引用（self._hot_reload_requested /
    # self.plugin_changed / self._identify_* 全部走实例成员），窗口关闭不停止则实例永不可回收。
    # 用引用计数 + stop_event 实现"最后一个窗口关闭时停止 watcher"：
    #   - refcount 在 __init__（_start_plugin_watcher）递增、cleanup 递减
    #   - 归零时设置 stop_event → watch() 生成器退出 → 线程结束 → 闭包释放 → 实例可回收
    #   - 新窗口启动时 refcount 从 0 递增会重新启动 watcher（stop_event 复位），热更新不丢失
    _plugin_watcher_refcount = 0  # 活跃 backend 引用计数
    _plugin_watcher_stop = None  # threading.Event：设置后 watch() 生成器退出
    _plugin_watcher_thread = None  # 当前 watcher 线程（cleanup 归零时 join 确保退出）

    def _start_plugin_watcher(self):
        """启动 watchfiles 插件文件变更监听（引用计数 +1，首个 backend 启动）"""
        self._plugin_watcher_refcount += 1
        if self._plugin_watcher_started:
            return
        self._plugin_watcher_started = True

        try:
            from watchfiles import watch
        except ImportError:
            logger.warning("[PluginHost] watchfiles 未安装，插件热更新不可用。pip install watchfiles")
            return

        # 自定义监听过滤器：在 watchfiles 默认 DefaultFilter（已排除 .git/__pycache__/
        # .pyc/.swp 等）基础上，额外排除插件内的“易变 / vendored”目录与产物文件。
        # 这是“文件多”插件（如带 lark_oapi SDK deps 的 gateway-feishu，deps 含数千 .py）
        # 热重载卡顿的根因：clone / 导入时成千上万的文件变更事件被 watchfiles 与后续
        # 分类逻辑处理，拖垮 watcher 线程并诱发主线程重载风暴。排除后事件量降 ~95%，
        # 且不影响各组件目录（agents/hooks/commands/... 与 deps 平级，不被排除）的热更新。
        from watchfiles import DefaultFilter

        class _PluginWatchFilter(DefaultFilter):
            # 额外排除的目录段（任意层级命中即忽略其下全部变更）
            _EXTRA_SKIP_DIRS = {
                "deps",
                "node_modules",
                ".venv",
                "venv",
                "build",
                "dist",
                "install_tmp",
                "__pycache__",
                ".git",
                ".hg",
                ".svn",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "tmp",
                "temp",
            }
            # 额外排除的产物 / 缓存文件
            _EXTRA_SKIP_EXTS = (
                ".pyc",
                ".pyo",
                ".pyd",
                ".so",
                ".egg-info",
                ".log",
                ".tmp",
                ".bak",
                ".swp",
            )

            def __call__(self, change, path):
                if not super().__call__(change, path):
                    return False
                norm = str(path).replace("\\", "/").lower()
                if any(seg in self._EXTRA_SKIP_DIRS for seg in norm.split("/")):
                    return False
                if norm.endswith(self._EXTRA_SKIP_EXTS):
                    return False
                return True

        watch_filter = _PluginWatchFilter()

        # 收集需要监听的插件目录
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()

        watch_paths = []
        from pathlib import Path as _Path

        # 系统插件目录
        if hasattr(pm, "_SYSTEM_PLUGIN_DIR") and pm._SYSTEM_PLUGIN_DIR.exists():
            watch_paths.append(str(pm._SYSTEM_PLUGIN_DIR.resolve()))
        # 用户插件目录（开发环境下可能是相对路径，统一 resolve 为绝对路径）
        if pm._app_data_dir:
            user_plugin_dir = pm._app_data_dir / pm._USER_PLUGIN_DIR_NAME
            # 确保目录存在，否则 watcher 无法监听（用户后创建目录时热更新不生效）
            user_plugin_dir.mkdir(parents=True, exist_ok=True)
            watch_paths.append(str(user_plugin_dir.resolve()))
        # Claude Code 插件目录（同时支持两种生态）
        claude_skills_dir = _Path.home() / ".claude" / "skills"
        claude_skills_dir.mkdir(parents=True, exist_ok=True)
        watch_paths.append(str(claude_skills_dir.resolve()))
        claude_cache_dir = _Path.home() / ".claude" / "plugins" / "cache"
        if claude_cache_dir.exists():
            watch_paths.append(str(claude_cache_dir.resolve()))

        if not watch_paths:
            logger.warning("[PluginHost] 无插件目录可监听，跳过热更新")
            return

        logger.info(f"[PluginHost] 启动插件文件变更监听: {watch_paths}")

        # 连接内部信号到主线程重载方法
        self._hot_reload_requested.connect(self._on_hot_reload_requested)

        # 预计算插件路径 → 插件名映射（用于快速定位变更文件所属插件）
        plugin_prefixes = self._build_plugin_path_index()

        import threading as _threading

        # 上次全部窗口关闭后 stop_event 可能处于 set 状态（watch() 已退出），
        # 此处重建新事件，支持 watcher 在下一个 backend 上重启（热更新不丢失）。
        self._plugin_watcher_stop = _threading.Event()

        # 去重缓存：(plugin_name, component) → 上次重载时间
        _dedup_cache: Dict[tuple, float] = {}
        # watchfiles 每 ~8s 对每个组件 emit 一次重载（防抖 2s + 触发间隙），
        # 3s 窗口拦不住 → 放宽到 10s 覆盖组件重载间隔，同插件+组件 10s 内重复请求只执行一次
        _DEDUP_INTERVAL = 10.0

        def _is_duplicate(plugin_name: str, component: str) -> bool:
            now = time.time()
            key = (plugin_name, component)
            last = _dedup_cache.get(key, 0.0)
            if now - last < _DEDUP_INTERVAL:
                return True
            _dedup_cache[key] = now
            return False

        # 用可变容器包装 plugin_prefixes，闭包内可更新
        _prefixes_ref = [plugin_prefixes]
        # 保存引用给主线程的 _on_hot_reload_requested，在重载完成后重建索引
        self._watcher_prefixes_ref = _prefixes_ref
        # 保存 dedup 缓存引用给主线程：插件删除/清理路径需清空对应键，
        # 防止"删除 → 3s 内重装"被 _is_duplicate 误吞（review B3）
        self._watcher_dedup_cache = _dedup_cache

        def _rebuild_prefixes():
            """重建插件路径索引（在 watch 线程中调用）"""
            _prefixes_ref[0] = self._build_plugin_path_index()

        def _try_identify_new_plugins(changes) -> set:
            """直接扫描变更路径的父目录链，检测所有新增插件的首次变更

            当 _identify_plugin_from_changes 返回 None 时调用此方法，
            作为 fallback 直接从文件系统查找插件清单。

            修复：原 _try_identify_new_plugin 找到第一个插件就 return，
            导致一次性复制多个新插件时只检测到 1 个。现改为返回所有新插件名集合。

            🛡️ 双重校验（2026-08-23 bug fix）：父目录链匹配 .drifox-plugin/plugin.json
            会把任何运行时数据目录（如 .drifox/backups/、.drifox/logs/、sessions.db）
            的变更误识别为插件变更（change_path 在 .drifox/backups/ 下，父目录链
            上溯到 .drifox/plugins/calendar/.drifox-plugin/plugin.json → 标记为
            calendar 新插件变更，但 change_path 实际不在 calendar 代码目录里 →
            _identify_components_from_changes_fallback 返回空 → emit("") →
            _reload_single_plugin(calendar, "") → "root change, skip component reload"
            → ui 组件永远不被 reload → 已打开标签页欢迎卡片消失 + 新建会话不出现）。
            修复：父目录链找到插件 manifest 后，再校验 change_path 必须**直接**
            在该插件的 plugin.path 子树下，否则丢弃该误判。
            """
            import json as _json
            from pathlib import Path as _Path

            from app.plugins.managers.plugin_manager import PluginManager as _PM

            found: set = set()
            for _, change_path in changes:
                p = _Path(change_path)
                # 遍历变更路径及其所有父目录
                for parent in [p] + list(p.parents):
                    if not parent.exists() or not parent.is_dir():
                        continue
                    # 检查 .drifox-plugin 格式
                    manifest = parent / ".drifox-plugin" / "plugin.json"
                    if not manifest.exists():
                        # 检查 .claude-plugin 格式
                        manifest = parent / ".claude-plugin" / "plugin.json"
                        if not manifest.exists():
                            continue
                    try:
                        data = _json.loads(manifest.read_text(encoding="utf-8"))
                        candidate_name = data.get("name", parent.name)
                    except Exception:
                        candidate_name = parent.name
                    # 双重校验：candidate_name 是否已注册，且 change_path 必须**直接**
                    # 在其 plugin.path 子树下（排除 .drifox/backups/ 等运行时目录
                    # 通过父目录链误命中插件 manifest 的情况）。
                    plugin = _PM.get_instance().get_plugin(candidate_name)
                    if plugin is None:
                        # 未注册：manifest 位于受监控的插件根目录下 → 这是真正的新装
                        # 插件（整目录复制进 plugins/ 的场景），交给后续 rescan_plugin
                        # 注册；否则（.drifox/backups/ 等运行时数据目录）按误判丢弃。
                        # 旧逻辑无条件 break 导致「新装插件的首次变更」永远无法触发
                        # 注册，用户必须手动重启/重载才能看到新插件。
                        parent_lower = str(parent.resolve()).lower().rstrip(os.sep)
                        in_watch_root = any(
                            parent_lower.startswith(str(w).lower().rstrip(os.sep) + os.sep) for w in watch_paths
                        )
                        if in_watch_root:
                            found.add(candidate_name)
                        else:
                            logger.debug(
                                f"[PluginHost] _try_identify_new_plugins: 未注册插件 "
                                f"candidate={candidate_name} 的 manifest={parent} 不在监控"
                                f"插件根目录下，丢弃"
                            )
                        break  # 未注册分支处理完毕，跳出父目录链
                    plugin_root = str(plugin.path.resolve()).lower()
                    cp_lower = str(change_path).lower().replace("/", os.sep)
                    if cp_lower == plugin_root or cp_lower.startswith(plugin_root + os.sep):
                        found.add(candidate_name)
                    else:
                        logger.debug(
                            f"[PluginHost] _try_identify_new_plugins: 丢弃误判 "
                            f"change_path={change_path} 命中 manifest={candidate_name} "
                            f"但不在 plugin.path={plugin_root} 子树下"
                        )
                    break  # 跳出父目录链，处理下一个变更路径
            return found

        def _watch_loop():
            """后台线程: 监听插件目录文件变更，识别所属插件后请求主线程增量重载

            stop_event 被设置时 watch() 生成器正常退出（cleanup 归零引用后触发），
            线程随之结束，闭包对 self（PluginHostService 实例）的引用被释放。
            """
            logger.debug("[PluginHost] watchfiles 监听线程已启动")
            # 跟踪抑制窗口状态，用于“退出抑制时消费 pending 触发一次兜底重载”
            _prev_suppressed = False
            try:
                for changes in watch(
                    *watch_paths,
                    recursive=True,
                    debounce=2000,  # 2秒防抖
                    yield_on_timeout=False,
                    stop_event=self._plugin_watcher_stop,
                    watch_filter=watch_filter,  # 排除 deps/node_modules 等易变目录
                ):
                    # changes: set of (Change, Path)
                    if not changes:
                        continue
                    # 过滤掉 .git/ __pycache__/ .pyc 等无关变更
                    relevant_changes = []
                    for change_type, change_path in changes:
                        p = change_path.lower()
                        # 跳过 git/__pycache__/pyc 等无关文件
                        if ".git" in p or "__pycache__" in p or p.endswith(".pyc"):
                            continue
                        # 目录的 Change.modified 是子项变更的副作用（如 __pycache__ 创建/删除导致
                        # 父目录 ui/ 被标记为 modified），实际变更已被子项事件或 DefaultFilter 捕获，
                        # 过滤掉避免误触发跨插件重载。
                        if change_type == 2:  # Change.modified
                            # 以分隔符结尾 or 不含扩展名 → 疑似目录
                            if p.endswith(("\\", "/")) or "." not in p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]:
                                continue
                        # 跳过用户自定义目录中的内部数据文件（避免自我触发）
                        # 这些是 hook 持久化的数据文件，不是插件源码，修改它们不需要触发插件热更新
                        if "user-custom" in p:
                            pname = change_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
                            if pname in ("hooks_overrides.json", "hooks.json", "hook_states.json"):
                                continue
                        relevant_changes.append((change_type, change_path))

                    if not relevant_changes:
                        continue

                    # 🚀 P5e：外部批量变更期间（gitee 同步解压 user-custom、或插件市场
                    # installer 落盘 plugins/ 目录）抑制 watcher 热重载链。窗口内所有
                    # 变更先标记 pending 并跳过 emit，避免 watcher 链（rescan + agents
                    # 重载 + reload_all_commands ~2500ms + LSP 子进程 + plugin_changed
                    # 广播）与「clone/解压期间成百上千文件涌入」同时爆发叠加阻塞主线程；
                    # 也避免半安装插件被提前 import 报错。窗口结束后由调用方
                    # （config_sync 下载完成 / installer 安装完成）主动触发一次
                    # reload_plugin_subsystems 兜底加载，pending 事件不丢失。
                    if time.time() < self._suppress_watcher_until:
                        self._watcher_pending_reload = True
                        logger.info(
                            f"[PluginHost] 抑制窗口内收到 {len(relevant_changes)} 处变更，标记 pending 待合并重载"
                        )
                        _prev_suppressed = True
                        continue

                    # 刚退出抑制窗口：若窗口内累积了 pending 变更，触发一次兜底重载
                    # （经 _on_hot_reload_requested 去抖合并，仅 reload 一次），
                    # 使被抑制的变更不丢失，且避免窗口结束后每批真实 emit 形成风暴。
                    if _prev_suppressed and self._watcher_pending_reload:
                        self._watcher_pending_reload = False
                        logger.info("[PluginHost] 抑制窗口结束，合并触发一次兜底重载（消费 pending）")
                        self._hot_reload_requested.emit("", "")  # 空名 → reload_plugin_subsystems
                    _prev_suppressed = False

                    current_prefixes = _prefixes_ref[0]

                    # 识别变更所属插件
                    plugin_name = self._identify_plugin_from_changes(relevant_changes, current_prefixes)

                    if plugin_name == "__ALL__":
                        # 跨插件变更：逐一识别受影响的插件，各自走增量重载路径
                        affected_plugins = self._identify_all_affected_plugins(relevant_changes, current_prefixes)
                        logger.info(
                            f"[PluginHost] 跨插件文件变更 ({len(relevant_changes)} 处，"
                            f"涉及 {len(affected_plugins)} 个插件: {', '.join(sorted(affected_plugins))})，"
                            f"逐一增量重载..."
                        )
                        for pname in affected_plugins:
                            all_components = self._identify_all_components_from_changes(
                                relevant_changes, current_prefixes, pname
                            )
                            if all_components:
                                ordered = sorted(
                                    all_components,
                                    key=lambda c: self._COMPONENT_ORDER.get(c, 99),
                                )
                                for component in ordered:
                                    if _is_duplicate(pname, component):
                                        continue
                                    logger.info(
                                        f"[PluginHost] 插件 [{pname}] ({component}) "
                                        f"跨插件文件变更，请求主线程增量重载..."
                                    )
                                    self._hot_reload_requested.emit(pname, component)
                            else:
                                # 变更不在已知组件目录中（如 data/ 等非相关目录），跳过不触发重载
                                # 特殊 case：插件根目录被删除（整个插件被移出），此时 path 精确等于
                                # plugin_path，被 _identify_all_components_from_changes 跳过（continue），
                                # 导致 all_components 为空。需要在此处兜底检测并触发全组件卸载。
                                _root_deleted = any(
                                    ct == 3 and cp.lower() == path
                                    for path, name in current_prefixes.items()
                                    if name == pname
                                    for ct, cp in relevant_changes
                                )
                                if _root_deleted:
                                    logger.info(
                                        f"[PluginHost] 插件 [{pname}] 目录已被删除，跨插件变更中触发全组件卸载..."
                                    )
                                    self._hot_reload_requested.emit(pname, "")
                                else:
                                    logger.debug(
                                        f"[PluginHost] 插件 [{pname}] 跨插件文件变更不涉及已知组件，"
                                        f"跳过重载: {relevant_changes[0][1]}"
                                    )
                    elif plugin_name:
                        # 识别变更所属组件（agents/hooks/commands/themes/skills/mcp/lsp/ui）
                        # 多组件批处理：一次 watchfiles batch 中可能同时修改多个组件目录
                        # 原代码用 _identify_component_from_changes 只返回第一个组件，导致
                        # 多组件变更时只有第一个被处理，其他被静默忽略 → UI 不更新。
                        # 现改为识别所有涉及的组件，按优先级顺序逐个 emit。
                        all_components = self._identify_all_components_from_changes(
                            relevant_changes, current_prefixes, plugin_name
                        )
                        if all_components:
                            # 按优先级排序（agents 先于 commands 先于 skills 等）
                            ordered = sorted(all_components, key=lambda c: self._COMPONENT_ORDER.get(c, 99))
                            for component in ordered:
                                detail = f" ({component})"
                                # 去重：同一插件+组件短时间内重复触发则跳过
                                if _is_duplicate(plugin_name, component):
                                    logger.debug(f"[PluginHost] 插件 [{plugin_name}]{detail} 文件变更，去重跳过...")
                                    continue
                                logger.info(
                                    f"[PluginHost] 插件 [{plugin_name}]{detail} "
                                    f"文件变更 ({len(relevant_changes)} 处)，请求主线程增量重载..."
                                )
                                self._hot_reload_requested.emit(plugin_name, component)
                        else:
                            # 变更不在已知组件目录中（如 data/ 等非相关目录），跳过不触发重载
                            # 特殊 case：插件根目录被删除（整个插件被移出），此时 path 精确等于
                            # plugin_path，被 _identify_all_components_from_changes 跳过（continue），
                            # 导致 all_components 为空。需要在此处兜底检测并触发全组件卸载。
                            _root_deleted = any(
                                ct == 3 and cp.lower() == path
                                for path, name in current_prefixes.items()
                                if name == plugin_name
                                for ct, cp in relevant_changes
                            )
                            if _root_deleted:
                                logger.info(f"[PluginHost] 插件 [{plugin_name}] 目录已被删除，触发全组件卸载...")
                                self._hot_reload_requested.emit(plugin_name, "")
                            else:
                                logger.debug(
                                    f"[PluginHost] 插件 [{plugin_name}] 文件变更不涉及已知组件，"
                                    f"跳过重载: {relevant_changes[0][1]}"
                                )
                    else:
                        # 无法通过路径索引识别：尝试直接从文件系统检测新插件
                        new_names = _try_identify_new_plugins(relevant_changes)
                        if new_names:
                            logger.info(
                                f"[PluginHost] 检测到 {len(new_names)} 个新插件文件变更"
                                f"「{', '.join(sorted(new_names))}」，逐一请求增量重载..."
                            )
                            from app.plugins.managers.plugin_manager import PluginManager as _PM

                            for new_name in sorted(new_names):
                                # 已注册插件（索引未及时重建导致路径识别失败）：
                                # 不再走 __NEW__ 全量路径，改按组件增量重载，
                                # 避免 bridge.json 等运行时文件变更反复触发全量加载
                                if _PM.get_instance().has_plugin(new_name):
                                    # 路径索引过期时，使用插件实际路径识别变更组件，
                                    # 避免空组件导致 _reload_single_plugin 跳过所有子系统重载
                                    _identified = self._identify_components_from_changes_fallback(
                                        new_name, relevant_changes
                                    )
                                    if _identified:
                                        for _comp in _identified:
                                            if _is_duplicate(new_name, _comp):
                                                continue
                                            logger.info(
                                                f"[PluginHost] 插件 [{new_name}] ({_comp}) "
                                                f"文件变更（索引未更新），请求主线程增量重载..."
                                            )
                                            self._hot_reload_requested.emit(new_name, _comp)
                                        continue
                                    # fallback 未识别到组件变更（仅根目录文件变更）→ 空组件重载（跳过子系统）
                                    logger.debug(
                                        f"[PluginHost] 插件 [{new_name}] fallback 未识别到组件变更 "
                                        f"({len(relevant_changes)} 处)，按已知插件增量重载（跳过子系统）..."
                                    )
                                    logger.info(
                                        f"[PluginHost] 插件 [{new_name}] 已注册（索引未更新），改按已知插件增量重载..."
                                    )
                                    self._hot_reload_requested.emit(new_name, "")
                                    continue
                                # 未注册：先 rescan_plugin 复查——对目录存在 + plugin.json
                                # 有效但注册表缺失的插件（如 user-custom），rescan 会直接注册成功，
                                # 此时应走已知插件增量路径，避免误判为「新插件」触发 __NEW__ 全量加载
                                _PM.get_instance().rescan_plugin(new_name)
                                if _PM.get_instance().has_plugin(new_name):
                                    # 全新安装/重装判定：本批变更含"新增"(Changed.added==1)事件
                                    # 落在插件根目录或其下（根目录/组件目录被重建），说明插件此前
                                    # 未注册且组件从未加载 → 走 __NEW__ 全组件加载，避免空组件
                                    # 跳过导致 new_build 组件永不生效（卸载后重装必触发此分支）。
                                    # 仅运行时数据文件变更（如 bridge.json 等 Modified/已存在目录）
                                    # 不算新增，继续走已知插件增量/跳过，避免全量加载。
                                    _plugin = _PM.get_instance().get_plugin(new_name)
                                    _root_path = str(_plugin.path.resolve()).lower().rstrip("\\/")
                                    _is_fresh_install = any(
                                        ct == 1
                                        and (cp.lower() == _root_path or cp.lower().startswith(_root_path + os.sep))
                                        for ct, cp in relevant_changes
                                    )
                                    if _is_fresh_install:
                                        logger.info(
                                            f"[PluginHost] 插件 [{new_name}] 检测到新增事件，"
                                            f"判定为全新安装，请求 __NEW__ 全组件加载..."
                                        )
                                        # 预填充 dedup cache，防止路径索引重建后同一批
                                        # watch 事件的剩余部分以已知插件路径再次触发
                                        _dedup_cache[(new_name, "")] = time.time() + _DEDUP_INTERVAL
                                        self._hot_reload_requested.emit(self._NEW_PLUGIN_SENTINEL, new_name)
                                        continue
                                    # 非全新安装：使用插件实际路径识别变更组件
                                    _identified = self._identify_components_from_changes_fallback(
                                        new_name, relevant_changes
                                    )
                                    if _identified:
                                        for _comp in _identified:
                                            if _is_duplicate(new_name, _comp):
                                                continue
                                            logger.info(
                                                f"[PluginHost] 插件 [{new_name}] ({_comp}) "
                                                f"rescan 后文件变更，请求主线程增量重载..."
                                            )
                                            self._hot_reload_requested.emit(new_name, _comp)
                                        continue
                                    # fallback 未识别到组件变更 → 空组件重载（跳过子系统）
                                    logger.debug(
                                        f"[PluginHost] 插件 [{new_name}] rescan 后 fallback 未识别到组件变更 "
                                        f"({len(relevant_changes)} 处)，按已知插件增量重载（跳过子系统）..."
                                    )
                                    logger.info(
                                        f"[PluginHost] 插件 [{new_name}] rescan 后已注册，改按已知插件增量重载..."
                                    )
                                    self._hot_reload_requested.emit(new_name, "")
                                    continue
                                # 预填充 dedup cache，防止路径索引重建后同一批 watch 事件
                                # 的剩余部分以已知插件路径再次触发（ghost trigger）
                                _dedup_cache[(new_name, "")] = time.time() + _DEDUP_INTERVAL
                                # 发射新插件标记，走 _reload_new_plugin 增量路径
                                # 只扫描这一个插件目录，不触发全量 rescan
                                self._hot_reload_requested.emit(self._NEW_PLUGIN_SENTINEL, new_name)
                        else:
                            # 无法识别的新增文件变更（如编辑器临时文件、git 残留等）
                            # 跳过不处理，等下次事件重试。不触发全量重扫
                            logger.debug(f"[PluginHost] 文件变更无法识别所属插件，跳过: {relevant_changes[0][1]}")
            except Exception as e:
                logger.error(f"[PluginHost] watchfiles 监听异常退出: {e}")

        import threading as _threading

        t = _threading.Thread(target=_watch_loop, daemon=True, name="plugin-watcher")
        self._plugin_watcher_thread = t
        t.start()

    def _stop_plugin_watcher(self):
        """backend 关闭时递减 watcher 引用计数；归零时停止 watchfiles 线程。

        泄漏修复（P1）：watcher 闭包持有启动它的第一个 backend 实例引用
        （self._hot_reload_requested / self.plugin_changed / self._identify_*），
        若窗口关闭而线程不退出，该实例（及其整棵窗口对象树）永远无法被 GC。

        - refcount > 0：仍有活跃窗口，维持 watcher（热更新继续工作）
        - refcount == 0：设置 stop_event → watch() 生成器退出 → join 等待线程结束
          → 闭包释放 → 首个 backend 实例可回收；同时复位标志，允许新窗口
          重新启动 watcher（stop_event 在 _start_plugin_watcher 中重建），热更新不丢失。
        """
        self._plugin_watcher_refcount = max(0, self._plugin_watcher_refcount - 1)
        if self._plugin_watcher_refcount > 0:
            return
        stop = self._plugin_watcher_stop
        if stop is not None:
            stop.set()
        t = self._plugin_watcher_thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        self._plugin_watcher_thread = None
        self._plugin_watcher_started = False

    def _build_plugin_path_index(self) -> Dict[str, str]:
        """构建插件路径前缀 → 插件名的映射表

        前缀不带尾部分隔符，匹配时同时支持目录本身和目录内文件。
        Returns:
            {小写路径: 插件名}
        """
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        prefixes = {}
        for plugin in pm.list_plugins():
            path = str(plugin.path.resolve()).lower().rstrip("\\/")
            prefixes[path] = plugin.name
        return prefixes

    def _identify_plugin_from_changes(self, changes: list, plugin_prefixes: Dict[str, str]) -> Optional[str]:
        """从变更文件路径识别所属插件名称

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件根路径 → 插件名 映射

        Returns:
            - 插件名: 单一插件变更，可增量重载
            - "__ALL__": 跨插件变更，需要全量重载
            - None: 无法识别（不属于任何已知插件），跳过
        """
        # 按路径长度降序排列（优先精确匹配）
        sorted_prefixes = sorted(plugin_prefixes.keys(), key=len, reverse=True)

        found = set()
        for _, change_path in changes:
            cp = change_path.lower()
            for prefix in sorted_prefixes:
                # 精确匹配目录本身 或 匹配目录内文件
                if cp == prefix or cp.startswith(prefix + os.sep):
                    found.add(plugin_prefixes[prefix])
                    break

        if not found:
            return None

        if len(found) == 1:
            return next(iter(found))

        # 跨插件变更，需要全量重载
        logger.debug(f"[PluginHost] 跨插件变更: {found}，触发全量重载")
        return "__ALL__"

    def _identify_all_affected_plugins(self, changes: list, plugin_prefixes: Dict[str, str]) -> set:
        """从变更文件路径识别所有涉及的插件名集合

        与 _identify_plugin_from_changes 共享路径匹配逻辑，但返回完整集合
        而非在跨插件时返回 __ALL__。用于 watch_loop 在跨插件变更时
        逐个插件增量重载。

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件路径 → 插件名 映射

        Returns:
            set[str]: 受影响的所有插件名集合
        """
        sorted_prefixes = sorted(plugin_prefixes.keys(), key=len, reverse=True)
        found: set = set()
        for _, change_path in changes:
            cp = change_path.lower()
            for prefix in sorted_prefixes:
                if cp == prefix or cp.startswith(prefix + os.sep):
                    found.add(plugin_prefixes[prefix])
                    break
        return found

    def _identify_component_from_changes(self, changes: list, plugin_prefixes: Dict[str, str], plugin_name: str) -> str:
        """从变更文件路径识别所属组件子目录

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件路径 → 插件名 映射
            plugin_name: 已识别出的插件名

        Returns:
            "agents" | "hooks" | "commands" | "themes" | "skills" | "mcp" | "lsp" | "ui"
            | "" (根目录/无法确定)

        注意：watchfiles 的 2 秒防抖会把同一批变更聚合。一次 batch 中可能同时
        修改多个组件目录下的文件（如同时编辑 commands/ 和 skills/）。
        本方法只返回第一个匹配的组件，多组件场景请使用
        `_identify_all_components_from_changes()` 并配合 watch_loop 多次 emit。
        """
        components = self._identify_all_components_from_changes(changes, plugin_prefixes, plugin_name)
        if not components:
            return ""
        # 优先返回优先级最高的组件（与原行为兼容：先匹配的先返回）
        return sorted(components, key=lambda c: self._COMPONENT_ORDER.get(c, 99))[0]

    # 组件优先级（用于在多组件批处理中决定先后顺序）
    # agents 最先：它会影响 commands 和 hooks 同步
    _COMPONENT_ORDER = {
        "agents": 0,
        "hooks": 1,
        "commands": 2,
        "themes": 3,
        "skills": 4,
        "mcp": 5,
        "lsp": 6,
        "ui": 7,
    }

    def _identify_all_components_from_changes(
        self, changes: list, plugin_prefixes: Dict[str, str], plugin_name: str
    ) -> set:
        """从变更文件路径识别所有涉及的组件子目录（多组件批处理）

        一次 watchfiles batch 中可能同时修改多个组件目录下的文件。
        原 _identify_component_from_changes 只返回第一个组件，导致多组件
        同时变更时只有一个被处理，其他被静默忽略，UI 不刷新。
        本方法返回所有涉及的组件，让 watch_loop 拆分多次 emit。

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件路径 → 插件名 映射
            plugin_name: 已识别出的插件名

        Returns:
            set[str]: 涉及的所有组件名；空 set 表示根目录/无法识别
        """
        # 找到该插件的路径前缀
        plugin_path = None
        for path, name in plugin_prefixes.items():
            if name == plugin_name:
                plugin_path = path
                break
        if not plugin_path:
            return set()

        from app.plugins.kernel import KNOWN_COMPONENTS, ROOT_FILE_COMPONENTS

        components: set = set()
        for _, change_path in changes:
            cp = change_path.lower()
            if cp == plugin_path:
                continue  # 插件根目录本身变更，留给后续逻辑判断
            if cp.startswith(plugin_path + os.sep):
                rel = cp[len(plugin_path) + 1 :]  # 去掉 "plugin_path\"
                first_seg = rel.split(os.sep)[0] if os.sep in rel else rel
                if first_seg in KNOWN_COMPONENTS:
                    components.add(first_seg)
                    continue
                # 根目录的关键文件（如 .mcp.json）映射到对应组件
                if first_seg in ROOT_FILE_COMPONENTS:
                    components.add(ROOT_FILE_COMPONENTS[first_seg])
        return components

    def _identify_components_from_changes_fallback(self, plugin_name: str, changes: list) -> list:
        """从变更文件路径识别涉及的组件（使用插件实际路径，不依赖路径索引）

        当路径索引过期（新安装/更新插件后索引尚未重建）时，
        _identify_all_components_from_changes 无法找到插件路径，
        导致返回空 set，最终空组件 emit 使 _reload_single_plugin 跳过所有重载。
        本方法直接从 pm.get_plugin() 获取插件实际路径，绕过过期索引。

        Args:
            plugin_name: 已识别出的插件名
            changes: [(Change, path_str), ...]

        Returns:
            list[str]: 按优先级排序的组件名列表；空列表表示无组件变更
        """
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        plugin = pm.get_plugin(plugin_name)
        if not plugin:
            return []

        plugin_path = str(plugin.path.resolve()).lower().rstrip("\\/")
        from app.plugins.kernel import KNOWN_COMPONENTS, ROOT_FILE_COMPONENTS

        components: set = set()
        for _, change_path in changes:
            cp = change_path.lower()
            if cp == plugin_path:
                continue
            if cp.startswith(plugin_path + os.sep):
                rel = cp[len(plugin_path) + 1 :]
                first_seg = rel.split(os.sep)[0] if os.sep in rel else rel
                if first_seg in KNOWN_COMPONENTS:
                    components.add(first_seg)
                    continue
                if first_seg in ROOT_FILE_COMPONENTS:
                    components.add(ROOT_FILE_COMPONENTS[first_seg])

        if not components:
            return []
        return sorted(components, key=lambda c: self._COMPONENT_ORDER.get(c, 99))

    def _on_hot_reload_requested(self, plugin_name: str, component: str):
        """主线程中执行的插件热更新

        单次请求同步执行（保持原有增量语义与单元测试同步断言）；300ms 内的
        重复 / 风暴请求走去抖合并：首次立即执行，窗口内的后续请求不再逐个触发
        主线程重载（reload_agents / reload_all_commands ~2500ms），改为计划一次
        合并的 reload_plugin_subsystems，避免“文件多”的插件在批量写入 / clone 时
        每批都重载阻塞 UI。

        Args:
            plugin_name: 插件名
                - "" (空) → 全量重载（走 reload_plugin_subsystems）
                - _NEW_PLUGIN_SENTINEL → 新增插件（component 参数存储插件名）
                - 其他 → 已知插件的增量重载（component 为具体组件名或 "" 表示根目录变更）
            component: 组件名（"" 表示该插件的全部组件，否则为 agents/hooks/commands/themes/skills/mcp/lsp）
        """
        now = time.time()
        last = getattr(self, "_last_reload_at", 0.0)
        if now - last < 0.3:
            # 去抖窗口内：不立即重载，改为计划一次合并兜底（仅执行一次）
            self._schedule_debounced_reload()
            return
        self._last_reload_at = now
        try:
            result = self._do_single_reload(plugin_name, component)
            self.emit_plugin_changed(result, plugin_name)
        except Exception as e:
            logger.error(f"[PluginHost] 插件热更新失败: {e}")
        finally:
            # 重载完成后重建 watchfiles 路径索引（finally 保证异常路径也更新，
            # 否则已注册插件会被反复识别为"新插件"，触发 bridge.json 自触发循环）
            self._rebuild_watcher_prefixes()

    def emit_plugin_changed(self, result: dict, plugin_name: str = "", action: Optional[str] = None) -> None:
        """广播插件变更结果到全部活跃 backend 的 plugin_changed 信号

        附加事件标识（仅广播用，不污染业务 result 消费方）：
        - _event_seq：实例级递增序号——同一事件广播到多 backend/多窗口时
          指纹一致可去重；不同事件即使 result 相同（如 10s 内连续热重载
          两个插件均为 {ui: True}）也不会被窗口级指纹短窗误吞。
        - _plugin_name：本次变更的插件名（"" = 全量/合并路径），UI 据此
          精准重绘该插件已挂载的视图（消息内容块 / 浮动卡片 / 欢迎卡片）。

        PluginChanged hook：重载完成后同步触发（diff 明细见 _trigger_plugin_changed_hook），
        hook 输出经 on_hook_finished → _hook_message_queue 注入，AI 下一轮对话可见。

        Args:
            action: 变更动作语义。None 时按 plugin_name 自动推断：
                _NEW_PLUGIN_SENTINEL → installed；插件已不在注册表 → uninstalled；
                其余（热重载/全量/更新）→ updated。市场路径传精确值
                （installed/updated/uninstalled/enabled/disabled）
        """
        # PluginChanged hook 触发（广播前，result 尚未被 _event_seq 污染）
        try:
            self._trigger_plugin_changed_hook(result, plugin_name, action)
        except Exception as e:
            logger.debug(f"[PluginHost] PluginChanged hook trigger failed: {e}")

        seq = getattr(self, "_hot_reload_seq", 0) + 1
        self._hot_reload_seq = seq
        annotated = dict(result)
        annotated["_event_seq"] = seq
        annotated["_plugin_name"] = plugin_name
        self.plugin_changed.emit(annotated)

    def _infer_plugin_changed_action(self, plugin_name: str) -> str:
        """按 plugin_name 推断 PluginChanged 的 action 语义"""
        if plugin_name == self._NEW_PLUGIN_SENTINEL:
            return "installed"
        if plugin_name:
            try:
                from app.plugins.managers.plugin_manager import PluginManager

                if not PluginManager.get_instance().has_plugin(plugin_name):
                    return "uninstalled"
            except Exception:
                pass
        return "updated"

    def _trigger_plugin_changed_hook(self, result: dict, plugin_name: str, action: Optional[str]) -> None:
        """触发 PluginChanged hook 并广播到全部活跃 backend（多标签页统一出口）

        主线程同步触发（prompt hook 注入顺序）；diff 由
        hook_manager.trigger_plugin_changed_hook 统一计算（模块级基线，
        与 mcp/启停触发点共享）。

        ★ 多标签页修复：此前只在宿主 backend 上触发，其余标签页的
        _hook_message_queue 收不到注入（plugin_changed UI 刷新信号已广播，
        但 hook 输出未广播）。现对宿主 + 全部活跃 backend 各自触发一次
        trigger_event——_hooks 注册表类级共享（任一实例等效），但
        on_hook_finished 闭包与 _hook_message_queue 是实例级的，必须用每个
        backend 自己的 hook_manager 实例触发，输出才进各自队列。
        ★ diff 在宿主上只算一次，广播时携带已算好的 diff/sub_actions——
        模块级快照基线首次消费后第二次 _compute_plugin_snapshot_diff()
        返回 None，逐个重算会导致非宿主 backend 拿不到 diff。

        context 经 stdin JSON 传给 command hook，字段：
        - action: installed/updated/uninstalled/enabled/disabled
        - plugin_name: 变更插件名（sentinel 解析后置空）
        - components: 各组件重载结果（agents 数量/其余布尔）
        - diff: 工具与 MCP 服务器增减明细（tools_added/tools_removed/mcp_added/mcp_removed）
        """
        from app.core.backend import ChatBackend

        instances = [
            b
            for b in list(ChatBackend._active_instances)
            if getattr(b, "_ui_valid", True) and b._hook_manager is not None
        ]
        if not instances or "PluginChanged" not in instances[0]._hook_manager._hooks:
            # 无注册 hook 仍刷新基线（懒导入失败静默，不影响主流程）
            try:
                from app.core.hook_manager import _compute_plugin_snapshot_diff

                _compute_plugin_snapshot_diff()
            except Exception:
                pass
            return
        if action is None:
            action = self._infer_plugin_changed_action(plugin_name)
        name = plugin_name if plugin_name != self._NEW_PLUGIN_SENTINEL else ""
        context: Dict[str, Any] = {
            "action": action,
            "plugin_name": name,
            "components": {k: v for k, v in result.items() if not k.startswith("_")},
        }
        try:
            from app.core.hook_manager import _compute_plugin_snapshot_diff, _sub_actions_from_diff

            diff = _compute_plugin_snapshot_diff()
        except Exception:
            diff = None
        if diff:
            context["diff"] = diff
            context["sub_actions"] = _sub_actions_from_diff(diff)
        # 广播：全部活跃 backend 的 hook_manager 各自触发（hook 输出进各自队列）。
        # 服务自身的 _host_hook_manager 仅作 AgentManager 重载时的注册目标
        # （_hooks 类级共享，注册即全局可见），无对话队列、不作为触发目标。
        targets = [
            _b._hook_manager
            for _b in list(ChatBackend._active_instances)
            if getattr(_b, "_ui_valid", True) and _b._hook_manager is not None
        ]
        for _hm in targets:
            try:
                _hm.trigger_event("PluginChanged", dict(context))
            except RuntimeError:
                pass  # 窗口关闭竞态：backend 已 deleteLater 但未 cleanup

    def _schedule_debounced_reload(self):
        """风暴期合并重载：300ms 后执行一次全量 reload_plugin_subsystems（仅触发一次）

        修 #2 timer：改用 QTimer.singleShot 静态方法，不持有 QTimer 实例
        （PySide6 下即便 stop+deleteLater 仍残留 +1 QTimer，singleShot 零实例）。
        用 _reload_pending 守卫保留"风暴期合并"语义：多次调用只在首个 300ms 后触发一次。
        """
        if getattr(self, "_reload_pending", False):
            return
        self._reload_pending = True
        QTimer.singleShot(300, self._do_debounced_reload)

    def _do_debounced_reload(self):
        """去抖到期：合并执行一次全量重载（已内部对 added/changed/removed 增量处理）"""
        self._reload_pending = False
        self._last_reload_at = time.time()
        try:
            result = self.reload_plugin_subsystems()
            self.emit_plugin_changed(result)
        except Exception as e:
            logger.error(f"[PluginHost] 插件热更新失败: {e}")
        finally:
            self._rebuild_watcher_prefixes()

    def _do_single_reload(self, plugin_name: str, component: str) -> dict:
        """执行单条增量重载（不含 emit / 广播 / 索引重建，由 _flush_reload_intents 统一处理）"""
        if plugin_name == self._NEW_PLUGIN_SENTINEL:
            # 新增插件：只扫描这一个插件目录，增量加载其组件。
            # 不再"已注册则降级为空组件跳过"——watch 线程可能在 emit 前
            # 对全新安装的插件做过 rescan 注册（组件尚未加载），此时
            # 降级为空组件（""）会全 False 跳过，导致重装后的插件组件
            # 永不生效。_reload_new_plugin 对已注册插件幂等（组件错重载，
            # UI/LSP 先卸载后加载），可直接复用。
            return self._reload_new_plugin(component)
        elif plugin_name:
            return self._reload_single_plugin(plugin_name, component)
        else:
            return self.reload_plugin_subsystems()

    def _rebuild_watcher_prefixes(self):
        """重建 watchfiles 线程的插件路径索引（主线程调用）"""
        prefixes_ref = getattr(self, "_watcher_prefixes_ref", None)
        if prefixes_ref is not None:
            prefixes_ref[0] = self._build_plugin_path_index()

    def _reload_new_plugin(self, plugin_name: str) -> dict:
        """增量加载新增插件的所有组件，不重启已有子系统

        与 _reload_single_plugin 的区别：
        - 由 _watch_loop 检测到全新插件时调用（emit "__NEW__"）
        - 只扫描这一个插件目录（避免全量 rescan）
        - 只注册/启动该插件新增的 LSP 服务器（不碰已有的）
        - 不触发全量 rescan 也就不触发全量 reload_plugin_subsystems

        Args:
            plugin_name: 新增插件名

        Returns:
            dict: 各组件重载结果；key 集合基于 kernel.KNOWN_COMPONENTS 动态生成，
            agents → int（数量），其余 → bool。新增组件类型时无需改此处。
        """
        from app.plugins.kernel import KNOWN_COMPONENTS as _KC

        # result 基于 KNOWN_COMPONENTS 动态生成：新增组件类型零改动（Task 7）
        result: dict = {k: (0 if k == "agents" else False) for k in _KC}

        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[PluginHost] PluginManager not initialized, cannot reload")
                return result

            # 1. 只重新扫描这一个插件目录（不走全量 rescan）
            pm.rescan_plugin(plugin_name)

            plugin = pm.get_plugin(plugin_name)
            if not plugin:
                logger.warning(f"[PluginHost] New plugin '{plugin_name}' not found after scan")
                return result

            comps = plugin.components
            logger.info(f"[PluginHost] 检测到新插件「{plugin_name}」，执行增量加载")

            # 2. 智能体 + hooks
            if comps.get("agents") and self._agent_manager:
                result["agents"] = self._agent_manager.reload_plugin_agents(plugin_name)
                result["hooks"] = True  # agents 组件包含 hooks 重载
                try:
                    from app.core.builtin_commands import reload_agent_commands

                    reload_agent_commands()
                    result["commands"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[PluginHost] Failed to reload commands after agent change: {e}")

            if comps.get("hooks") and not comps.get("agents") and self._agent_manager:
                self._agent_manager.reload_plugin_hooks(plugin_name)
                result["hooks"] = True

            # 3. 命令
            if comps.get("commands") and not result["commands"]:
                try:
                    from app.core.builtin_commands import reload_all_commands

                    reload_all_commands()
                    result["commands"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[PluginHost] Failed to reload commands: {e}")

            # 4. 主题
            if comps.get("themes"):
                try:
                    from app.utils.config import update_theme_options
                    from app.utils.theme_manager import theme_manager

                    theme_manager.reload()
                    update_theme_options()
                    result["themes"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[PluginHost] Failed to reload themes: {e}")

            # 5. 技能 / MCP：懒加载，只需标记
            if comps.get("skills"):
                from app.utils.utils import invalidate_skills_cache
                invalidate_skills_cache()
            result["skills"] = bool(comps.get("skills"))
            result["mcp"] = bool(comps.get("mcp"))

            # 6. LSP：先移除旧服务再注册新服务（幂等，避免对已注册插件重复加载）
            if comps.get("lsp"):
                try:
                    from app.core.lsp.lsp_manager import get_lsp_manager

                    lsp_mgr = get_lsp_manager()
                    # 先移除该插件已有的 LSP 服务器（若此前已加载），再注册新配置
                    lsp_mgr.remove_plugin_servers(plugin_name)
                    lsp_config = pm.get_plugin_lsp_config(plugin_name)
                    if lsp_config:
                        count = lsp_mgr.add_plugin_servers(plugin_name, lsp_config["config"])
                        result["lsp"] = count > 0
                    logger.info(f"[PluginHost] Plugin '{plugin_name}' LSP 增量加载完成")
                except Exception as e:
                    logger.error(f"[PluginHost] Plugin '{plugin_name}' LSP 增量加载失败: {e}")

            # 7. UI 组件：增量加载，不重复加载已存在的插件
            if comps.get("ui"):
                try:
                    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

                    UIPluginRegistry.get_instance().load_plugin(plugin_name, plugin.path)
                    result["ui"] = True
                    logger.info(f"[PluginHost] Plugin '{plugin_name}' UI 组件已加载")
                except Exception as e:
                    logger.error(f"[PluginHost] Plugin '{plugin_name}' UI 加载失败: {e}")

            # 8. 其余组件：与 builtin_reloaders 同构的 kernel 分派
            # 走内核注册表而非硬编码 if，保持单源真理（新增组件类型零改动）。
            # 遍历 COMPONENT_ORDER（排除上方 1-7 已手工处理的组件），覆盖
            # tools/providers/team_templates/model_adapters/loop_policies/
            # storages/serializers/gateways 全部 registry 分派组件——
            # 历史 bug：此处曾硬编码 3 项漏掉 gateways，卸载重装（__NEW__ 路径）
            # 后 gateway 平台 def 不注册/adapter 不建/连接不启 → 机器人无响应。
            from app.plugins.builtin_reloaders import bind_runtime, register_builtin_reloaders
            from app.plugins.kernel import COMPONENT_ORDER, ReloadContext, get_reloader_registry

            bind_runtime(self._agent_manager)
            registry = get_reloader_registry()
            register_builtin_reloaders(registry)  # 幂等
            _MANUAL_STEPS = {"agents", "hooks", "commands", "themes", "skills", "mcp", "lsp", "ui"}
            for comp in COMPONENT_ORDER:
                if comp in _MANUAL_STEPS or not comps.get(comp):
                    continue
                reloaded = registry.reload(
                    ReloadContext(
                        plugin_name=plugin_name,
                        plugin=plugin,
                        component=comp,
                        is_new_plugin=True,
                    )
                )
                result[comp] = reloaded if reloaded is not None else False

            logger.info(
                f"[PluginHost] 新插件增量加载「{plugin_name}」完成: "
                f"agents={result['agents']}, commands={result['commands']}, "
                f"themes={result['themes']}, skills={result['skills']}, "
                f"mcp={result['mcp']}, lsp={result['lsp']}, ui={result['ui']}, "
                f"tools={result['tools']}, providers={result['providers']}, "
                f"team_templates={result['team_templates']}"
            )
        except Exception as e:
            logger.error(f"[PluginHost] Failed to reload new plugin '{plugin_name}': {e}")

        return result

    def _cleanup_removed_plugin_components(
        self,
        plugin_name: str,
        removed_components: dict,
        result: dict,
        result_keys: tuple,
    ) -> dict:
        """精准清理已移除插件的全部组件（删除段核心，供两处复用）

        - `_reload_single_plugin` 删除段（rescan 前捕获的 removed_components）
        - `reload_plugin_subsystems` diff 的 removed 分支（rescan 后插件已不在索引，
          components 取自 rescan 返回的 Plugin 对象，避免「索引已移除 → 捕获为空 → 卸载不干净」）

        只处理该插件实际含有的组件：agents→cleanup_plugin_artifacts、hooks-only→unregister、
        commands→reload_all、themes→reload、skills→invalidate、ui→unload、lsp→remove_only。
        """
        # 清空该插件的 watcher 去重缓存键（review B3）：
        # 防止"删除 → 3s 内重装"被 _is_duplicate 误判为重复而吞掉重装加载
        dedup_cache = getattr(self, "_watcher_dedup_cache", None)
        if dedup_cache is not None:
            stale_keys = [k for k in dedup_cache.keys() if k[0] == plugin_name]
            for k in stale_keys:
                dedup_cache.pop(k, None)
            if stale_keys:
                logger.debug(f"[PluginHost] 插件 [{plugin_name}] 移除，清空 {len(stale_keys)} 个 watcher 去重键")

        # 表分派：遍历该插件原有组件 → registry.reload(plugin=None) 走清理语义
        # 遍历序按 COMPONENT_ORDER（tuple）— KNOWN_COMPONENTS 是 set 序不确定，
        # 漏遍历会跳过某组件的清理。agents 置首：先于 hooks/commands 处理以保留其联动标记语义。
        from app.plugins.kernel import COMPONENT_ORDER, ReloadContext, get_reloader_registry

        registry = get_reloader_registry()
        for comp in COMPONENT_ORDER:
            if not removed_components.get(comp):
                continue
            reloaded = registry.reload(
                ReloadContext(
                    plugin_name=plugin_name,
                    plugin=None,
                    component=comp,
                    is_new_plugin=False,
                )
            )
            if comp in result_keys:
                # agents 返回 int(数量)，其余 True/False — agents 删除归零
                result[comp] = reloaded if reloaded is not None else False
            # agents 联动标记：与旧 backend elif 语义一致 — agents 命中后置 commands=True 走 plugin_changed 广播
            # 重建快捷键（agents-only 插件无 commands 组件时也置，触发 _on_plugin_hot_reload 双保险）。
            # hooks 是否置位由下方 _reload_hooks 遍历按 removed_components.get("hooks") 自然决定，
            # 对齐旧代码 `result["hooks"] = removed_components.get("hooks", False)`。
            # 此处显式置 True 是保守保留旧行为：删除段可能由 watchfiles 单点事件触发而非完整
            # 遍历原 components，强制 hooks=True 保证窗口侧 UI 刷新链不漏。
            if comp == "agents":
                result["hooks"] = True
                result["commands"] = True

        logger.info(
            f"[PluginHost] Plugin '{plugin_name}' cleanup done via kernel: { {k: result[k] for k in result_keys} }"
        )
        return result

    def _reload_single_plugin(self, plugin_name: str, component: str = "") -> dict:
        """增量重载单个插件（不清除其他插件的数据）

        根据变更的组件名精确重载，不触发无关子系统：

        - "agents"   → 重载智能体 + hooks
        - "hooks"    → 仅重载 hooks（不碰智能体）
        - "commands" → 重载命令
        - "themes"   → 重载主题
        - "skills"   → 重载技能（PluginManager 已更新，UI 下次调用 get_local_skills() 自动生效）
        - "mcp"      → 重载 MCP 配置（PluginManager 已更新，UI 下次调用 get_mcp_servers() 自动生效）
        - "lsp"      → 热重载 LSP 配置（使用增量 API：先移除旧服务 → 再注册新服务）
        - "ui"       → 热重载 UI 组件（先卸载后加载，reload_plugin）
        - ""         → 跳过（根目录文件变更如 README/LICENSE，不影响运行时）

        Args:
            plugin_name: 插件名称
            component: 变更的组件名

        Returns:
            dict: 各组件重载结果；key 集合基于 kernel.KNOWN_COMPONENTS 动态生成，
            agents → int（数量），其余 → bool。新增组件类型时无需改此处。
        """
        from app.plugins.kernel import KNOWN_COMPONENTS as _KC

        # 表分派：原 8 分支 if 已在 builtin_reloaders（commit 0e141cd9）— 此处仅查注册表
        # result / result_keys 基于 KNOWN_COMPONENTS 动态生成：新增组件类型零改动（Task 7）
        result: dict = {k: (0 if k == "agents" else False) for k in _KC}
        result_keys = tuple(_KC)

        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[PluginHost] PluginManager not initialized, cannot reload")
                return result

            # 1. 捕获移除前的插件组件信息，用于精确清理
            plugin_before = pm.get_plugin(plugin_name)
            removed_components = dict(plugin_before.components) if plugin_before else {}

            pm.rescan_plugin(plugin_name)

            plugin = pm.get_plugin(plugin_name)
            if not plugin:
                # 插件已被删除（目录或 manifest 已不存在）—— 删除清理段并入 reloader 分派
                # 仅清理该插件实际含有的组件（按 kernel.KNOWN_COMPONENTS 优先级遍历）
                logger.info(
                    f"[PluginHost] Plugin '{plugin_name}' removed, "
                    f"components={ {k for k, v in removed_components.items() if v} or {'(unknown)'} }, "
                    f"cleaning up artifacts..."
                )
                return self._cleanup_removed_plugin_components(plugin_name, removed_components, result, result_keys)

            # 2-N. 组件分派：查 kernel reloader 注册表（原 8 分支 if 已迁 builtin_reloaders）
            # 注册 / 注入 runtime 句柄由 _do_deferred + reload_plugin_subsystems 集中完成
            # （builtin_reloaders._BUILTIN_REGISTERED 幂等保护，此处不重复）
            from app.plugins.kernel import ReloadContext, get_reloader_registry

            registry = get_reloader_registry()

            if component == "__manifest__":
                # manifest 变更 = 组件清单可能增删，必须全组件重载以重新探测差异
                # rescan 已在函数前置（pm.rescan_plugin(plugin_name)）保证 plugin.components 是最新
                # 遍历按 COMPONENT_ORDER：agents 先 → tools/providers/team_templates 后（保 hooks/commands 联动标记自然置位）
                from app.plugins.kernel import COMPONENT_ORDER

                for comp in COMPONENT_ORDER:
                    if not plugin.has_component(comp):
                        continue
                    reloaded = registry.reload(
                        ReloadContext(
                            plugin_name=plugin_name,
                            plugin=plugin,
                            component=comp,
                            is_new_plugin=False,
                        )
                    )
                    if comp in result_keys:
                        result[comp] = reloaded if reloaded is not None else False
                    # agents 联动标记保持旧行为
                    if comp == "agents":
                        result["hooks"] = True
                        result["commands"] = True
                logger.info(
                    f"[PluginHost] Plugin '{plugin_name}' manifest changed, reloaded all components: "
                    f"{ {k: result[k] for k in result_keys} }"
                )
            elif component:
                # 统一守卫层：plugin 缺失或无该组件时跳过（对齐原 commands/ui 分支的 has_component 前置）
                if plugin is not None and not plugin.has_component(component):
                    logger.debug(f"[PluginHost] Plugin '{plugin_name}' has no '{component}' component, skip")
                else:
                    reloaded = registry.reload(
                        ReloadContext(
                            plugin_name=plugin_name,
                            plugin=plugin,
                            component=component,
                            is_new_plugin=False,
                        )
                    )
                    if component in result_keys:
                        result[component] = reloaded if reloaded is not None else False
                    # agents 联动标记保持旧行为：agents 变更 → hooks/commands 视为已处理
                    if component == "agents":
                        result["hooks"] = True
                        result["commands"] = True
                    logger.info(
                        f"[PluginHost] Plugin [{plugin_name}] reloaded via kernel: "
                        f"component={component}, outcome={reloaded} → {result}"
                    )
            else:
                # 🛡️ 根因修复（2026-08-23 bug fix）：component="" 原本仅在纯根文件变更
                # （README/LICENSE）时由 _watch_loop 传入。但 _identify_components_from_changes_fallback
                # 在路径索引过期（rescan 路径 vs plugin.path 不一致）时也会误传空字符串，
                # 导致有 ui 组件的插件（如 calendar / context-stats / project-dashboard）
                # 装/卸后 ui 组件不重载 → registry 里的 welcome tabs / 浮动卡片 残留陈旧快照
                # → 已打开标签页欢迎卡片消失、新建会话不出现。
                # 修复：component="" 且插件实际拥有 ui/tools 组件时，主动按组件重载，
                # 不再全跳过——否则 ui=False 路径走完后 _on_plugin_hot_reload
                # 完全不刷新已打开标签页（输入区插件按钮 / 欢迎卡片 / launcher 全不刷）。
                if plugin is None:
                    logger.debug(f"[PluginHost] Plugin [{plugin_name}] root change, plugin missing, skip")
                else:
                    _reloaded_any = False
                    for comp in ("ui", "tools"):
                        if not plugin.has_component(comp):
                            continue
                        try:
                            _r = registry.reload(
                                ReloadContext(
                                    plugin_name=plugin_name,
                                    plugin=plugin,
                                    component=comp,
                                    is_new_plugin=False,
                                )
                            )
                        except Exception as _re:
                            logger.warning(
                                f"[PluginHost] Plugin [{plugin_name}] root change, {comp} reload failed: {_re}"
                            )
                            continue
                        if comp in result_keys:
                            result[comp] = _r if _r is not None else False
                        if _r:
                            _reloaded_any = True
                            logger.info(
                                f"[PluginHost] Plugin [{plugin_name}] root change fallback "
                                f"reloaded component={comp} outcome={_r}"
                            )
                    if not _reloaded_any:
                        logger.debug(
                            f"[PluginHost] Plugin [{plugin_name}] root change, no ui/tools components to reload"
                        )

            logger.info(
                f"[PluginHost] Plugin [{plugin_name}] reloaded: "
                f"agents={result['agents']}, commands={result['commands']}, "
                f"themes={result['themes']}, skills={result['skills']}, "
                f"mcp={result['mcp']}, lsp={result.get('lsp', False)}, "
                f"ui={result.get('ui', False)}, tools={result.get('tools', False)}, "
                f"providers={result.get('providers', False)}, "
                f"team_templates={result.get('team_templates', False)}"
            )
        except Exception as e:
            logger.error(f"[PluginHost] Failed to reload plugin '{plugin_name}': {e}")

        return result

    def reload_plugin_targeted(self, plugin_name: str, action: Optional[str] = None) -> dict:
        """精准重载单个插件（UI 安装/更新/启用/禁用专用），不触发全量子系统重载

        与 reload_plugin_subsystems（全量：rescan + 所有 agents/hooks/commands/
        themes/skills/mcp/lsp/ui 重载）的区别：只处理目标插件，避免
        「卸载一个插件却把全部插件的 hooks 注销重注册、全部 agents 重载」。

        实现：复用 _reload_single_plugin 的 __manifest__ 分派——
        - 插件已不存在（禁用/卸载，目录已移走）→ rescan 后走删除段，仅清理该插件
          原有组件（agents/hooks/commands/lsp/ui/tools 按需精准清理）
        - 插件存在（安装/启用/更新）→ 遍历该插件全部组件精准加载
          （agents/hooks 按插件名精准，仅 commands 因全局注册表需全量重建）

        ★ 重载完成后主动 emit_plugin_changed：本方法由插件市场 Installer 调用
        （安装/更新/启停），不走 watcher 的 _on_hot_reload_requested（那条链路
        自己 emit）——若此处不广播，窗口收不到 ui=True，已打开标签页的输入区
        插件按钮/消息内容块全部不刷新（watcher 抑制解除后的 fallback 事件组件
        归类常为 root → ui=False，顶替不了本事件的刷新语义）。

        Args:
            action: PluginChanged hook 的动作语义（installer 传入精确值：
                installed/updated/uninstalled/enabled/disabled；None 时自动推断）
        """
        if not plugin_name:
            result = self.reload_plugin_subsystems()
        else:
            result = self._reload_single_plugin(plugin_name, "__manifest__")
        try:
            self.emit_plugin_changed(result, plugin_name, action=action)
        except Exception as e:
            logger.warning(f"[PluginHost] reload_plugin_targeted 广播失败: {e}")
        return result

    def reload_plugin_subsystems(self, force_full: bool = False) -> dict:
        """重载插件子系统（默认 diff 精准；force_full=True 走全量）

        默认行为（新增/移除/变更场景，全部调用方自动受益）：
        rescan 对比出 added/removed/changed 插件后**逐个精准处理**——
        - removed → 精准清理该插件实际含有的组件（agents/hooks/commands/lsp/ui/tools）
        - added/changed → 精准重载该插件全部组件（_reload_single_plugin "__manifest__"）
        不触碰无关插件的 agents/hooks/commands，避免「卸载一个插件却把全部插件的
        hooks 注销重注册、全部 agents 重载」的性能浪费；无变更时不重载任何子系统。

        force_full=True：设置面板「重载插件」按钮的显式语义——无论是否有变更，
        全量重载所有子系统（agents/hooks/commands/themes/skills/mcp/lsp/ui）。

        Returns:
            dict: 各子系统的重载结果；key 集合基于 kernel.KNOWN_COMPONENTS 动态生成，
            agents → int（数量），其余 → bool。新增组件类型时无需改此处。
        """
        from app.plugins.kernel import KNOWN_COMPONENTS as _KC

        # result / result_keys 基于 KNOWN_COMPONENTS 动态生成：新增组件类型零改动（Task 7）
        result: dict = {k: (0 if k == "agents" else False) for k in _KC}
        result_keys = tuple(_KC)

        # 表分派：内置 reloader 注册（幂等）+ runtime 句柄注入
        try:
            from app.plugins.builtin_reloaders import bind_runtime, register_builtin_reloaders
            from app.plugins.kernel import get_reloader_registry

            bind_runtime(self._agent_manager)
            register_builtin_reloaders(get_reloader_registry())
        except Exception as e:
            logger.error(f"[PluginHost] 内置 reloader 注册失败: {e}")

        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[PluginHost] PluginManager not initialized, cannot reload")
                return result

            # 1. 重新扫描插件目录，获取变更详情
            diff = pm.rescan()
            added = diff.get("added", [])
            removed = diff.get("removed", [])
            changed = diff.get("changed", [])

            if force_full:
                # ── 全量路径：设置面板「重载插件」显式语义 ──
                return self._reload_all_subsystems(pm, result, result_keys)

            # ── 精准路径：无变更不重载任何子系统 ──
            if not added and not removed and not changed:
                # 组件差异兑底（2026-08-28）：watcher 300ms 去抖会把同批后到的组件请求
                # 合并进本方法，而首拍组件的 rescan_plugin 已消费插件级 changed → 此处
                # diff 恒为空。精准型组件（tools/providers/ui）无自愈轮询，被吞后到重
                # 启前无人加载（实测：cron-tasks 同批 ui+tools+__manifest__，tools 漏载）。
                # 此处探测 manifest 声明 vs 运行时注册的差异并精准补载。
                missed = self._detect_unloaded_components(pm)
                if missed:
                    for pname, comps in missed.items():
                        for comp in comps:
                            logger.info(f"[PluginHost] 组件差异补载: [{pname}] ({comp})")
                            sub = self._reload_single_plugin(pname, comp)
                            self._merge_reload_result(result, sub, result_keys)
                    logger.info(f"[PluginHost] 组件差异补载完成: {missed}")
                else:
                    logger.debug("[PluginHost] 无插件变更，跳过子系统重载")
                return result

            logger.info(
                f"[PluginHost] 插件变更 diff: added={[p.name for p in added]}, "
                f"removed={[p.name for p in removed]}, changed={[p.name for p in changed]}"
            )

            # 2. removed：精准清理。注意 rescan 已把插件移出索引，
            #    组件信息必须取自 rescan 返回的 Plugin 对象（get_plugin 已反查不到，
            #    否则 removed_components 为空 → 卸载不干净）。
            for plugin in removed:
                self._cleanup_removed_plugin_components(plugin.name, dict(plugin.components), result, result_keys)

            # 3. added/changed：逐插件精准加载全部组件（__manifest__ 语义）
            for plugin in list(added) + list(changed):
                sub = self._reload_single_plugin(plugin.name, "__manifest__")
                self._merge_reload_result(result, sub, result_keys)

            logger.info(f"[PluginHost] 插件精准重载完成: { {k: result[k] for k in result_keys} }")
        except Exception as e:
            logger.error(f"[PluginHost] Failed to reload plugin subsystems: {e}")

        return result

    @staticmethod
    def _merge_reload_result(result: dict, sub: dict, result_keys: tuple) -> None:
        """合并单插件重载结果到汇总 result（agents 计数累加，其余置 True）"""
        for k in result_keys:
            v = sub.get(k)
            if not v:
                continue
            if k == "agents":
                result[k] = result.get(k, 0) + v
            else:
                result[k] = True

    # ------------------------------------------------------------------
    #  组件差异探测（精准型组件漏载兑底）
    # ------------------------------------------------------------------

    def _detect_unloaded_components(self, pm) -> Dict[str, List[str]]:
        """扫描 manifest 声明 vs 运行时已注册，返回漏载组件 {插件名: [组件名]}

        只探测可轻量判定的精准型组件（tools/providers/ui）：
        - runtime 系 7 类（storages/serializers/gateways/engines/model_adapters/
          loop_policies/hook_policies）自带 5s 轮询自愈，无需重复探测；
        - agents/hooks/commands 等全量/懒生效组件变更通常伴随插件级 changed，
          走上方精准路径即可。
        禁用插件跳过（其组件本就不该加载）。
        """
        missed: Dict[str, List[str]] = {}
        for plugin in pm.list_plugins():
            if not pm.is_enabled(plugin.name):
                continue
            declared = {c for c, v in (plugin.components or {}).items() if v}
            if not declared:
                continue
            lack = [c for c in sorted(declared) if self._component_registered(plugin.name, c) is False]
            if lack:
                missed[plugin.name] = lack
        return missed

    def _component_registered(self, plugin_name: str, comp: str) -> Optional[bool]:
        """单组件运行时注册探测。True=已注册 False=未注册 None=无法判定（不补载）"""
        try:
            if comp == "tools":
                from app.plugins.loaders.plugin_tool_loader import ensure_plugin_tool_watcher

                watcher = ensure_plugin_tool_watcher()
                if watcher is None:
                    return None
                if watcher._loaded.get(plugin_name):
                    return True
                # 兑底：_loaded 记忆失真时以注册表实际内容为准（对齐 unload_plugin 防御）
                return any(reg.source == f"plugin:{plugin_name}" for reg in watcher._registry.list())
            if comp == "providers":
                from app.plugins.loaders.provider_loader import ensure_provider_watcher

                watcher = ensure_provider_watcher()
                if watcher is None:
                    return None
                # ProviderWatcher 无自身记忆，以注册表实际内容为准（对齐 scan_now 语义）
                return f"plugin:{plugin_name}" in watcher._registry.provider_sources()
            if comp == "ui":
                from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

                return UIPluginRegistry.get_instance().is_loaded(plugin_name)
        except Exception as e:
            logger.debug(f"[PluginHost] 组件注册探测失败: {plugin_name}/{comp}: {e}")
        return None

    def _reload_all_subsystems(self, pm, result: dict, result_keys: tuple) -> dict:
        """全量重载所有子系统（设置面板「重载插件」显式语义，force_full=True）"""
        # 2. 重载 AgentManager（智能体 + hooks）
        if self._agent_manager:
            self._agent_manager.reload_agents()
            result["agents"] = len(self._agent_manager.list_agents(include_hidden=True))
            result["hooks"] = True

        # 3. 重载命令
        try:
            from app.core.builtin_commands import reload_all_commands

            reload_all_commands()
            result["commands"] = True
        except (ImportError, Exception) as e:
            logger.error(f"[PluginHost] Failed to reload commands: {e}")

        # 4. 重载主题
        try:
            from app.utils.config import update_theme_options
            from app.utils.theme_manager import theme_manager

            theme_manager.reload()
            update_theme_options()
            result["themes"] = True
        except (ImportError, Exception) as e:
            logger.error(f"[PluginHost] Failed to reload themes: {e}")

        # 5. 技能：PluginManager 已更新，UI 通过 get_local_skills() 懒加载
        result["skills"] = True

        # 6. MCP 配置：PluginManager 已更新，UI 通过 get_mcp_servers() 懒加载
        result["mcp"] = True

        # 7. LSP 配置：重新初始化 LspManager（停止旧服务 → 加载新配置 → 启动新服务）
        try:
            from app.core.lsp.lsp_manager import get_lsp_manager

            lsp_mgr = get_lsp_manager()
            lsp_configs = pm.get_lsp_configs()
            workdir = os.getcwd()
            from app.tools.mcp_tools import MCPClientManager

            workdir_src = MCPClientManager.get_instance()
            if workdir_src and getattr(workdir_src, "_workdir", None):
                workdir = str(workdir_src._workdir)
            lsp_mgr.initialize(workdir, lsp_configs)
            lsp_mgr.start_all_background()
            result["lsp"] = True
            logger.info(f"[PluginHost] LSP 全量重载完成，已注册 {len(lsp_mgr._clients)} 个服务器")
        except Exception as e:
            logger.error(f"[PluginHost] LSP 全量重载失败: {e}")

        # 8. UI 组件：全量 rescan 已在 _load_plugin_ui/_unload_plugin_ui 中处理，
        #    此处标记为 True 以通知 UI 刷新
        result["ui"] = True

        logger.info(
            f"[PluginHost] Plugin subsystems reloaded: agents={result['agents']}, "
            f"commands={result['commands']}, themes={result['themes']}, "
            f"skills={result['skills']}, mcp={result['mcp']}, lsp={result.get('lsp', False)}, "
            f"ui={result['ui']}, tools={result.get('tools', False)}, "
            f"providers={result.get('providers', False)}, "
            f"team_templates={result.get('team_templates', False)}"
        )
        return result

    # ========== MCP 自动发现 ==========

    def _discover_mcp_servers(self):
        """自动发现其他工具的 MCP 配置并保存到 user-custom 插件（仅首次运行生效）"""
        from app.plugins.managers.plugin_manager import PluginManager
        from app.utils.config import Settings

        cfg = Settings.get_instance()

        # 已处理过则跳过
        if cfg.mcp_discovered.value:
            return

        from app.tools.mcp_tools import discover_and_merge

        merged, new_ones = discover_and_merge()
        if new_ones:
            # 将发现的服务器写入 user-custom 插件
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for server_data in new_ones:
                    name = server_data.get("name", "")
                    if name:
                        pm.add_mcp_server(name, server_data)
                logger.info(f"[PluginHost] MCP 自动发现完成，导入 {len(new_ones)} 个新服务器")

        # 标记已处理
        cfg.set(cfg.mcp_discovered, True, save=True)

    # ========== ChatEngine 代理方法 ==========

    def _init_mcp_connections(self):
        """初始化 MCP 服务器连接（后台异步，不阻塞 UI）

        MCP 配置完全由插件驱动，从 PluginManager 获取。
        """
        from app.plugins.managers.plugin_manager import PluginManager
        from app.utils.config import Settings

        from app.tools.mcp_tools import MCPClientManager

        mcp_manager = MCPClientManager.get_instance()

        if mcp_manager.is_connected:
            logger.info("[PluginHost] MCP 已连接，复用现有连接")
            return

        cfg = Settings.get_instance()
        if not cfg.mcp_enabled.value:
            logger.info("[PluginHost] MCP 全局开关已关闭，跳过连接")
            return

        # 从 PluginManager 获取 MCP 服务器列表
        pm = PluginManager.get_instance()
        servers = pm.get_mcp_servers()
        if not servers:
            logger.info("[PluginHost] 无 MCP 服务器配置，跳过连接")
            return

        mcp_manager.connect_all_background(
            servers,
            on_done=lambda ok, total, failed: logger.info(
                f"[PluginHost] MCP 后台连接完成: {ok}/{total}" + (f", 失败: {failed}" if failed else "")
            ),
        )