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


def _resolve_active_host(host: Any):
    """host 为空时（无窗口的调用点）解析一个活跃主窗口。

    工具/hook 类调用点没有 MainWidget，而模型配置只存在于主窗口内存态
    （密钥模式下磁盘 app.config 是密文）。经 UIPluginRegistry 拿活跃窗口。
    """
    if host is not None:
        return host
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        return getattr(reg, "_main_widget", None) or next(
            iter(getattr(reg, "_window_main_widgets", {}).values()), None
        )
    except Exception:
        return None


def _model_config(host: Any):
    real = _resolve_active_host(host)

    def current() -> dict:
        if real is None:
            return {}
        try:
            return real._get_current_model_config() or {}
        except Exception:
            return {}

    return current


def _provider_config(host: Any):
    real = _resolve_active_host(host)

    def resolve(provider: str = "", model: str = "") -> dict:
        if real is None:
            return {}
        try:
            return real._resolve_provider_config(provider, model) or {}
        except Exception:
            return {}

    return resolve


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


def _session_read(host: Any):
    """当前会话的只读快照（插件拿消息内容，不拿会话对象）。

    给副本而非对象：插件无法直接 mutate 会话（改会话走 session.write / manage）。
    """

    def read() -> dict:
        backend = getattr(host, "backend", None)
        session = backend.get_current_session() if backend is not None else None
        if session is None:
            return {"session_id": "", "messages": [], "system_prompt": ""}
        return {
            "session_id": str(getattr(session, "session_id", "") or ""),
            "messages": list(getattr(session, "messages", None) or []),
            "system_prompt": str(getattr(session, "system_prompt", "") or ""),
        }

    return read


def _session_list(host: Any):
    """历史会话列表（只读元数据，不含完整消息）。"""

    def list_sessions() -> list:
        sm = getattr(host, "session_manager", None)
        if sm is None:
            return []
        out = []
        for s in getattr(sm, "sessions", None) or []:
            out.append(
                {
                    "session_id": str(getattr(s, "session_id", "") or ""),
                    "title": str(getattr(s, "title", "") or ""),
                    "message_count": len(getattr(s, "messages", None) or []),
                }
            )
        return out

    return list_sessions


def _session_invalidate(host: Any):
    """失效所有窗口的会话 system_prompt 缓存。

    用途：切换助手 / 改身份后强制重建 system_prompt（重新触发
    BuildSystemPrompt hooks）。插件原先自己遍历 ChatBackend._active_instances
    和 ChatEngine._instances 摸私有类属性，这里封装成一次语义动作。
    """

    def invalidate(system_prompt_only: bool = True) -> int:
        n = 0
        try:
            from app.core.conversation.backend import ChatBackend

            for backend in list(getattr(ChatBackend, "_active_instances", None) or []):
                sm = getattr(backend, "session_manager", None)
                if sm is None:
                    continue
                sessions = getattr(sm, "sessions", None)
                if isinstance(sessions, dict):
                    items = list(sessions.values())
                else:
                    try:
                        items = list(sm.get_all_sessions() or [])
                    except Exception:
                        items = []
                for session in items:
                    try:
                        session.system_prompt = ""
                        if hasattr(session, "_system_prompt_agent"):
                            session._system_prompt_agent = ""
                        n += 1
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            from app.core.engines.ui.engine import ChatEngine

            for engine in list(getattr(ChatEngine, "_instances", None) or []):
                try:
                    engine._invalidate_session_system_prompt_cache()
                except Exception:
                    pass
        except Exception:
            pass
        return n

    return invalidate


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


def _ui_dialog(host: Any):
    """标准对话框（跟随主题，parent 由主程序解析）。

    返回 dict of callable：
    - confirm(title, content)                  -> bool
    - prompt(title, hint, default)             -> str | None（取消返回 None）
    - pick_file(title, start_dir, filter)      -> str | ""
    - pick_dir(title, start_dir)               -> str | ""
    - save_file(title, start_dir, filter)      -> str | ""

    对话框是**同步模态**（exec_），因此直接返回结果；这与 UI 事件总线那类
    异步回调不同，插件不用自己接信号。
    """

    def _parent():
        return _resolve_tab_window() or (host.window() if host is not None else None)

    def confirm(title: str, content: str = "", confirm_text: str = "确认") -> bool:
        from app.widgets.common_dialogs import ConfirmDialog

        dlg = ConfirmDialog(title, content, confirm_text=confirm_text, parent=_parent())
        result = {"ok": False}
        dlg.confirmed.connect(lambda: result.__setitem__("ok", True))
        dlg.exec_()
        return bool(result["ok"])

    def prompt(title: str, hint: str = "", default_text: str = "") -> object:
        from app.widgets.common_dialogs import SingleInputDialog

        dlg = SingleInputDialog(
            title, hint=hint, default_text=default_text, parent=_parent()
        )
        box = {}
        dlg.confirmed.connect(lambda text: box.__setitem__("text", text))
        dlg.exec_()
        return box.get("text")

    def pick_file(title: str = "选择文件", start_dir: str = "", filter: str = "") -> str:
        from PyQt5.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(_parent(), title, start_dir, filter)
        return str(path or "")

    def pick_dir(title: str = "选择目录", start_dir: str = "") -> str:
        from PyQt5.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(_parent(), title, start_dir)
        return str(path or "")

    def save_file(title: str = "保存文件", start_dir: str = "", filter: str = "") -> str:
        from PyQt5.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(_parent(), title, start_dir, filter)
        return str(path or "")

    def api() -> dict:
        return {
            "confirm": confirm,
            "prompt": prompt,
            "pick_file": pick_file,
            "pick_dir": pick_dir,
            "save_file": save_file,
        }

    return api


