# -*- coding: utf-8 -*-
"""子智能体运行面板的窗口桥接

职责：把「每窗口一个 SubAgentManager」的事件流投影到该窗口最新 assistant 卡片上的
内嵌运行面板。三件事：

1. **窗口发现**：管理器由 ``SubAgentHost`` 按窗口创建，本桥只按 window_id 取实例
   （1s 心跳重取，管理器换实例即重绑；其余全部走信号）。
2. **状态投影**：运行中任务的状态记在桥里（``_tasks``），行只是投影。迁卡后新卡
   上新面板从桥里全量补齐，不依赖旧面板实例。
3. **动作回落**：行内「进入会话」「停止」交给窗口宿主（``SubAgentHost``）执行。
"""

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from PySide6.QtCore import QObject, QTimer

PANEL_ID = "subagent_run"
_SELF_PLUGIN = "system-subagent"
_HEARTBEAT_MS = 1000


def _alive(widget: Any) -> bool:
    """widget 是否存活（已 deleteLater 的 wrapper 一律按不存活处理）"""
    if widget is None:
        return False
    try:
        import shiboken6 as sip

        return sip.isValid(widget)
    except Exception:
        return True


class _Binding:
    """单个窗口的 manager 绑定：连接表 + 宿主引用"""

    __slots__ = ("window_id", "main_widget", "manager", "connections")

    def __init__(self, window_id: str, main_widget: Any, manager: Any):
        self.window_id = window_id
        self.main_widget = main_widget
        self.manager = manager
        # [(signal, slot)]，解绑时逐个 disconnect
        self.connections: List[Tuple[Any, Any]] = []


