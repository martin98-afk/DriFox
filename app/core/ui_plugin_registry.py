# -*- coding: utf-8 -*-
"""UI 插件注册表 — 管理 UI 组件（content renderer / message factory / floating card）

单例模式，与 AgentManager/MemoryManagerCore 一致。
插件通过 register_ui(registry) 在加载时注册组件。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from app.core.command_manager import CommandType  # noqa: F401


@dataclass(frozen=True)
class ContentRendererInfo:
    """自定义内容块渲染器

    Attributes:
        plugin_name: 所属插件名
        type_name: 内容块类型名（用于 content 中的 custom_type 字段）
        render_func: 渲染函数，签名 (data: dict, context) -> str(HTML)
        priority: 优先级（同 type_name 时高者覆盖低者）
        metadata: 附加元数据
    """

    plugin_name: str
    type_name: str
    render_func: Callable[[Dict[str, Any], Optional[Any]], str]
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageFactoryInfo:
    """消息元素工厂

    Attributes:
        plugin_name: 所属插件名
        name: 工厂名（用于调试）
        condition_func: 判断消息是否由此工厂处理 (message: dict) -> bool
        factory_func: 创建 widget  (message: dict, parent) -> QWidget
        priority: 优先级（高者优先尝试）
    """

    plugin_name: str
    name: str
    condition_func: Callable[[Dict[str, Any]], bool]
    factory_func: Callable[[Dict[str, Any], Any], Any]
    priority: int = 0


@dataclass(frozen=True)
class FloatingCardInfo:
    """浮动卡片注册信息

    Attributes:
        plugin_name: 所属插件名
        card_id: 卡片唯一 ID（同时也是自动注册的命令名）
        widget_class: QWidget 子类
        container: 容器位置 "top" | "bottom"
        title: 卡片标题（用于命令列表显示）
        default_visible: 默认是否可见
        metadata: 附加元数据
        context_provider: 可选，卡片专属上下文提供者。不传则使用全局 context_provider。
    """

    plugin_name: str
    card_id: str
    widget_class: type
    container: str
    title: str = ""
    default_visible: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    context_provider: Optional[Callable[[], Dict[str, Any]]] = None


class UIPluginRegistry:
    """UI 插件注册表（单例）"""

    _instance: Optional["UIPluginRegistry"] = None

    def __init__(self):
        self._content_renderers: Dict[str, ContentRendererInfo] = {}
        self._message_factories: List[MessageFactoryInfo] = []
        self._floating_cards: Dict[str, FloatingCardInfo] = {}
        self._loaded_plugins: set = set()
        self._main_widget: Optional[Any] = None  # 注入的主窗口引用（兼容旧代码，优先使用显式传参）
        self._card_widget_instances: Dict[str, Dict[str, Any]] = {}  # {window_id: {card_id: widget}} — per-window 隔离
        self._ui_command_names: set = set()  # 由 UI 插件注册的命令名集合
        self._context_provider: Optional[Callable[[], Dict[str, Any]]] = None  # 向后兼容，单例上下文提供者
        # 多窗口隔离：每个窗口独立上下文提供者 (window_id → provider)
        self._context_providers: Dict[str, Callable[[], Dict[str, Any]]] = {}
        # 多窗口隔离：window_id → main_widget 映射，用于 unload 时正确清理容器
        self._window_main_widgets: Dict[str, Any] = {}

    @classmethod
    def get_instance(cls) -> "UIPluginRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---- 内部注册表操作（Task 2 起填充）----

    def get_content_renderer(self, type_name: str) -> Optional[ContentRendererInfo]:
        return self._content_renderers.get(type_name)

    def register_content_renderer(
        self,
        plugin_name: str,
        type_name: str,
        render_func: Callable[[Dict[str, Any], Optional[Any]], str],
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册自定义内容块渲染器

        Args:
            plugin_name: 所属插件名
            type_name: 内容块类型名
            render_func: 渲染函数
            priority: 优先级（同 type_name 时高者覆盖低者）
            metadata: 附加元数据
        """
        if metadata is None:
            metadata = {}
        info = ContentRendererInfo(
            plugin_name=plugin_name,
            type_name=type_name,
            render_func=render_func,
            priority=priority,
            metadata=metadata,
        )
        existing = self._content_renderers.get(type_name)
        if existing is not None and existing.priority > priority:
            # 低优先级注册被忽略
            return
        self._content_renderers[type_name] = info

    def register_message_factory(
        self,
        plugin_name: str,
        name: str,
        condition_func: Callable[[Dict[str, Any]], bool],
        factory_func: Callable[[Dict[str, Any], Any], Any],
        priority: int = 0,
    ) -> None:
        """注册消息元素工厂

        Args:
            plugin_name: 所属插件名
            name: 工厂名（用于调试）
            condition_func: 判断消息是否由此工厂处理
            factory_func: 创建 widget  (message, parent) -> QWidget
            priority: 优先级（高者优先尝试）
        """
        self._message_factories.append(
            MessageFactoryInfo(
                plugin_name=plugin_name,
                name=name,
                condition_func=condition_func,
                factory_func=factory_func,
                priority=priority,
            )
        )

    def register_floating_card(
        self,
        plugin_name: str,
        card_id: str,
        widget_class: type,
        container: str,
        title: str = "",
        default_visible: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        context_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """注册浮动卡片

        Args:
            plugin_name: 所属插件名
            card_id: 卡片唯一 ID
            widget_class: QWidget 子类
            container: "top" | "bottom"
            title: 卡片标题
            default_visible: 默认是否可见
            metadata: 附加元数据
            context_provider: 可选，卡片专属上下文提供者。
                             不传则在显示时使用全局 context_provider。

        Side Effects:
            自动注册对应命令 /{card_id}（用户插件带命名空间前缀）
        """
        if container not in ("top", "bottom"):
            raise ValueError(f"container must be 'top' or 'bottom', got {container!r}")
        if metadata is None:
            metadata = {}
        info = FloatingCardInfo(
            plugin_name=plugin_name,
            card_id=card_id,
            widget_class=widget_class,
            container=container,
            title=title,
            default_visible=default_visible,
            metadata=metadata,
            context_provider=context_provider,
        )
        self._floating_cards[card_id] = info
        # 联动注册命令
        self._register_command_for_card(info)

    def _register_command_for_card(self, card_info: FloatingCardInfo) -> None:
        """为浮动卡片自动注册对应 FUNCTION 命令"""
        from app.core.command_manager import CommandManager, CommandType
        from app.core.builtin_commands import FunctionCommandHandlers

        # 命名空间规则：
        # - card_id 已含 ":"（如 "plug-a:mycard"）→ 直接使用
        # - card_id 是简单名且 plugin_name == "system" → 使用短名
        # - card_id 是简单名且不等于 plugin_name → 使用 plugin_name:card_id 形式
        # - card_id == plugin_name（如 "plugin-marketplace" 插件的 card_id 也是这个名字）→ 短名
        if ":" in card_info.card_id:
            cmd_name = card_info.card_id
        elif card_info.plugin_name == "system" or card_info.card_id == card_info.plugin_name:
            cmd_name = card_info.card_id
        else:
            cmd_name = f"{card_info.plugin_name}:{card_info.card_id}"

        cmd_mgr = CommandManager.get_instance()
        if cmd_mgr.has_command(cmd_name):
            return  # 命令已存在则不重复注册

        cmd_mgr.register(
            name=cmd_name,
            command_type=CommandType.FUNCTION,
            description=card_info.title or f"打开 {card_info.card_id}",
            argument_hint="",
        )
        self._ui_command_names.add(cmd_name)

        # 注册处理器：延迟到执行时获取 main_widget
        def _handler(args: str, cid=card_info.card_id):
            self._show_floating_card(cid)

        FunctionCommandHandlers.register(cmd_name, _handler)

    def toggle_floating_card(self, card_id: str, main_widget=None) -> None:
        """切换浮动卡片显示（公开的窗口级入口，供 Launcher / 外部触发器调用）

        内部直接复用 ``_show_floating_card()`` 的创建 + toggle 逻辑，
        不改变现有卡片缓存、互斥、关闭行为。已有斜杠命令继续走原 handler。

        Args:
            card_id: 卡片唯一 ID（与 ``FloatingCardInfo.card_id`` 一致）
            main_widget: 目标主窗口（多窗口隔离用）。None 时退回到
                         ``self._main_widget``（单例兼容路径）。
        """
        self._show_floating_card(card_id, main_widget=main_widget)

    def _show_floating_card(self, card_id: str, main_widget=None) -> None:
        """显示浮动卡片

        首次调用时自动创建 widget 实例、加入容器布局并注册到 CardManager。

        Args:
            card_id: 卡片唯一 ID
            main_widget: 目标主窗口实例（多窗口隔离用）。
                         不传则使用 self._main_widget（单例模式，多窗口会被覆盖）。
        """
        mw = main_widget or self._main_widget
        if mw is None:
            return
        card_manager = getattr(mw, "_card_manager", None)
        window_id = getattr(mw, "_window_id", None)
        if card_manager is None or window_id is None:
            return

        # 记录 window_id → main_widget 映射，供 unload 时清理容器使用
        self._window_main_widgets[window_id] = mw

        card_info = self._floating_cards.get(card_id)
        if card_info is None:
            return

        # 获取或创建 widget 实例（per-window 隔离缓存）
        win_instances = self._card_widget_instances.setdefault(window_id, {})
        widget = win_instances.get(card_id)
        if widget is None:
            from app.widgets.cards.card_manager import ContainerType

            # 确定容器类型
            container_type = ContainerType.TOP if card_info.container == "top" else ContainerType.BOTTOM
            # 获取正确的容器控件（TopCardContainer / BottomCardContainer）
            container_attr = "_top_card_container" if card_info.container == "top" else "_bottom_card_container"
            container = getattr(mw, container_attr, None)
            if container is None:
                return
            # 以容器为父级创建卡片 widget
            widget = card_info.widget_class(parent=container)

            # ==== 注入上下文提供函数（拉模型）====
            # 让卡片自己能在需要时（如 showEvent）调用此函数获取最新上下文，
            # 不再由 registry 在外部手动推数据。
            # 优先级：
            #   1. set_context_provider(provider) — 拉模型，卡片自行按需调用
            #   2. set_context(context)          — 推模型，向后兼容旧卡片
            #   3. widget._card_context           — 兜底属性
            ctx_provider = self._make_context_provider(card_info, window_id)
            if hasattr(widget, "set_context_provider") and callable(widget.set_context_provider):
                widget.set_context_provider(ctx_provider)
            elif hasattr(widget, "set_context") and callable(widget.set_context):
                # 旧卡片：首次创建时推一次初始上下文
                widget.set_context(ctx_provider())
            else:
                widget._card_context = ctx_provider()
                widget._card_context_provider = ctx_provider  # 也保留 provider 供有需要的卡片自行调用
            # ==== 注入结束 ====

            win_instances[card_id] = widget
            # 加入容器布局并注册到 CardManager
            container.add_card(card_id, widget)
            card_manager.register_card(window_id, container_type, card_id, widget)

            # 连接 closed 信号：手动关闭时同步 CardManager 状态
            # 避免再次 toggle 时 visible_cards 状态过时导致无法显示
            if hasattr(widget, "closed"):
                widget.closed.connect(lambda c=card_id, w=window_id: card_manager.hide_card(c, w))

            # 注册为系统卡片：显示时自动隐藏输入区域（与系统配置卡片行为一致）。
            # 仅当 main_widget 暴露该 API 时调用（旧版本/测试 stub 可安全降级）。
            # 幂等：register_system_card 内部用 set 去重，重复注册不会重复绑定回调。
            if hasattr(mw, "register_system_card"):
                mw.register_system_card(card_id)

        # toggle：显示/隐藏切换
        card_manager.toggle_card(card_id, window_id)

    def load_plugin(self, plugin_name: str, plugin_path) -> bool:
        """加载插件的 ui 组件

        Args:
            plugin_name: 插件名
            plugin_path: 插件根目录 Path

        Returns:
            True 加载成功；False 失败（不影响其他插件）
        """
        from loguru import logger

        if plugin_path is None:
            return False
        ui_init = plugin_path / "ui" / "__init__.py"
        if not ui_init.exists():
            return False

        # 先卸载旧版本
        if self.is_loaded(plugin_name):
            self.unload_plugin(plugin_name)

        try:
            import importlib.util
            import sys

            # 将连字符替换为下划线（Python 模块名不允许连字符）
            safe_name = plugin_name.replace("-", "_").replace(":", "_")
            ui_path = str(plugin_path / "ui")
            # 添加 ui 目录到 sys.path 以支持 from .renderers 等相对导入
            if ui_path not in sys.path:
                sys.path.insert(0, ui_path)
            module_name = f"ui_plugin_{safe_name}"

            # 清理字节码缓存（__pycache__），确保修改后的 .py 文件被重新编译，
            # 而不是使用旧的 .pyc 缓存（Python 的 mtime 检查在同一秒内可能失效）
            # 注意：此操作会导致父目录（如 ui/）产生 Change.modified 事件，
            # 加在 _watch_loop 中已通过过滤目录 modified 事件来防止误触发跨插件重载。
            from pathlib import Path as _Path

            ui_pycache = _Path(ui_path) / "__pycache__"
            if ui_pycache.exists():
                import shutil as _shutil

                _shutil.rmtree(str(ui_pycache))

            # 通知 import 系统所有缓存已失效
            importlib.invalidate_caches()

            # 清理所有子模块缓存（如 ui_plugin_xxx.cards），确保热重载时
            # cards.py 等被修改的文件能被重新导入，而不是命中 sys.modules 旧缓存
            prefix = f"{module_name}."
            for mod_name in list(sys.modules.keys()):
                if mod_name == module_name or mod_name.startswith(prefix):
                    del sys.modules[mod_name]
            spec = importlib.util.spec_from_file_location(module_name, ui_init)
            if spec is None or spec.loader is None:
                logger.error(f"[UIPluginRegistry] Failed to load spec for {plugin_name}")
                return False
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            register_func = getattr(module, "register_ui", None)
            if register_func is None:
                logger.error(
                    f"[UIPluginRegistry] Plugin {plugin_name} ui/__init__.py missing register_ui(registry) function"
                )
                return False
            register_func(self)
            self._loaded_plugins.add(plugin_name)
            logger.info(f"[UIPluginRegistry] Loaded UI components for plugin: {plugin_name}")
            return True
        except Exception as e:
            logger.error(f"[UIPluginRegistry] Failed to load UI for {plugin_name}: {e}")
            return False

    def reload_plugin(self, plugin_name: str, plugin_path) -> bool:
        """重新加载插件 UI（先卸载后加载）"""
        return self.load_plugin(plugin_name, plugin_path)

    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载插件 UI，清理所有该插件的注册"""
        from loguru import logger

        if plugin_name not in self._loaded_plugins:
            return False
        # 清理 content renderers
        self._content_renderers = {k: v for k, v in self._content_renderers.items() if v.plugin_name != plugin_name}
        # 清理 message factories
        self._message_factories = [f for f in self._message_factories if f.plugin_name != plugin_name]
        # 清理 floating cards + 对应命令
        cards_to_remove = [cid for cid, info in self._floating_cards.items() if info.plugin_name == plugin_name]
        for cid in cards_to_remove:
            self._unregister_command_for_card(cid)
            self._floating_cards.pop(cid, None)
            # 清理所有窗口中该 card_id 的 widget 实例（含容器布局移除 + CardManager 注销）
            for win_id, win_instances in list(self._card_widget_instances.items()):
                widget = win_instances.pop(cid, None)
                if widget is not None:
                    self._remove_widget_from_container(win_id, cid, widget)
        self._loaded_plugins.discard(plugin_name)
        logger.info(f"[UIPluginRegistry] Unloaded UI components for plugin: {plugin_name}")
        return True

    def _remove_widget_from_container(self, window_id: str, card_id: str, widget) -> None:
        """从容器布局和 CardManager 中移除指定 widget（不触发删除，仅 UI 清理）

        Args:
            window_id: 窗口 ID
            card_id: 卡片 ID
            widget: 要移除的 widget 控件
        """
        try:
            # 1. 从 CardManager 隐藏并解除注册
            mw = self._window_main_widgets.get(window_id)
            if mw is not None:
                card_manager = getattr(mw, "_card_manager", None)
                if card_manager is not None:
                    # 如果卡片当前可见，先隐藏
                    if card_manager.is_card_visible(card_id, window_id):
                        card_manager.hide_card(card_id, window_id)
                    # 从 CardManager 注册表中移除（不保留引用）
                    if hasattr(card_manager, "_window_data") and window_id in card_manager._window_data:
                        win_data = card_manager._window_data[window_id]
                        container_type = win_data.get("containers", {}).get(card_id)
                        if container_type:
                            win_data["cards"].get(container_type, {}).pop(card_id, None)
                            win_data["containers"].pop(card_id, None)

            # 2. 从容器布局移除
            parent_container = widget.parent()
            if parent_container is not None and hasattr(parent_container, "remove_card"):
                try:
                    parent_container.remove_card(card_id)
                except Exception:
                    pass

            # 3. 标记为待删除
            widget.setParent(None)
            widget.deleteLater()

            # ── 兜底恢复：强制检查并恢复输入区 ──
            # 场景：上述 hide_card 可能因 RuntimeError（widget 已被销毁）提前返回
            # 而未触发 hidden_callbacks（_on_system_card_closed），导致输入区永久隐藏。
            # 此处主动调用恢复逻辑，确保无论回调链是否断裂，输入区都能正确恢复。
            try:
                from loguru import logger

                if mw is not None and hasattr(mw, "_on_system_card_closed"):
                    # 先检查是否还有其他系统卡片可见
                    card_manager = getattr(mw, "_card_manager", None)
                    wnd_id = window_id
                    if card_manager is not None and wnd_id is not None:
                        all_closed = True
                        # 通过 mw._system_card_ids 获取系统卡片 ID 集合
                        sys_card_ids = getattr(mw, "_system_card_ids", None)
                        if sys_card_ids is not None:
                            for cid in sys_card_ids:
                                if card_manager.is_card_visible(cid, wnd_id):
                                    all_closed = False
                                    break
                        else:
                            all_closed = not card_manager.is_card_visible(card_id, wnd_id)
                        if all_closed and getattr(mw, "_system_cards_open", False):
                            mw._on_system_card_closed(card_id)
                            logger.debug(f"[UIPluginRegistry] 兜底恢复输入区（card={card_id}, window={window_id}）")
            except Exception:
                pass
        except RuntimeError:
            # widget 已被销毁，忽略
            pass

    def _unregister_command_for_card(self, card_id: str) -> None:
        """卸载浮动卡片对应的命令"""
        from app.core.command_manager import CommandManager
        from app.core.builtin_commands import FunctionCommandHandlers

        cmd_mgr = CommandManager.get_instance()
        # card_id 可能是 "plug-a:mycard" 或 "mycard"
        cmd_name = card_id
        cmd_mgr.unregister(cmd_name)
        FunctionCommandHandlers._handlers.pop(cmd_name, None)
        self._ui_command_names.discard(cmd_name)

    def load_all_enabled_plugins(self, plugin_dirs) -> int:
        """批量加载所有已启用的 UI 插件

        Args:
            plugin_dirs: List[Tuple[plugin_name, plugin_path]]

        Returns:
            成功加载的数量
        """
        count = 0
        for name, path in plugin_dirs:
            if self.load_plugin(name, path):
                count += 1
        return count

    def get_message_factories(self) -> List[MessageFactoryInfo]:
        """按 priority 降序返回"""
        return sorted(self._message_factories, key=lambda f: -f.priority)

    def get_floating_cards(self) -> Dict[str, FloatingCardInfo]:
        return dict(self._floating_cards)

    def is_loaded(self, plugin_name: str) -> bool:
        return plugin_name in self._loaded_plugins

    def list_loaded_plugins(self) -> List[str]:
        return sorted(self._loaded_plugins)

    def get_ui_command_names(self) -> set:
        """获取所有由 UI 插件注册的命令名集合"""
        return self._ui_command_names.copy()

    def set_main_widget(self, widget: Any) -> None:
        self._main_widget = widget

    def set_context_provider(self, provider: Callable[[], Dict[str, Any]], window_id: str = None) -> None:
        """设置上下文提供者（多窗口隔离：按 window_id 存储）

        UI 浮动卡片首次显示时，会调用此 provider 获取上下文 dict，
        然后通过 ``widget.set_context(context)`` 传递给卡片。

        Args:
            provider: 无参可调用对象，返回包含上下文键值对的 dict。
                      建议至少提供：project_root, project_name, session_id, window_id。
            window_id: 窗口唯一标识。传入时注册为窗口专属 provider；
                      不传则仅设置向后兼容的全局 provider。
        """
        if window_id:
            self._context_providers[window_id] = provider
        else:
            self._context_provider = provider

    def _build_card_context(self, card_info: FloatingCardInfo, window_id: str = None) -> Dict[str, Any]:
        """构建卡片上下文 dict

        优先级：卡片专属 context_provider > 窗口级 provider > 全局兼容 provider。
        最后叠加卡片元信息（plugin_name, card_id），不会被覆盖。

        Returns:
            上下文 dict（可能为空）
        """
        ctx: Dict[str, Any] = {}
        try:
            if card_info.context_provider is not None:
                ctx.update(card_info.context_provider())
            elif window_id and window_id in self._context_providers:
                ctx.update(self._context_providers[window_id]())
            elif self._context_provider is not None:
                ctx.update(self._context_provider())
        except Exception:
            pass
        # 卡片元信息始终注入
        ctx.setdefault("plugin_name", card_info.plugin_name)
        ctx.setdefault("card_id", card_info.card_id)
        return ctx

    def _make_context_provider(
        self, card_info: FloatingCardInfo, window_id: str = None
    ) -> Callable[[], Dict[str, Any]]:
        """创建一个上下文提供函数（闭包），供卡片按需拉取最新上下文

        返回的无参函数每次调用都会通过 _build_card_context 重新构建上下文，
        反映当前最新的 project_root / session_id / theme 等状态。

        多窗口隔离：window_id 用于查找对应窗口的专属 context_provider。

        新卡片应实现 ``set_context_provider(provider)`` 接口，
        在 showEvent / show_card 等时机自行调用 provider 获取最新上下文。
        """

        def _provider() -> Dict[str, Any]:
            return self._build_card_context(card_info, window_id)

        return _provider

    def re_register_all_commands(self) -> None:
        """重新注册所有浮动卡片命令到 CommandManager

        用于 register_all_commands / reload_all_commands 之后
        恢复 UI 插件命令（这些命令会被 reload 清空）。
        """
        for card_info in self._floating_cards.values():
            self._register_command_for_card(card_info)

    def clear_window_cards(self, window_id: str) -> None:
        """清空指定窗口的缓存卡片实例，下次显示时重新创建

        用于项目/会话切换等场景，确保卡片用最新的上下文重建。

        Args:
            window_id: 窗口 ID
        """
        win_instances = self._card_widget_instances.get(window_id, {})
        for card_id in list(win_instances.keys()):
            widget = win_instances.pop(card_id, None)
            if widget is not None:
                self._remove_widget_from_container(window_id, card_id, widget)

    def reset(self) -> None:
        """清空所有状态（仅供测试使用）"""
        self._content_renderers.clear()
        self._message_factories.clear()
        self._floating_cards.clear()
        self._loaded_plugins.clear()
        self._main_widget = None
        self._ui_command_names.clear()
        self._card_widget_instances.clear()
        self._context_provider = None
        self._window_main_widgets.clear()
        self._context_providers.clear()
        # 重置单例本身（建议）——让下一次 get_instance() 重新创建，
        # 避免测试间残留 _instance 上的实例属性
        UIPluginRegistry._instance = None
