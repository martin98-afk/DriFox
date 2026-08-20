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
class WelcomeTabInfo:
    """欢迎卡片插件 tab 注册信息

    Attributes:
        plugin_name: 所属插件名
        mode_key: tab 唯一标识（同时用作 welcome mode 值）
        label: tab 显示文本
        render_func: 渲染函数，签名 (context: dict) -> str(HTML 片段)
        priority: 优先级（同 mode_key 时高者覆盖低者）
        metadata: 附加元数据
    """

    plugin_name: str
    mode_key: str
    label: str
    render_func: Callable[[Dict[str, Any]], str]
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
        container: 容器位置 "top" | "bottom" | "left" | "right" | "full"
                  "full" 表示完整覆盖对话区（与系统配置卡片一致，走覆盖层）
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


@dataclass(frozen=True)
class SidebarItemInfo:
    """侧边栏插件项注册信息（Phase D：与 floating card 解耦的独立扩展点）

    Attributes:
        plugin_name: 所属插件名
        item_id: 项唯一 ID
        label: 侧边栏显示文本
        icon_path: 图标路径（可选，缺省用 label 首字）
        group: 分组 "system"（系统组，在前）| "custom"（自定义组，在后）
        default_visible: 默认是否可见
        priority: 优先级（同 item_id 时高者覆盖低者）
        on_click: 点击回调，签名 (context: dict) -> None
        metadata: 附加元数据
    """

    plugin_name: str
    item_id: str
    label: str
    icon_path: str = ""
    group: str = "custom"
    default_visible: bool = True
    priority: int = 0
    on_click: Optional[Callable[[Dict[str, Any]], None]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InputButtonInfo:
    """输入区插件按钮注册信息（Phase D）

    Attributes:
        plugin_name: 所属插件名
        button_id: 按钮唯一 ID
        icon_path: 图标路径
        tooltip: 悬停提示
        group: 分组（默认 "plugin"，用于与系统按钮分隔线区分）
        priority: 优先级（同 button_id 时高者覆盖低者）
        on_click: 点击回调，签名 (context: dict) -> None（context 含 window_id 等）
        metadata: 附加元数据
    """

    plugin_name: str
    button_id: str
    icon_path: str = ""
    tooltip: str = ""
    group: str = "plugin"
    priority: int = 0
    on_click: Optional[Callable[[Dict[str, Any]], None]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextMenuActionInfo:
    """右键菜单插件项注册信息（Phase D）

    Attributes:
        plugin_name: 所属插件名
        action_id: 菜单项唯一 ID（同 target 内唯一）
        target: 注入目标 "message_card"（消息卡片菜单）| "tab"（tab 标签菜单）
        label: 菜单显示文本
        action_func: 点击处理，签名 (context: dict) -> bool（False=处理完成关菜单）
        enabled_func: 可选置灰判断，签名 (context: dict) -> bool
        separator_before: 是否在本项前插入分隔线
        priority: 优先级（同 target+action_id 时高者覆盖低者）
        metadata: 附加元数据
    """

    plugin_name: str
    action_id: str
    target: str
    label: str
    action_func: Callable[[Dict[str, Any]], bool]
    enabled_func: Optional[Callable[[Dict[str, Any]], bool]] = None
    separator_before: bool = False
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SettingsCardInfo:
    """设置面板插件卡片注册信息（Phase D）

    Attributes:
        plugin_name: 所属插件名
        card_id: 卡片唯一 ID
        title: 卡片标题（分区内显示）
        widget_class: QWidget 子类（构造时无参，或支持 parent 参数）
        group: 分组（默认 "plugin"）
        priority: 优先级（同 card_id 时高者覆盖低者）
        metadata: 附加元数据
    """

    plugin_name: str
    card_id: str
    title: str
    widget_class: type
    group: str = "plugin"
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class UIPluginRegistry:
    """UI 插件注册表（单例）"""

    _instance: Optional["UIPluginRegistry"] = None

    def __init__(self):
        self._content_renderers: Dict[str, ContentRendererInfo] = {}
        self._message_factories: List[MessageFactoryInfo] = []
        self._floating_cards: Dict[str, FloatingCardInfo] = {}
        self._welcome_tabs: Dict[str, WelcomeTabInfo] = {}
        # Phase D：四类新扩展点（键为 item_id/button_id/action_id/card_id）
        self._sidebar_items: Dict[str, SidebarItemInfo] = {}
        self._input_buttons: Dict[str, InputButtonInfo] = {}
        self._context_actions: Dict[str, ContextMenuActionInfo] = {}
        self._settings_cards: Dict[str, SettingsCardInfo] = {}
        self._loaded_plugins: set = set()
        self._main_widget: Optional[Any] = None  # 注入的主窗口引用（兼容旧代码，优先使用显式传参）
        self._card_widget_instances: Dict[str, Dict[str, Any]] = {}  # {window_id: {card_id: widget}} — per-window 隔离
        self._ui_command_names: set = set()  # 由 UI 插件注册的命令名集合
        self._context_provider: Optional[Callable[[], Dict[str, Any]]] = None  # 向后兼容，单例上下文提供者
        # 多窗口隔离：每个窗口独立上下文提供者 (window_id → provider)
        self._context_providers: Dict[str, Callable[[], Dict[str, Any]]] = {}
        # 多窗口隔离：window_id → main_widget 映射，用于 unload 时正确清理容器
        self._window_main_widgets: Dict[str, Any] = {}
        # 欢迎卡片刷新 debounce：插件批量加载/卸载时合并为一次刷新，
        # 避免 N 个 welcome tab 插件逐个触发 N 次欢迎卡片重建（QWebEngineView
        # 每次 100-500ms 主线程占用）导致启动/热重载卡顿
        self._welcome_refresh_pending: bool = False

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

    def register_welcome_tab(
        self,
        plugin_name: str,
        mode_key: str,
        label: str,
        render_func: Callable[[Dict[str, Any]], str],
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册欢迎卡片插件 tab

        Args:
            plugin_name: 所属插件名
            mode_key: tab 唯一标识（同时用作 welcome mode 值，需避免与
                      系统内置 mode 冲突：sessions / projects / changelog）
            label: tab 显示文本（SegmentedWidget 上展示）
            render_func: 渲染函数，签名 (context: dict) -> str(HTML 片段)，
                         片段会拼进欢迎卡片 body 的 markdown 管线渲染
            priority: 优先级（同 mode_key 时高者覆盖低者）
            metadata: 附加元数据
        """
        if metadata is None:
            metadata = {}
        info = WelcomeTabInfo(
            plugin_name=plugin_name,
            mode_key=mode_key,
            label=label,
            render_func=render_func,
            priority=priority,
            metadata=metadata,
        )
        existing = self._welcome_tabs.get(mode_key)
        if existing is not None and existing.priority > priority:
            # 低优先级注册被忽略
            return
        self._welcome_tabs[mode_key] = info

    def get_welcome_tabs(self) -> Dict[str, WelcomeTabInfo]:
        """获取所有已注册的欢迎卡片插件 tab（插入序）"""
        return dict(self._welcome_tabs)

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

        Args:
            plugin_name: 所属插件名
            card_id: 卡片唯一 ID
            widget_class: QWidget 子类
            container: "top" | "bottom" | "left" | "right" | "full"
                       （Tab 模式下卡片挂在 Tab 窗口级全局容器的对应方位；
                         "full" 表示完整覆盖对话区，与系统配置卡片一致）
            title: 卡片标题
            default_visible: 默认是否可见
            metadata: 附加元数据
            context_provider: 可选，卡片专属上下文提供者。
                             不传则在显示时使用全局 context_provider。

        Side Effects:
            自动注册对应命令 /{card_id}（用户插件带命名空间前缀）
        """
        if container not in ("top", "bottom", "left", "right", "full"):
            raise ValueError(f"container must be one of 'top'/'bottom'/'left'/'right'/'full', got {container!r}")
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

    def register_sidebar_item(
        self,
        plugin_name: str,
        item_id: str,
        label: str,
        icon_path: str = "",
        group: str = "custom",
        default_visible: bool = True,
        priority: int = 0,
        on_click: Optional[Callable[[Dict[str, Any]], None]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册侧边栏插件项（Phase D 独立扩展点，与 floating card 解耦）"""
        if metadata is None:
            metadata = {}
        info = SidebarItemInfo(
            plugin_name=plugin_name,
            item_id=item_id,
            label=label,
            icon_path=icon_path,
            group=group,
            default_visible=default_visible,
            priority=priority,
            on_click=on_click,
            metadata=metadata,
        )
        existing = self._sidebar_items.get(item_id)
        if existing is not None and existing.priority > priority:
            return
        self._sidebar_items[item_id] = info

    def get_sidebar_items(self) -> List[SidebarItemInfo]:
        """获取全部侧边栏插件项（group 排序：system 在前，custom 在后；同组按注册序）"""
        system = [v for v in self._sidebar_items.values() if v.group == "system"]
        custom = [v for v in self._sidebar_items.values() if v.group != "system"]
        return system + custom

    def register_input_button(
        self,
        plugin_name: str,
        button_id: str,
        icon_path: str = "",
        tooltip: str = "",
        group: str = "plugin",
        priority: int = 0,
        on_click: Optional[Callable[[Dict[str, Any]], None]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册输入区插件按钮（Phase D）"""
        if metadata is None:
            metadata = {}
        info = InputButtonInfo(
            plugin_name=plugin_name,
            button_id=button_id,
            icon_path=icon_path,
            tooltip=tooltip,
            group=group,
            priority=priority,
            on_click=on_click,
            metadata=metadata,
        )
        existing = self._input_buttons.get(button_id)
        if existing is not None and existing.priority > priority:
            return
        self._input_buttons[button_id] = info

    def get_input_buttons(self) -> List[InputButtonInfo]:
        """获取全部输入区插件按钮（注册序）"""
        return list(self._input_buttons.values())

    def register_context_menu_action(
        self,
        plugin_name: str,
        action_id: str,
        target: str,
        label: str,
        action_func: Callable[[Dict[str, Any]], bool],
        enabled_func: Optional[Callable[[Dict[str, Any]], bool]] = None,
        separator_before: bool = False,
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册右键菜单插件项（Phase D，target ∈ {"message_card", "tab"}）"""
        if metadata is None:
            metadata = {}
        key = f"{target}:{action_id}"
        info = ContextMenuActionInfo(
            plugin_name=plugin_name,
            action_id=action_id,
            target=target,
            label=label,
            action_func=action_func,
            enabled_func=enabled_func,
            separator_before=separator_before,
            priority=priority,
            metadata=metadata,
        )
        existing = self._context_actions.get(key)
        if existing is not None and existing.priority > priority:
            return
        self._context_actions[key] = info

    def get_context_actions(self, target: str) -> List[ContextMenuActionInfo]:
        """获取指定 target 的菜单插件项（注册序；separator_before 为渲染标记）"""
        return [v for k, v in self._context_actions.items() if k.startswith(f"{target}:")]

    def register_settings_card(
        self,
        plugin_name: str,
        card_id: str,
        title: str,
        widget_class: type,
        group: str = "plugin",
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册设置面板插件卡片（Phase D）"""
        if metadata is None:
            metadata = {}
        info = SettingsCardInfo(
            plugin_name=plugin_name,
            card_id=card_id,
            title=title,
            widget_class=widget_class,
            group=group,
            priority=priority,
            metadata=metadata,
        )
        existing = self._settings_cards.get(card_id)
        if existing is not None and existing.priority > priority:
            return
        self._settings_cards[card_id] = info

    def get_settings_cards(self) -> List[SettingsCardInfo]:
        """获取全部设置面板插件卡片（注册序）"""
        return list(self._settings_cards.values())

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

    def _resolve_global_host(self):
        """获取 Tab 管理器全局卡片宿主（Tab 模式下浮动卡片统一挂这里）

        Returns:
            TabManagerWindow 实例（具备 _card_manager/_window_id/四向容器属性），
            不可用时返回 None（回退到 per-window 模式）。
        """
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None and getattr(tm, "_card_manager", None) is not None:
                return tm
        except Exception:
            pass
        return None

    def move_floating_card(self, card_id: str, container: str, main_widget=None) -> bool:
        """动态移动浮动卡片到另一方位（top/bottom/left/right/full）

        实现方式：更新注册信息 → 销毁旧实例 → 若原本可见则在新方位立即重建显示。
        卡片 widget 会以新容器为父级重新创建（带新方位的展开动画）。

        Args:
            card_id: 卡片唯一 ID
            container: 目标方位 "top" | "bottom" | "left" | "right" | "full"
                      "full" 表示完整覆盖对话区（与系统配置卡片一致，走覆盖层）
            main_widget: 目标主窗口（仅 per-window 回退模式需要）

        Returns:
            True 移动成功；False 卡片未注册或方位非法
        """
        if container not in ("top", "bottom", "left", "right", "full"):
            return False
        info = self._floating_cards.get(card_id)
        if info is None:
            return False
        if info.container == container:
            return True

        from dataclasses import replace as _dc_replace

        self._floating_cards[card_id] = _dc_replace(info, container=container)

        # 记录迁移前是否可见（任一窗口）
        was_visible = False
        host = self._resolve_global_host()
        if host is not None:
            cm = getattr(host, "_card_manager", None)
            wid = getattr(host, "_window_id", None)
            if cm is not None and wid is not None:
                was_visible = cm.is_card_visible(card_id, wid)
        if not was_visible:
            for win_id, mw in list(self._window_main_widgets.items()):
                cm = getattr(mw, "_card_manager", None)
                if cm is not None and cm.is_card_visible(card_id, win_id):
                    was_visible = True
                    break

        # 销毁所有已创建实例（下次显示时按新方位重建）
        for win_id, win_instances in list(self._card_widget_instances.items()):
            widget = win_instances.pop(card_id, None)
            if widget is not None:
                self._remove_widget_from_container(win_id, card_id, widget)

        if was_visible:
            self._show_floating_card(card_id, main_widget=main_widget)
        return True

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

        Tab 模式：卡片统一挂在 TabManagerWindow 的四向全局容器
        （GLOBAL_WINDOW_ID 作用域），不再绑定单个对话窗口；
        上下文通过 provider 动态解析到当前活跃对话窗口。
        TabManagerWindow 不可用时回退到 per-window 模式（main_widget）。

        Args:
            card_id: 卡片唯一 ID
            main_widget: 回退模式的目标主窗口实例（多窗口隔离用）。
        """
        host = self._resolve_global_host()
        mw = host or main_widget or self._main_widget
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

            # 确定容器类型：
            # - "full" 表示完整覆盖对话区：映射到 TOP 覆盖层容器（Tab 模式下
            #   _top_card_container 即 _global_top_container，已启用 overlay
            #   覆盖层模式，与系统配置卡片行为一致）；per-window 模式回退到 top。
            # - 其余 top/bottom/left/right 与 ContainerType 值一一对应。
            if card_info.container == "full":
                container_type = ContainerType.TOP
            else:
                try:
                    container_type = ContainerType(card_info.container)
                except ValueError:
                    container_type = ContainerType.TOP
            # 获取对应方位的容器控件（命名约定：_{方位}_card_container）
            container = getattr(mw, f"_{container_type.value}_card_container", None)
            if container is None:
                # 回退：旧对话窗口没有 left/right 容器时挂到 top
                container_type = ContainerType.TOP
                container = getattr(mw, "_top_card_container", None)
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
            card_manager.register_card(window_id, container_type, card_id, widget, system_card=True)

            # 连接 closed 信号：手动关闭时同步 CardManager 状态
            # 避免再次 toggle 时 visible_cards 状态过时导致无法显示
            if hasattr(widget, "closed"):
                widget.closed.connect(lambda c=card_id, w=window_id: card_manager.hide_card(c, w))

            # 注册为系统卡片：显示时自动隐藏输入区域（与系统配置卡片行为一致）。
            # 仅当 main_widget 暴露该 API 时调用（旧版本/测试 stub 可安全降级）。
            # 幂等：register_system_card 内部用 set 去重，重复注册不会重复绑定回调。
            if hasattr(mw, "register_system_card"):
                mw.register_system_card(card_id)

        # ── 显式关闭命令卡片和文件卡片 ──
        # UI 插件卡片以 system_card 身份打开时，CardManager 的系统卡片分支
        # 会通过 _hide_same_container_cards 或跨容器遍历隐藏它们，但由于
        # CommandCard 没有 hide_card 方法，hide_card 仅调 setVisible(False)
        # 而不清理内部 _visible 状态。此处直接调用 dismiss() 确保彻底关闭。
        for pc in ("command", "file_mention"):
            if card_manager.is_card_visible(pc, window_id):
                card_manager.hide_card(pc, window_id)
        if hasattr(mw, "_command_card") and mw._command_card.is_card_visible:
            mw._command_card.dismiss()
        if hasattr(mw, "_file_mention_card") and mw._file_mention_card.is_card_visible:
            mw._file_mention_card.dismiss()

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

                # Windows 上旧模块对象可能仍持有 .pyc 句柄（热重载循环中上一轮
                # 实例未 GC），rmtree 会抛 WinError 32。降级：忽略删除失败，
                # 依赖下方 importlib.invalidate_caches + 重新导入的 mtime 校验；
                # 不能让 UI 加载因缓存清理失败而中断。
                try:
                    _shutil.rmtree(str(ui_pycache))
                except OSError as e:
                    logger.warning(f"[UIPluginRegistry] 清理 {ui_pycache} 失败，降级处理（不影响导入）: {e}")

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
            # 仅当该插件注册了欢迎卡片 tab 时才刷新欢迎卡片；
            # 普通 UI 插件（渲染器/悬浮卡/命令）不触碰欢迎卡片缓存，
            # 避免每次加载/刷新 UI 插件都重建欢迎卡片。
            before_tabs = {k for k, v in self._welcome_tabs.items() if v.plugin_name == plugin_name}
            register_func(self)
            self._loaded_plugins.add(plugin_name)
            logger.info(f"[UIPluginRegistry] Loaded UI components for plugin: {plugin_name}")
            after_tabs = {k for k, v in self._welcome_tabs.items() if v.plugin_name == plugin_name}
            if before_tabs != after_tabs:
                self._schedule_welcome_refresh()
            return True
        except Exception as e:
            logger.error(f"[UIPluginRegistry] Failed to load UI for {plugin_name}: {e}")
            return False

    def reload_plugin(self, plugin_name: str, plugin_path) -> bool:
        """重新加载插件 UI（先卸载后加载）"""
        return self.load_plugin(plugin_name, plugin_path)

    def _has_any_registration(self, plugin_name: str) -> bool:
        """该插件是否在任何 UI 扩展点注册表中留有条目。

        用于 unload_plugin 的幂等判定：无 ui/ 组件但注册过 config_schema
        自动设置卡的插件（如 gateway 平台插件）不在 _loaded_plugins，
        但其 settings card 须能被卸载清理，故不能仅凭 _loaded_plugins 拦截。
        """
        return (
            any(v.plugin_name == plugin_name for v in self._content_renderers.values())
            or any(f.plugin_name == plugin_name for f in self._message_factories)
            or any(v.plugin_name == plugin_name for v in self._welcome_tabs.values())
            or any(v.plugin_name == plugin_name for v in self._floating_cards.values())
            or any(v.plugin_name == plugin_name for v in self._sidebar_items.values())
            or any(v.plugin_name == plugin_name for v in self._input_buttons.values())
            or any(v.plugin_name == plugin_name for v in self._context_actions.values())
            or any(v.plugin_name == plugin_name for v in self._settings_cards.values())
        )

    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载插件 UI，清理所有该插件的注册

        支持插件可选卸载回调：若插件模块定义了 ``unload_ui(registry)``，
        在清理注册表前调用，用于释放子进程/线程/资源（如代理池子进程）。
        热重载路径（load_plugin → unload_plugin → 重新加载）中，
        旧模块此时仍在 sys.modules，回调可访问旧模块的单例与子进程句柄。
        """
        from loguru import logger

        # 原逻辑以 ``not in _loaded_plugins`` 为前置拦截；但 config_schema 的
        # 自动设置卡（E1）在扫描阶段即注册，独立于 ui/ 组件加载——gateway 平台
        # 插件无 ui/ 目录、从不在 _loaded_plugins，卸载时若仅凭此拦截会零清理，
        # 残留空卡片「插件配置」。故改为：仅当确无任何注册项且未 loaded 时才
        # 视为已卸载（幂等返回 False）。
        if plugin_name not in self._loaded_plugins and not self._has_any_registration(plugin_name):
            return False
        # 0) 调用插件可选 unload_ui 回调（先于注册表清理，便于释放外部资源）
        try:
            import sys as _sys

            safe_name = plugin_name.replace("-", "_").replace(":", "_")
            old_module = _sys.modules.get(f"ui_plugin_{safe_name}")
            if old_module is not None:
                unload_ui = getattr(old_module, "unload_ui", None)
                if callable(unload_ui):
                    unload_ui(self)
        except Exception as e:
            logger.warning(f"[UIPluginRegistry] unload_ui 回调失败 ({plugin_name}): {e}")
        # 清理 content renderers
        self._content_renderers = {k: v for k, v in self._content_renderers.items() if v.plugin_name != plugin_name}
        # 清理 message factories
        self._message_factories = [f for f in self._message_factories if f.plugin_name != plugin_name]
        # 记录该插件是否注册过欢迎卡片 tab（决定卸载后是否刷新欢迎卡片）
        had_welcome_tabs = any(v.plugin_name == plugin_name for v in self._welcome_tabs.values())
        # 清理 welcome tabs
        self._welcome_tabs = {k: v for k, v in self._welcome_tabs.items() if v.plugin_name != plugin_name}
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
        # 清理 Phase D 四类扩展点注册
        self._sidebar_items = {k: v for k, v in self._sidebar_items.items() if v.plugin_name != plugin_name}
        self._input_buttons = {k: v for k, v in self._input_buttons.items() if v.plugin_name != plugin_name}
        self._context_actions = {k: v for k, v in self._context_actions.items() if v.plugin_name != plugin_name}
        self._settings_cards = {k: v for k, v in self._settings_cards.items() if v.plugin_name != plugin_name}
        self._loaded_plugins.discard(plugin_name)
        logger.info(f"[UIPluginRegistry] Unloaded UI components for plugin: {plugin_name}")
        if had_welcome_tabs:
            self._schedule_welcome_refresh()
        return True

    def _schedule_welcome_refresh(self) -> None:
        """延迟合并欢迎卡片刷新（debounce）

        插件批量加载/卸载（load_all_enabled_plugins / 插件市场批量操作）会逐个
        触发 load_plugin/unload_plugin，若每个注册 welcome tab 的插件都立即
        _refresh_welcome_cards，会对所有窗口执行 N 次欢迎卡片重建
        （QWebEngineView 每次 100-500ms 主线程占用）→ 启动/热重载卡顿。

        用 QTimer.singleShot(0) 把同一事件循环批次内的多次请求合并为一次刷新：
        同步 for 循环全部跑完后，事件循环才执行单次刷新回调。
        """
        if self._welcome_refresh_pending:
            return
        self._welcome_refresh_pending = True
        try:
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, self._flush_welcome_refresh)
        except Exception:
            # 无 Qt 事件循环环境（测试等）：同步兜底刷新
            self._flush_welcome_refresh()

    def _flush_welcome_refresh(self) -> None:
        """执行合并后的欢迎卡片刷新（singleShot 回调 / 同步兜底）"""
        self._welcome_refresh_pending = False
        try:
            self._refresh_welcome_cards()
        except Exception:
            pass

    def _refresh_welcome_cards(self) -> None:
        """插件 UI 加载/卸载后刷新所有窗口的欢迎卡片

        欢迎卡片的 tabs 在卡片创建时从 registry 一次性构建（
        MessageCard._build_welcome_mode_tabs）；若卡片先于插件 UI 加载
        创建（插件热重载 / 启用 / 加载顺序变化），tabs 不会自动更新。
        此处对已缓存卡片的窗口：失效缓存（下次显示重建），当前正显示
        欢迎卡片的窗口立即重建。尚无缓存卡片的窗口（正常启动路径：
        卡片在插件加载后才创建）无需处理。
        """
        for mw in list(self._window_main_widgets.values()):
            try:
                if not hasattr(mw, "_invalidate_welcome_card"):
                    continue
                window_id = getattr(mw, "_window_id", None)
                if window_id is None or window_id not in getattr(mw, "_welcome_card_cache", {}):
                    continue  # 尚无缓存卡片，正常启动路径无需刷新
                mw._invalidate_welcome_card()
                if getattr(mw, "_displayed_session_id", None) is None:
                    # 当前正显示欢迎卡片（无会话上下文）→ 立即重建，避免空白。
                    # 走交错时间片调度（_schedule_initial_welcome），避免 N 个窗口
                    # 的 QWebEngineView 重建（100-500ms/个）在同一事件批次连续
                    # 同步执行卡死 UI（对齐 _create_new_session 的 C2 优化）。
                    if hasattr(mw, "_schedule_initial_welcome"):
                        mw._schedule_initial_welcome()
                    else:
                        mw._show_initial_welcome()
            except Exception:
                pass

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
        # 注册到窗口映射（_refresh_welcome_cards 等遍历依赖）。_window_main_widgets
        # 语义为"所有窗口"（unregister_window 按 window_id 清理，见其 docstring），
        # 但历史上唯一注册点是 _show_floating_card——仅显示过浮动卡片的窗口在册，
        # 导致 Tab 子窗口 / 未显示浮动卡片的窗口在插件 UI 加载/卸载时欢迎卡片不刷新。
        # 窗口 __init__ 即注册（main_widget L3166 调用点），生命周期仍由
        # unregister_window（closeEvent）清理。
        try:
            wid = getattr(widget, "_window_id", None)
            if wid:
                self._window_main_widgets[wid] = widget
        except Exception:
            pass

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
            else:
                # 全局作用域（Tab 级卡片）：动态解析当前活跃对话窗口的 provider，
                # 切 Tab 后卡片拉到的是新活跃窗口的 project_root/session_id
                active_provider = self._resolve_active_window_provider()
                if active_provider is not None:
                    ctx.update(active_provider())
                elif self._context_provider is not None:
                    ctx.update(self._context_provider())
        except Exception:
            pass
        # 卡片元信息始终注入
        ctx.setdefault("plugin_name", card_info.plugin_name)
        ctx.setdefault("card_id", card_info.card_id)

        # 注入插件图标路径（供卡片在头部/标题等位置展示）
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            pi = pm.get_plugin(card_info.plugin_name)
            if pi and pi.icon_config:
                icon_info = {}
                for theme in ("light", "dark"):
                    p = pi.icon_config.get(theme)
                    if p:
                        icon_info[theme] = str(p)
                if icon_info:
                    ctx["plugin_icon"] = icon_info
        except Exception:
            pass

        return ctx

    def _resolve_active_window_provider(self) -> Optional[Callable[[], Dict[str, Any]]]:
        """解析当前活跃对话窗口的上下文 provider（全局卡片作用域用）

        Returns:
            活跃窗口的 provider；Tab 管理器不可用或无对应 provider 时返回 None
        """
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is None:
                return None
            active = tm.get_current_window()
            if active is None:
                return None
            wid = getattr(active, "_window_id", None)
            if wid and wid in self._context_providers:
                return self._context_providers[wid]
        except Exception:
            pass
        return None

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

    def unregister_window(self, window_id: str) -> None:
        """窗口关闭时注销该窗口的全部 UI 插件状态，释放窗口引用（泄漏修复 P0）。

        窗口 __init__ 会调用 set_main_widget(self) + set_context_provider(
        self._build_ui_context, self._window_id)——provider 闭包持有窗口引用，
        且 _window_main_widgets / _card_widget_instances 也按 window_id 缓存
        窗口引用。closeEvent 若不清理，这些注册表会持续强引用已关闭的窗口
        对象树，导致 C++ deleteLater 后 Python 对象无法回收。

        清理项：
        - _context_providers[window_id]：上下文提供者闭包（持有窗口引用）
        - _window_main_widgets[window_id]：窗口主 widget 引用
        - _card_widget_instances[window_id]：该窗口的浮动卡片 widget 实例
        - _main_widget：单例兼容路径，若指向该窗口则置 None

        Args:
            window_id: 窗口 ID（OpenAIChatToolWindow._window_id）
        """
        self._context_providers.pop(window_id, None)
        self._window_main_widgets.pop(window_id, None)
        self._card_widget_instances.pop(window_id, None)
        if self._main_widget is not None:
            try:
                wid = getattr(self._main_widget, "_window_id", None)
                if wid == window_id:
                    self._main_widget = None
            except Exception:
                pass

    def reset(self) -> None:
        """清空所有状态（仅供测试使用）"""
        self._content_renderers.clear()
        self._message_factories.clear()
        self._floating_cards.clear()
        self._welcome_tabs.clear()
        self._loaded_plugins.clear()
        self._main_widget = None
        self._ui_command_names.clear()
        self._card_widget_instances.clear()
        self._context_provider = None
        self._window_main_widgets.clear()
        self._context_providers.clear()
        self._welcome_refresh_pending = False
        # 重置单例本身（建议）——让下一次 get_instance() 重新创建，
        # 避免测试间残留 _instance 上的实例属性
        UIPluginRegistry._instance = None
