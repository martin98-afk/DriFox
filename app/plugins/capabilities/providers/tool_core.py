# -*- coding: utf-8 -*-
"""工具 surface 的能力 provider（host = ToolExecutor）。

覆盖 catalog 里标 ``_BOTH`` 的能力面。工具调用点没有 MainWidget（host 是
ToolExecutor），因此与 ui_core 的同名能力**不能直接复用 provider**：两者的
host 形状不同，数据源解析路径也不同。本模块按 ToolExecutor / BuiltinTools
实际持有的引用逐条实现，语义与 ui_core 对齐。

已覆盖：窗口级 KV、LSP、MCP、Gitee、子智能体、团队读写、工具读写与注入、
模型元数据 / schema / 配置、会话读写、agent 读取、工作目录、上下文压缩、
插件 logger。
"""

from __future__ import annotations

from typing import Any

from app.plugins.capabilities.providers import model_meta, net_sync, process_core


def register(registry) -> None:
    def _kv(host: Any):
        """窗口级 KV。

        host 有两种形态：工具 impl 里是 ToolExecutor（自身带 window_state_*），
        hook 里是 MainWidget（方法在它的 backend.tool_executor 上）。两种都要认，
        否则 hook 侧调 state.kv 会直接 AttributeError → 插件静默跳过。
        """
        target = host
        if not hasattr(target, "window_state_get"):
            backend = _backend(host)
            target = getattr(backend, "tool_executor", None) if backend is not None else None
        if target is None or not hasattr(target, "window_state_get"):
            return None
        return {
            "get": target.window_state_get,
            "set": target.window_state_set,
            "delete": target.window_state_delete,
        }

    def _lsp(host: Any):
        bt = getattr(host, "_builtin_tools", None)
        return getattr(bt, "_lsp_tools", None) if bt is not None else None

    def _mcp(host: Any):
        # MCP 连接与 schema 是全局单例（多窗口共享；BuiltinTools._mcp_manager
        # 也指向它）。后台 pool_for / hook 路径的 host 是窗口，没有 _builtin_tools
        # 属性，经 host 链取会恒 None（值班轮「MCP 未连接」误报的根因）。
        # 直接取单例：未连接时 _connections 为空，get_tool_schemas_by_server
        # 自然返回空 dict，无需守卫配置开关。
        try:
            from app.tools.mcp_tools import MCPClientManager

            return MCPClientManager.get_instance()
        except Exception:
            return None

    def _gitee(host: Any):
        bt = getattr(host, "_builtin_tools", None)
        return getattr(bt, "gitee_upload", None) if bt is not None else None

    def _subagent(host: Any):
        """子智能体窗口服务（按 ToolExecutor 所属窗口查插件注册的门面）。"""
        from app.plugins.registries.window_services import get_window_services

        backend = getattr(host, "_backend", None)
        window_id = str(getattr(backend, "_window_id", "") or "")
        if not window_id:
            try:
                from app.plugins.capabilities.resolve import active_main_widget

                window_id = str(getattr(active_main_widget(), "_window_id", "") or "")
            except Exception:  # noqa: BLE001
                window_id = ""
        return get_window_services().get("subagent", window_id)

    def _model_config_for(host: Any):
        """按 query 解析完整模型配置（复用 ui_core 实现，窗口经 active_main_widget）。

        工具侧 host 是 ToolExecutor，没有窗口的模型配置缓存；解析逻辑必须落到
        宿主窗口。取不到窗口返回恒 None 的实现（后台任务无窗口是正常态）。
        """
        real = None
        try:
            from app.plugins.capabilities.resolve import active_main_widget

            real = active_main_widget()
        except Exception:  # noqa: BLE001 - 无窗口场景退化为空实现
            real = None
        if real is None:
            return lambda query: None
        from app.plugins.capabilities.providers.ui_core import _model_config_for as _impl

        return _impl(real)

    def _model_complete(host: Any):
        """tool 面的 model.complete：委托 ui_core 实现。

        ToolExecutor 自身没窗口的模型配置与后端会话，取不到窗口时返回恒空实现
        （后台工具无窗口是正常态，非错误）。
        """
        real = None
        try:
            from app.plugins.capabilities.resolve import active_main_widget

            real = active_main_widget()
        except Exception:  # noqa: BLE001 - 无窗口场景退化为空实现
            real = None
        if real is None:
            return lambda *args, **kwargs: ""
        from app.plugins.capabilities.providers.ui_core import _model_complete as _impl

        return _impl(real)

    def _storage_db(host: Any, ctx: Any):
        """通用数据库执行权限（与 ui_core 同一受限代理实现）。"""
        from app.plugins.capabilities.providers.db_guard import PluginDBProxy

        return PluginDBProxy(getattr(ctx, "plugin", "") or "")

    def _artifacts_show(host: Any):
        """tool surface 的成果推送：host 是 ToolExecutor，无窗口引用。

        经 UIPluginRegistry 解析活跃 MainWidget 后复用 ui_core 同名实现；
        取不到窗口返回恒 0 的空实现（后台工具无窗口是正常状态，非错误）。
        工具插件的常规通道是 ``ToolResult.artifacts`` 字段（自动挂成果栏），
        本能力用于异步任务完成后显式回填原卡（target="call"）。
        """
        real = None
        try:
            from app.plugins.capabilities.resolve import active_main_widget

            real = active_main_widget()
        except Exception:  # noqa: BLE001 - 无窗口场景返回空实现
            real = None
        if real is None:
            return lambda items, target="latest", call_id="": 0
        from app.plugins.capabilities.providers.ui_core import _ui_artifacts_show

        return _ui_artifacts_show(real)

    def _backend(host: Any):
        """→ ChatBackend。

        ToolExecutor 自身持 `_backend`；MainWidget（hook 侧 host）则用 `backend` 属性。
        两种形态都要认，否则 hook 里取不到 backend → session.* 能力全部失效。
        """
        backend = getattr(host, "_backend", None)
        if backend is not None:
            return backend
        candidate = getattr(host, "backend", None)
        return candidate if hasattr(candidate, "get_current_session") else None

    def _builtin(host: Any):
        return getattr(host, "_builtin_tools", None)

    def _model_metadata(host: Any):
        """当前模型元数据（与 ui_core 同口径：provider / model）。"""

        def metadata() -> dict:
            getter = getattr(_builtin(host), "_get_llm_config", None)
            if not callable(getter):
                return {"provider": "", "model": ""}
            try:
                cfg = getter() or {}
            except Exception:
                return {"provider": "", "model": ""}
            return {
                "provider": cfg.get("provider_name") or cfg.get("provider") or "",
                "model": cfg.get("\u6a21\u578b\u540d\u79f0") or cfg.get("model") or "",
            }

        return metadata

    def _model_config(host: Any):
        """当前模型完整配置（含已解密明文 API key）。

        密钥模式下磁盘 app.config 是密文/空串，唯一可用来源是主程序装配给
        BuiltinTools 的 getter（backend.initialize 时注入，见
        ChatBackend._deferred_create_tool_executor）。
        """

        def current() -> dict:
            getter = getattr(_builtin(host), "_get_llm_config", None)
            if not callable(getter):
                return {}
            try:
                return getter() or {}
            except Exception:
                return {}

        return current

    def _model_schema(host: Any):
        """指定 agent 视角的工具 schema（含 deny 过滤，与 ui_core 同口径）。"""

        def schema(agent_name: str = "") -> list:
            am = getattr(_builtin(host), "_agent_manager", None)
            agent = am.get_agent(agent_name) if (am and agent_name) else None
            agent_perms = agent.permission if agent else {}
            denied = {name for name, val in agent_perms.items() if val in ("deny", False)}
            try:
                from app.tools import get_builtin_tools_schema

                all_tools = get_builtin_tools_schema(agent_manager=am, builtin_tools=_builtin(host))
            except Exception:
                return []
            return [t for t in all_tools if t.get("function", {}).get("name", "") not in denied]

        return schema

    def _agent_read(host: Any):
        """agent 管理器（与 ui_core 同口径：返回裸 manager 对象）。

        按名取提示词走 legacy 键 get_agent_prompt（语义不同的 API 单位，
        不能与主实现共用同一个返回值）。
        """
        return getattr(_builtin(host), "_agent_manager", None)

    def _agent_prompt(host: Any):
        """get_agent_prompt 旧键的专属实现（按名取提示词）。"""

        def prompt(name: str) -> str:
            am = _agent_read(host)
            agent = am.get_agent(name) if am else None
            return agent.prompt if agent else ""

        return prompt

    def _session_read(host: Any):
        """当前会话只读快照（拿副本，不拿会话对象）。"""

        def read() -> dict:
            backend = _backend(host)
            session = backend.get_current_session() if backend is not None else None
            if session is None:
                return {"session_id": "", "name": "", "messages": [], "system_prompt": ""}
            return {
                "session_id": str(getattr(session, "session_id", "") or ""),
                "name": str(getattr(session, "name", "") or ""),
                "messages": list(getattr(session, "messages", None) or []),
                "system_prompt": str(getattr(session, "system_prompt", "") or ""),
            }

        return read

    def _session_write(host: Any):
        """消息写回当前会话。

        工具侧没有 MainWidget._save_messages_to_session（那是 UI 侧封装），
        改为委托 ChatBackend 暴露的同名入口；缺失时返回 0 表示未写入。
        """

        def write(messages, task_prompt: str = "") -> int:
            backend = _backend(host)
            fn = getattr(backend, "save_messages_to_session", None) if backend is not None else None
            if not callable(fn):
                return 0
            try:
                fn(messages, task_prompt=task_prompt)
                return len(messages)
            except Exception:
                return 0

        return write

    def _session_control(host: Any):
        """会话控制三件套（入队语义；host 是 ToolExecutor）。

        工具 impl 跑在 worker 线程，直接停流会杀掉自己所在的 worker，
        所以只入队，由 main_widget 在本轮结束时 drain。
        window_id 从 backend 精确取，拿不到一律拒绝入队（防多窗口串窗）。
        send 支持三模式（queue/skip/hook），见 ui_core._send_with_mode。
        """
        from app.core.conversation.action_queue import get_action_queue

        backend = _backend(host)
        wid = str(getattr(backend, "_window_id", "") or getattr(host, "_window_id", "") or "")
        queue = get_action_queue()

        def interrupt() -> bool:
            return queue.enqueue(wid, "interrupt", {})

        def new_session() -> bool:
            return queue.enqueue(wid, "new", {})

        def send(text: str, *, ephemeral: bool = False, mode: str = "queue", hook_event: str = "SessionSend") -> bool:
            # 三模式复用 ui_core 同实现（先例：_ui_artifacts_show 跨 provider 复用）
            from app.plugins.capabilities.providers.ui_core import _send_with_mode

            return _send_with_mode(
                backend,
                queue,
                wid,
                text,
                ephemeral=ephemeral,
                mode=mode,
                hook_event=hook_event,
            )

        return {"interrupt": interrupt, "new": new_session, "send": send}

    def _session_interrupt(host: Any):
        return _session_control(host)["interrupt"]

    def _session_new(host: Any):
        return _session_control(host)["new"]

    def _session_send(host: Any):
        return _session_control(host)["send"]

    def _workdir_read(host: Any):
        """当前工作目录。

        直读 ``_workdir`` 而非 ``host.get_workdir()``：后者在未经
        ``set_workdir`` 显式设置时返回 None（启动路径不算用户选择），
        而工具侧要的是执行器当前实际工作目录，恒为字符串。
        """
        return lambda: getattr(host, "_workdir", None) or ""

    def _storage_data_dir(host: Any):
        """插件持久化数据根目录（<app_data>）。

        只给根目录不给插件专属目录：CapabilityPool 的 provider 是全局
        共享的，构建时虽知插件名但 _call 不传，per-plugin 隔离没有现成
        机制。插件自拼 plugin_data/<name>/（与 E1 配置契约同目录约定）。
        """
        from app.utils.utils import get_app_data_dir

        return get_app_data_dir()

    def _secret_read(host: Any, ctx: Any):
        """tool 面凭据读取：与 ui 面同一实现（不依赖窗口）。"""
        from app.plugins.capabilities.providers.ui_core import _secret_read as _impl

        return _impl(host, ctx)

    def _secret_write(host: Any, ctx: Any):
        from app.plugins.capabilities.providers.ui_core import _secret_write as _impl

        return _impl(host, ctx)

    def _net_http(host: Any, ctx: Any):
        from app.plugins.capabilities.providers.ui_core import _net_http as _impl

        return _impl(host, ctx)

    def _task_background(host: Any, ctx: Any):
        from app.plugins.capabilities.providers.ui_core import _task_background as _impl

        return _impl(host, ctx)

    def _tool_register(host: Any, ctx: Any):
        from app.plugins.capabilities.providers.ui_core import _tool_register as _impl

        return _impl(host, ctx)

    def _tool_unregister(host: Any, ctx: Any):
        from app.plugins.capabilities.providers.ui_core import _tool_unregister as _impl

        return _impl(host, ctx)

    def _plugin_deps(host: Any, ctx: Any):
        from app.plugins.capabilities.providers.ui_core import _plugin_deps as _impl

        return _impl(host, ctx)

    def _plugin_log(host: Any, ctx: Any):
        """带 plugin=<name> 前缀的 logger（与 ui_core 同实现）。"""
        from loguru import logger

        return logger.bind(plugin=ctx.plugin or "(builtin)")

    def _tool_execute(host: Any):
        """工具执行入口（与 ui_core 同口径：backend.tool_executor）。

        工具侧 host 本身就是 ToolExecutor，返回自身即可：工具内再调工具
        时走同一条 _execute_registered_tool 路径（会再建一次池，递归深度
        由调用方控制）。
        """
        return host

    def _team_read(host: Any):
        """团队只读面（与 ui_core 同名能力语义一致，声明见 capabilities.facets）。"""
        from app.plugins.capabilities.facets import build_facet

        return build_facet("team.read")

    def _team_send(host: Any):
        """团队写面：派发任务邮件（声明见 capabilities.facets）。"""
        from app.plugins.capabilities.facets import build_facet

        return build_facet("team.send")

    def _team_manage(host: Any):
        """团队管理面：建团/入退队/移除成员/群发（声明见 capabilities.facets）。"""
        from app.plugins.capabilities.facets import build_facet

        return build_facet("team.manage")

    def _action_queue(host: Any):
        """动作队列只读面：仅 peek（pending 开窗回执；声明见 capabilities.facets）。"""
        from app.plugins.capabilities.facets import build_facet

        return build_facet("action.queue")

    def _team_spawn(host: Any):
        """team.spawn：为团队拉起成员窗口（UI 副作用，入队语义）。

        先例同 _session_control：provider 属主程序侧，直接 import action_queue；
        只暴露语义化方法，不把通用 enqueue 交给插件（权限放大：可塞
        interrupt/new 等非团队动作）。window_id 从 backend → host 逐级取，
        全空拒绝入队（防多窗口串窗）。

        sync/clear_caller_context：窗口团队上下文回填/清空也收在这里——
        插件侧禁止直接碰 builtin_tools（test_impl_source_self_contained 护栏）。
        """
        from app.core.conversation.action_queue import get_action_queue

        backend = _backend(host)
        wid = str(getattr(backend, "_window_id", "") or getattr(host, "_window_id", "") or "")

        def _resolve_wid(window_id: str = "") -> str:
            return str(window_id or wid or "")

        def _builtin_tools():
            bt = getattr(host, "_builtin_tools", None)
            if bt is None:
                bt = getattr(backend, "_builtin_tools", None)
            return bt if hasattr(bt, "set_team_context") else None

        def spawn_members(
            agents,
            run_id,
            team_label="",
            team_name="",
            start_idx=0,
            leader_agent="",
            keep_current_active=False,
        ):
            """入队 team_spawn 开窗请求，返回结构化回执（submitted/pending_window）。

            leader_agent：开窗清单里由哪个角色担任 leader（create 路径传
            "leader"，drain 侧在该窗口创建后回写 leader_window_id）；
            keep_current_active：True 时搭建过程不抢当前焦点 tab（create
            路径用——原窗口保持非团队成员且不被切走）。
            """
            target = _resolve_wid()
            if not target:
                return {"submitted": False, "error": "无法定位调用方窗口（window_id 缺失）"}
            if not run_id:
                return {"submitted": False, "error": "run_id 必填（防团队框漂移）"}
            names = [a if isinstance(a, str) else (a or {}).get("agent_name", "") for a in (agents or [])]
            names = [n for n in names if n]
            if not names:
                return {"submitted": False, "error": "agents 为空，无成员可开窗"}
            payload = {
                "agents": names,
                "run_id": run_id,
                "team_label": team_label,
                "team_name": team_name,
                "start_idx": int(start_idx or 0),
                "leader_agent": str(leader_agent or ""),
                "keep_current_active": bool(keep_current_active),
            }
            # 实时路径优先：主窗口注册了 team_spawn 实时处理器时，窗口在本轮
            # 对话进行中就开始创建（用户要求创建即生效，不等流结束）。
            from app.core.conversation.action_queue import submit_immediate

            if submit_immediate(target, "team_spawn", payload):
                return {
                    "submitted": True,
                    "queued": len(names),
                    "pending_window": list(names),
                    "note": "窗口正在实时创建（本轮对话期间拉起）",
                }
            queue = get_action_queue()
            ok = queue.enqueue(target, "team_spawn", payload)
            if not ok:
                return {"submitted": False, "error": "入队被拒（窗口正忙），请重试"}
            pending = [
                i["payload"].get("agents", [])
                for i in queue.peek(target)
                if i.get("action") == "team_spawn"
            ]
            return {
                "submitted": True,
                "queued": len(names),
                "pending_window": [a for sub in pending for a in sub],
                "note": "成员窗口在本轮对话结束后拉起；下一轮用 team_manage(action=list) 验证",
            }

        def ensure_leader_watch(window_id: str = "", run_id: str = "", team_label: str = "", team_name: str = ""):
            """入队 team_leader_watch：本轮结束后为 leader 窗口做 UI 分组 + 启动邮箱 watcher。

            payload 携带 run_id/team_label/team_name——drain 侧据其设置窗口
            三标记并调 refresh_capsule_for_window 把 leader 窗口移进团队框。
            """
            target = _resolve_wid(window_id)
            if not target:
                return {"ok": False, "error": "window_id 缺失"}
            ok = get_action_queue().enqueue(
                target,
                "team_leader_watch",
                {
                    "run_id": str(run_id or ""),
                    "team_label": str(team_label or ""),
                    "team_name": str(team_name or ""),
                },
            )
            if not ok:
                return {"ok": False, "error": "入队被拒（窗口正忙），请重试"}
            return {"ok": True, "note": "leader 窗口分组与邮箱监听将在本轮结束后完成"}

        def sync_caller_context(agent_name: str = ""):
            """回填调用窗口团队上下文（set_team_context），返回解析出的 window_id。

            替代插件侧直碰 tool_ctx["builtin_tools"]（护栏禁止）；同时承担
            create 场景的 window_id 解析降级链（bt._team_window_id → homepage._window_id）。
            """
            bt = _builtin_tools()
            if bt is None:
                return {"ok": False, "error": "builtin_tools 不可用，窗口上下文未同步"}
            target = str(getattr(bt, "_team_window_id", "") or "")
            if not target:
                homepage = getattr(bt, "homepage", None)
                target = str(getattr(homepage, "_window_id", "") or "")
            if not target:
                return {"ok": False, "error": "无法定位调用窗口（window_id 缺失）"}
            if agent_name:
                # agent_name 空串 = 仅解析 window_id（不触碰现有上下文）
                try:
                    bt.set_team_context(target, agent_name)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": f"set_team_context 失败: {e}"}
            return {"ok": True, "window_id": target}

        def clear_caller_context():
            """清空调用窗口团队上下文（leave 语义，对齐 backend 清空行为）。"""
            bt = _builtin_tools()
            if bt is None:
                return {"ok": False, "error": "builtin_tools 不可用"}
            try:
                bt.set_team_context("", "")
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"set_team_context 失败: {e}"}
            return {"ok": True}

        return {
            "spawn_members": spawn_members,
            "ensure_leader_watch": ensure_leader_watch,
            "sync_caller_context": sync_caller_context,
            "clear_caller_context": clear_caller_context,
        }

    def _tool_policy_api():
        """工具权限策略只读面（与 ui_core 同构）。"""

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
        """工具注册表只读面（与 ui_core 的同名能力语义一致）。"""

        def api() -> dict:
            from app.tools.registry import ToolRegistry

            reg = ToolRegistry.get_instance()
            return {
                "names": reg.names,
                "list": reg.list,
                "schemas": reg.schemas,
                "get": reg.get,
                "has": reg.has,
                "groups": reg.group_map,
                "is_no_prune": reg.is_no_prune,
                "is_no_offload": reg.is_no_offload,
                "is_danger": reg.get_danger,
                "is_protected": reg.is_protected,
                "policy": _tool_policy_api(),
            }

        return api

    def _tool_inject(host: Any):
        """schema 过滤器注册面。"""

        def api() -> dict:
            from app.tools.registry import ToolRegistry

            reg = ToolRegistry.get_instance()
            return {
                "register_schema_filter": reg.register_schema_filter,
                "unregister_schema_filter": reg.unregister_schema_filter,
                "apply_schema_filters": reg.apply_schema_filters,
            }

        return api

    registry.register("state.kv", _kv, surface="tool", owner="core:tool")
    registry.register("plugin.log", _plugin_log, surface="tool", owner="core:tool")
    registry.register("model.metadata", _model_metadata, surface="tool", owner="core:tool")
    registry.register("model.schema", _model_schema, surface="tool", owner="core:tool")
    registry.register("model.config", _model_config, surface="tool", owner="core:tool")
    registry.register("model.config_for", _model_config_for, surface="tool", owner="core:tool")
    registry.register("model.complete", _model_complete, surface="tool", owner="core:tool")
    registry.register("agent.read", _agent_read, surface="tool", owner="core:tool")
    registry.register(
        "agent.read", _agent_prompt, surface="tool", owner="core:tool", legacy_key="get_agent_prompt"
    )
    registry.register("session.read", _session_read, surface="tool", owner="core:tool")
    registry.register("session.write", _session_write, surface="tool", owner="core:tool")
    registry.register("session.interrupt", _session_interrupt, surface="tool", owner="core:tool")
    registry.register("session.new", _session_new, surface="tool", owner="core:tool")
    registry.register("session.send", _session_send, surface="tool", owner="core:tool")
    registry.register("workdir.read", _workdir_read, surface="tool", owner="core:tool")
    registry.register("storage.data_dir", _storage_data_dir, surface="tool", owner="core:tool")
    registry.register("secret.read", _secret_read, surface="tool", owner="core:tool")
    registry.register("secret.write", _secret_write, surface="tool", owner="core:tool")
    registry.register("net.http", _net_http, surface="tool", owner="core:tool")
    registry.register("task.background", _task_background, surface="tool", owner="core:tool")
    registry.register("storage.db", _storage_db, surface="tool", owner="core:tool")
    registry.register("tool.execute", _tool_execute, surface="tool", owner="core:tool")
    registry.register("provider.list", model_meta.provider_list, surface="tool", owner="core:tool")
    registry.register("model.enumerate", model_meta.model_enumerate, surface="tool", owner="core:tool")
    registry.register("tool.read", _tool_read, surface="tool", owner="core:tool")
    registry.register("tool.register", _tool_register, surface="tool", owner="core:tool")
    registry.register("tool.unregister", _tool_unregister, surface="tool", owner="core:tool")
    registry.register("plugin.deps", _plugin_deps, surface="tool", owner="core:tool")
    registry.register("tool.inject", _tool_inject, surface="tool", owner="core:tool")
    registry.register("team.read", _team_read, surface="tool", owner="core:tool")
    registry.register("team.send", _team_send, surface="tool", owner="core:tool")
    registry.register("team.manage", _team_manage, surface="tool", owner="core:tool")
    registry.register("team.spawn", _team_spawn, surface="tool", owner="core:tool")
    registry.register("action.queue", _action_queue, surface="tool", owner="core:tool")
    registry.register("lsp.query", _lsp, surface="tool", owner="core:tool")
    registry.register("mcp.call", _mcp, surface="tool", owner="core:tool")
    registry.register("net.upload", _gitee, surface="tool", owner="core:tool")
    registry.register("net.download", net_sync.net_download, surface="tool", owner="core:net")
    registry.register("subagent.run", _subagent, surface="tool", owner="core:tool")
    registry.register("process.start", process_core.process_start, surface="tool", owner="core:tool")
    registry.register("ui.artifacts.show", _artifacts_show, surface="tool", owner="core:tool")
