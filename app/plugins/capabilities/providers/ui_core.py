# -*- coding: utf-8 -*-
"""UI surface 的能力 provider（host = MainWidget）。

一条能力一个 provider 函数；需要插件身份或 registry 时用双参 (host, ctx)。
provider 是纯函数，不持有窗口引用（host 由 build_pool 传入）。
"""

from __future__ import annotations

import threading
from typing import Any

from loguru import logger

from app.plugins.capabilities.providers import model_meta, net_sync, process_core
from app.utils.info_bar_guard import post_to_main


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


def _resolve_window_by_id(window_id: str) -> Any:
    """按 _window_id 找存活聊天窗口（找不到返回 None）。

    与 window.list 同源（遍历 TabManagerWindow._windows），供 window_id
    定向能力解析目标窗口。空 id 返回 None（调用方自行回退宿主窗口）。
    """
    wid = str(window_id or "").strip()
    if not wid:
        return None
    tm = _resolve_tab_window()
    if tm is None:
        return None
    for win in getattr(tm, "_windows", None) or []:
        if str(getattr(win, "_window_id", "") or "") == wid:
            return win
    return None


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


def _stream_errors(host: Any, ctx: Any):
    """stream.errors：订阅 backend.conversation_error(dict) 结构化报错事件。

    负载字段：window_id/session_id/provider/model/message/error_type/timestamp。
    连接登记到 CapabilityRegistry.track_subscription，插件卸载时由
    release_plugin 统一断开（与 _stream_subscribe 同模式）。
    """

    def subscribe(callback) -> bool:
        backend = getattr(host, "backend", None)
        if backend is None:
            raise RuntimeError("backend 未就绪，无法订阅对话报错事件")
        signal = getattr(backend, "conversation_error", None)
        if not hasattr(signal, "connect"):
            logger.warning("[Capability] backend 无 conversation_error 信号，跳过订阅")
            return False
        signal.connect(callback)
        ctx.registry.track_subscription(ctx.plugin, signal, callback)
        return True

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

    **host 优先**：浮动卡 / 工作台页等调用点的 host 就是其宿主窗口；多窗口
    多项目下 TabManager 选中标签可能与卡片宿主不同，此处必须回答「宿主窗口」
    的快照（这是 window.current 与 window.active 的语义分界），host 无效
    （None / 非窗口）才退到当前选中窗口。
    """

    def snapshot():
        # host 必须是对话窗口（有 backend）；__global__ 容器 / popout 壳这类
        # 带 _window_id 的非窗口宿主不算，否则拿到空快照还静默回落错窗口
        win = host if (getattr(host, "_window_id", "") and hasattr(host, "backend")) else None
        if win is None:
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


def _question_control(host: Any, ctx: Any):
    """提问卡控制面：订阅生命周期 / 读快照 / 代答 / 倒计时。

    只暴露提问交互所需的四件事，插件据此自行实现超时策略（跳过 / AI 代答 /
    不限时）。订阅随插件卸载自动退订（registry.track_subscription）。

    value_style=value：provider 直接返回 facade dict（不是需要再调一次的工厂），
    调用侧 ``pool["question.control"]["answer"](...)``。
    """
    real = host if host is not None else _resolve_active_host(None)

    def subscribe(callback):
        if real is None or not hasattr(real, "question_control_subscribe"):
            raise RuntimeError("宿主窗口不支持提问控制")
        if not real.question_control_subscribe(callback):
            return False
        ctx.registry.track_subscription(ctx.plugin, _QuestionSignal(real, callback), callback)
        return True

    def snapshot() -> dict:
        return dict(real.question_control_snapshot() or {}) if real is not None else {}

    def answer(text: str) -> bool:
        return bool(real.question_control_answer(str(text or ""))) if real is not None else False

    def countdown(seconds: int) -> bool:
        return bool(real.question_control_countdown(int(seconds or 0))) if real is not None else False

    def stop_countdown() -> bool:
        return bool(real.question_control_stop_countdown()) if real is not None else False

    def unsubscribe(callback) -> bool:
        return bool(real.question_control_unsubscribe(callback)) if real is not None else False

    return {
        "subscribe": subscribe,
        "unsubscribe": unsubscribe,
        "snapshot": snapshot,
        "answer": answer,
        "countdown": countdown,
        "stop_countdown": stop_countdown,
    }


class _QuestionSignal:
    """把「从宿主监听者列表移除」适配成 signal.disconnect 形状（供能力注册表退订）。"""

    def __init__(self, widget: Any, callback: Any):
        self._widget = widget
        self._callback = callback

    def disconnect(self, *_args) -> None:  # noqa: D401 - 与 Qt signal 同形
        try:
            self._widget.question_control_unsubscribe(self._callback)
        except Exception:  # noqa: BLE001 - 窗口已销毁时静默
            pass


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
    （密钥模式下磁盘 app.config 是密文）。

    ★ 走 ``resolve.active_main_widget``：多标签下必须取用户此刻所在标签页。
    ``UIPluginRegistry._main_widget`` 是「最后构造的窗口」，切标签页不更新。
    """
    if host is not None:
        return host
    from app.plugins.capabilities.resolve import active_main_widget

    return active_main_widget()


