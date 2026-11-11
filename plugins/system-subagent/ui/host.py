# -*- coding: utf-8 -*-
"""子智能体窗口宿主：每窗口一个管理器，并把它作为窗口级服务暴露给宿主。

三件事，全部是「桥」而非业务（业务在 manager.py）：

1. **窗口发现**：1s 心跳遍历窗口表，为每个窗口建管理器（依赖宿主已有的
   agent_manager / tool_executor / session_store；模型解析走 ``model.config_for``
   能力）。窗口消失即取消该窗口任务并摘除服务。
2. **服务注册**：管理器写入 ``WindowServiceRegistry``（key = ``subagent``，
   按 window_id 分桶），主程序与工具侧按窗口取用——主程序不再持有子智能体字段。
3. **宿主回调**：完成归档、批次汇总开新轮、权限弹窗、停止、进入会话、日志展示。
   这些原本是 main_widget 的槽位，随管理器一起迁入插件。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QObject, QTimer

from .manager_module import load_manager_module

SERVICE_KEY = "subagent"
_PLUGIN_NAME = "system-subagent"
_HEARTBEAT_MS = 1000


def _alive(widget: Any) -> bool:
    if widget is None:
        return False
    try:
        import sip

        return not sip.isdeleted(widget)
    except Exception:
        return True


class _WindowBinding:
    """单窗口绑定：管理器 + 能力池 + 信号连接表"""

    __slots__ = ("window_id", "main_widget", "manager", "pool", "connections")

    def __init__(self, window_id: str, main_widget: Any, manager: Any, pool: Any):
        self.window_id = window_id
        self.main_widget = main_widget
        self.manager = manager
        self.pool = pool
        self.connections: List[tuple] = []


class _WindowFacade:
    """暴露给主程序/工具侧的窗口门面：方法签名与原来直接拿管理器时一致。

    其余方法经 ``__getattr__`` 透传到该窗口管理器（如 get_tasks_status_with_details）。
    """

    __slots__ = ("_host", "_window_id")

    def __init__(self, host: "SubAgentHost", window_id: str):
        self._host = host
        self._window_id = window_id

    @property
    def manager(self) -> Optional[Any]:
        return self._host.manager_for(self._window_id)

    def __getattr__(self, name: str) -> Any:
        manager = self._host.manager_for(self._window_id)
        if manager is None:
            raise AttributeError(f"子智能体管理器未就绪: {name}")
        return getattr(manager, name)

    # 需要走宿主的动作（带 UI 收尾）

    def stop_task(self, task_id: str) -> None:
        self._host.stop_task(self._window_id, task_id)

    def show_session(self, task_id: str, agent_name: str) -> None:
        self._host.show_session(self._window_id, task_id, agent_name)

    def latest_task(self) -> Dict:
        return self._host.latest_task(self._window_id)


class SubAgentHost(QObject):
    """每窗口子智能体管理器的持有者与宿主门面。"""

    def __init__(self, registry: Any, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._registry = registry
        self._bindings: Dict[str, _WindowBinding] = {}
        self._services = None
        self._legacy_imported = False
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(_HEARTBEAT_MS)
        self._heartbeat.timeout.connect(self._sync_windows)

    # ── 生命周期 ──

    def start(self) -> None:
        from app.plugins.registries.window_services import get_window_services

        self._services = get_window_services()
        self._sync_windows()
        self._heartbeat.start()

    def stop(self) -> None:
        self._heartbeat.stop()
        for binding in list(self._bindings.values()):
            self._teardown(binding)
        self._bindings.clear()

    # ── 窗口发现 ──

    def _iter_windows(self):
        windows = getattr(self._registry, "_window_main_widgets", None) or {}
        for window_id, mw in list(windows.items()):
            if _alive(mw):
                yield window_id, mw

    def _sync_windows(self) -> None:
        seen = set()
        try:
            for window_id, mw in self._iter_windows():
                seen.add(window_id)
                binding = self._bindings.get(window_id)
                if binding is not None and binding.main_widget is mw:
                    continue
                if binding is not None:
                    self._teardown(binding)
                    self._bindings.pop(window_id, None)
                self._attach(window_id, mw)
        except Exception as e:  # noqa: BLE001 - 心跳异常不能打断 UI
            logger.warning(f"[system-subagent] 窗口同步失败: {e}")

        for window_id in [wid for wid in self._bindings if wid not in seen]:
            self._teardown(self._bindings.pop(window_id, None))

    def _build_pool(self, mw: Any) -> Any:
        from app.plugins.capabilities.bootstrap import ensure_registered
        from app.plugins.capabilities.registry import CapabilityRegistry

        ensure_registered("ui")
        return CapabilityRegistry.get_instance().build_pool(surface="ui", host=mw, plugin=_PLUGIN_NAME)

    def _attach(self, window_id: str, mw: Any) -> None:
        backend = getattr(mw, "backend", None)
        if backend is None:
            return
        if getattr(backend, "tool_executor", None) is None or getattr(backend, "agent_manager", None) is None:
            return  # 延迟组件未就绪，下个心跳再试
        try:
            pool = self._build_pool(mw)
            module = load_manager_module()
            manager = module.create_manager(backend, caps=pool)
        except Exception as e:  # noqa: BLE001 - 插件内部失败不拖垮窗口
            logger.warning(f"[system-subagent] 窗口 {window_id} 管理器创建失败: {e}")
            return
        try:
            store = module.SubAgentTaskStore(pool["storage.db"] if "storage.db" in pool else None)
            if not self._legacy_imported:
                store.import_legacy()
                self._legacy_imported = True
            manager.set_task_store(store)
            if "session.read" in pool:
                read = pool["session.read"]
                manager.set_history_getter(lambda: list(read().get("messages") or []))
            if "session.send" in pool:
                manager.set_session_send(pool["session.send"])
            manager.start_stall_detector()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 管理器接线失败: {e}")

        binding = _WindowBinding(window_id, mw, manager, pool)
        for signal, slot in (
            (manager.task_finished, lambda tid, result, _wid=window_id: self._on_task_finished(_wid, tid, result)),
            (
                manager.batch_finished,
                lambda content, failed, _wid=window_id: self._on_batch_finished(_wid, content, failed),
            ),
            (
                manager.permission_requested,
                lambda wid, tid, tool, args, _wid=window_id: self._on_permission_requested(_wid, wid, tid, tool, args),
            ),
        ):
            try:
                signal.connect(slot)
                binding.connections.append((signal, slot))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[system-subagent] 信号连接失败: {e}")
        self._bindings[window_id] = binding
        if self._services is not None:
            self._services.register(
                SERVICE_KEY, _WindowFacade(self, window_id), plugin_name=_PLUGIN_NAME, window_id=window_id
            )
        logger.info(f"[system-subagent] 窗口 {window_id} 子智能体管理器已就绪")

    def _teardown(self, binding: Optional[_WindowBinding]) -> None:
        if binding is None:
            return
        for signal, slot in binding.connections:
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        binding.connections.clear()
        try:
            binding.manager.cancel_all()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 取消窗口 {binding.window_id} 任务失败: {e}")
        if self._services is not None:
            try:
                self._services.drop_window(binding.window_id)
            except Exception:
                pass

    # ── 对外门面（主程序/工具侧按 window_id 调用） ──

    def manager_for(self, window_id: str) -> Optional[Any]:
        binding = self._bindings.get(window_id)
        if binding is not None:
            return binding.manager
        if self._services is not None:
            return self._services.get(SERVICE_KEY, window_id)
        return None

    def execute_task(self, window_id: str, **kwargs) -> bool:
        manager = self.manager_for(window_id)
        if manager is None:
            logger.warning(f"[system-subagent] 窗口 {window_id} 无管理器，任务未派发")
            return False
        return bool(manager.execute_task(**kwargs))

    def get_task_logs(self, window_id: str, task_id: str) -> Dict:
        manager = self.manager_for(window_id)
        return manager.get_task_logs(task_id) if manager is not None else {"found": False}

    def respond_permission(self, window_id: str, task_id: str, allow: bool) -> None:
        manager = self.manager_for(window_id)
        if manager is not None:
            manager.respond_permission(task_id, allow)

    def stop_task(self, window_id: str, task_id: str) -> None:
        """中止任务：置取消 + 错误标记后走标准完成链（UI 与归档一起收尾）。"""
        manager = self.manager_for(window_id)
        if manager is None:
            return
        executor = manager._running_tasks.get(task_id)
        if executor is None:
            logger.warning(f"[system-subagent] 停止失败: 任务 {task_id} 不在运行中")
            return
        agent_name = executor.agent_name
        description = getattr(executor, "task_description", "")
        manual_error = "用户已手动中止子智能体"
        try:
            executor.cancel(manual_error)
        except TypeError:
            executor.cancel()
        executor._execution_error = manual_error
        try:
            manager.task_finished.emit(task_id, "")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[system-subagent] task_finished.emit 失败 (中止路径): {e}")
        _notify(
            window_id,
            self._bindings,
            "子智能体已中止",
            f"已手动中止子智能体「{agent_name}」: {description[:40]}{'...' if len(description) > 40 else ''}",
        )

    def latest_task(self, window_id: str) -> Dict:
        """该窗口当前会话最近一个任务（会话卡显示时恢复内容）。"""
        binding = self._bindings.get(window_id)
        if binding is None:
            return {"found": False}
        backend = getattr(binding.main_widget, "backend", None)
        try:
            session = backend.get_current_session() if backend is not None else None
        except Exception:  # noqa: BLE001
            session = None
        session_id = str(getattr(session, "session_id", "") or "")
        try:
            return binding.manager.latest_task_for_session(session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 恢复会话卡内容失败: {e}")
            return {"found": False}

    def show_session(self, window_id: str, task_id: str, agent_name: str) -> None:
        """进入子智能体会话：读任务日志并灌入会话卡。"""
        manager = self.manager_for(window_id)
        if manager is None:
            # 此前静默 return：用户点「进入会话」无任何反应且日志零痕迹
            logger.warning(f"[system-subagent] 进入会话失败: 窗口 {window_id} 管理器未就绪 (task={task_id[:8]})")
            _notify(
                window_id,
                self._bindings,
                "子智能体服务未就绪",
                "窗口管理器尚未初始化，请稍候 1-2 秒后重试",
            )
            return
        data = manager.get_task_logs(task_id)
        if not data.get("found"):
            logger.warning(
                f"[system-subagent] 进入会话失败: 任务日志未找到 (task={task_id[:8]}, "
                f"agent={agent_name}, window={window_id})——任务可能早于 7 天清理期或落库失败"
            )
            _notify(window_id, self._bindings, "任务日志不存在", f"任务 {task_id[:8]} 的日志查不到，可能已被清理")
            return
        logs = data.get("logs", [])
        summary = data.get("summary", {})
        summary["status"] = data.get("status", "unknown")
        summary["agent_name"] = agent_name
        service = self._registry.get_service("subagent_session")
        if service is None:
            logger.warning(f"[system-subagent] 会话服务不可用，跳过进入会话 (task={task_id[:8]})")
            _notify(window_id, self._bindings, "会话卡不可用", "会话服务未注册，请检查 system-subagent 插件是否正常加载")
            return
        service.show(
            task_id=task_id,
            agent_name=agent_name,
            logs=logs,
            summary=summary,
            logs_provider=lambda: manager.get_task_logs(task_id),
        )

    # ── 宿主回调（原 main_widget 槽位） ──

    def _on_task_finished(self, window_id: str, task_id: str, result: str) -> None:
        """任务完成：内存归档 + DB 推进到 finished。

        完成链（批次计数/流式注入/全完成判定）在 manager 内部；运行面板收尾由
        run_bridge 订阅同一信号完成。
        """
        binding = self._bindings.get(window_id)
        if binding is None:
            return
        manager = binding.manager
        executor = manager._running_tasks.get(task_id)
        execution_error = getattr(executor, "_execution_error", None) if executor else None
        try:
            manager.mark_task_finished(task_id, result, execution_error or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 任务归档失败 {task_id}: {e}")

    def _on_batch_finished(self, window_id: str, callback_content: str, failed: int) -> None:
        """批次全部完成（非流式路径）：经 session.send 开新轮承接汇总。"""
        binding = self._bindings.get(window_id)
        if binding is None:
            return
        logger.info(f"[system-subagent] batch_finished 非流式路径，开新轮 failed={failed}")
        send = binding.pool["session.send"] if "session.send" in binding.pool else None
        if not callable(send):
            logger.warning("[system-subagent] session.send 能力不可用，批次汇总未发送")
            return
        try:
            send(callback_content, ephemeral=False, mode="queue")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 批次汇总发送失败: {e}")

    def _on_permission_requested(
        self, window_id: str, owner_window_id: str, task_id: str, tool_name: str, arguments: dict
    ) -> None:
        """权限弹窗（主线程）。window_id 与请求来源不符时不弹（多窗口隔离）。"""
        binding = self._bindings.get(window_id)
        if binding is None:
            return
        if owner_window_id and owner_window_id != window_id:
            return
        mw = binding.main_widget
        set_state = getattr(mw, "_set_ai_state", None)
        if callable(set_state):
            try:
                set_state("question")
            except Exception:
                pass
        notify = getattr(mw, "_notify_if_inactive", None)
        if callable(notify):
            try:
                notify("子智能体待授权", f"子智能体请求使用工具 {tool_name}")
            except Exception:
                pass
        allow = False
        try:
            from PyQt5.QtWidgets import QMessageBox

            args_preview = ""
            if arguments:
                try:
                    args_preview = json.dumps(arguments, ensure_ascii=False)[:200]
                except Exception:
                    args_preview = str(arguments)[:200]
            box = QMessageBox(mw)
            box.setWindowTitle("子智能体工具权限请求")
            box.setIcon(QMessageBox.Question)
            box.setText(f"子智能体请求使用工具「{tool_name}」\n\n参数: {args_preview}\n\n是否允许？")
            allow_btn = box.addButton("允许", QMessageBox.AcceptRole)
            deny_btn = box.addButton("拒绝", QMessageBox.RejectRole)
            box.setDefaultButton(deny_btn)
            box.setEscapeButton(deny_btn)
            box.exec_()
            allow = box.clickedButton() is allow_btn
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 权限弹窗失败: {e}")
            allow = False
        try:
            binding.manager.respond_permission(task_id, allow)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 权限响应转发失败: {e}")


def _notify(window_id: str, bindings: Dict[str, _WindowBinding], title: str, content: str) -> None:
    binding = bindings.get(window_id)
    if binding is None:
        return
    try:
        from qfluentwidgets import InfoBar, InfoBarPosition

        InfoBar.info(
            title=title,
            content=content,
            parent=binding.main_widget.window(),
            duration=3000,
            position=InfoBarPosition.BOTTOM,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[system-subagent] 通知失败: {e}")
