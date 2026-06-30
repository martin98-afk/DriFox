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
        self._context_provider: Optional[Callable[[], Dict[str, Any]]] = None  # 全局上下文提供者

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

            # ===== 上下文注入：把 project_root / session_id 等喂进卡片 =====
            context = self._build_card_context(card_info)
            if context:
                if hasattr(widget, "set_context") and callable(widget.set_context):
                    widget.set_context(context)
                else:
                    # 兜底：通过属性注入（方便旧卡片兼容）
                    widget._card_context = context
            # ===== 注入结束 =====

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
            if module_name in sys.modules:
                del sys.modules[module_name]
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
            # 清理所有窗口中该 card_id 的 widget 实例
            for win_instances in self._card_widget_instances.values():
                win_instances.pop(cid, None)
        self._loaded_plugins.discard(plugin_name)
        logger.info(f"[UIPluginRegistry] Unloaded UI components for plugin: {plugin_name}")
        return True

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

    def set_context_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        """设置全局上下文提供者

        UI 浮动卡片首次显示时，会调用此 provider 获取上下文 dict，
        然后通过 ``widget.set_context(context)`` 传递给卡片。

        Args:
            provider: 无参可调用对象，返回包含上下文键值对的 dict。
                      建议至少提供：project_root, project_name, session_id, window_id。
        """
        self._context_provider = provider

    def _build_card_context(self, card_info: FloatingCardInfo) -> Dict[str, Any]:
        """构建卡片上下文 dict

        优先级：卡片专属 context_provider > 全局 context_provider。
        最后叠加卡片元信息（plugin_name, card_id），不会被覆盖。

        Returns:
            上下文 dict（可能为空）
        """
        ctx: Dict[str, Any] = {}
        try:
            if card_info.context_provider is not None:
                ctx.update(card_info.context_provider())
            elif self._context_provider is not None:
                ctx.update(self._context_provider())
        except Exception:
            pass
        # 卡片元信息始终注入
        ctx.setdefault("plugin_name", card_info.plugin_name)
        ctx.setdefault("card_id", card_info.card_id)
        return ctx

    def re_register_all_commands(self) -> None:
        """重新注册所有浮动卡片命令到 CommandManager

        用于 register_all_commands / reload_all_commands 之后
        恢复 UI 插件命令（这些命令会被 reload 清空）。
        """
        for card_info in self._floating_cards.values():
            self._register_command_for_card(card_info)

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
