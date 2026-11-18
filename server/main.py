"""DriFox headless 服务启动器。

流程（docs/tauri/server-arch.md §一/§三）：
import 拦截（禁运 QtWidgets/QtGui）→ 端口判重 → QCoreApplication →
uvicorn 工作线程 → ChatBackend headless 初始化 → 事件桥绑定（双向就绪）→
stdout 握手 JSON → stdin EOF 自退 → QCoreApplication.exec()

启动：python -m server.main [--port 0]
"""
import argparse
import json
import logging
import os
import queue
import secrets
import sys
import threading
import time

from server import __version__

logger = logging.getLogger("drifox.server")


def install_import_guard() -> None:
    """必须在任何 app/ 模块 import 之前调用。"""
    from server.runtime.import_guard import install_headless_guard

    install_headless_guard()


def ensure_single_instance(port: int) -> bool:
    """端口判重：已占用且 /health 确认是本服务 → 打印提示并返回 False。"""
    from server.runtime import port_guard

    if not port_guard.is_in_use(port):
        return True
    existing = port_guard.probe_existing(port)
    if existing:
        print(f"已有 DriFox 服务实例: {existing}", flush=True)
    else:
        print(f"端口 {port} 已被其他进程占用", flush=True)
    return False


def init_chat_backend_headless():
    """ChatBackend headless 初始化（Qt 主线程调用）。

    架构文档约束：不改 app/ 既有文件；ToolPermissionController 构造 +
    set_tool_permission_controller 注入顺序照搬主程序 main_widget。
    """
    from app.core.conversation.backend import ChatBackend
    from app.core.tools.tool_permission_controller import ToolPermissionController

    backend = ChatBackend(window_id="headless")
    backend.set_tool_permission_controller(ToolPermissionController())

    def get_model_config():
        """真实模型配置（M3a）：Settings 单源，引擎按 provider_name 解析适配器。

        键结构对齐引擎契约（backend._build_conversation_error_payload 消费
        display_name/provider_name/模型名称）。PUT /api/config/model 改
        llm_selected_model 后下轮对话自动生效（此处实时读取）。
        """
        try:
            from app.utils.config import Settings
            st = Settings.get_instance()
            selected = st.llm_selected_model.value or ""
            providers = st.llm_saved_providers.value or {}
            overrides = st.llm_model_overrides.value or {}
            cfg = dict(providers.get(selected) or {})
            cfg.setdefault("provider_name", selected)
            cfg.setdefault("display_name", selected)
            model_name = ""
            if isinstance(cfg.get("models"), list) and cfg["models"]:
                first = cfg["models"][0]
                model_name = first.get("name") or first.get("模型名称") or "" if isinstance(first, dict) else str(first)
            cfg.setdefault("模型名称", model_name)
            if selected in overrides:
                cfg.update(overrides.get(selected) or {})
            return cfg
        except Exception:
            logger.exception("get_model_config 失败")
            return {}

    _GET_MODEL_CONFIG_CB["fn"] = get_model_config

    try:
        backend.initialize(get_model_config, workdir=os.getcwd())
        logger.info("ChatBackend headless 初始化完成")
    except Exception:
        # 同步段失败不阻塞服务起（M1 验收只要求骨架通路）；
        # 失败明细由 ChatBackend 内部日志给出，health 侧 backend_ready=false 可见
        logger.exception("ChatBackend 初始化失败（服务继续，backend_ready=false）")
    return backend


def _watch_stdin(on_eof) -> None:
    """stdin EOF → 自退（Tauri 壳 kill 之外的双保险）。"""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except Exception:
        pass
    logger.info("stdin EOF，服务退出")
    on_eof()


def _find_session(backend, session_id: str):
    """按 session_id 查会话，返回 (index, session)；未命中 (-1, None)。"""
    sessions = backend.session_manager.get_all_sessions()
    for i, s in enumerate(sessions):
        if s.session_id == session_id:
            return i, s
    return -1, None


def _session_summary(s) -> dict:
    return {
        "session_id": s.session_id,
        "title": s.name,
        "message_count": s.message_count,
        "created_at": s.created_at,
        "last_updated": s.last_updated,
        "project": s.originating_project,
        "is_temp": s.is_temp,
    }