class SubAgentRunBridge(QObject):
    """把子智能体运行态推到消息卡内嵌面板（每窗口一份绑定）。"""

    def __init__(self, registry: Any, host: Any, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._registry = registry
        self._host = host
        self._bindings: Dict[str, _Binding] = {}
        # task_id → 运行中任务记录（完成后即出表，行留在面板上）
        self._tasks: Dict[str, Dict[str, Any]] = {}
        # 已接线动作信号的面板（id 防重复连接；面板销毁后 id 可复用，故同时校验属性）
        self._wired_panels: set = set()

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(_HEARTBEAT_MS)
        self._heartbeat.timeout.connect(self._sync_windows)

    # ── 生命周期 ──

    def start(self) -> None:
        """启动心跳并立刻做一次扫描（插件在窗口已就绪时加载，别等 1s）。"""
        self._sync_windows()
        self._heartbeat.start()

    def stop(self) -> None:
        """停心跳 + 解绑全部 manager + 清面板（热重载/退出）"""
        self._heartbeat.stop()
        for binding in list(self._bindings.values()):
            self._unbind(binding)
        self._bindings.clear()
        self._tasks.clear()
        self._wired_panels.clear()
        try:
            self._registry.detach_card_inline_panel(PANEL_ID)
        except Exception:
            pass

    # ── 窗口扫描与绑定 ──

    def _iter_windows(self):
        """遍历存活窗口：优先用注册表的窗口表（system-timer 同款先例）。"""
        windows = getattr(self._registry, "_window_main_widgets", None) or {}
        for window_id, mw in list(windows.items()):
            if _alive(mw):
                yield window_id, mw

    def _sync_windows(self) -> None:
        """心跳：窗口增减 / manager 变更时重建绑定。"""
        seen = set()
        try:
            for window_id, mw in self._iter_windows():
                seen.add(window_id)
                manager = self._host.manager_for(window_id)
                binding = self._bindings.get(window_id)
                if binding is not None and (binding.manager is manager and binding.main_widget is mw):
                    continue
                if binding is not None:
                    self._unbind(binding)
                    self._bindings.pop(window_id, None)
                if manager is not None:
                    self._bind(window_id, mw, manager)
        except Exception as e:  # noqa: BLE001 - 心跳异常不能打断 UI
            logger.warning(f"[system-subagent] 运行面板窗口同步失败: {e}")

        # 窗口已关闭：解绑（面板随卡片一起销毁）
        for window_id in [wid for wid in self._bindings if wid not in seen]:
            self._unbind(self._bindings.pop(window_id, None))

    def _bind(self, window_id: str, main_widget: Any, manager: Any) -> None:
        binding = _Binding(window_id, main_widget, manager)

        def _connect(signal, slot) -> None:
            try:
                signal.connect(slot)
                binding.connections.append((signal, slot))
            except (RuntimeError, TypeError) as e:
                logger.warning(f"[system-subagent] 信号连接失败 {signal}: {e}")

        _connect(
            manager.task_started,
            lambda tid, name, desc, _wid=window_id: self._on_task_started(_wid, tid, name, desc),
        )
        _connect(
            manager.task_finished,
            lambda tid, result, _wid=window_id: self._on_task_finished(_wid, tid, result),
        )
        self._bindings[window_id] = binding
        logger.debug(f"[system-subagent] 运行面板已绑定窗口 {window_id}")

        # 补投影：绑定建立前已在跑的任务（插件晚加载 / 心跳间隙）
        for task_id, rec in list(self._tasks.items()):
            if rec.get("window_id") == window_id:
                self._project(window_id, task_id)

    @staticmethod
    def _unbind(binding: Optional[_Binding]) -> None:
        if binding is None:
            return
        for signal, slot in binding.connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        binding.connections.clear()

    # ── manager 事件 ──

    def _on_task_started(self, window_id: str, task_id: str, agent_name: str, task_description: str) -> None:
        binding = self._bindings.get(window_id)
        if binding is None:
            return
        manager = binding.manager
        executor = getattr(manager, "_running_tasks", {}).get(task_id)

        model_name = ""
        if executor is not None:
            llm_cfg = getattr(executor, "llm_config", {}) or {}
            model_name = str(llm_cfg.get("模型名称", "") or llm_cfg.get("model", "") or "")

        self._tasks[task_id] = {
            "window_id": window_id,
            "agent_name": agent_name,
            "description": task_description,
            "model_name": model_name,
            "tool_count": 0,
            "context": "",
            "finished": False,
            "success": True,
        }

        # 执行器实时信号：工具计数与上下文用量（连接随执行器销毁自动失效）
        if executor is not None:
            try:
                executor.tool_call_started.connect(
                    lambda tid, name, args, _wid=window_id: self._on_tool_call(_wid, tid)
                )
            except (RuntimeError, TypeError):
                pass
            try:
                executor.token_usage_updated.connect(
                    lambda tid, pt, ct, tt, _wid=window_id: self._on_token_usage(_wid, tid, tt)
                )
            except (RuntimeError, TypeError):
                pass

        self._project(window_id, task_id)

    def _on_tool_call(self, window_id: str, task_id: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        rec["tool_count"] = int(rec.get("tool_count", 0)) + 1
        panel = self._panel_or_project(window_id, task_id)
        if panel is not None:
            try:
                panel.set_task_tool_count(task_id, rec["tool_count"])
            except (RuntimeError, AttributeError):
                pass

    def _on_token_usage(self, window_id: str, task_id: str, total_tokens: int) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        # 显示单次 API 调用的 token 数（累加会随多次工具调用无限膨胀）
        rec["context"] = f"{total_tokens / 1000:.1f}K tokens" if total_tokens >= 1000 else f"{total_tokens} tokens"
        panel = self._panel_or_project(window_id, task_id)
        if panel is not None:
            try:
                panel.set_task_context(task_id, rec["context"])
            except (RuntimeError, AttributeError):
                pass

    def _on_task_finished(self, window_id: str, task_id: str, result: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        success = True
        binding = self._bindings.get(window_id)
        if binding is not None:
            # 主程序的归档槽（mark_task_finished）会先把 executor 从 _running_tasks
            # 弹出，槽序在主程序之后时读不到 executor；先查归档表里的 error。
            entry = getattr(binding.manager, "_finished_tasks", {}).get(task_id) or {}
            error = entry.get("error")
            if not error:
                executor = getattr(binding.manager, "_running_tasks", {}).get(task_id)
                error = getattr(executor, "_execution_error", None) if executor else None
            success = not error
        panel = self._panel_or_project(window_id, task_id)
        if panel is not None:
            try:
                panel.finish_task(task_id, success)
            except (RuntimeError, AttributeError):
                pass
        # 完成后即出表：面板行保留展示，新批次不再迁移旧行（跟最新卡语义）
        self._tasks.pop(task_id, None)

    # ── 投影 ──

    def _panel(self, window_id: str):
        """取该窗口最新卡片上的运行面板（不存在则触发创建/迁卡）。"""
        binding = self._bindings.get(window_id)
        mw = binding.main_widget if binding is not None else None
        if not _alive(mw):
            return None
        try:
            panel = self._registry.attach_card_inline_panel(PANEL_ID, main_widget=mw)
        except Exception as e:  # noqa: BLE001 - 宿主卡片异常不拖垮任务链
            logger.debug(f"[system-subagent] 面板挂载失败: {e}")
            return None
        if panel is None:
            return None
        self._wire_panel(panel, window_id)
        return panel

    def _wire_panel(self, panel: Any, window_id: str) -> None:
        """接面板的动作信号（每实例一次）"""
        if id(panel) in self._wired_panels or getattr(panel, "_drifox_run_bridge_wired", False):
            return
        try:
            panel.enterSessionRequested.connect(lambda tid, name: self._on_enter_session(window_id, tid, name))
            panel.stopRequested.connect(lambda tid: self._on_stop_requested(window_id, tid))
            panel._drifox_run_bridge_wired = True
        except (RuntimeError, AttributeError) as e:
            logger.debug(f"[system-subagent] 面板信号接线失败: {e}")
            return
        self._wired_panels.add(id(panel))

    def _project(self, window_id: str, task_id: str, panel: Any = None) -> Any:
        """把桥里的任务状态全量投影到当前面板（幂等）。

        panel 缺省时自行取（含迁卡）；调用方已拿到面板则直接传入，避免重复 attach。
        """
        rec = self._tasks.get(task_id)
        if rec is None:
            return panel
        if panel is None:
            panel = self._panel(window_id)
        if panel is None:
            return None
        try:
            panel.add_task(task_id, rec["agent_name"], rec["description"], rec["model_name"])
            if rec["tool_count"]:
                panel.set_task_tool_count(task_id, rec["tool_count"])
            if rec["context"]:
                panel.set_task_context(task_id, rec["context"])
            if rec["finished"]:
                panel.finish_task(task_id, rec["success"])
        except (RuntimeError, AttributeError) as e:
            logger.debug(f"[system-subagent] 面板投影失败 {task_id}: {e}")
        return panel

    def _panel_or_project(self, window_id: str, task_id: str):
        """取面板；若该行不在当前面板上（迁卡后的新卡），先全量补齐再返回。"""
        panel = self._panel(window_id)
        if panel is None:
            return None
        try:
            if not panel.has_task(task_id):
                self._project(window_id, task_id, panel)
        except (RuntimeError, AttributeError):
            return None
        return panel

    # ── 动作：交给窗口宿主（管理器在插件内，不再回落 main_widget） ──

    def _on_enter_session(self, window_id: str, task_id: str, agent_name: str) -> None:
        try:
            self._host.show_session(window_id, task_id, agent_name)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 进入会话失败: {e}")

    def _on_stop_requested(self, window_id: str, task_id: str) -> None:
        try:
            self._host.stop_task(window_id, task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 停止子智能体失败: {e}")