def _ui_card_show(host: Any):
    """显示已注册的浮动卡（与 ui.card.hide 对称）。"""

    def show(card_id: str) -> bool:
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance()._show_floating_card(
                card_id, main_widget=host if host is not None else None
            )
            return True
        except Exception as e:
            logger.warning(f"[Capability] 显示浮动卡 {card_id} 失败: {e}")
            return False

    return show


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


def _tool_policy_api():
    """工具权限策略只读面（toggles / policy / off_behavior 的判定）。"""

    def api() -> dict:
        from app.core.tools.tool_permission_controller import resolve_tool_off_policy
        from app.utils.config import Settings

        def off_policy(name: str) -> str:
            s = Settings.get_instance()
            toggles = dict(s.tool_toggles.value or {})
            if toggles.get(name, True) is not False:
                return "allow"
            policies = dict(s.tool_permission_policy.value or {})
            behavior = s.tool_off_behavior.value or "deny"
            try:
                return str(resolve_tool_off_policy(name, None, policies, behavior) or "")
            except Exception:
                return ""

        def toggles() -> dict:
            return dict(Settings.get_instance().tool_toggles.value or {})

        return {"off_policy": off_policy, "toggles": toggles}

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
            # 工具权限策略（tool_search 中转前的安全判定）
            "policy": _tool_policy_api(),
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


def _team_read(host: Any):
    """团队只读面（成员表 / 模板 / 任务状态）。

    只暴露只读查询；派任务、改状态等写操作不在此能力内。
    """

    def api() -> dict:
        from app.core.team.team_manager import TeamManager

        tm = TeamManager.get_instance()
        return {
            "members": tm.get_members,
            "template": tm.get_template,
            "run_ids": tm.get_team_run_ids,
            "label_by_run": tm.get_team_label_by_run,
            "member_capability": tm.get_member_capability,
            "member_busy_status": tm.get_member_busy_status,
            "running_tasks": tm.get_running_tasks,
            "pending_tasks": tm.get_pending_tasks,
            "find_member": tm.find_member,
        }

    return api


def _cache_purge(host: Any):
    """清理主程序进程内 LRU 缓存（HTML 渲染 / token 估算 / grep 编译等）。"""

    def purge() -> bool:
        try:
            from app.main_widget import _cleanup_global_lru_caches

            _cleanup_global_lru_caches()
            return True
        except Exception as e:
            logger.warning(f"[Capability] 清理进程内缓存失败: {e}")
            return False

    return purge


def _host_raw(host: Any):
    return host


def register(registry) -> None:
    reg = registry
    # 对话流 / 窗口
    reg.register("stream.subscribe", _stream_subscribe, surface="ui", owner="core:ui")
    reg.register("window.active", _window_active, surface="ui", owner="core:ui")
    reg.register("window.host", _window_host, surface="ui", owner="core:ui")
    reg.register("window.current", _window_current, surface="ui", owner="core:ui")
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
    reg.register("session.read", _session_read, surface="ui", owner="core:ui")
    reg.register("session.list", _session_list, surface="ui", owner="core:ui")
    reg.register("session.invalidate", _session_invalidate, surface="ui", owner="core:ui")
    reg.register("session.write", _session_write, surface="ui", owner="core:ui")
    # 界面
    reg.register("input.read", _input_read, surface="ui", owner="core:ui")
    reg.register("input.write", _input_write, surface="ui", owner="core:ui")
    reg.register("ui.notify", _ui_notify, surface="ui", owner="core:ui")
    reg.register("ui.dialog", _ui_dialog, surface="ui", owner="core:ui")
    reg.register("ui.card.show", _ui_card_show, surface="ui", owner="core:ui")
    reg.register("ui.card.hide", _ui_card_hide, surface="ui", owner="core:ui")
    reg.register("ui.events", _ui_events, surface="ui", owner="core:ui")
    reg.register("command.read", _command_read, surface="ui", owner="core:ui")
    reg.register("command.reload", _command_reload, surface="ui", owner="core:ui")
    reg.register("plugin.read", _plugin_read, surface="ui", owner="core:ui")
    reg.register("plugin.control", _plugin_control, surface="ui", owner="core:ui")
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
    # 团队
    reg.register("team.read", _team_read, surface="ui", owner="core:ui")
    # 进程级
    reg.register("cache.purge", _cache_purge, surface="ui", owner="core:ui")
    # 逃生舱
    reg.register("host.raw", _host_raw, surface="ui", owner="core:ui")