STUB_MODEL = {"enabled": False}  # --stub-model 时置位（send 命令假流式）
STUB_STATE = {"event": None, "permission": None}  # stub 剧本裁决挂起点（M2b 场景 2）
STUB_BRIDGE = {"ref": None}  # stub 剧本的事件桥引用（handle_command 注入）


def _run_stub_scenario(bridge, backend, content: str) -> None:
    """stub 剧本（独立线程，M2b 场景 2/3 驱动）。

    - 内容含 "perm"：permission 介入剧本——tool.started + permission.requested
      后挂起等裁决（REST/WS permission_decide 唤醒）；approve→tool.result 必达，
      deny→无 tool.result 但 finished 必达（T13 异常流 A）
    - 内容含 "retry"：started 前 retry.status → 短暂等待 → retry.resolved
      （T13 异常流 B 序列）
    - 其余：直接流（场景 1 回归路径）
    事件经 bridge.emit_custom / backend 信号双通道，覆盖事件桥全链。
    """
    def work():
        # 🟡5：magic 前缀全等匹配，防用户真实消息含「重试/perm」误入假流式
        if content.startswith("[stub:perm]"):
            tc = "tc_stub_perm"
            STUB_STATE["permission"] = None
            ev = threading.Event()
            STUB_STATE["event"] = ev
            backend.stream_started.emit()
            bridge.emit_custom("tool.started", {
                "tool_call_id": tc, "tool_name": "bash",
                "arguments": {"command": "echo stub"},
            })
            bridge.emit_custom("permission.requested", {
                "tool_call_id": tc, "tool_name": "bash",
                "arguments": {"command": "echo stub"},
            })
            ev.wait(30)  # 等裁决；超时按 deny 语义收尾
            decision = (STUB_STATE["permission"] or {}).get("decision", "deny")
            if decision == "approve":
                bridge.emit_custom("tool.result", {
                    "tool_call_id": tc, "tool_name": "bash", "result": "stub 执行输出",
                })
                reply = "已批准执行完成"
            else:
                reply = "工具执行被拒绝，本轮结束"
        elif content.startswith("[stub:retry]"):
            bridge.emit_custom("retry.status", {
                "error_type": "rate_limit", "attempt": 1,
                "max_retries": 3, "wait_time": 1.0,
            })
            time.sleep(0.3)
            bridge.emit_custom("retry.resolved", {})
            backend.stream_started.emit()
            reply = "重试成功后的回复"
        else:
            backend.stream_started.emit()
            reply = f"（stub 回复）收到：{content[:20]}"
        chunks = [reply[i : i + 2] for i in range(0, len(reply), 2)]
        for c in chunks:
            backend.stream_chunk.emit(c)
            time.sleep(0.03)
        backend.stream_finished.emit(reply)
        STUB_STATE["event"] = None

    threading.Thread(target=work, name="stub-scenario", daemon=True).start()


_GET_MODEL_CONFIG_CB = {"fn": None}  # main() 注入的真实现


def get_model_config_impl() -> dict:
    fn = _GET_MODEL_CONFIG_CB.get("fn")
    return fn() if fn else {}


