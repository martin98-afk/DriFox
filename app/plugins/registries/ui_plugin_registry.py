# -*- coding: utf-8 -*-
"""UI 插件注册表 — 管理 UI 组件（content renderer / message factory / floating card）

单例模式，与 AgentManager/MemoryManagerCore 一致。
插件通过 register_ui(registry) 在加载时注册组件。
"""

import os
import re
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

if TYPE_CHECKING:
    from app.core.command_manager import CommandType  # noqa: F401

# re-export：让 `from app.plugins.registries.ui_plugin_registry import WorkspacePageInfo` 直接可用
from app.plugins.contracts.ui_page import WorkspacePageInfo as WorkspacePageInfo  # noqa: E402,F401


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
class TagRendererInfo:
    """消息文本内联标签渲染器

    LLM 输出文本中的 <tag>...</tag> 块（如 assistant_hub 人格的
    <mood> 内心独白），由插件注册渲染函数转成卡片 HTML 展示。
    未注册的 tag 保持默认行为（当作普通文本）。

    Attributes:
        plugin_name: 所属插件名
        tag_name: 标签名（小写；对应文本中的 <tag_name>...</tag_name>）
        render_func: 渲染函数，签名 (content: str, ctx: dict) -> str(HTML)。
                     content 为标签内文本；ctx 含 tag/completed/compact。
                     输出需双端兼容：QWebEngineView（全 HTML/CSS）与
                     QLabel 富文本（QTextDocument 子集，无 border-radius/flex，
                     卡片建议单格 <table> 写法）。
        priority: 优先级（同 tag_name 时高者覆盖低者）
        metadata: 附加元数据
    """

    plugin_name: str
    tag_name: str
    render_func: Callable[[str, Dict[str, Any]], str]
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FenceRendererInfo:
    """消息正文 fence 代码块渲染器

    让插件注册一种 ```<lang> fence 的渲染方式。宿主内置的 echarts / mermaid /
    svg / html **不迁移**（它们是协议的参考实现），且同名时内置优先——插件
    无法劫持内置类型。

    Attributes:
        plugin_name: 所属插件名
        lang: fence 语言标记（小写规范化）
        render_func: 渲染函数，签名 (code: str, ctx: dict) -> str(HTML 片段)。
                     ctx 含 theme_is_dark / card_id / window_id / message_id /
                     fence_index。须为纯函数（在后台渲染线程调用，禁止触碰
                     Qt widget）。
        streaming_placeholder: 流式半截 fence 的占位；str 或
                     callable(半截源码) -> str。缺省用宿主的通用图表骨架。
        priority: 优先级（同 lang 时高者覆盖低者）
        assets: 相对插件根路径的资源声明，形如
                {"js": "ui/assets/fence/renderer.js", "css": "..."}。
                宿主按卡片实际用到的 fence 按需注入。
        bridge_permissions: 桥权限声明，取值见 FENCE_BRIDGE_PERMISSIONS。
                未声明的方法在页面中为 undefined。
        metadata: 附加元数据
    """

    plugin_name: str
    lang: str
    render_func: Callable[[str, Dict[str, Any]], str]
    streaming_placeholder: Optional[Any] = None
    priority: int = 0
    assets: Dict[str, str] = field(default_factory=dict)
    bridge_permissions: Tuple[str, ...] = ()
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
class WelcomeActionInfo:
    """欢迎卡片插件点击动作注册信息

    欢迎卡片 HTML 内的 .context-tag 元素携带自定义 data-type 时，主程序
    handle_recommended_question 把未知 action 派发到此处注册的 handler，
    让欢迎 tab 插件能实现自定义交互（如 marketplace-recommend 的点击安装）。

    Attributes:
        plugin_name: 所属插件名
        action: 动作名（与 .context-tag 的 data-type 一致）
        handler: 回调，签名 (content: str, ctx: dict) -> None；
                 ctx 含 window_id / main_widget 等窗口上下文
        metadata: 附加元数据
    """

    plugin_name: str
    action: str
    handler: Callable[[str, Dict[str, Any]], None]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MentionProviderInfo:
    """输入框 @ 提及条目提供者注册信息

    提供者向 @ 卡片顶部贡献「非文件」类提及条目（如 assistant_hub 的
    智能体角色）。主程序 file_mention_card 只负责渲染与选中，选中后的
    语义行为（临时切换助手等）由 on_selected 接手。

    Attributes:
        plugin_name: 所属插件名
        provider_id: 提供者唯一标识
        list_func: 条目提供回调 list_func() -> List[dict]，每项含：
                   key(str 唯一键) / name(str 显示名) / description(str 描述，可空)
                   / icon_path(str 头像路径，可空) / color(str 主色，可空)
        on_selected: 选中回调 (entry: dict, ctx: dict) -> None；
                     entry 为 list_func 返回的单项，ctx 含 window_id / main_widget /
                     session_id（当前会话，可能为空）
        metadata: 附加元数据
    """

    plugin_name: str
    provider_id: str
    list_func: Callable[[], List[Dict[str, Any]]]
    on_selected: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None
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
        metadata: 附加元数据；支持 hide_sidebar=True（卡片不进 Tab 侧边栏
                  插件列表，仅经命令/输入按钮/代码弹出——如 autoloop 双卡）
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
class TitlebarTabInfo:
    """标题栏常驻 tab 注册信息

    常驻 tab 显示在无边框窗口标题栏中央 tab 区（「聊天」tab 右侧），
    无 × 关闭钮；点击仅触发插件回调自展示，主程序不接管内容区。
    （full 容器卡片是「非常驻可关闭」tab，走事件同步动态增删，不经过本槽位。）

    Attributes:
        plugin_name: 所属插件名
        tab_id: tab 唯一 ID（与 full 卡片 card_id 共用标题栏 tab 命名空间，勿冲突）
        label: tab 显示文本
        icon_path: 图标资源路径（可选，按钮内左侧 14px）
        on_click: 点击回调（签名 () -> None），由插件自行决定展示方式
        priority: 优先级（同 tab_id 时高者覆盖低者）
        metadata: 附加元数据
    """

    plugin_name: str
    tab_id: str
    label: str
    icon_path: str = ""
    on_click: Optional[Callable[[], None]] = None
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkbenchTabInfo:
    """右侧工作台页签注册信息

    插件通过本槽位向右侧工作台（WorkbenchPanel）注册新页签，与内置
    「产物 / 记忆」页签平级展示，避免插件内容挤进已有页签造成混乱。

    Attributes:
        plugin_name: 所属插件名
        page_id: 页签唯一 ID（同时用于 set_current_tab）
        label: 页签显示文本
        widget_class: 页签 widget 类（构造 parent + context，同 WorkspacePageInfo）
        priority: 优先级（同 page_id 时高者覆盖低者）
        metadata: 附加元数据
    """

    plugin_name: str
    page_id: str
    label: str
    widget_class: Any
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


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
        icon_path: 图标路径（深色主题默认图标）
        icon_light_path: 浅色主题图标路径（可选，缺省回退 icon_path）
        tooltip: 悬停提示
        group: 分组（默认 "plugin"，用于与系统按钮分隔线区分）
        priority: 优先级（同 button_id 时高者覆盖低者）
        on_click: 点击回调，签名 (context: dict) -> None（context 含 window_id 等）
        on_right_click: 右键回调，签名同 on_click（可选；缺省时按钮右键无行为）
        metadata: 附加元数据
    """

    plugin_name: str
    button_id: str
    icon_path: str = ""
    icon_light_path: str = ""
    tooltip: str = ""
    group: str = "plugin"
    priority: int = 0
    on_click: Optional[Callable[[Dict[str, Any]], None]] = None
    on_right_click: Optional[Callable[[Dict[str, Any]], None]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    position: str = "end"  # "start" | "before:<id>" | "after:<id>" | "end"（默认追加末尾）


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
    section: str = "plugins"  # 挂载分区：plugins/llm/common/appearance/update（对应设置面板 tab）


class UIPluginRegistry:
    """UI 插件注册表（单例）"""

    _instance: Optional["UIPluginRegistry"] = None

    def __init__(self):
        self._content_renderers: Dict[str, ContentRendererInfo] = {}
        # 消息文本内联标签渲染器：{tag_name(小写): TagRendererInfo}
        self._tag_renderers: Dict[str, TagRendererInfo] = {}
        # 正文 fence 代码块渲染器：{lang(小写): FenceRendererInfo}
        self._fence_renderers: Dict[str, FenceRendererInfo] = {}
        # 已加载插件的根目录：{plugin_name: 目录绝对路径}
        # fence 渲染器的 assets 是相对插件根声明的，宿主注入时需按此解析。
        self._plugin_paths: Dict[str, str] = {}
        self._message_factories: List[MessageFactoryInfo] = []
        self._floating_cards: Dict[str, FloatingCardInfo] = {}
        self._welcome_tabs: Dict[str, WelcomeTabInfo] = {}
        # 欢迎卡片插件点击动作：{action: WelcomeActionInfo}
        self._welcome_actions: Dict[str, WelcomeActionInfo] = {}
        # @ 提及条目提供者：{provider_id: MentionProviderInfo}
        self._mention_providers: Dict[str, MentionProviderInfo] = {}
        # Phase D：四类新扩展点（键为 item_id/button_id/action_id/card_id）
        self._sidebar_items: Dict[str, SidebarItemInfo] = {}
        self._input_buttons: Dict[str, InputButtonInfo] = {}
        self._context_actions: Dict[str, ContextMenuActionInfo] = {}
        self._settings_cards: Dict[str, SettingsCardInfo] = {}
        # 标题栏常驻 tab 槽位：{tab_id: TitlebarTabInfo}
        self._titlebar_tabs: Dict[str, TitlebarTabInfo] = {}
        # 右侧工作台页签槽位：{page_id: WorkbenchTabInfo}
        self._workbench_tabs: Dict[str, WorkbenchTabInfo] = {}
        # right 容器卡片的工作区 tab 登记簿：{card_id: host_window_id}
        # ★ right 容器卡片不再挂对话区右侧停靠区，改挂工作台（WorkbenchPanel）
        #   动态 tab 页。此类卡片不走 CardManager（无互斥/堆叠需求，关闭即摘 tab），
        #   也不参与 per-tab 显隐投影（与产物/记忆页同为宿主级全局页）。
        self._workbench_card_tabs: Dict[str, str] = {}
        # 工作台卡片 tab 的 per-tab 打开集合：{scope(window_id): set(card_id)}
        # 切换对话标签页时按目标集合投影（open/close），实现各标签页工作区内容独立
        self._workbench_card_scopes: Dict[str, set] = {}
        # 工作区页面槽（Phase G）：{page_id: WorkspacePageInfo}，页面级扩展
        self._workspace_pages: Dict[str, Any] = {}
        # 通用区域挂载模型（Phase E）：宿主声明区域 → 插件挂载条目
        # 结构 {region_id: {"kind": str, "entries": {entry_id: SlotEntry}}}
        self._regions: Dict[str, Dict[str, Any]] = {}
        # UI 模块槽（Phase F）：{module_id: [(plugin_name, priority, factory), ...]}
        # 多实现并存（system + 多个插件 override），胜者 = max(priority)
        self._ui_modules: Dict[str, list] = {}
        # 主程序内置区域（宿主在窗口装配时也可再声明，幂等）
        from app.plugins.contracts.ui_slots import LIST_ITEM, MENU, PANEL, TOOLBAR_BUTTON

        for rid, kind, desc in [
            ("sidebar", LIST_ITEM, "左侧边栏插件项"),
            ("toolbar:input", TOOLBAR_BUTTON, "输入区工具栏按钮"),
            ("menu:message_card", MENU, "消息卡片右键菜单"),
            ("menu:tab", MENU, "Tab 标签右键菜单"),
            ("menu:input_area", MENU, "输入框右键菜单"),
            ("settings:plugins", PANEL, "设置面板插件分区"),
            ("settings:llm", PANEL, "设置面板大模型分区插件卡"),
            ("settings:common", PANEL, "设置面板通用分区插件卡"),
            ("settings:appearance", PANEL, "设置面板外观分区插件卡"),
            ("settings:update", PANEL, "设置面板更新分区插件卡"),
        ]:
            self.declare_region(rid, kind, desc)
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
        # 热重载恢复队列：unload 时记录「卸载前可见」的浮动卡片 (plugin_name, window_id, card_id)，
        # 重新加载成功后在对应窗口按新 widget_class 重建并恢复显示（否则旧标签页中
        # 已打开的卡片只能等用户手动重开才看到新版）。
        # 条目按 plugin_name 作用域：仅被同插件的 load_plugin 消费——插件删除路径
        # （standalone unload，无后续 load）的过期条目不会泄漏给其他插件的加载。
        self._pending_card_restore: list[tuple[str, str, str]] = []
        # P2-2：UI 目录 mtime 签名（静默写入兑底轮询用）
        self._ui_signatures: dict = {}
        self._signature_watch_started = False
        # ── Tab 模式浮动卡片按标签页隔离（per-tab 可见集合）──
        # 卡片 widget 单实例挂 TabManagerWindow 全局容器；CardManager 的
        # GLOBAL 可见记录是「当前活跃标签页可见集合」的投影。切换标签时按
        # 目标标签页的记录 show/hide（走 CardManager 标准路径，互斥/容器
        # 展开/覆盖层切换自动生效）。
        # {tab_window_id: {card_id, ...}}
        self._tab_card_visibility: Dict[str, set] = {}
        # 当前投影的标签页 window_id（None 时按需解析活跃窗口）
        self._active_tab_scope: Optional[str] = None
        # 投影同步中标志：切换标签触发的 hide 不清除当前标签可见集合
        self._tab_sync_in_progress: bool = False

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
        from loguru import logger

        if metadata is None:
            metadata = {}
        # P2-1：UI 回调 watchdog 包装（单次/滑窗超时 degrade → 连续熔断停用；
        # 重新注册即重置计数=修复恢复语义）
        try:
            from app.core.ui_callback_watchdog import wrap_ui_callback

            render_func = wrap_ui_callback(plugin_name, f"content:{type_name}", render_func)
        except Exception:
            pass
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
        if existing is not None and existing.plugin_name != plugin_name:
            logger.warning(
                f"[UIPluginRegistry] content renderer '{type_name}' 被插件 {plugin_name!r} 覆盖"
                f"（原注册方: {existing.plugin_name!r}, "
                f"priority {existing.priority} -> {priority}）"
            )
        self._content_renderers[type_name] = info

    def register_tag_renderer(
        self,
        plugin_name: str,
        tag_name: str,
        render_func: Callable[[str, Dict[str, Any]], str],
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册消息文本内联标签渲染器

        Args:
            plugin_name: 所属插件名
            tag_name: 标签名（自动转小写；对应文本中的 <tag_name>...</tag_name>）
            render_func: 渲染函数 (content: str, ctx: dict) -> str(HTML)，
                         需双端兼容 QLabel 富文本与 WebEngine；
                         ctx 含 tag/completed/compact。须为纯函数（可能在
                         后台渲染线程调用，禁止触碰 Qt widget）
            priority: 优先级（同 tag_name 时高者覆盖低者）
            metadata: 附加元数据
        """
        from loguru import logger

        if metadata is None:
            metadata = {}
        key = tag_name.strip().lower()
        if not key or not re.fullmatch(r"[a-z0-9_-]+", key):
            raise ValueError(f"invalid tag_name {tag_name!r}: must match [a-z0-9_-]+")
        # P2-1：UI 回调 watchdog 包装（同 content renderer 口径）
        try:
            from app.core.ui_callback_watchdog import wrap_ui_callback

            render_func = wrap_ui_callback(plugin_name, f"tag:{key}", render_func)
        except Exception:
            pass
        info = TagRendererInfo(
            plugin_name=plugin_name,
            tag_name=key,
            render_func=render_func,
            priority=priority,
            metadata=metadata,
        )
        existing = self._tag_renderers.get(key)
        if existing is not None and existing.priority > priority:
            # 低优先级注册被忽略
            return
        if existing is not None and existing.plugin_name != plugin_name:
            logger.warning(
                f"[UIPluginRegistry] tag '{key}' 被插件 {plugin_name!r} 覆盖"
                f"（原注册方: {existing.plugin_name!r}, "
                f"priority {existing.priority} -> {priority}）"
            )
        self._tag_renderers[key] = info

    # fence 渲染器：允许的资源键与桥权限白名单
    FENCE_ASSET_KEYS = ("js", "css")
    FENCE_BRIDGE_PERMISSIONS = ("theme", "sendPrompt", "storage")
    # 内置 fence 保留名：这些 lang 在宿主渲染链中位于插件查询之后
    # （见 widgets/message_card.py 的 _render_code_block），一旦被插件注册，
    # 内置实现将永久失效 —— 插件可据此劫持全应用图表/HTML/SVG 渲染。
    # 注册表侧此前只校验 lang 字符正则，未落实该约束，此处补齐。
    RESERVED_FENCE_LANGS = frozenset({"echarts", "mermaid", "svg", "html", "widget"})
    # 单个 asset 文件上限（设计稿 §3 防呆）。插件合计上限由宿主注入时控。
    FENCE_ASSET_MAX_BYTES = 2 * 1024 * 1024

    def register_fence_renderer(
        self,
        plugin_name: str,
        lang: str,
        render_func: Callable[[str, Dict[str, Any]], str],
        streaming_placeholder: Optional[Any] = None,
        priority: int = 0,
        assets: Optional[Dict[str, str]] = None,
        bridge_permissions: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册消息正文 fence 代码块渲染器。

        Args:
            plugin_name: 所属插件名
            lang: fence 语言标记（自动转小写）。与宿主内置 echarts / mermaid /
                  svg / html / widget 同名时内置优先，插件不会覆盖内置行为。
            render_func: (code, ctx) -> HTML 片段，须为纯函数（后台渲染线程调用）
            streaming_placeholder: 流式半截 fence 的占位（str 或 callable），
                           缺省用宿主的通用图表骨架
            priority: 同 lang 时高者覆盖低者
            assets: {"js": 相对插件根路径, "css": ...}。宿主按卡片实际用到的
                    fence 按需注入，未用到的插件不注入。
            bridge_permissions: 桥权限声明，取值 "theme" / "sendPrompt" /
                    "storage"。未声明的桥方法在页面中为 undefined。
            metadata: 附加元数据

        Raises:
            ValueError: lang 非法、assets 键非法或路径不安全、权限名未知
        """
        if metadata is None:
            metadata = {}
        key = lang.strip().lower()
        if not key or not re.fullmatch(r"[a-z0-9_+.-]+", key):
            raise ValueError(f"invalid fence lang {lang!r}: must match [a-z0-9_+.-]+")
        if key in self.RESERVED_FENCE_LANGS:
            raise ValueError(
                f"fence lang {key!r} 为宿主内置保留名，插件不可注册"
                f"（保留名: {sorted(self.RESERVED_FENCE_LANGS)}）"
            )

        safe_assets: Dict[str, str] = {}
        for akey, apath in (assets or {}).items():
            if akey not in self.FENCE_ASSET_KEYS:
                raise ValueError(f"invalid asset key {akey!r}: expected one of {self.FENCE_ASSET_KEYS}")
            if not isinstance(apath, str) or not apath:
                continue
            # 防呆：只接受插件根内的相对路径（体积校验由宿主注入时做，
            # registry 侧拿不到插件根目录）
            norm = apath.replace("\\", "/").lstrip("/")
            if not norm or norm.startswith("../") or "/../" in norm or os.path.isabs(apath):
                raise ValueError(f"unsafe asset path {apath!r}: must be relative to plugin root")
            safe_assets[akey] = norm

        perms: Tuple[str, ...] = tuple(bridge_permissions or ())
        unknown = [p for p in perms if p not in self.FENCE_BRIDGE_PERMISSIONS]
        if unknown:
            raise ValueError(
                f"unknown bridge_permissions {unknown}: expected subset of {list(self.FENCE_BRIDGE_PERMISSIONS)}"
            )

        info = FenceRendererInfo(
            plugin_name=plugin_name,
            lang=key,
            render_func=render_func,
            streaming_placeholder=streaming_placeholder,
            priority=priority,
            assets=safe_assets,
            bridge_permissions=perms,
            metadata=metadata,
        )
        existing = self._fence_renderers.get(key)
        if existing is not None and existing.priority > priority:
            # 低优先级注册被忽略（与 register_content_renderer / register_tag_renderer 同范式）
            return
        self._fence_renderers[key] = info

    def get_fence_renderer(self, lang: str) -> Optional[FenceRendererInfo]:
        """按 fence 语言标记取渲染器；未注册返回 None。

        宿主分发时先查表，命中走插件路径，未命中走内置硬编码链。
        """
        return self._fence_renderers.get((lang or "").strip().lower())

    def get_all_fence_renderers(self) -> Dict[str, FenceRendererInfo]:
        """返回全部已注册 fence 渲染器（副本）。

        宿主用它做按需注入扫描：只有卡片里真的出现了该 lang 的 fence，
        才把对应插件的 assets 注入这张卡片的骨架。
        """
        return dict(self._fence_renderers)

    def get_plugin_path(self, plugin_name: str) -> Optional[str]:
        """已加载插件的根目录绝对路径；未加载 / 未知插件返回 None。"""
        return self._plugin_paths.get(str(plugin_name))

    def resolve_fence_assets(self, plugin_name: str, assets: Dict[str, str]) -> Dict[str, str]:
        """把插件声明的 assets 相对路径解析为绝对路径（含三重校验）。

        校验项：① 键必须在 FENCE_ASSET_KEYS 内；② 解析后必须仍在插件根内
        （防路径穿越，注册时已挡一道，这里再挡一道——插件目录可能被外部改动）；
        ③ 文件存在且不超过 FENCE_ASSET_MAX_BYTES。

        任一条不满足就静默丢弃该条目（插件的一部分能力失效，但不影响卡片其余渲染）。
        """
        out: Dict[str, str] = {}
        root = self._plugin_paths.get(str(plugin_name))
        if not root or not assets:
            return out
        root_norm = os.path.normpath(root)
        prefix = root_norm + os.sep
        for key, rel in assets.items():
            if key not in self.FENCE_ASSET_KEYS or not isinstance(rel, str) or not rel:
                continue
            try:
                path = os.path.normpath(os.path.join(root_norm, rel.replace("\\", "/")))
            except Exception:
                continue
            if not path.startswith(prefix):
                continue
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getsize(path) > self.FENCE_ASSET_MAX_BYTES:
                    continue
            except OSError:
                continue
            out[key] = path
        return out

    def get_tag_renderer(self, tag_name: str) -> Optional[TagRendererInfo]:
        """按标签名查询渲染器（未注册返回 None，调用方回退默认渲染）"""
        return self._tag_renderers.get(tag_name.strip().lower())

    def get_registered_tag_names(self) -> List[str]:
        """全部已注册标签名（小写）"""
        return list(self._tag_renderers.keys())

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
                      系统内置 mode 冲突：sessions / projects）
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

    def register_welcome_action(
        self,
        plugin_name: str,
        action: str,
        handler: Callable[[str, Dict[str, Any]], None],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册欢迎卡片插件点击动作

        欢迎 tab HTML 中 .context-tag 的自定义 data-type 经
        handle_recommended_question 派发到此 handler（后注册覆盖先注册）。

        Args:
            plugin_name: 所属插件名
            action: 动作名（建议带插件前缀避免冲突，如 "mkr-install"）
            handler: 回调，签名 (content: str, ctx: dict) -> None
            metadata: 附加元数据
        """
        if metadata is None:
            metadata = {}
        self._welcome_actions[action] = WelcomeActionInfo(
            plugin_name=plugin_name,
            action=action,
            handler=handler,
            metadata=metadata,
        )

    def dispatch_welcome_action(self, action: str, content: str, ctx: Optional[Dict[str, Any]] = None) -> bool:
        """派发欢迎卡片点击动作到注册插件

        Args:
            action: 动作名（.context-tag 的 data-type）
            content: 内容（.context-tag 的 data-content）
            ctx: 窗口上下文（window_id / main_widget 等）

        Returns:
            True 已派发（action 有注册 handler）；False 无人处理
        """
        info = self._welcome_actions.get(action)
        if info is None:
            return False
        if ctx is None:
            ctx = {}
        info.handler(content, ctx)
        return True

    def register_mention_provider(
        self,
        plugin_name: str,
        provider_id: str,
        list_func: Callable[[], List[Dict[str, Any]]],
        on_selected: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册 @ 提及条目提供者（条目渲染在 @ 卡片顶部）

        Args:
            plugin_name: 所属插件名
            provider_id: 提供者唯一标识（重复注册覆盖）
            list_func: 条目提供回调，同步、主线程调用，禁止耗时 I/O
            on_selected: 选中回调 (entry, ctx)；ctx 含 window_id/main_widget/session_id
            metadata: 附加元数据
        """
        if metadata is None:
            metadata = {}
        self._mention_providers[provider_id] = MentionProviderInfo(
            plugin_name=plugin_name,
            provider_id=provider_id,
            list_func=list_func,
            on_selected=on_selected,
            metadata=metadata,
        )

    def get_mention_providers(self) -> List[MentionProviderInfo]:
        """获取所有 @ 提及条目提供者（插入序）"""
        return list(self._mention_providers.values())

    def dispatch_mention_selected(
        self, provider_id: str, entry: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
    ) -> bool:
        """派发 @ 提及条目选中事件到提供者插件

        Returns:
            True 已派发；False 无该 provider 或未注册选中回调
        """
        info = self._mention_providers.get(provider_id)
        if info is None or info.on_selected is None:
            return False
        info.on_selected(entry, ctx or {})
        return True

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
        # 写入 region 存储（Phase E 单源化）
        self.register_slot_entry("sidebar", item_id, plugin_name, priority=priority, payload=info, metadata=metadata)

    def get_sidebar_items(self) -> List[SidebarItemInfo]:
        """获取全部侧边栏插件项（group 排序：system 在前，custom 在后；同组按 priority 降序 → 注册序）

        数据源：region 存储（Phase E 单源化）"""
        items = [e.payload for e in self.get_region_entries("sidebar") if isinstance(e.payload, SidebarItemInfo)]
        system = [v for v in items if v.group == "system"]
        custom = [v for v in items if v.group != "system"]
        return system + custom

    # ── 标题栏常驻 tab 槽位 ──

    def register_titlebar_tab(
        self,
        plugin_name: str,
        tab_id: str,
        label: str,
        icon_path: str = "",
        on_click: Optional[Callable[[], None]] = None,
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册标题栏常驻 tab（无 × 关闭钮；点击走 on_click 回调自展示）"""
        if metadata is None:
            metadata = {}
        info = TitlebarTabInfo(
            plugin_name=plugin_name,
            tab_id=tab_id,
            label=label,
            icon_path=icon_path,
            on_click=on_click,
            priority=priority,
            metadata=metadata,
        )
        existing = self._titlebar_tabs.get(tab_id)
        if existing is not None and existing.priority > priority:
            return
        self._titlebar_tabs[tab_id] = info

    def unregister_titlebar_tabs(self, plugin_name: str) -> None:
        """注销某插件的全部常驻 tab（插件卸载时调用）"""
        for tab_id in [tid for tid, v in self._titlebar_tabs.items() if v.plugin_name == plugin_name]:
            del self._titlebar_tabs[tab_id]

    def get_titlebar_tabs(self) -> List[TitlebarTabInfo]:
        """获取全部常驻 tab（按注册序返回，tab 栏位置即注册顺序）"""
        return list(self._titlebar_tabs.values())

    # ── 右侧工作台页签槽位 ──

    def register_workbench_tab(
        self,
        plugin_name: str,
        page_id: str,
        label: str,
        widget_class: Any,
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册右侧工作台页签（同 page_id 高优先级覆盖低优先级）"""
        if metadata is None:
            metadata = {}
        info = WorkbenchTabInfo(
            plugin_name=plugin_name,
            page_id=page_id,
            label=label,
            widget_class=widget_class,
            priority=priority,
            metadata=metadata,
        )
        existing = self._workbench_tabs.get(page_id)
        if existing is not None and existing.priority > priority:
            return
        self._workbench_tabs[page_id] = info

    def unregister_workbench_tabs(self, plugin_name: str) -> None:
        """注销某插件的全部工作台页签（插件卸载时调用）"""
        for page_id in [pid for pid, v in self._workbench_tabs.items() if v.plugin_name == plugin_name]:
            del self._workbench_tabs[page_id]

    def get_workbench_tabs(self) -> List[WorkbenchTabInfo]:
        """获取全部工作台页签（按注册序返回）"""
        return list(self._workbench_tabs.values())

    # ── Phase G：WorkspacePage 页面槽 ──

    def register_workspace_page(
        self,
        plugin_name: str,
        page_id: str,
        title: str,
        widget_class: Any,
        icon_path: str = "",
        icon_light_path: str = "",
        order_hint: int = 500,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册工作区页面（同 page_id 后注册覆盖；order_hint 升序排布）"""
        from app.plugins.contracts.ui_page import WorkspacePageInfo

        self._workspace_pages[page_id] = WorkspacePageInfo(
            plugin_name=plugin_name,
            page_id=page_id,
            title=title,
            widget_class=widget_class,
            icon_path=icon_path,
            icon_light_path=icon_light_path,
            order_hint=order_hint,
            metadata=metadata or {},
        )

    def get_workspace_pages(self) -> List[Any]:
        """全部工作区页面（order_hint 升序 → 注册序）"""
        infos = sorted(
            self._workspace_pages.values(),
            key=lambda i: (i.order_hint, list(self._workspace_pages).index(i.page_id)),
        )
        return list(infos)

    def get_workspace_page(self, page_id: str) -> Optional[Any]:
        """按 page_id 精确查询"""
        return self._workspace_pages.get(page_id)

    def register_input_button(
        self,
        plugin_name: str,
        button_id: str,
        icon_path: str = "",
        icon_light_path: str = "",
        tooltip: str = "",
        group: str = "plugin",
        priority: int = 0,
        on_click: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_right_click: Optional[Callable[[Dict[str, Any]], None]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        position: str = "end",
    ) -> None:
        """注册输入区插件按钮（Phase D）

        icon_path 为深色主题默认图标；icon_light_path 为浅色主题图标
        （可选，缺省时浅色主题回退 icon_path）。主题切换时主程序自动刷新。

        on_right_click: 右键点击回调（可选，签名同 on_click）；注册后按钮右键
        不再弹系统菜单，改为派发本回调。

        position: "start" | "before:<button_id>" | "after:<button_id>" | "end"
        （Phase E：允许插件声明按钮位置——锚定系统按钮 memory/history/new_session
        或其他插件按钮 id；锚点缺失降级末尾追加）
        """
        import re

        if position not in ("start", "end") and not re.fullmatch(r"(before|after):[\w:-]+", position):
            raise ValueError(f"invalid position {position!r}: use 'start'/'end'/'before:<id>'/'after:<id>'")
        if metadata is None:
            metadata = {}
        info = InputButtonInfo(
            plugin_name=plugin_name,
            button_id=button_id,
            icon_path=icon_path,
            icon_light_path=icon_light_path,
            tooltip=tooltip,
            group=group,
            priority=priority,
            on_click=on_click,
            on_right_click=on_right_click,
            metadata=metadata,
            position=position,
        )
        existing = self._input_buttons.get(button_id)
        if existing is not None and existing.priority > priority:
            return
        self._input_buttons[button_id] = info
        # 写入 region 存储（Phase E 单源化）
        self.register_slot_entry(
            "toolbar:input", button_id, plugin_name, priority=priority, payload=info, metadata=metadata
        )

    def get_input_buttons(self) -> List[InputButtonInfo]:
        """获取全部输入区插件按钮（priority 降序 → 注册序）

        数据源：region 存储（Phase E 单源化）"""
        return [e.payload for e in self.get_region_entries("toolbar:input") if isinstance(e.payload, InputButtonInfo)]

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
        # 写入 region 存储（Phase E 单源化）：按 menu:<target> 分区
        self.register_slot_entry(
            f"menu:{target}", action_id, plugin_name, priority=priority, payload=info, metadata=metadata
        )

    def get_context_actions(self, target: str) -> List[ContextMenuActionInfo]:
        """获取指定 target 的菜单插件项（priority 降序 → 注册序；separator_before 为渲染标记）

        数据源：region 存储（Phase E 单源化）。target 开放：宿主声明新 menu:<target> 区域即可消费。"""
        return [
            e.payload for e in self.get_region_entries(f"menu:{target}") if isinstance(e.payload, ContextMenuActionInfo)
        ]

    def register_settings_card(
        self,
        plugin_name: str,
        card_id: str,
        title: str,
        widget_class: type,
        group: str = "plugin",
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        section: str = "plugins",
    ) -> None:
        """注册设置面板插件卡片（Phase D + E）

        section: 挂载分区（plugins/llm/common/appearance/update），
        对应设置面板 5 个 tab。声明 section 时自动懒声明 settings:<section> 区域。
        """
        valid_sections = ("plugins", "llm", "common", "appearance", "update")
        if section not in valid_sections:
            raise ValueError(f"invalid section {section!r}, must be one of {valid_sections}")
        # 懒声明目标分区区域（幂等）
        if f"settings:{section}" not in self._regions:
            from app.plugins.contracts.ui_slots import PANEL

            self.declare_region(f"settings:{section}", PANEL, f"设置面板 {section} 分区插件卡")
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
            section=section,
        )
        existing = self._settings_cards.get(card_id)
        if existing is not None and existing.priority > priority:
            return
        self._settings_cards[card_id] = info
        # 写入 region 存储（Phase E 单源化：按 section 分区）
        self.register_slot_entry(
            f"settings:{section}", card_id, plugin_name, priority=priority, payload=info, metadata=metadata
        )

    def get_settings_cards(self) -> List[SettingsCardInfo]:
        """获取全部设置面板插件卡片（全 section 合集，按 priority 降序）

        数据源：region 存储（Phase E 单源化，按 section 聚合）"""
        infos = []
        for region_id, region in self._regions.items():
            if not region_id.startswith("settings:"):
                continue
            infos.extend(e.payload for e in region["entries"].values() if isinstance(e.payload, SettingsCardInfo))
        infos.sort(key=lambda i: -i.priority)
        return infos

    # ── Phase E：Region 通用挂载模型 ──

    def declare_region(self, region_id: str, kind: str, description: str = "") -> None:
        """宿主声明 UI 区域（幂等；重复声明覆盖 description/kind）

        region_id 命名约定：
        - "menu:<target>"     右键/下拉菜单（如 "menu:input_area"）
        - "toolbar:<name>"    工具栏（如 "toolbar:input"）
        - 简单名              列表/面板区域（如 "sidebar"、"settings:plugins"）
        """
        from app.plugins.contracts.ui_slots import VALID_REGION_KINDS

        if kind not in VALID_REGION_KINDS:
            raise ValueError(f"invalid region kind {kind!r}, must be one of {sorted(VALID_REGION_KINDS)}")
        self._regions.setdefault(region_id, {"kind": kind, "entries": {}})

    def register_slot_entry(
        self,
        region_id: str,
        entry_id: str,
        plugin_name: str,
        priority: int = 0,
        payload: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """向已声明区域挂载条目；同 entry_id 高 priority 覆盖低者"""
        region = self._regions.get(region_id)
        if region is None:
            raise ValueError(f"undeclared region {region_id!r}; call declare_region() first")
        from app.plugins.contracts.ui_slots import SlotEntry

        existing = region["entries"].get(entry_id)
        if existing is not None and existing.priority > priority:
            return
        region["entries"][entry_id] = SlotEntry(
            entry_id=entry_id,
            plugin_name=plugin_name,
            region_id=region_id,
            priority=priority,
            payload=payload,
            metadata=metadata or {},
        )

    def get_region_entries(self, region_id: str) -> list:
        """获取区域条目（priority 降序 → 注册序；未声明区域返回空列表）"""
        region = self._regions.get(region_id)
        if region is None:
            return []
        entries = sorted(region["entries"].values(), key=lambda e: -e.priority)
        return list(entries)

    def get_region_entry(self, region_id: str, entry_id: str):
        """按 entry_id 精确查条目（未声明区域/无条目返回 None）"""
        region = self._regions.get(region_id)
        if region is None:
            return None
        return region["entries"].get(entry_id)

    # ── Phase F：UIModule 模块槽 ──

    SYSTEM_MODULE_PRIORITY = 0  # 系统模块基线；插件覆盖须 >= 100

    def register_ui_module(
        self,
        module_id: str,
        factory,
        plugin_name: str = "system",
        priority: int = 0,
    ) -> None:
        """注册 UI 模块实现（factory 延迟构造；多实现并存，胜者=最高 priority）

        Args:
            module_id: 模块槽 ID（与 UIModule.module_id 一致）
            factory: 无参可调用，返回 UIModule 实例（延迟到 get_ui_module 时构造）
            plugin_name: 所属插件名（unload 时按此清理）
            priority: 同 id 时高者胜；系统基线 = SYSTEM_MODULE_PRIORITY (0)，
                     插件覆盖建议 >= 100
        """
        self._ui_modules.setdefault(module_id, []).append((plugin_name, priority, factory))

    def get_ui_module(self, module_id: str):
        """取胜者模块实例（高 priority 胜；同 priority 后注册胜；无注册返回 None）"""
        slot = self._ui_modules.get(module_id)
        if not slot:
            return None
        # 胜者 = max(priority, 注册索引) — 索引作 tiebreaker 让后注册胜
        _idx, (_name, _priority, factory) = max(enumerate(slot), key=lambda x: (x[1][1], x[0]))
        return factory()

    def list_ui_module_ids(self) -> List[str]:
        """按注册序返回所有 module_id（供 compose 排序验证）"""
        return list(self._ui_modules.keys())

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

        探测顺序：协议优先（实现 as_ui_host）→ 鸭子属性兜底（legacy 路径）。
        """
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None:
                # 协议优先：宿主实现 as_ui_host() 即直接采用（二期 UIModule 全走此路径）
                if callable(getattr(tm, "as_ui_host", None)):
                    return tm.as_ui_host()
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

        # 记录迁移前是否可见（任一窗口；含工作台卡片 tab）
        was_visible = card_id in self._workbench_card_tabs
        if not was_visible:
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

    def hide_floating_card_globally(self, card_id: str) -> bool:
        """隐藏浮动卡片（公开 API — EP6 公开面，给插件调用）

        插件（尤其是 ``full`` 容器卡片，如 autoloop config/running）需要从
        自身业务逻辑隐藏卡片时，不应触碰 main_widget 私有属性
        ``_card_manager`` / ``_window_id``，而应调用此方法。

        行为：
            - Tab 模式：经 ``_resolve_global_host()`` 取 TabManagerWindow，
              调 ``CardManager.hide_card(card_id, host_wid)``。
            - 回退模式：无 host 时返回 ``False``，由调用方决定后续
              （例如 fallback 到 services["hide_card"]）。
            - 已注册到本注册表的 card_id 才会被处理；未注册的 card_id
              不报错，但返回 ``False`` 以便调用方判断。

        Args:
            card_id: 卡片唯一 ID（与 ``FloatingCardInfo.card_id`` 一致）

        Returns:
            True 隐藏成功（Tab host 路径）；False 不可用或卡片未注册。
        """
        host = self._resolve_global_host()
        if host is None:
            return False
        # right 工作台卡：不在 CardManager 登记，关 tab 即隐藏
        if card_id in self._workbench_card_tabs:
            if card_id not in self._floating_cards:
                return False
            self._close_workbench_card_tab(card_id)
            return True
        card_manager = getattr(host, "_card_manager", None)
        host_wid = getattr(host, "_window_id", None)
        if card_manager is None or not host_wid:
            return False
        if card_id not in self._floating_cards:
            return False
        try:
            card_manager.hide_card(card_id, host_wid)
        except Exception:
            return False
        return True

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
        card_info = self._floating_cards.get(card_id)
        if card_info is None:
            return

        # right 容器卡片 → 工作台动态 tab 页（Tab 模式；宿主无工作台时回退旧路径）
        panel = getattr(host, "workbench_panel", None) if host is not None else None
        if card_info.container == "right" and panel is not None:
            self._show_floating_card_in_workbench(card_info, panel, host)
            return

        card_manager = getattr(mw, "_card_manager", None)
        window_id = getattr(mw, "_window_id", None)
        if card_manager is None or window_id is None:
            return

        # 记录 window_id → main_widget 映射，供 unload 时清理容器使用
        self._window_main_widgets[window_id] = mw

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

            # Phase G：dock 堆叠声明 — widget 属性优先于注册元数据
            # （容器侧 CardManager.is_card_stackable 按 property 查询）
            if card_info.metadata.get("stack"):
                try:
                    widget.setProperty("stackInDock", True)
                except Exception:
                    pass

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

            # Tab 模式 per-tab 隔离：卡片被关闭（用户点关闭 / 全局系统卡互斥）时，
            # 从当前标签页的可见集合移除（切回该标签不再自动恢复）；切换标签
            # 投影期间触发的 hide 是「其他标签的状态切换」，不清除集合。
            if host is not None:

                def _on_hidden_for_tab(cid=card_id):
                    if self._tab_sync_in_progress:
                        return
                    scope = self._active_tab_scope or self._resolve_tab_scope()
                    if scope:
                        self._tab_card_visibility.get(scope, set()).discard(cid)

                card_manager.on_card_hidden(window_id, card_id, _on_hidden_for_tab)

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

        # Tab 模式：按标签页记录可见状态（切换标签时投影恢复/隐藏）
        if host is not None:
            self._record_tab_card_state(card_id, card_manager, window_id)

    def _show_floating_card_in_workbench(
        self, card_info: Any, panel, host, auto_expand: bool = True, activate: bool = True
    ) -> None:
        """right 容器卡片 → 工作台动态 tab 页（v2：对话区右侧停靠区移除）

        与旧路径的差异：
        - 不走 CardManager（无互斥/堆叠/压制需求；关闭 = 摘 tab）
        - 不注册系统卡（tab 页不覆盖对话区，不隐藏输入区）
        - 不参与 per-tab 显隐投影（与产物/记忆页同为宿主级全局页）
        - 保留实例缓存与上下文注入（热重载/卸载清理路径与旧卡片一致）

        Args:
            auto_expand: 工作台隐藏时是否自动展开（用户打开=True；投影恢复=False）
            activate: 是否激活为当前工作台页签（用户打开=True；投影恢复=False，
                只挂载不抢当前页签——切换对话标签页时恢复卡片 tab 不应把用户
                停留的工作台页签带走）
        """
        card_id = card_info.card_id
        window_id = getattr(host, "_window_id", None)
        if not window_id:
            from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

            window_id = GLOBAL_WINDOW_ID
        # 记录 window_id → 宿主映射，供 unload 时清理容器使用
        self._window_main_widgets[window_id] = host

        win_instances = self._card_widget_instances.setdefault(window_id, {})
        widget = win_instances.get(card_id)
        if widget is None:
            # tab × 关闭钮 → registry 同步清理卡片状态并摘 tab。
            # UniqueConnection 防止多次 open 重复连接导致 _close_workbench_card_tab 多次调用。
            if not getattr(self, "_workbench_card_signal_wired", False):
                from PyQt5.QtCore import Qt as _Qt

                panel.card_tab_close_requested.connect(self._close_workbench_card_tab, type=_Qt.UniqueConnection)
                self._workbench_card_signal_wired = True
            widget = card_info.widget_class(parent=panel)
            if card_info.metadata.get("stack"):
                try:
                    widget.setProperty("stackInDock", True)
                except Exception:
                    pass
            # 上下文注入（与旧路径同一优先级：拉模型 provider > 推模型 set_context > 兼容属性）
            ctx_provider = self._make_context_provider(card_info, window_id)
            if hasattr(widget, "set_context_provider") and callable(widget.set_context_provider):
                widget.set_context_provider(ctx_provider)
            elif hasattr(widget, "set_context") and callable(widget.set_context):
                widget.set_context(ctx_provider())
            else:
                widget._card_context = ctx_provider()
                widget._card_context_provider = ctx_provider
            win_instances[card_id] = widget
            # 卡片内部关闭按钮 → 摘除工作台 tab（与 tab × 同一入口）
            if hasattr(widget, "closed"):
                widget.closed.connect(lambda _c=card_id: self._close_workbench_card_tab(_c))

        self._workbench_card_tabs[card_id] = window_id
        # per-tab 归属：记录到当前活跃对话标签页的打开集合（切 tab 投影用）
        scope = self._resolve_tab_scope() or window_id
        self._workbench_card_scopes.setdefault(scope, set()).add(card_id)
        panel.open_card_tab(card_id, card_info.title or card_id, widget, activate=activate)
        # ★ 卡片数据加载入口：旧路径经 CardManager.show_card 调 widget.show_card()，
        #   工作台路径必须显式补调，否则卡片只建骨架不拉数据（表现为 tab 空白）。
        # ★ 仅激活路径调用：插件模板的 show_card() 末尾普遍带 setVisible(True)，
        #   投影恢复路径（activate=False，切对话 tab 回来只挂载不激活）误调会把
        #   QStackedWidget 的非当前页强行 show 出来——幽灵可见页被常驻页 raise
        #   后压在背景层，与常驻页内容重叠且不可点击。恢复时数据此前已加载。
        if activate:
            show_card = getattr(widget, "show_card", None)
            if callable(show_card):
                try:
                    show_card()
                except Exception:
                    pass
        # 工作台隐藏时自动展开（否则用户点插件按钮无可见反馈）；
        # 投影恢复路径（auto_expand=False）不抢焦点也不强开面板
        if auto_expand:
            try:
                if not host.is_workbench_visible():
                    host.set_workbench_visible(True)
            except Exception:
                pass

    def _close_workbench_card_tab(self, card_id: str) -> None:
        """摘除工作台卡片 tab（tab × / 卡片内部关闭按钮 / hide_floating_card_globally 共用）"""
        self._workbench_card_tabs.pop(card_id, None)
        # 从所有对话标签页的打开集合移除（用户关闭 = 任何标签页都不再恢复）
        for cards in self._workbench_card_scopes.values():
            cards.discard(card_id)
        host = self._resolve_global_host()
        panel = getattr(host, "workbench_panel", None) if host is not None else None
        if panel is not None:
            try:
                panel.close_card_tab(card_id)
            except Exception:
                pass

    def sync_workbench_cards_to_tab(self, scope: Optional[str]) -> None:
        """切换对话标签页时按目标标签页投影工作台卡片 tab（per-tab 隔离）

        卡片 widget 单实例；切换时摘除不属于目标标签页的卡片 tab、恢复
        目标标签页曾打开的卡片 tab（auto_expand=False，不强开工作台）。
        """
        panel = None
        try:
            host = self._resolve_global_host()
            panel = getattr(host, "workbench_panel", None) if host is not None else None
        except Exception:
            return
        if panel is None or not scope:
            return
        target = self._workbench_card_scopes.get(scope, set())
        # 摘除/恢复前后保持当前页签（按 tab_id）：投影只是页签集合的增减，
        # 不应把用户当前停留的工作台页签带走（per-window 页签记忆 restore
        # 是另一层兜底；这里保证不可见期间/无记忆窗口也不跳页）
        prev_id = None
        try:
            prev_id = panel._tab_id_at(panel.current_tab())
        except Exception:
            prev_id = None
        # 关：当前挂载但不属于目标标签页的卡片 tab。
        # ★ 只摘 tab 不清 scopes 集合——这是「临时隐藏」（其他标签页仍保留打开记录），
        #   走 _close_workbench_card_tab 会把用户关闭语义混进来误清其他标签页的集合。
        for card_id in list(self._workbench_card_tabs.keys()):
            if card_id not in target:
                self._workbench_card_tabs.pop(card_id, None)
                try:
                    panel.close_card_tab(card_id)
                except Exception:
                    pass
        # 开：目标标签页曾打开但当前未挂载的卡片 tab（只挂载不激活——恢复
        # 卡片不应抢走当前页签；该标签页的页签记忆由宿主的 restore 负责定稿）
        for card_id in target:
            if card_id in self._workbench_card_tabs:
                continue
            card_info = self._floating_cards.get(card_id)
            if card_info is None:
                continue
            self._show_floating_card_in_workbench(
                card_info, panel, self._resolve_global_host(), auto_expand=False, activate=False
            )
        # 还原摘除前所在页签（若摘除的正是当前卡片页，则保持 Qt 选定的邻近页）
        if prev_id is not None:
            try:
                restore_idx = panel._tab_id_index(prev_id)
                if restore_idx is not None and panel.current_tab() != restore_idx:
                    panel.set_current_tab(restore_idx)
            except Exception:
                pass

    def _record_tab_card_state(self, card_id: str, card_manager, host_window_id: str) -> None:
        """把卡片当前可见状态记录到活跃标签页的可见集合（Tab 模式）

        toggle 后调用：可见 → 加入集合；不可见 → 移出。
        """
        scope = self._resolve_tab_scope()
        if scope is None:
            return
        try:
            visible = card_manager.is_card_visible(card_id, host_window_id)
        except Exception:
            return
        cards = self._tab_card_visibility.setdefault(scope, set())
        if visible:
            cards.add(card_id)
        else:
            cards.discard(card_id)

    def _resolve_tab_scope(self) -> Optional[str]:
        """解析浮动卡片可见状态的当前标签页作用域（活跃对话窗口 window_id）"""
        if self._active_tab_scope is not None:
            return self._active_tab_scope
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None:
                active = tm.get_current_window()
                wid = getattr(active, "_window_id", None)
                if wid:
                    return wid
        except Exception:
            pass
        return None

    @contextmanager
    def tab_sync_guard(self) -> Iterator[None]:
        """Tab 切换投影保护（上下文管理器，公开面）

        切换标签页期间对浮动卡片的 show/hide 属于「per-tab 状态投影」——
        被隐藏的卡片仍是其归属标签页的打开卡片（open 集合保留），仅暂时
        不可见。此期间触发的 on_card_hidden 回调（_on_hidden_for_tab）必须
        跳过 per-tab 可见集合的清除，否则卡片从集合丢失后，下次切换回来
        sync_floating_cards_to_tab 会因 want=False & now=True 误执行
        hide_card，进而触发 TabManagerWindow 的 120ms 关闭去抖判定，
        导致 full 卡片被误关。

        使用方：本类的 sync_floating_cards_to_tab，以及 TabManagerWindow
        的 _sync_overlay_cards_to_active_window（覆盖层投影的另一条路径）。
        """
        self._tab_sync_in_progress = True
        try:
            yield
        finally:
            self._tab_sync_in_progress = False

    def sync_floating_cards_to_tab(self, scope: str) -> None:
        """切换标签页时把浮动卡片显隐投影到目标标签页（Tab 模式 per-tab 隔离）

        卡片 widget 单实例挂全局容器；CardManager 的 GLOBAL 可见记录
        视为「当前活跃标签页可见集合」的投影。切换标签时按目标标签页的
        记录 show/hide（走 CardManager 标准路径：互斥、容器展开/折叠、
        覆盖层 QStackedWidget 切换自动生效）。

        Args:
            scope: 目标标签页（对话窗口）的 window_id
        """
        host = self._resolve_global_host()
        if host is None:
            return
        cm = getattr(host, "_card_manager", None)
        host_wid = getattr(host, "_window_id", None)
        if cm is None or host_wid is None or not scope:
            return
        self._active_tab_scope = scope
        # 工作台卡片 tab 投影（per-tab 独立）：先于 CardManager 卡片投影执行，
        # 随后的 refresh_workbench 会按新活跃窗口拉取任务/产物/项目数据
        try:
            self.sync_workbench_cards_to_tab(scope)
        except Exception:
            pass
        target = self._tab_card_visibility.get(scope, set())
        instances = self._card_widget_instances.get(host_wid, {})
        with self.tab_sync_guard():
            for card_id in list(self._floating_cards.keys()):
                if card_id not in instances:
                    # 从未实例化的卡片无需投影（首次打开时才创建）
                    continue
                if card_id in self._workbench_card_tabs:
                    # 工作台卡片 tab：宿主级全局页，不参与 per-tab 显隐投影
                    continue
                try:
                    want = card_id in target
                    now = cm.is_card_visible(card_id, host_wid)
                    if want and not now:
                        cm.show_card(card_id, host_wid)
                    elif not want and now:
                        cm.hide_card(card_id, host_wid)
                except Exception:
                    continue

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
        # 记录插件根路径：fence 渲染器的 assets 以插件根为基准声明，
        # 宿主注入时要按此拼成 file:// URL（见 resolve_fence_assets）。
        # 即便 ui/__init__.py 不存在也记 —— 插件根路径本身对宿主有用。
        try:
            self._plugin_paths[str(plugin_name)] = str(plugin_path)
        except Exception:
            pass
        ui_init = plugin_path / "ui" / "__init__.py"
        if not ui_init.exists():
            return False

        # 先卸载旧版本。卸载前备份旧模块引用：register 失败时回滚恢复旧注册
        # （避免插件陷入「已卸载未加载」且 result[ui]=False 导致窗口零刷新的黑洞态）。
        import sys as _sys_backup

        safe_name = plugin_name.replace("-", "_").replace(":", "_")
        module_name = f"ui_plugin_{safe_name}"
        old_module = _sys_backup.modules.get(module_name) if self.is_loaded(plugin_name) else None
        if self.is_loaded(plugin_name):
            self.unload_plugin(plugin_name)
        # 丢弃本插件残留的过期恢复条目（上一轮 standalone unload / 回滚失败的残留），
        # 仅保留其他插件的条目——避免跨插件泄漏导致已删除卡片的错误恢复。
        self._pending_card_restore = [e for e in self._pending_card_restore if e[0] != plugin_name]

        try:
            import importlib.util
            import sys

            ui_path = str(plugin_path / "ui")
            # 添加 ui 目录到 sys.path 以支持 from .renderers 等相对导入
            if ui_path not in sys.path:
                sys.path.insert(0, ui_path)

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
            # A1：exec 前 AST 安全网——ui loader 原先全裸奔（恶意模块级代码在
            # exec 时已执行，事后 getattr 检查为时已晚）。此处拒绝 sys.modules
            # 污染 + 强制 register_ui 入口，拒载语义与 tool/runtime loader 对齐。
            try:
                ui_source = ui_init.read_text(encoding="utf-8")
            except OSError as e:
                logger.error(f"[UIPluginRegistry] 读取 {ui_init} 失败: {e}")
                return False
            from app.plugins.contracts.manifest_schema import read_manifest_module_prefixes
            from app.plugins.loaders._ast_guard import guard_plugin_module

            # 声明式放行：插件在 plugin.json 的 module_prefixes 里声明自有模块命名空间后，
            # 允许其按文件路径 importlib 注册共享模块（assistant_hub 的 assistant_hub_manager
            # 属此类）。未声明 → 任何 sys.modules 写入仍拒载（默认最严）。
            if not guard_plugin_module(
                ui_source,
                ui_init,
                require_register=True,
                component="UIPluginRegistry",
                entry_names=("register_ui",),
                allowed_sys_modules=read_manifest_module_prefixes(plugin_path),
                plugin_dir=plugin_path,
            ):
                logger.error(
                    f"[UIPluginRegistry] 插件 {plugin_name} ui/__init__.py 未通过 AST 安全网，拒载: {ui_init}"
                )
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
            # 刷新欢迎卡片：只要该插件注册过/注册着 welcome tab 就失效缓存——
            # 仅改渲染函数实现（tab 集合不变）也须刷新已打开窗口的快照，
            # 否则「热重载前打开的标签页」永远显示旧 tab 内容。
            before_tabs = {k for k, v in self._welcome_tabs.items() if v.plugin_name == plugin_name}
            register_func(self)
            self._loaded_plugins.add(plugin_name)
            logger.info(f"[UIPluginRegistry] Loaded UI components for plugin: {plugin_name}")
            # P2-2：记录 ui/ 目录 mtime 签名（静默写入兑底轮询基准）
            try:
                self._ui_signatures[plugin_name] = self._compute_ui_signature(plugin_path)
            except Exception:
                pass
            after_tabs = {k for k, v in self._welcome_tabs.items() if v.plugin_name == plugin_name}
            if before_tabs or after_tabs:
                self._schedule_welcome_refresh()
            # 热重载恢复：卸载前可见的浮动卡片 → 按新 widget_class 重建并恢复显示
            # （旧实例已在 unload 中销毁，_show_floating_card 会创建新实例 + toggle 显示）
            restore = [(win_id, cid) for (pname, win_id, cid) in self._pending_card_restore if pname == plugin_name]
            self._pending_card_restore = [e for e in self._pending_card_restore if e[0] != plugin_name]
            for win_id, cid in restore:
                try:
                    mw = self._window_main_widgets.get(win_id)
                    if mw is None:
                        continue
                    self._show_floating_card(cid, main_widget=mw)
                except Exception as re:
                    logger.warning(f"[UIPluginRegistry] 恢复可见卡片 {cid} 失败: {re}")
            return True
        except Exception as e:
            logger.error(f"[UIPluginRegistry] Failed to load UI for {plugin_name}: {e}")
            # P4：回滚——恢复旧模块与旧注册，保持插件上一版本状态可用。
            # 目标场景：插件文件短暂语法错误时已加载的工作树页等 UI 不消失，
            # 修好后下次重载自动恢复。回滚失败时也至少保 _loaded_plugins + sys.modules，
            # 避免旧 module 被彻底从 Python 命名空间抹掉（下一次重载可继续尝试）。
            if old_module is not None:
                try:
                    import sys as _sys_rollback

                    _sys_rollback.modules[module_name] = old_module
                    reg = getattr(old_module, "register_ui", None)
                    if callable(reg):
                        reg(self)
                    self._loaded_plugins.add(plugin_name)
                    logger.warning(f"[UIPluginRegistry] 已回滚 {plugin_name} 至上一版本 UI 注册")
                except Exception as re:
                    # 回滚也失败：保底——把旧 module 装回 sys.modules + 标记已加载，
                    # 注册表项可能为空（旧 register_ui 不可用），但不出现「黑洞」状态。
                    try:
                        import sys as _sys_fallback
                        _sys_fallback.modules[module_name] = old_module
                    except Exception:
                        pass
                    self._loaded_plugins.add(plugin_name)
                    logger.error(
                        f"[UIPluginRegistry] 回滚失败 {plugin_name}（{re}），"
                        f"已保底保留旧 module 引用与 _loaded_plugins 标记，"
                        f"待下次重载或用户手动重启用恢复"
                    )
            else:
                # 首次加载就失败且无旧 module——记录在 _loaded_plugins 中阻止重入黑洞
                # （reload_plugin / 后续 load_plugin 重入会再走原逻辑）
                logger.warning(
                    f"[UIPluginRegistry] {plugin_name} 首次加载失败且无旧版本可回滚，"
                    f"用户修复文件后下次 watchfiles 重载将自动重试"
                )
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
            or any(v.plugin_name == plugin_name for v in self._tag_renderers.values())
            or any(v.plugin_name == plugin_name for v in self._fence_renderers.values())
            or any(v.plugin_name == plugin_name for v in self._welcome_tabs.values())
            or any(v.plugin_name == plugin_name for v in self._welcome_actions.values())
            or any(v.plugin_name == plugin_name for v in self._mention_providers.values())
            or any(v.plugin_name == plugin_name for v in self._floating_cards.values())
            or any(v.plugin_name == plugin_name for v in self._sidebar_items.values())
            or any(v.plugin_name == plugin_name for v in self._input_buttons.values())
            or any(v.plugin_name == plugin_name for v in self._context_actions.values())
            or any(v.plugin_name == plugin_name for v in self._settings_cards.values())
            or any(v.plugin_name == plugin_name for v in self._workspace_pages.values())
            or any(v.plugin_name == plugin_name for v in self._workbench_tabs.values())
            or any(
                e.plugin_name == plugin_name for region in self._regions.values() for e in region["entries"].values()
            )
            or any(name == plugin_name for impls in self._ui_modules.values() for name, _p, _f in impls)
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
        # 清理 tag renderers
        self._tag_renderers = {k: v for k, v in self._tag_renderers.items() if v.plugin_name != plugin_name}
        # 清理 message factories
        self._message_factories = [f for f in self._message_factories if f.plugin_name != plugin_name]
        # 记录该插件是否注册过欢迎卡片 tab（决定卸载后是否刷新欢迎卡片）
        had_welcome_tabs = any(v.plugin_name == plugin_name for v in self._welcome_tabs.values())
        # 清理 welcome tabs
        self._welcome_tabs = {k: v for k, v in self._welcome_tabs.items() if v.plugin_name != plugin_name}
        # 清理 welcome actions
        self._welcome_actions = {k: v for k, v in self._welcome_actions.items() if v.plugin_name != plugin_name}
        self._mention_providers = {k: v for k, v in self._mention_providers.items() if v.plugin_name != plugin_name}
        # 清理 floating cards + 对应命令
        cards_to_remove = [cid for cid, info in self._floating_cards.items() if info.plugin_name == plugin_name]
        for cid in cards_to_remove:
            self._unregister_command_for_card(cid)
            self._floating_cards.pop(cid, None)
            # per-tab 可见集合同步清理（插件卸载后卡片不再存在，残留条目
            # 会在重新安装同名插件时误恢复显示）
            for tab_cards in self._tab_card_visibility.values():
                tab_cards.discard(cid)
            # 清理所有窗口中该 card_id 的 widget 实例（含容器布局移除 + CardManager 注销）
            for win_id, win_instances in list(self._card_widget_instances.items()):
                widget = win_instances.pop(cid, None)
                if widget is not None:
                    # 记录卸载前可见状态：热重载（unload→load）后按新 widget_class 重建恢复，
                    # 避免「已打开标签页中的卡片视图不更新，必须重开标签页」
                    mw = self._window_main_widgets.get(win_id)
                    cm = getattr(mw, "_card_manager", None) if mw is not None else None
                    was_visible = cid in self._workbench_card_tabs
                    if not was_visible and cm is not None:
                        try:
                            was_visible = cm.is_card_visible(cid, win_id)
                        except (RuntimeError, AttributeError):
                            was_visible = False
                    self._remove_widget_from_container(win_id, cid, widget)
                    # 显式销毁旧实例：_remove_widget_from_container 仅 UI 清理不触发删除，
                    # 不 deleteLater 会残留旧模块闭包/信号槽引用（Qt 父对象引用链）
                    try:
                        widget.deleteLater()
                    except RuntimeError:
                        pass
                    if was_visible:
                        self._pending_card_restore.append((plugin_name, win_id, cid))
        # 清理 fence 渲染器注册
        self._fence_renderers = {k: v for k, v in self._fence_renderers.items() if v.plugin_name != plugin_name}
        self._plugin_paths.pop(plugin_name, None)
        # 清理 Phase D 四类扩展点注册
        self._sidebar_items = {k: v for k, v in self._sidebar_items.items() if v.plugin_name != plugin_name}
        self._input_buttons = {k: v for k, v in self._input_buttons.items() if v.plugin_name != plugin_name}
        self._context_actions = {k: v for k, v in self._context_actions.items() if v.plugin_name != plugin_name}
        self._settings_cards = {k: v for k, v in self._settings_cards.items() if v.plugin_name != plugin_name}
        # 清理工作区页面槽（Phase G）
        self._workspace_pages = {k: v for k, v in self._workspace_pages.items() if v.plugin_name != plugin_name}
        # 清理右侧工作台页签槽位
        self.unregister_workbench_tabs(plugin_name)
        # 清理标题栏常驻 tab 槽位
        self.unregister_titlebar_tabs(plugin_name)
        # 清理通用区域条目（Phase E）
        for region in self._regions.values():
            region["entries"] = {k: v for k, v in region["entries"].items() if v.plugin_name != plugin_name}
        # 清理 UI 模块槽（Phase F）：仅移除该 plugin 的实现，其余保留
        for module_id, impls in list(self._ui_modules.items()):
            kept = [s for s in impls if s[0] != plugin_name]
            if kept:
                self._ui_modules[module_id] = kept
            else:
                self._ui_modules.pop(module_id, None)
        # 事件总线退订：防止悬挂回调引用已卸载的旧模块闭包
        from app.core.ui_event_bus import UIEventBus

        UIEventBus.get_instance().unsubscribe_plugin(plugin_name)
        self._loaded_plugins.discard(plugin_name)
        logger.info(f"[UIPluginRegistry] Unloaded UI components for plugin: {plugin_name}")
        if had_welcome_tabs:
            self._schedule_welcome_refresh()
        return True

    # ── P2-2：热重载 last-known-good——静默写入兜底轮询 ──────────────

    @staticmethod
    def _compute_ui_signature(plugin_path) -> float:
        """ui/ 目录 mtime 签名（全部文件 mtime 最大值；目录不存在返回 -1）。"""
        ui_dir = Path(plugin_path) / "ui"
        if not ui_dir.is_dir():
            return -1.0
        latest = -1.0
        for f in ui_dir.rglob("*"):
            if f.is_file():
                try:
                    latest = max(latest, f.stat().st_mtime)
                except OSError:
                    continue
        return latest

    def poll_silent_ui_changes(self) -> list:
        """比对 ui/ 目录 mtime 签名，静默写入（错过 watchfiles 事件）触发重载。

        重载复用 load_plugin（失败走既有回滚恢复旧注册，即 last-known-good）。
        Returns: 本轮触发重载的插件名列表。
        """
        reloaded: list = []
        for name, sig in list(self._ui_signatures.items()):
            plugin = self._plugin_paths.get(name)
            if not plugin:
                continue
            current = self._compute_ui_signature(Path(plugin))
            if current == sig:
                continue
            logger.warning(
                f"[UIPluginRegistry] 插件 '{name}' ui 目录签名变化（静默写入兜底触发重载）"
            )
            ok = self.reload_plugin(name, Path(plugin))
            if ok:
                reloaded.append(name)
            self._ui_signatures[name] = self._compute_ui_signature(Path(plugin))
        return reloaded

    def start_signature_watch(self, interval: float = 30.0) -> None:
        """启动 30s 空闲周期签名轮询（QTimer，须在主线程调用；幂等）。"""
        if self._signature_watch_started:
            return
        try:
            from PyQt5.QtCore import QTimer

            self._signature_watch_started = True
            timer = QTimer()
            timer.timeout.connect(self.poll_silent_ui_changes)
            timer.start(int(interval * 1000))
            self._signature_timer = timer
            logger.info(f"[UIPluginRegistry] UI 签名轮询已启动（{interval:.0f}s）")
        except Exception as e:
            self._signature_watch_started = False
            logger.debug(f"[UIPluginRegistry] UI 签名轮询启动失败（无 Qt 环境？）: {e}")

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

        🛡️ DEBUG 日志（2026-08-23 bug fix）：原 except: pass 完全静默，
        invalidate 后重建链任何一环崩溃（QWebEngineView 初始化 / Settings 单例
        异常 / 窗口契约属性缺失）都无法从日志追踪。改为 DEBUG 输出每步决策，
        异常时 WARNING 暴露堆栈，便于排查「欢迎卡片消失 / 新建会话不出现」。
        """
        from loguru import logger

        total = len(self._window_main_widgets)
        skipped_no_method = 0
        skipped_no_cache = 0
        invalidated = 0
        rescheduled = 0
        for mw in list(self._window_main_widgets.values()):
            try:
                if not hasattr(mw, "_invalidate_welcome_card"):
                    skipped_no_method += 1
                    logger.debug(
                        f"[UIPluginRegistry] _refresh_welcome_cards: window={getattr(mw, '_window_id', '?')} "
                        f"缺 _invalidate_welcome_card 方法，跳过"
                    )
                    continue
                window_id = getattr(mw, "_window_id", None)
                cache = getattr(mw, "_welcome_card_cache", {})
                if window_id is None or window_id not in cache:
                    skipped_no_cache += 1
                    continue  # 尚无缓存卡片，正常启动路径无需刷新
                mw._invalidate_welcome_card()
                invalidated += 1
                if getattr(mw, "_displayed_session_id", None) is None:
                    # 当前正显示欢迎卡片（无会话上下文）→ 立即重建，避免空白。
                    # 走交错时间片调度（_schedule_initial_welcome），避免 N 个窗口
                    # 的 QWebEngineView 重建（100-500ms/个）在同一事件批次连续
                    # 同步执行卡死 UI（对齐 _create_new_session 的 C2 优化）。
                    if hasattr(mw, "_schedule_initial_welcome"):
                        mw._schedule_initial_welcome()
                        rescheduled += 1
                    else:
                        mw._show_initial_welcome()
                        rescheduled += 1
            except Exception as e:  # noqa: BLE001
                wid = getattr(mw, "_window_id", "<unknown>")
                logger.warning(f"[UIPluginRegistry] _refresh_welcome_cards: window={wid} 处理失败: {e}")
        logger.debug(
            f"[UIPluginRegistry] _refresh_welcome_cards: total={total} "
            f"skipped_no_method={skipped_no_method} skipped_no_cache={skipped_no_cache} "
            f"invalidated={invalidated} rescheduled={rescheduled}"
        )

    def _remove_widget_from_container(self, window_id: str, card_id: str, widget) -> None:
        """从容器布局和 CardManager 中移除指定 widget（不触发删除，仅 UI 清理）

        Args:
            window_id: 窗口 ID
            card_id: 卡片 ID
            widget: 要移除的 widget 控件
        """
        try:
            # 0. 工作台卡片 tab：从工作台页签条摘除（widget 由下方统一销毁）
            if card_id in self._workbench_card_tabs:
                self._workbench_card_tabs.pop(card_id, None)
                for cards in self._workbench_card_scopes.values():
                    cards.discard(card_id)
                host = self._resolve_global_host()
                panel = getattr(host, "workbench_panel", None) if host is not None else None
                if panel is not None:
                    try:
                        panel.close_card_tab(card_id)
                    except Exception:
                        pass

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

    def get_card_widget(self, card_id: str, window_id: str = "") -> Optional[Any]:
        """获取浮动卡在某窗口的实例（懒创建：未显示过则 None）

        供插件在 toggle 显示后取回实例（如 autoloop 运行卡绑定控制器）。
        window_id 为空时回退全局兼容缓存（单窗口模式）。
        """
        if window_id and window_id in self._card_widget_instances:
            return self._card_widget_instances[window_id].get(card_id)
        for instances in self._card_widget_instances.values():
            w = instances.get(card_id)
            if w is not None:
                return w
        return None

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
        # per-tab 可见集合随窗口销毁清理；若被关闭的是当前投影标签，重置作用域
        self._tab_card_visibility.pop(window_id, None)
        if self._active_tab_scope == window_id:
            self._active_tab_scope = None
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
        self._fence_renderers.clear()
        self._plugin_paths.clear()
        self._message_factories.clear()
        self._floating_cards.clear()
        self._welcome_tabs.clear()
        self._welcome_actions.clear()
        self._mention_providers.clear()
        self._loaded_plugins.clear()
        self._main_widget = None
        self._ui_command_names.clear()
        self._card_widget_instances.clear()
        self._context_provider = None
        self._window_main_widgets.clear()
        self._context_providers.clear()
        self._welcome_refresh_pending = False
        self._tab_card_visibility.clear()
        self._active_tab_scope = None
        self._tab_sync_in_progress = False
        # 重置单例本身（建议）——让下一次 get_instance() 重新创建，
        # 避免测试间残留 _instance 上的实例属性
        UIPluginRegistry._instance = None