def _model_config(host: Any):
    real = _resolve_active_host(host)

    def current(config_id: str = "", model_name: str = "") -> dict:
        """当前模型配置；传入 config_id 时按该服务商取（utility_model 场景）。

        两种形态共用同一能力：无参 = 本窗口当前模型；带参 = 指定
        服务商/模型（内部走 _resolve_provider_config 的五级匹配，回填空密钥）。
        """
        if real is None:
            return {}
        try:
            if config_id:
                return dict(real._resolve_provider_config(config_id, model_name) or {})
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

def _subagent_manager(host: Any):
    """子智能体窗口服务（按 window_id 取插件注册的门面；未就绪返回 None）。

    管理器归 system-subagent 插件所有，主程序不持有实例：插件把每窗口门面
    写入 ``WindowServiceRegistry``，这里只做按窗口查询。
    """
    try:
        from app.plugins.registries.window_services import get_window_services

        return get_window_services().get("subagent", str(getattr(host, "_window_id", "") or ""))
    except Exception as e:  # noqa: BLE001 - 服务表不可用按未加载处理
        logger.debug(f"[Capability] 子智能体服务查询失败: {e}")
        return None


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


def _branch_refresh(host: Any):
    """刷新标题栏分支标签（git 分支名变了之后调用）。

    为什么要单独的语义能力：分支标签的检测有 30s 类级缓存
    （WorktreeService._branch_cache），切分支后缓存里还是旧分支名。
    插件自己清不到那份缓存（在别的模块），也调不到主程序的刷新入口。
    这里由宿主统一做「失效缓存 + 重新检测」，插件只需说「分支变了」。
    """

    def refresh() -> None:
        # 1) 失效分支缓存：worktree-manager 的 service 缓存与 detector 缓存
        try:
            from app.plugins.sdk import GitWorktreeDetector

            GitWorktreeDetector._branch_cache.clear()
        except Exception:
            pass
        try:
            import sys

            mod = sys.modules.get("ui_plugin_worktree_manager.service")
            svc_cls = getattr(mod, "WorktreeService", None)
            if svc_cls is not None:
                svc_cls._branch_cache.clear()
        except Exception:
            pass

        # 2) 让宿主窗口重新检测并上屏
        backend = getattr(host, "backend", None)
        fn = getattr(host, "_update_branch", None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass
        # 回退：直接走 service（宿主没有 _update_branch 时）
        try:
            import sys

            mod = sys.modules.get("ui_plugin_worktree_manager.service")
            svc_cls = getattr(mod, "WorktreeService", None)
            if svc_cls is not None:
                svc_cls().update_branch(host)
        except Exception:
            pass

    return refresh


def _project_workdir(host: Any):
    """写库式切换项目工作目录（worktree / 分支切换场景）。

    与 ``workdir.write`` 的分界：那条只改会话内状态（缓存 + executor + 广播 +
    UI 同步），进程重启即失效；本能力把改动**持久化**到 project_workdir 表。
    """

    def _mm():
        backend = getattr(host, "backend", None)
        return getattr(backend, "memory_manager", None) if backend else None

    def _resolve_project(project: str) -> str:
        return str(project or getattr(host, "_current_project", "") or "")

    def switch(project: str, path: str, *, added_by: str = "", restore_mark: bool = True) -> bool:
        """持久化项目工作目录。

        ``added_by`` / ``restore_mark`` 保留在签名里仅为兼容既有插件调用点：
        关键文档表下线后，工作目录已是单值语义，无需「登记条目标记」这类动作。
        """
        project, path = _resolve_project(project), str(path or "")
        mm = _mm()
        if mm is None or not project or not path:
            return False
        try:
            return bool(mm.set_working_directory(project, path))
        except Exception as e:
            logger.warning(f"[Capability] 切换项目工作目录失败 project={project}: {e}")
            return False

    def set_only(project: str, path: str) -> bool:
        """只持久化工作目录（与 switch 等价，保留给回主仓库场景的调用点）。"""
        return switch(project, path)

    return {"switch": switch, "set_only": set_only}


# ──────────────────── 上下文 / 引擎 ────────────────────


def _theme_read(host: Any):
    return host._resolve_theme_snapshot


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

    def read(window_id: str = "") -> dict:
        target_host = host
        if str(window_id or "").strip():
            target_host = _resolve_window_by_id(str(window_id))
            if target_host is None:
                return {"session_id": "", "name": "", "messages": [], "system_prompt": ""}
        backend = getattr(target_host, "backend", None)
        session = backend.get_current_session() if backend is not None else None
        if session is None:
            return {"session_id": "", "name": "", "messages": [], "system_prompt": ""}
        session_id = str(getattr(session, "session_id", "") or "")

        def load_extras(idxs: list) -> dict:
            """按消息序号懒读 extras（reasoning 等大字段，按需加载）。"""
            try:
                from app.core.conversation.backend import get_session_storage

                storage = get_session_storage()
                fn = getattr(storage, "load_msg_extras", None)
                if not callable(fn):
                    return {}
                return dict(fn(session_id, list(idxs)) or {})
            except Exception:
                return {}

        return {
            "session_id": session_id,
            "name": str(getattr(session, "name", "") or ""),
            "messages": list(getattr(session, "messages", None) or []),
            "system_prompt": str(getattr(session, "system_prompt", "") or ""),
            "extras": load_extras,
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

    def invalidate(session_id: str = "") -> int:
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
                        if session_id and str(getattr(session, "session_id", "") or "") != session_id:
                            continue
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
    """消息写回会话（可按 window_id 定向到其他窗口）。

    宿主无 _save_messages_to_session / 写入失败返回 0（与 tool 面同语义）。
    """

    def write(messages, task_prompt: str = "", window_id: str = "") -> int:
        target_host = host
        if str(window_id or "").strip():
            target_host = _resolve_window_by_id(str(window_id))
            if target_host is None:
                return 0
        fn = getattr(target_host, "_save_messages_to_session", None)
        if not callable(fn):
            return 0
        try:
            fn(messages, task_prompt=task_prompt)
            return len(messages) if messages is not None else 0
        except Exception as e:  # noqa: BLE001 — 写回失败不抛给插件
            logger.warning(f"[Capability] session.write 失败: {e}")
            return 0

    return write


def _engine_busy(backend: Any) -> bool:
    """worker 真实繁忙判据（core 层 chat_engine.is_streaming）。

    与 main_widget._worker_actually_busy 同源；backend 拿不到 / engine 未建
    一律视为不忙（安全默认：普通入队路径兜底）。
    """
    engine = getattr(backend, "chat_engine", None)
    if engine is None:
        return False
    try:
        return bool(engine.is_streaming)
    except Exception:  # noqa: BLE001 - 状态读取失败按空闲处理
        return False


def _send_with_mode(
    backend: Any,
    queue: Any,
    wid: str,
    text: str,
    *,
    ephemeral: bool = False,
    mode: str = "queue",
    hook_event: str = "SessionSend",
) -> bool:
    """session.send 三模式（queue 默认 / skip 流式中忽略 / hook 流式中注入消息列表）。

    - queue：入队等本轮结束 drain 后发送（原契约）。
    - skip：流式中直接拒绝（返回 False，调用方感知被忽略）；空闲正常入队。
    - hook：流式中经 backend._hook_message_queue 注入当前对话上下文，worker
      工具循环下一轮消费（不建卡不打断，同 SubAgentFinished 通道）；空闲时
      降级为普通 send 开新轮（hook 队列无 worker 消费，滞留会拖到下轮开头）。
    hook_event：注入消息的 _hook_event 事件标记（也用作 _format_hook_output
    标签），调用方可自定义（如 SubAgentFinished）保留语义。

    判忙 stale 误判方向保守：skip 可能误拒（语义安全），hook 注入可能落到
    下轮开头消费（行为安全）。
    """
    mode = str(mode or "queue").strip().lower()
    if mode not in ("queue", "skip", "hook"):
        mode = "queue"
    hook_event = str(hook_event or "SessionSend").strip() or "SessionSend"
    text = str(text or "")
    if mode != "queue":
        if _engine_busy(backend):
            if mode == "skip":
                return False
            hook_q = getattr(backend, "_hook_message_queue", None)
            if hook_q is not None:
                try:
                    from app.core.conversation.backend import _format_hook_output

                    hook_q.put(
                        {
                            "role": "user",
                            "content": _format_hook_output(hook_event, text, wrap_system_reminder=False),
                            "_hook_event": hook_event,
                        }
                    )
                    return True
                except Exception as e:  # noqa: BLE001 - 注入失败退化为普通入队
                    import logging

                    logging.getLogger(__name__).warning(f"[session.send] hook 注入失败，退化排队: {e}")
        elif mode == "skip":
            pass  # 空闲：skip 与 queue 同路径
    return queue.enqueue(wid, "send", {"text": text, "ephemeral": bool(ephemeral)})


def _session_control(host: Any):
    """会话控制三件套（入队语义，不直接操作 UI）。

    调用方可能在 worker 线程（工具 impl），所以只往窗口队列里塞动作，
    真正执行由 main_widget 在本轮结束时 drain。window_id 只做精确匹配，
    拿不到就拒绝——写操作回退「活跃窗口」会在多窗口同时交接时串窗。
    send 支持三模式（queue/skip/hook），见 _send_with_mode。
    """
    from app.core.conversation.action_queue import get_action_queue

    wid = str(getattr(host, "_window_id", "") or "")
    queue = get_action_queue()
    backend = getattr(host, "backend", None)

    def interrupt() -> bool:
        return queue.enqueue(wid, "interrupt", {})

    def new_session() -> bool:
        return queue.enqueue(wid, "new", {})

    def send(
        text: str,
        *,
        ephemeral: bool = False,
        mode: str = "queue",
        hook_event: str = "SessionSend",
        window_id: str = "",
    ) -> bool:
        target_wid = str(window_id or "").strip() or wid
        if not target_wid:
            return False
        target_backend = backend
        if target_wid != wid:
            target_win = _resolve_window_by_id(target_wid)
            target_backend = getattr(target_win, "backend", None) if target_win is not None else None
        return _send_with_mode(
            target_backend, queue, target_wid, text, ephemeral=ephemeral, mode=mode, hook_event=hook_event
        )

    return {"interrupt": interrupt, "new": new_session, "send": send}


def _session_interrupt(host: Any):
    return _session_control(host)["interrupt"]


def _session_new(host: Any):
    return _session_control(host)["new"]


def _session_send(host: Any):
    return _session_control(host)["send"]


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
    """写入输入框文本并聚焦（可选追加模式）。

    Tab 模式下 host 可能解析为 TabManagerWindow（full 卡等覆盖场景），输入区
    在活跃对话窗口上：先降级解析真实输入框；写入后强制切回对话视图——
    写入语义是「让用户看到并发送」，停留在覆盖页写入不可见（magpie「让 AI
    帮我装」实测盲写）。
    """

    def _resolve_area():
        area = getattr(host, "input_area", None)
        if area is not None:
            return area
        # Tab 模式：host 是 TabManagerWindow → 活跃对话窗口
        try:
            wins = getattr(host, "_windows", None) or []
            panel = getattr(host, "_tab_panel", None)
            idx = getattr(panel, "_active_index", 0) if panel is not None else 0
            if 0 <= idx < len(wins):
                area = getattr(wins[idx], "input_area", None)
                if area is not None:
                    return area
        except Exception:
            pass
        # registry 兜底：任一已登记对话窗口
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            for w in (getattr(UIPluginRegistry.get_instance(), "_window_main_widgets", {}) or {}).values():
                area = getattr(w, "input_area", None)
                if area is not None:
                    return area
        except Exception:
            pass
        return None

    def _back_to_chat():
        # 写入即回对话视图：full 卡覆盖时不切回，用户看不到写入结果。
        # host 常是 MainWidget（窗口级能力池 / 浮动卡宿主），覆盖页却由
        # TabManagerWindow 托管，MainWidget 上没有回对话入口 → 候选依次为
        # host 自身与全局 Tab 管理器；正规入口（含 stack 切页 + 隐藏 replace 卡）
        # 优先，退化到 titleBar 高亮复位（titleBar / _title_bar 两种属性名）。
        owners = []
        for owner in (host, _resolve_tab_window()):
            if owner is not None and all(owner is not o for o in owners):
                owners.append(owner)
        for owner in owners:
            try:
                show_conv = getattr(owner, "_show_conversation_view", None)
            except Exception:
                continue
            if callable(show_conv):
                try:
                    show_conv()
                    return
                except Exception:
                    pass
        for owner in owners:
            try:
                title_bar = getattr(owner, "titleBar", None) or getattr(owner, "_title_bar", None)
            except Exception:
                continue
            set_active = getattr(title_bar, "set_active_tab", None)
            if callable(set_active):
                try:
                    from app.widgets.tab_manager_window import CHAT_TAB_ID

                    set_active(CHAT_TAB_ID, animate=False)
                    return
                except Exception:
                    pass

    def write(text: str, append: bool = False, focus: bool = True) -> None:
        area = _resolve_area()
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
        _back_to_chat()
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

        dlg = SingleInputDialog(title, hint=hint, default_text=default_text, parent=_parent())
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

            UIPluginRegistry.get_instance()._show_floating_card(card_id, main_widget=host if host is not None else None)
            return True
        except Exception as e:
            logger.warning(f"[Capability] 显示浮动卡 {card_id} 失败: {e}")
            return False

    return show


def _ui_notify(host: Any):
    def notify(title: str, message: str, kind: str = "success", duration: int = 5000) -> None:
        from qfluentwidgets import InfoBar, InfoBarPosition

        def _show() -> None:
            bar_cls = getattr(InfoBar, kind, None)
            if not callable(bar_cls):  # 非法 kind 回落 success，不让通知因拼写错误丢消息
                bar_cls = InfoBar.success
            bar_cls(
                str(title),
                str(message),
                parent=_resolve_tab_window() or host.window(),
                duration=int(duration),
                position=InfoBarPosition.BOTTOM,
            )

        if threading.current_thread() is threading.main_thread():
            _show()
            return
        # 后台线程（cron-tasks 等后台任务通道）：InfoBar 的构造/show/manager
        # 注册必须落主线程，跨线程创建会在 manager 列表留死壳（详见
        # info_bar_guard 模块注释，2026-10-24 崩溃根因）
        if not post_to_main(_show):
            logger.warning("[Capability] notify 丢弃：QApplication 未就绪，无法投递主线程")

    return notify


def _ui_artifacts_show(host: Any):
    """向当前窗口最新 assistant 卡片的成果栏推送成果条目。

    返回 ``show(items, target="latest", call_id="") -> int``：
    返回投递的条目数；未找到目标卡片返回 0。线程安全——后台线程调用经
    ``post_to_main`` 投递主线程执行（面板/QWidget 只能主线程碰）。

    target="call" 时按 ``call_id`` 经宿主 ``_tool_card_map`` 定位发起轮次的卡片，
    供异步任务（生成完成晚于轮次结束）回填原卡。
    """

    def show(items, target: str = "latest", call_id: str = "") -> int:
        try:
            payload = [dict(x) for x in (items or []) if isinstance(x, dict)]
        except (TypeError, ValueError):
            return 0
        if not payload:
            return 0

        def _apply() -> int:
            card = None
            if target == "call" and call_id:
                card = (getattr(host, "_tool_card_map", None) or {}).get(call_id)
            if card is None:
                card = getattr(host, "_current_assistant_card", None)
                if card is None:
                    finder = getattr(host, "_find_latest_assistant_card", None)
                    card = finder() if callable(finder) else None
            if card is None:
                logger.warning("[Capability] artifacts.show 未找到目标卡片，丢弃")
                return 0
            updater = getattr(card, "update_artifact_list", None)
            if not callable(updater):
                return 0
            try:
                updater(payload)
            except RuntimeError:
                # deleted wrapper：卡片已销毁，静默丢弃（P029 教训）
                logger.warning("[Capability] artifacts.show 目标卡片已销毁")
                return 0
            return len(payload)

        if threading.current_thread() is threading.main_thread():
            # 主线程直跑：返回实际挂载数（未找到卡片 → 0，契约可测）
            return _apply()
        if not post_to_main(_apply):
            logger.warning("[Capability] artifacts.show 丢弃：QApplication 未就绪")
            return 0
        # 后台投递：真实结果在主线程落地，返回预期条目数（失败场景见日志）
        return len(payload)

    return show


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

        return {"suppress_watcher": suppress_watcher, "resume_watcher": resume_watcher, "reload": reload}

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

    暴露方法清单声明在 ``app.plugins.capabilities.facets``，两个 surface 共用。
    派任务、改状态等写操作不在白名单内，插件访问会得到 AttributeError。
    """
    from app.plugins.capabilities.facets import build_facet

    return build_facet("team.read")


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


def _config_read(host: Any):
    """按 key 读主程序配置项（非敏感标量）。

    刻意**不**返回整个 Settings 对象：那等于把 model.config 才该给的明文密钥
    一起交出去。按 key 取，调用方只能拿到它显式要的那一项。

    密钥类字段（api_key / token 之类）一律拒绝，插件要密钥走 model.config。
    """

    _SECRET_HINTS = ("key", "token", "secret", "password", "credential")

    def read(key: str, default: Any = None) -> Any:
        if not key:
            return default
        lowered = key.lower()
        if any(h in lowered for h in _SECRET_HINTS):
            logger.warning(f"[Capability] config.read 拒绝密钥类字段: {key}")
            return default
        try:
            from app.utils.config import Settings

            s = Settings.get_instance()
            item = getattr(s, key, None)
            if item is None:
                return default
            value = getattr(item, "value", item)
            return default if value is None else value
        except Exception as e:
            logger.debug(f"[Capability] config.read 取 {key} 失败: {e}")
            return default

    return read


def _config_write(host: Any):
    """按 key 写主程序配置项（仅已声明项；密钥类字段一律拒绝）。

    只认 Settings 上的具名 ConfigItem：插件能改的范围与「界面里存在的开关」
    一致，写不进从未声明的字段。密钥类字段与 config.read 同口径拒绝（密钥
    走 model.config 的写路径不存在，也不该存在）。
    """

    _SECRET_HINTS = ("key", "token", "secret", "password", "credential")

    def write(key: str, value: Any, save: bool = True) -> bool:
        if not key:
            return False
        if any(h in key.lower() for h in _SECRET_HINTS):
            logger.warning(f"[Capability] config.write 拒绝密钥类字段: {key}")
            return False
        try:
            from app.utils.config import Settings

            s = Settings.get_instance()
            item = getattr(s, key, None)
            if item is None or not hasattr(item, "value"):
                logger.warning(f"[Capability] config.write 未声明的配置项: {key}")
                return False
            item.value = value
            if save:
                s.save()
            return True
        except Exception as e:
            logger.warning(f"[Capability] config.write 写 {key} 失败: {e}")
            return False

    return write

def _model_config_for(host: Any):
    """按 query 解析出可执行的完整模型配置（含明文 key）。

    query 三种格式与 ``/subagents --model=`` / 标题生成默认模型一致：
    模型名（当前服务商模糊匹配）/ 服务商名（config_id、display_name、provider_name
    皆可）/ ``服务商:模型名``。解析逻辑留在宿主窗口（`_resolve_model_config_by_query`）：
    它依赖窗口内的 ``_valid_configs`` 与模型列表缓存，插件侧没有等价数据源。

    capability_type 由调用方按意图传入（缺省 chat）；宿主解析器据此过滤候选集。
    """

    def config_for(query: str, capability_type: str = "chat") -> Any:
        if not query:
            return None
        resolver = getattr(host, "_resolve_model_config_by_query", None)
        if not callable(resolver):
            logger.debug("[Capability] model.config_for 宿主无模型解析器")
            return None
        try:
            config = resolver(query, show_error=False, capability_type=capability_type)
            if isinstance(config, dict):
                config.setdefault("capability_type", capability_type)
            return config
        except TypeError:
            # 旧宿主解析器不接受新参数：退回原签名，避免能力池整体不可用
            try:
                return resolver(query, show_error=False)
            except Exception as e:
                logger.warning(f"[Capability] model.config_for 解析失败 {query!r}: {e}")
                return None
        except Exception as e:
            logger.warning(f"[Capability] model.config_for 解析失败 {query!r}: {e}")
            return None

    return config_for

def _model_set(host: Any):
    """model.set：设置当前窗口的服务商与模型（只改窗口实例态，不写全局默认）。

    解析复用宿主 ``_resolve_history_model_target``（config_id 直查 → provider
    反查 → 模型名唯一匹配；多匹配不明确返回 None），该函数是纯解析，复用安全。
    成功后刷新模型按钮与上下文用量；不置 ``_user_manually_selected_model``
    （插件选择不视为用户偏好，云端同步仍可覆盖）、不写 ``cfg.llm_selected_model``。
    流式中调用允许：本轮 worker 已持有 llm_config，下一轮生效。
    """

    def set_model(model: str = "", provider: str = "", config_id: str = "", window_id: str = "") -> dict:
        target_host = host
        if str(window_id or "").strip():
            target_host = _resolve_window_by_id(str(window_id))
            if target_host is None:
                return {"ok": False, "error": "window_not_found", "detail": f"窗口不存在: {window_id}"}
        resolver = getattr(target_host, "_resolve_history_model_target", None)
        if not callable(resolver):
            return {"ok": False, "error": "not_found", "detail": "宿主窗口不支持模型解析"}
        try:
            target = resolver(str(model or ""), str(provider or ""), str(config_id or ""))
        except Exception as e:  # noqa: BLE001 — 解析异常按 not_found 降级
            logger.warning(f"[Capability] model.set 解析失败: {e}")
            return {"ok": False, "error": "not_found", "detail": str(e)}
        if not target:
            return {"ok": False, "error": "not_found", "detail": "无法解析到唯一的服务商/模型组合"}
        resolved_cid, resolved_model = target
        target_host._current_provider_name = resolved_cid
        target_host._current_model_name = resolved_model
        refresh_btn = getattr(target_host, "_update_model_selector_btn", None)
        if callable(refresh_btn):
            refresh_btn()
        refresh_ctx = getattr(target_host, "_refresh_context_usage_indicator", None)
        if callable(refresh_ctx):
            refresh_ctx()
        return {"ok": True, "config_id": resolved_cid, "model": resolved_model}

    return set_model

def _storage_db(host: Any, ctx: Any):
    """通用数据库执行权限：受限代理（表名须 ``plugin_<name>_`` 前缀）。"""
    from app.plugins.capabilities.providers.db_guard import PluginDBProxy

    return PluginDBProxy(getattr(ctx, "plugin", "") or "")

def _host_raw(host: Any):
    return host


def _storage_data_dir(host: Any):
    """插件持久化数据根目录（<app_data>，与 tool_core 同实现）。

    只给根目录不给插件专属目录：provider 全局共享、_call 不传插件名，
    per-plugin 隔离没有现成机制。插件自拼 plugin_data/<name>/。
    """
    from app.utils.utils import get_app_data_dir

    return get_app_data_dir()


def register(registry) -> None:
    reg = registry
    # 对话流 / 窗口
    reg.register("stream.subscribe", _stream_subscribe, surface="ui", owner="core:ui")
    reg.register("stream.errors", _stream_errors, surface="ui", owner="core:ui")
    reg.register("window.active", _window_active, surface="ui", owner="core:ui")
    reg.register("window.host", _window_host, surface="ui", owner="core:ui")
    reg.register("window.current", _window_current, surface="ui", owner="core:ui")
    reg.register("window.list", _window_list, surface="ui", owner="core:ui")
    reg.register("window.workbench", _window_workbench, surface="ui", owner="core:ui")
    reg.register("window.events", _window_events, surface="ui", owner="core:ui")
    # 模型 / 智能体
    reg.register("model.config", _model_config, surface="ui", owner="core:ui")
    reg.register("model.config", _provider_config, surface="ui", owner="core:ui", legacy_key="get_provider_config")
    reg.register("model.config_for", _model_config_for, surface="ui", owner="core:ui")
    reg.register("model.set", _model_set, surface="ui", owner="core:ui")
    reg.register("model.schema", _model_schema, surface="ui", owner="core:ui")
    reg.register("model.metadata", _model_metadata, surface="ui", owner="core:ui")
    reg.register("provider.list", model_meta.provider_list, surface="ui", owner="core:ui")
    reg.register("model.enumerate", model_meta.model_enumerate, surface="ui", owner="core:ui")
    reg.register("agent.read", _agent_manager, surface="ui", owner="core:ui")
    reg.register("agent.read", _agent_prompt, surface="ui", owner="core:ui", legacy_key="get_agent_prompt")
    reg.register("subagent.run", _subagent_manager, surface="ui", owner="core:ui")
    # 工作目录
    reg.register("workdir.read", _workdir_read, surface="ui", owner="core:ui")
    reg.register("workdir.write", _workdir_write, surface="ui", owner="core:ui")
    reg.register("project.workdir", _project_workdir, surface="ui", owner="core:ui")
    reg.register("branch.refresh", _branch_refresh, surface="ui", owner="core:ui")
    reg.register(
        "workdir.write", _sync_working_directory, surface="ui", owner="core:ui", legacy_key="sync_working_directory"
    )
    # 存储
    reg.register("storage.data_dir", _storage_data_dir, surface="ui", owner="core:ui")
    reg.register("storage.db", _storage_db, surface="ui", owner="core:ui")
    # 上下文 / 引擎
    reg.register("theme.read", _theme_read, surface="ui", owner="core:ui")
    reg.register("engine.stack", _engine_stack, surface="ui", owner="core:ui")
    reg.register("engine.session", _engine_session, surface="ui", owner="core:ui")
    reg.register("session.read", _session_read, surface="ui", owner="core:ui")
    reg.register("session.list", _session_list, surface="ui", owner="core:ui")
    reg.register("session.invalidate", _session_invalidate, surface="ui", owner="core:ui")
    reg.register("session.write", _session_write, surface="ui", owner="core:ui")
    reg.register("session.interrupt", _session_interrupt, surface="ui", owner="core:ui")
    reg.register("session.new", _session_new, surface="ui", owner="core:ui")
    reg.register("session.send", _session_send, surface="ui", owner="core:ui")
    # 界面
    reg.register("input.read", _input_read, surface="ui", owner="core:ui")
    reg.register("input.write", _input_write, surface="ui", owner="core:ui")
    reg.register("ui.notify", _ui_notify, surface="ui", owner="core:ui")
    reg.register("ui.dialog", _ui_dialog, surface="ui", owner="core:ui")
    reg.register("ui.card.show", _ui_card_show, surface="ui", owner="core:ui")
    reg.register("ui.card.hide", _ui_card_hide, surface="ui", owner="core:ui")
    reg.register("ui.artifacts.show", _ui_artifacts_show, surface="ui", owner="core:ui")
    reg.register("ui.events", _ui_events, surface="ui", owner="core:ui")
    reg.register("question.control", _question_control, surface="ui", owner="core:ui")
    reg.register("command.read", _command_read, surface="ui", owner="core:ui")
    reg.register("command.reload", _command_reload, surface="ui", owner="core:ui")
    reg.register("plugin.read", _plugin_read, surface="ui", owner="core:ui")
    reg.register("plugin.control", _plugin_control, surface="ui", owner="core:ui")
    reg.register("ui.exclusive", _ui_exclusive, surface="ui", owner="core:ui")
    reg.register("ui.exclusive", _enter_exclusive, surface="ui", owner="core:ui", legacy_key="enter_exclusive_ui_mode")
    reg.register("ui.exclusive", _exit_exclusive, surface="ui", owner="core:ui", legacy_key="exit_exclusive_ui_mode")
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
    reg.register("process.start", process_core.process_start, surface="ui", owner="core:ui")
    # process.start 首启确认框从插件后台线程走 _dispatch：_Dispatcher 单例必须
    # 在此（主线程）预热，否则由后台线程首建 QObject 再 moveToThread 会损坏
    # Qt 同步状态（实测确认框模态循环内堆崩溃，magpie 0.5.2/0.5.3）。
    # QApplication 未建（极端启动序）时静默跳过——_confirm_dialog 内还有兜底预热。
    try:
        from app.plugins.capabilities.providers.ui_bridge import preheat_main_thread_dispatcher

        preheat_main_thread_dispatcher()
    except RuntimeError:
        pass
    # 配置（按 key 读非敏感标量）
    reg.register("config.read", _config_read, surface="ui", owner="core:ui")
    reg.register("config.write", _config_write, surface="ui", owner="core:ui")
    # 网络 / 云同步（host 无关实现，与 tool_core 共用 net_sync）
    reg.register("net.avatar", net_sync.net_avatar, surface="ui", owner="core:net")
    reg.register("net.download", net_sync.net_download, surface="ui", owner="core:net")
    reg.register("sync.register", net_sync.sync_register, surface="ui", owner="core:net")
    # 逃生舱
    reg.register("host.raw", _host_raw, surface="ui", owner="core:ui")