def run_backend_command(backend, name: str, args: dict) -> dict:
    """backend 命令分发表（仅 Qt 主线程执行，架构文档 §一 命令方向）。"""
    if name == "send":
        session_id = args.get("session_id", "")
        content = args.get("content", "")
        idx, session = _find_session(backend, session_id)
        if session is None:
            raise ValueError(f"会话不存在: {session_id}")
        if backend.get_current_session() is None or backend.get_current_session().session_id != session_id:
            backend.switch_session(idx)  # emit session_changed
        if STUB_MODEL["enabled"]:
            # stub 假流式（无模型配置时的链路验收）：绕过引擎 worker，
            # 直接 emit 引擎信号 / bridge 事件 → WS 广播；不落库
            _run_stub_scenario(STUB_BRIDGE["ref"], backend, content)
            return {"session_id": session_id, "stub": True}
        # 延迟组件兜底（首条消息可能赶在 QTimer 错峰窗口前）
        backend.ensure_deferred_components()
        backend.send_message(content, agent_name=args.get("agent") or None)
        return {"session_id": session_id}

    if name == "stop":
        # UI 主线程路径必须短预算（backend.py:953 注释：300~500ms）
        r = backend.stop_streaming(timeout_ms=400)
        return {"stopped": bool(r)}

    if name == "create_session":
        s = backend.create_session()
        title = args.get("title") or ""
        if title:
            s.name = title
        if args.get("project"):
            # 首发项目快照（chat_session.py originating_project 契约：一旦锁定不改）
            if not s.originating_project:
                s.originating_project = args["project"]
        if args.get("agent"):
            s.metadata["agent"] = args["agent"]
        return {"session_id": s.session_id, "title": s.name}

    if name == "list_sessions":
        project = args.get("project", "")
        items = [_session_summary(s) for s in backend.session_manager.get_all_sessions()]
        if project:
            items = [it for it in items if it["project"] == project]
        return {"items": items}

    if name == "get_session":
        _, s = _find_session(backend, args.get("session_id", ""))
        if s is None:
            raise ValueError(f"会话不存在: {args.get('session_id')}")
        s.ensure_messages()
        out = _session_summary(s)
        out["messages"] = [
            {k: v for k, v in m.items() if k in ("role", "content", "timestamp")}
            for m in (s.messages or [])
        ]
        return out

    if name == "patch_session":
        _, s = _find_session(backend, args.get("session_id", ""))
        if s is None:
            raise ValueError(f"会话不存在: {args.get('session_id')}")
        title = args.get("title") or ""
        if title:
            s.name = title
            s.topic_summary = title
            s.set_user_edited_title()
        return {"ok": True, "title": s.name}

    if name == "delete_session":
        idx, _ = _find_session(backend, args.get("session_id", ""))
        if idx < 0:
            raise ValueError(f"会话不存在: {args.get('session_id')}")
        return {"ok": bool(backend.delete_session(idx))}

    if name == "permission_decide":
        tool_call_id = args.get("tool_call_id", "")
        decision = args.get("decision", "")
        if decision not in ("approve", "deny"):
            raise ValueError(f"decision 枚举非法: {decision}")
        if STUB_STATE["event"] is not None:
            # stub 剧本挂起中：结果交还剧本线程收尾（场景 2 可测性）
            STUB_STATE["permission"] = args
            STUB_STATE["event"].set()
            return {"ok": True, "stub": True}
        engine = getattr(backend, "_chat_engine", None)
        if engine is None:
            raise RuntimeError("引擎未就绪，无法裁决")
        if decision == "approve":
            scope = args.get("auto_allow", "")
            engine.approve_tool_permission(
                tool_call_id,
                auto_allow=(scope == "round"),
                session_allow=bool(args.get("session_allow")) or scope == "session",
            )
        else:
            engine.deny_tool_permission(tool_call_id, reason=args.get("reason", ""))
        return {"ok": True}

    if name == "question_answer":
        answers = args.get("answers", [])
        if not isinstance(answers, list):
            raise ValueError("answers 必须为数组")
        # worker.provide_answer 语义为文本回答（主程序输入框直传）
        backend.provide_question_answer("\n".join(str(a) for a in answers))
        return {"ok": True}

    if name == "list_agents":
        am = getattr(backend, "agent_manager", None)
        agents = am.list_agents(include_hidden=False) if am else []
        return {
            "items": [
                {
                    "name": a.name,
                    "description": a.description,
                    "mode": a.mode,
                    "color": a.color,
                    "model": a.model,
                }
                for a in agents
            ]
        }

    if name == "get_config_model":
        return {"config": get_model_config_impl()}

    if name == "put_config_model":
        from app.utils.config import Settings as _S
        st = _S.get_instance()
        cfg = args.get("config") or {}
        selected = cfg.get("provider_name") or cfg.get("provider") or ""
        if selected:
            st.llm_selected_model.value = selected
            st.save()
        return {"ok": True, "provider": selected}

    if name == "list_plugins":
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
        from app.utils import app_state
        schemas = PluginConfigRegistry.get_instance().list_schemas()
        by_name = {sc.plugin_name: sc for sc in schemas}
        # 插件目录扫描（系统 plugins/ 与用户 ~/.drifox/plugins/）
        import glob as _glob
        dirs = []
        for pattern in ("plugins/*/.drifox-plugin/plugin.json",
                        os.path.expanduser("~/.drifox/plugins/*/.drifox-plugin/plugin.json")):
            dirs.extend(_glob.glob(pattern))
        items = []
        for manifest in sorted(dirs):
            try:
                with open(manifest, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            name = meta.get("name", "")
            sc = by_name.get(name)
            fields = []
            if sc:
                for fd in sc.fields:
                    fields.append({
                        "key": fd.key, "label": fd.label, "type": fd.type,
                        "default": fd.default, "env": fd.env or "",
                        "placeholder": fd.placeholder or "",
                        "description": fd.description or "",
                        **({"options": [{"value": v, "label": lb} for v, lb in fd.options]} if getattr(fd, "options", None) else {}),
                        **({"url": fd.url} if getattr(fd, "url", None) else {}),
                        **({"action": _jsonable(fd.action) if fd.action else None} if getattr(fd, "action", None) else {}),
                    })
            enabled = app_state.get(f"server.plugin_enabled.{name}", True)
            items.append({
                "name": name,
                "enabled": bool(enabled),
                "version": meta.get("version", ""),
                "title": (sc.title if sc else "") or meta.get("display_name", ""),
                "config_schema": {"title": sc.title if sc else "", "fields": fields} if sc else None,
            })
        return {"items": items}

    if name == "set_plugin_enabled":
        from app.utils import app_state
        plugin_name = args.get("name", "")
        enabled = bool(args.get("enabled", True))
        app_state.set(f"server.plugin_enabled.{plugin_name}", enabled)
        # 触发既有热更新钩子（组件级启停待 M3b 与 app/ 侧 manager 对齐，见差异清单）
        hm = getattr(backend, "_hook_manager", None)
        if hm is not None:
            try:
                hm.trigger_event("PluginChanged", context={"plugin": plugin_name, "enabled": enabled}, current_message="")
            except Exception:
                logger.exception("PluginChanged 触发失败")
        return {"ok": True, "name": plugin_name, "enabled": enabled}

    if name == "plugin_action":
        # 插件动作触发：config_schema type=action 的 ToolActionSpec 执行入口
        # 无既有 headless 执行链（差异清单 M3b），当前回执 schema 校验结果
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
        sc = PluginConfigRegistry.get_instance().get(args.get("name", ""))
        fk = args.get("field_key", "")
        fd = sc.get_field(fk) if sc else None
        if fd is None or fd.type != "action":
            raise ValueError(f"action 字段不存在: {args.get('name')}.{fk}")
        return {"ok": True, "stub": True,
                "action": _jsonable(fd.action),
                "note": "headless 执行链待 M3b 对齐 app/ 侧 ToolActionSpec 运行时"}

    if name == "uninstall_plugin":
        # 卸载属破坏性操作且依赖 app/ 侧插件 manager（目录删除+组件清理+钩子），
        # M3a 不做 shell 删除（越权），返回未实现语义（差异清单）
        return {"ok": False, "code": "not_implemented",
                "note": "uninstall 需 app/ 侧插件 manager 参与，M3b 对齐"}

    if name == "context_usage":
        usage = backend.get_context_usage()
        return {"token_count": int(usage[0] or 0), "limit": int(usage[1] or 0)}

    if name == "input_history":
        # InputHistoryRepository(db_manager)；db 从 SessionStore 单例取
        try:
            from app.core.store.input_history_repo import InputHistoryRepository
            store = backend.session_store
            db = getattr(store, "_db_manager", None) or getattr(store, "db_manager", None)
            if db is None:
                return {"items": []}
            repo = InputHistoryRepository(db)
            rows = repo.get_all(limit=50) or []
            return {"items": [
                {"content": r.get("content", ""), "ts": r.get("created_at") or r.get("ts")}
                for r in rows if isinstance(r, dict)
            ]}
        except Exception:
            logger.exception("input_history 读取失败")
            return {"items": []}

    if name == "coding_plan_usage":
        # 无既有 headless 数据源（coding_plan_fetcher 依赖 UI 侧套餐绑定），占位结构
        return {"items": [], "note": "占位：headless 无套餐绑定数据源（frontend-map §三 双圆环左环）"}

    if name == "user_profile":
        import getpass
        return {"name": getpass.getuser(), "source": "local"}

    if name == "ui_sidebar_items":
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        from app.utils import app_state
        items = []
        for info in UIPluginRegistry.get_instance().get_sidebar_items():
            pinned_key = f"ui.sidebar.pinned.{info.item_id}"
            items.append({
                "plugin_name": info.plugin_name,
                "item_id": info.item_id,
                "label": info.label,
                "group": info.group,
                "pinned": bool(app_state.get(pinned_key, info.default_visible)),
            })
        return {"items": items}

    if name == "set_ui_pinned":
        from app.utils import app_state
        key = args.get("key", "")
        if not key.startswith("ui.sidebar.pinned."):
            raise ValueError("pinned key 必须以 ui.sidebar.pinned. 开头")
        app_state.set(key, bool(args.get("value", False)))
        return {"ok": True}

    if name == "ui_context_menu":
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        reg = UIPluginRegistry.get_instance()
        items = []
        for aid, info in reg._context_actions.items():
            items.append({
                "action_id": aid,
                "plugin_name": getattr(info, "plugin_name", ""),
                "target": getattr(info, "target", ""),
                "label": getattr(info, "label", ""),
            })
        return {"items": items}

    if name == "ui_plugin_windows":
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        reg = UIPluginRegistry.get_instance()
        items = []
        for wid, info in reg._windows.items():
            items.append({
                "window_id": wid,
                "plugin_name": info.plugin_name,
                "title": info.title,
                "group": info.group or "",
                "width": info.width,
                "height": info.height,
            })
        return {"items": items}

    if name == "welcome_tabs":
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        tabs = UIPluginRegistry.get_instance().get_welcome_tabs()
        return {"items": [
            {"plugin_name": i.plugin_name, "mode_key": i.mode_key,
             "label": i.label, "priority": i.priority}
            for i in tabs.values()
        ]}

    if name == "list_projects":
        projects = sorted(
            {
                s.originating_project
                for s in backend.session_manager.get_all_sessions()
                if s.originating_project
            }
        )
        return {"items": projects}

    raise ValueError(f"未知 backend 命令: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="DriFox headless 服务")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0=自动分配")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--no-stdin-guard",
        action="store_true",
        help="禁用 stdin EOF 自退（验收脚本等非管道宿主用；Tauri 壳默认启用）",
    )
    parser.add_argument(
        "--stub-model",
        action="store_true",
        help="send 命令改走假流式（直接 emit 引擎信号），用于无模型配置的链路验收",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    STUB_MODEL["enabled"] = bool(args.stub_model)

    # 1. import 拦截必须最先（任何 app/ 或 QtWidgets 触达之前）
    install_import_guard()

    # 2. 端口判重
    from server.runtime import port_guard

    port = port_guard.pick_port(args.port)
    if not ensure_single_instance(port):
        return 1

    # 3. Qt 主线程载体 + 两条转发队列
    from server.bridge.event_bridge import EventBridge
    from server.runtime.command_pump import CommandPump
    from server.runtime.qt_loop import create_core_app

    qt_app = create_core_app(sys.argv)
    command_queue: "queue.Queue" = queue.Queue()  # api 线程 → Qt 主线程泵
    ctx_holder: dict = {}  # bridge/backend 引用（api 回调线程安全读取）

    # 4. uvicorn 工作线程（asyncio loop）
    import uvicorn

    from server.api.app import ServerContext, create_api

    # 5b. 事件桥 + 命令分发（定义必须在 api_thread.start() 之前：🟡4 join 会拉长主线程
    # 到达后续定义的时间窗，startup 回调先触发将致闭包自由变量未绑定 NameError）
    bridge = EventBridge()
    ctx_holder["bridge"] = bridge
    bind_lock = threading.Lock()
    bind_state = {"bound": False}


    def try_bind_bridge() -> None:
        """双向就绪绑定：loop + asyncio_queue + backend 三者齐备才 attach。

        🟡1 修正（review R3）：on_loop_ready（asyncio 线程）与步骤 6（主线程）
        并发可达，无锁 dict 检查存在双 attach 竞态 → 信号双 connect 重复广播。
        check-and-set 全程持锁原子化；attach 内部仅 getattr/connect，线程安全。
        """
        with bind_lock:
            if bind_state["bound"]:
                return
            # 经 ctx_holder 读（定义顺序无关，规避闭包自由变量时序）
            ctx_ref = ctx_holder.get("ctx")
            if ctx_ref is None or ctx_ref.loop is None or ctx_ref.asyncio_queue is None:
                return
            backend = ctx_holder.get("backend")
            if backend is None:
                return
            bridge.attach(backend, ctx_ref.loop, ctx_ref.asyncio_queue)
            bind_state["bound"] = True

    def handle_command(cmd: dict) -> None:
        """Qt 主线程命令分发：qt_call 调用（reply Future）→ dispatch 表。"""
        if cmd.get("kind") != "call":
            logger.warning("未知命令格式: %r", cmd.get("name"))
            return
        try:
            STUB_BRIDGE["ref"] = ctx_holder.get("bridge")
            result = run_backend_command(ctx_holder["backend"], cmd["name"], cmd.get("args", {}))
            cmd["reply"].set_result(result)
        except Exception as e:
            logger.exception("命令 %s 执行失败", cmd.get("name"))
            cmd["reply"].set_exception(e)

    pump = CommandPump(command_queue, handle_command, interval_ms=10)



    auth_token = secrets.token_hex(16)  # 🟡6 WS 握手鉴权
    ctx = ServerContext(
        version=__version__,
        pid=os.getpid(),
        port=port,
        command_queue=command_queue,
        auth_token=auth_token,
    )
    ctx_holder["ctx"] = ctx  # try_bind_bridge 经此读取（定义顺序无关）
    ctx.bridge = ctx_holder["bridge"]  # health 端点读取（ctx 定义后回填）

    def on_loop_ready() -> None:
        """api startup 回调：loop 就绪后尝试绑定事件桥。"""
        try_bind_bridge()

    ctx.on_loop_ready = on_loop_ready  # 属性赋值：startup 时读最新值，规避闭包绑定
    api = create_api(ctx)
    config = uvicorn.Config(
        api, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)

    api_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    api_thread.start()

    # 🟡4：bind 失败（端口被抢）时 uvicorn 线程静默死亡 → 显式检测非零退出
    if not server.started:
        api_thread.join(timeout=20)
        if not server.started:
            if api_thread.is_alive():
                print(
                    "ERROR: uvicorn 20s 未完成 startup（疑似端口被占）",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            print(
                f"ERROR: 服务监听 {port} 失败（bind 失败或端口被抢），进程退出",
                file=sys.stderr,
                flush=True,
            )
            return 1

    # 5a. stdout 握手：端口就绪即打（Tauri 壳读此行注入端口，越早越好；
    # backend_ready 状态由 GET /api/health 反映，不阻塞壳启动流程）
    print(
        '{"drifox_server":{"port":%d,"pid":%d,"version":"%s","token":"%s"}}'
        % (port, os.getpid(), __version__, auth_token),
        flush=True,
    )

    # 6. ChatBackend 在 Qt 主线程初始化（QTimer 延迟段由 exec 驱动）
    ctx_holder["backend"] = init_chat_backend_headless()
    ctx.backend = ctx_holder["backend"]  # health 端点读取 backend_ready
    try_bind_bridge()
    # M2b：question/permission/retry 下行走引擎回调链（backend 信号无 emit 点）
    ctx_holder["bridge"].register_engine_callbacks(ctx_holder["backend"])

    # 7. 命令泵启动（QTimer 待 exec 后才 tick）
    pump.start()

    # 9. stdin EOF 自退（Tauri 壳 kill 之外的双保险；非管道宿主可 --no-stdin-guard）
    if not args.no_stdin_guard:
        threading.Thread(
            target=_watch_stdin, args=(qt_app.quit,), name="stdin-watch", daemon=True
        ).start()

    # 10. Qt 事件循环（阻塞至 quit）
    try:
        qt_app.exec_()
    finally:
        pump.stop()
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
