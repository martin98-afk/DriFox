# -*- coding: utf-8 -*-
"""UI surface 的能力 provider（host = MainWidget）。

一条能力一个 provider 函数；需要插件身份或 registry 时用双参 (host, ctx)。
provider 是纯函数，不持有窗口引用（host 由 build_pool 传入）。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class _BusSignal:
    """把 UIEventBus 适配成 signal 形状，供 release_plugin 统一断开。

    UIEventBus.unsubscribe 需要 (event, callback)，故登记时记住事件名。
    """

    __slots__ = ("_bus", "_event")

    def __init__(self, bus, event: str):
        self._bus = bus
        self._event = event

    def disconnect(self, slot):
        self._bus.unsubscribe(self._event, slot)


def _resolve_tab_window():
    """延迟解析 TabManagerWindow 单例（测试可 monkeypatch 本函数）。"""
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        return TabManagerWindow.get_instance()
    except Exception:
        return None


# ──────────────────── 对话流 / 窗口 ────────────────────


def _stream_subscribe(host: Any, ctx: Any):
    """返回 subscribe(events, callback) -> 已绑定信号名列表。

    events 支持两种形态：
    - 列表/字符串 + 单一 callback：所有信号共用一个回调（按参数形状自行分派）
    - 映射 {事件名: 回调}：每个信号各自的回调（同签名信号无法按参数区分时用这个）

    连接登记到 CapabilityRegistry.track_subscription，插件卸载时由
    UIPluginRegistry.unload_plugin → release_plugin 统一断开。
    """

    def subscribe(events, callback=None):
        backend = getattr(host, "backend", None)
        if backend is None:
            raise RuntimeError("backend 未就绪，无法订阅对话流事件")
        if isinstance(events, dict):
            pairs = list(events.items())
        else:
            names = [events] if isinstance(events, str) else list(events)
            pairs = [(name, callback) for name in names]
        bound = []
        for name, cb in pairs:
            signal = getattr(backend, name, None)
            if not hasattr(signal, "connect") or cb is None:
                logger.warning(f"[Capability] backend 无可用信号 {name}，跳过订阅")
                continue
            signal.connect(cb)
            ctx.registry.track_subscription(ctx.plugin, signal, cb)
            bound.append(name)
        return bound

    return subscribe


def _window_active(host: Any):
    def current():
        tm = _resolve_tab_window()
        return tm.get_current_window() if tm is not None else None

    return current


def _window_host(host: Any):
    """宿主顶层窗口（弹窗 / InfoBar 的合适 parent）。

    插件原先自己 `from app.widgets.tab_manager_window import TabManagerWindow;
    TabManagerWindow.get_instance() or self.window()`，只为拿一个 parent。
    这里封装掉：优先 Tab 管理器（多窗口模式下的顶层），回退到卡片窗口。
    """

    def resolve():
        tm = _resolve_tab_window()
        if tm is not None:
            return tm
        try:
            return host.window()
        except Exception:
            return None

    return resolve


def _window_current(host: Any):
    """当前窗口的**语义快照**，不是窗口对象本身。

    插件原先 `tm.get_current_window()` 后再摸 `_current_session_id` /
    `_current_project` / `_current_workdir` 私有字段。这里一次性给出这些字段
    的语义副本：插件拿不到窗口对象，自然无从摸私有状态。
    """

    def snapshot():
        tm = _resolve_tab_window()
        win = tm.get_current_window() if tm is not None else None
        if win is None:
            return None
        project = getattr(win, "_current_project", "") or ""
        cache = getattr(win, "_current_workdir", None) or {}
        workdir = cache.get(project) or ""
        if not workdir:
            resolver = getattr(win, "_resolve_project_workdir", None)
            if callable(resolver):
                try:
                    workdir = resolver() or ""
                except Exception:
                    workdir = ""
        return {
            "window_id": getattr(win, "_window_id", ""),
            "backend": getattr(win, "backend", None),
            "session_id": getattr(win, "_current_session_id", ""),
            "project": project,
            "workdir": workdir,
        }

    return snapshot


def _window_workbench(host: Any):
    """打开 / 刷新右侧工作台页。"""

    def open_page(key: str) -> None:
        tm = _resolve_tab_window()
        if tm is None:
            return
        fn = getattr(tm, "open_workbench_memory", None)
        if callable(fn):
            fn(key)

    def refresh() -> None:
        tm = _resolve_tab_window()
        if tm is None:
            return
        fn = getattr(tm, "refresh_workbench", None)
        if callable(fn):
            fn()

    return {"open": open_page, "refresh": refresh}


def _ui_events(host: Any, ctx: Any):
    """UI 事件总线的订阅 / 发布（订阅随插件卸载自动退订）。"""

    def api() -> dict:
        from app.core.infra.ui_event_bus import UIEventBus

        bus = UIEventBus.get_instance()

        def subscribe(event: str, callback):
            bus.subscribe(event, callback, plugin_name=ctx.plugin or None)
            # 登记到能力注册表：release_plugin 时按插件名退订。
            # UIEventBus 按 callback 退订，故包一层适配成 signal.disconnect 形状。
            ctx.registry.track_subscription(ctx.plugin, _BusSignal(bus, event), callback)
            return True

        def publish(event: str, **payload):
            bus.publish(event, **payload)

        return {"subscribe": subscribe, "publish": publish}

    return api


def _window_list(host: Any):
    def ids():
        tm = _resolve_tab_window()
        if tm is None:
            return set()
        return {getattr(w, "_window_id", "") for w in getattr(tm, "_windows", [])}

    return ids


def _window_events(host: Any, ctx: Any):
    def subscribe(callback):
        tm = _resolve_tab_window()
        if tm is None or not hasattr(tm, "activeTabChanged"):
            raise RuntimeError("TabManagerWindow 未就绪，无法订阅窗口事件")
        tm.activeTabChanged.connect(callback)
        ctx.registry.track_subscription(ctx.plugin, tm.activeTabChanged, callback)
        return True

    return subscribe


# ──────────────────── 模型 / 智能体 ────────────────────


def _model_config(host: Any):
    return host._get_current_model_config


def _provider_config(host: Any):
    return host._resolve_provider_config


def _model_schema(host: Any):
    return host._build_agent_tools_schema


def _model_metadata(host: Any):
    def metadata() -> dict:
        cfg = host._get_current_model_config() or {}
        return {
            "provider": cfg.get("provider_name") or cfg.get("provider") or "",
            "model": cfg.get("模型名称") or cfg.get("model") or "",
        }

    return metadata


def _agent_manager(host: Any):
    backend = getattr(host, "backend", None)
    return getattr(backend, "agent_manager", None) if backend else None


def _agent_prompt(host: Any):
    def prompt(name: str) -> str:
        am = _agent_manager(host)
        agent = am.get_agent(name) if am else None
        return agent.prompt if agent else ""

    return prompt


# ──────────────────── 工作目录 ────────────────────


def _workdir_read(host: Any):
    return lambda: host._resolve_project_workdir() or ""


def _workdir_write(host: Any):
    def set_workdir(path: str) -> None:
        """一次语义动作：实例缓存 + tool_executor 同步 + 团队广播 + UI 同步。

        插件不需要知道这三个内部字段存在（worktree-manager 曾自己拼这三步）。
        """
        project = getattr(host, "_current_project", "")
        path = str(path or "")
        if project and path:
            host._current_workdir[project] = path
        backend = getattr(host, "backend", None)
        executor = getattr(backend, "tool_executor", None) if backend else None
        if executor is not None and path:
            executor.set_workdir(path)
        if path:
            try:
                host._broadcast_team_workdir(path)
            except Exception:
                pass
        try:
            host._sync_working_directory()
        except Exception:
            pass

    return set_workdir


# ──────────────────── 上下文 / 引擎 ────────────────────


def _theme_read(host: Any):
    return host._resolve_theme_snapshot


def _context_compactor(host: Any):
    backend = getattr(host, "backend", None)
    ce = getattr(backend, "chat_engine", None) if backend else None
    return getattr(ce, "_compactor", None) if ce else None


def _engine_stack(host: Any):
    def factory():
        from app.core.conversation.stack_factory import ConversationStackImpl

        return ConversationStackImpl()

    return factory


def _engine_session(host: Any):
    def create(engine_name: str, **kwargs):
        from app.core.conversation.engine_session import EngineSessionImpl

        backend = getattr(host, "backend", None)
        return EngineSessionImpl(
            engine_name=engine_name,
            get_model_config=host._get_current_model_config,
            tool_executor=getattr(backend, "tool_executor", None) if backend else None,
            agent_manager=getattr(backend, "agent_manager", None) if backend else None,
            backend=backend,
            **kwargs,
        )

    return create


def _session_write(host: Any):
    return host._save_messages_to_session


# ──────────────────── 界面 ────────────────────


def _input_read(host: Any):
    """读输入框文本与附件列表。"""

    def read() -> dict:
        area = getattr(host, "input_area", None)
        text = area.toPlainText() if area is not None else ""
        attachments = list(getattr(host, "_attachments", None) or [])
        return {"text": text, "attachments": attachments}

    return read


def _input_write(host: Any):
    """写入输入框文本并聚焦（可选追加模式）。"""

    def write(text: str, append: bool = False, focus: bool = True) -> None:
        area = getattr(host, "input_area", None)
        if area is None:
            raise RuntimeError("输入框不可用")
        if append:
            area.setPlainText(area.toPlainText() + "\n" + text)
        else:
            area.setPlainText(text)
        if focus:
            area.setFocus()
            cursor = area.textCursor()
            cursor.movePosition(cursor.End)
            area.setTextCursor(cursor)

    return write


def _ui_notify(host: Any):
    def notify(title: str, message: str) -> None:
        from qfluentwidgets import InfoBar, InfoBarPosition

        InfoBar.success(
            title,
            message,
            parent=_resolve_tab_window() or host.window(),
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    return notify


def _ui_card_hide(host: Any):
    return lambda card_id: host._card_manager.hide_card(card_id, host._window_id)


def _ui_exclusive(host: Any):
    def exclusive(source_id: str, enabled: bool = True) -> None:
        if enabled:
            host.enter_exclusive_ui_mode(source_id)
        else:
            host.exit_exclusive_ui_mode(source_id)

    return exclusive


def _enter_exclusive(host: Any):
    return host.enter_exclusive_ui_mode


def _exit_exclusive(host: Any):
    return host.exit_exclusive_ui_mode


def _sync_working_directory(host: Any):
    return host._sync_working_directory


# ──────────────────── 工具 ────────────────────


def _tool_execute(host: Any):
    backend = getattr(host, "backend", None)
    return lambda: getattr(backend, "tool_executor", None) if backend else None


def _command_read(host: Any):
    """命令列表查询。"""

    def api() -> dict:
        from app.core.commands.command_manager import CommandManager

        mgr = CommandManager.get_instance()
        return {"all": mgr.get_all_commands}

    return api


def _command_reload(host: Any):
    """命令热重载（改命令文件后生效）。"""

    def api() -> dict:
        def reload_all() -> None:
            from app.core.commands.builtin_commands import reload_all_commands

            reload_all_commands()

        return {"reload": reload_all}

    return api


def _plugin_control(host: Any):
    """插件启停 / 重载控制（含 watcher 抑制）。"""

    def api() -> dict:
        def suppress_watcher() -> None:
            from app.core.services.plugin_host_service import suppress_plugin_watcher

            suppress_plugin_watcher()

        def resume_watcher() -> None:
            from app.core.services.plugin_host_service import resume_plugin_watcher

            resume_plugin_watcher()

        def reload(name: str) -> None:
            from app.core.services.plugin_host_service import PluginHostService

            PluginHostService.get_instance().reload_plugin_targeted(name)

        return {"suppress_watcher": suppress_watcher, "resume_watcher": resume_watcher,
                "reload": reload}

    return api


def _plugin_read(host: Any):
    """已安装插件列表 / 元数据查询。"""

    def api() -> dict:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        return {
            "get": pm.get_plugin,
            "enabled": pm.get_enabled_plugins,
            "is_enabled": pm.is_enabled,
        }

    return api


def _tool_read(host: Any):
    """工具注册表的只读查询面（不含注册/修改，那是 tool.inject）。"""

    def api() -> dict:
        from app.tools.registry import ToolRegistry

        reg = ToolRegistry.get_instance()
        return {
            "names": reg.names,
            "schemas": reg.schemas,
            "get": reg.get,
            "has": reg.has,
            "groups": reg.group_map,
            "is_no_prune": reg.is_no_prune,
            "is_no_offload": reg.is_no_offload,
            "is_danger": reg.get_danger,
            "is_protected": reg.is_protected,
        }

    return api


def _tool_inject(host: Any):
    """向工具注册表注入 schema 过滤器（改变 LLM 可见工具集）。"""

    def api() -> dict:
        from app.tools.registry import ToolRegistry

        reg = ToolRegistry.get_instance()
        return {
            "register_schema_filter": reg.register_schema_filter,
            "unregister_schema_filter": reg.unregister_schema_filter,
            "apply_schema_filters": reg.apply_schema_filters,
        }

    return api


# ──────────────────── 基础（恒放行） ────────────────────


def _state_kv(host: Any):
    """窗口级 KV：插件自己的沙箱，不触达宿主状态。"""
    kv = getattr(host, "_cap_kv", None)
    if kv is None:
        kv = {}
        host._cap_kv = kv
    return {
        "get": kv.get,
        "set": lambda k, v: kv.__setitem__(k, v),
        "delete": lambda k: kv.pop(k, None),
    }


def _plugin_log(host: Any, ctx: Any):
    return logger.bind(plugin=ctx.plugin or "(builtin)")


# ──────────────────── 逃生舱 ────────────────────


def _host_raw(host: Any):
    return host


def register(registry) -> None:
    reg = registry
    # 对话流 / 窗口
    reg.register("stream.subscribe", _stream_subscribe, surface="ui", owner="core:ui")
    reg.register("window.active", _window_active, surface="ui", owner="core:ui")
    reg.register("window.host", _window_host, surface="ui", owner="core:ui")
    reg.register("window.current", _window_current, surface="ui", owner="core:ui")
    reg.register("window.host", _window_host, surface="ui", owner="core:ui")
    reg.register("window.list", _window_list, surface="ui", owner="core:ui")
    reg.register("window.workbench", _window_workbench, surface="ui", owner="core:ui")
    reg.register("window.events", _window_events, surface="ui", owner="core:ui")
    # 模型 / 智能体
    reg.register("model.config", _model_config, surface="ui", owner="core:ui")
    reg.register("model.config", _provider_config, surface="ui", owner="core:ui", legacy_key="get_provider_config")
    reg.register("model.schema", _model_schema, surface="ui", owner="core:ui")
    reg.register("model.metadata", _model_metadata, surface="ui", owner="core:ui")
    reg.register("agent.read", _agent_manager, surface="ui", owner="core:ui")
    reg.register("agent.read", _agent_prompt, surface="ui", owner="core:ui", legacy_key="get_agent_prompt")
    # 工作目录
    reg.register("workdir.read", _workdir_read, surface="ui", owner="core:ui")
    reg.register("workdir.write", _workdir_write, surface="ui", owner="core:ui")
    reg.register(
        "workdir.write", _sync_working_directory, surface="ui", owner="core:ui", legacy_key="sync_working_directory"
    )
    # 上下文 / 引擎
    reg.register("theme.read", _theme_read, surface="ui", owner="core:ui")
    reg.register("context.compact", _context_compactor, surface="ui", owner="core:ui")
    reg.register("engine.stack", _engine_stack, surface="ui", owner="core:ui")
    reg.register("engine.session", _engine_session, surface="ui", owner="core:ui")
    reg.register("session.write", _session_write, surface="ui", owner="core:ui")
    # 界面
    reg.register("input.read", _input_read, surface="ui", owner="core:ui")
    reg.register("input.write", _input_write, surface="ui", owner="core:ui")
    reg.register("ui.notify", _ui_notify, surface="ui", owner="core:ui")
    reg.register("ui.events", _ui_events, surface="ui", owner="core:ui")
    reg.register("command.read", _command_read, surface="ui", owner="core:ui")
    reg.register("command.reload", _command_reload, surface="ui", owner="core:ui")
    reg.register("plugin.read", _plugin_read, surface="ui", owner="core:ui")
    reg.register("plugin.control", _plugin_control, surface="ui", owner="core:ui")
    reg.register("ui.card.hide", _ui_card_hide, surface="ui", owner="core:ui")
    reg.register("ui.exclusive", _ui_exclusive, surface="ui", owner="core:ui")
    reg.register(
        "ui.exclusive", _enter_exclusive, surface="ui", owner="core:ui", legacy_key="enter_exclusive_ui_mode"
    )
    reg.register(
        "ui.exclusive", _exit_exclusive, surface="ui", owner="core:ui", legacy_key="exit_exclusive_ui_mode"
    )
    # 工具
    reg.register("tool.execute", _tool_execute, surface="ui", owner="core:ui")
    reg.register("tool.read", _tool_read, surface="ui", owner="core:ui")
    reg.register("tool.inject", _tool_inject, surface="ui", owner="core:ui")
    # 基础
    reg.register("state.kv", _state_kv, surface="ui", owner="core:ui")
    reg.register("plugin.log", _plugin_log, surface="ui", owner="core:ui")
    # 逃生舱
    reg.register("host.raw", _host_raw, surface="ui", owner="core:ui")
