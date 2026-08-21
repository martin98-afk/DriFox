# -*- coding: utf-8 -*-
"""AutoLoop 控制器 — Worker 生命周期、信号接线、窗口会话管理（多窗口隔离）

从主程序 main_widget 的 AutoLoop 槽函数族平移而来：
- request_start / request_stop / request_archive：入口
- _on_* 槽：worker 信号 → 运行卡 UI 更新
- _finish：收尾（解锁 UI / 保存会话 / 清理 worker / 通知）

对话能力全部经 ctx["services"]（main_widget._build_ui_services 注入），
不触碰主程序内部结构。
"""

import os
from typing import Any, Dict, Optional

from loguru import logger
from PyQt5.QtCore import Qt

PLUGIN_ID = "autoloop"


class _WindowSession:
    """单窗口运行会话（worker + 运行卡 + 服务句柄）"""

    def __init__(self):
        self.worker: Optional[Any] = None
        self.running_card: Optional[Any] = None
        self.services: Optional[Dict[str, Any]] = None
        self.window_id: str = ""
        self.finishing: bool = False


class AutoLoopController:
    """AutoLoop 插件控制器（进程级单例，按 window_id 隔离会话）"""

    _instance: Optional["AutoLoopController"] = None

    @classmethod
    def get_instance(cls) -> "AutoLoopController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._sessions: Dict[str, _WindowSession] = {}
        self._running_cards: Dict[str, Any] = {}  # window_id -> RunningCard（bind 时机早于 start）

    # ================================================================
    #  卡片绑定（运行卡 set_context_provider → showEvent 上报）
    # ================================================================

    def bind_running_card(self, card, ctx: Dict[str, Any]):
        """运行卡实例向控制器注册（每窗口一次，卡片显示时）"""
        window_id = str(ctx.get("window_id") or "")
        if not window_id:
            return
        self._running_cards[window_id] = card
        session = self._sessions.setdefault(window_id, _WindowSession())
        session.window_id = window_id
        session.running_card = card
        # 窗口关闭 / 插件卸载：卡片销毁时清理该窗口会话
        try:
            card.destroyed.connect(lambda _=None, wid=window_id: self._on_card_destroyed(wid))
        except TypeError, RuntimeError:
            pass

    def _on_card_destroyed(self, window_id: str):
        card = self._running_cards.pop(window_id, None)
        session = self._sessions.get(window_id)
        if session is not None:
            session.running_card = None
            if session.worker is not None:
                self._cancel_worker(session)
            self._sessions.pop(window_id, None)
        if card is None:
            return

    # ================================================================
    #  入口：开始 / 停止 / 归档
    # ================================================================

    def is_running(self, window_id: str) -> bool:
        session = self._sessions.get(window_id)
        return bool(session and session.worker and session.worker.isRunning())

    def request_start(self, config, ctx: Dict[str, Any]):
        """配置卡点击开始 — 创建 worker 并启动循环

        全局单会话：任一窗口在跑时拒绝新请求（Tab 模式下运行卡为全局单实例）。
        """
        window_id = str(ctx.get("window_id") or "")
        services = ctx.get("services") or {}
        if not window_id or not services:
            logger.warning("[AutoLoop] start rejected: no window context")
            return
        if any(self.is_running(w) for w in self._sessions):
            services.get("notify", lambda *_: None)("AutoLoop", "已有 AutoLoop 在运行，请先停止")
            return

        # 工作目录：config 显式路径 → 服务解析 → cwd 兜底
        project_path = (config.project_path or "").strip()
        if not project_path:
            project_path = services.get("get_workdir", lambda: "")() or os.getcwd()
        abs_path = os.path.abspath(project_path)
        if os.path.isdir(abs_path):
            services.get("set_workdir", lambda _p: None)(abs_path)
            config.project_path = abs_path
            logger.info(f"[AutoLoop] Workdir set to: {abs_path}")
        else:
            logger.warning(f"[AutoLoop] Project path does not exist: {abs_path}")

        # 隐藏配置卡、显示运行卡（full 覆盖层）
        services.get("hide_card", lambda _c: None)("config")

        session = self._sessions.setdefault(window_id, _WindowSession())
        session.window_id = window_id
        session.services = services
        session.finishing = False

        running_card = self._running_cards.get(window_id)
        if running_card is None:
            logger.warning("[AutoLoop] running card not bound yet; start aborted")
            return

        # 确保运行卡显示在覆盖层（toggle：不可见 → 显示）
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance().toggle_floating_card("running", main_widget=ctx.get("main_widget"))
        running_card.show_stop_button()
        running_card.start_animation()
        running_card.set_max_tokens(config.max_tokens)
        running_card.set_task(config.task_prompt)

        # 锁定 UI（独占模式）
        services.get("enter_exclusive_ui_mode", lambda _s: None)(PLUGIN_ID)

        # 创建 worker（tools schema 按 auto_loop agent 视角过滤）
        from autoloop_core.worker import AutoLoopWorker

        worker = AutoLoopWorker()
        worker.configure(
            config=config,
            model_config_getter=services["get_model_config"],
            tool_executor=services.get("get_tool_executor")(),
            tools_schema=services.get("get_tools_schema", lambda _n: [])("auto_loop"),
            agent_system_prompt_getter=services.get("get_agent_prompt", lambda _n: ""),
            agent_manager=services.get("get_agent_manager")(),
        )
        session.worker = worker

        # 接线：worker 信号 → 运行卡更新（QueuedConnection 保 UI 线程安全）
        worker.iteration_started.connect(
            lambda cur, total, wid=window_id: self._on_iteration_started(wid, cur, total), Qt.QueuedConnection
        )
        worker.iteration_completed.connect(
            lambda it, summary, wid=window_id: self._on_iteration_completed(wid, it, summary), Qt.QueuedConnection
        )
        worker.progress_updated.connect(lambda p, wid=window_id: self._on_progress(wid, p), Qt.QueuedConnection)
        worker.loop_completed.connect(lambda msg, wid=window_id: self._on_completed(wid, msg), Qt.QueuedConnection)
        worker.loop_error.connect(lambda msg, wid=window_id: self._on_error(wid, msg), Qt.QueuedConnection)
        worker.loop_stopped.connect(lambda wid=window_id: self._on_stopped(wid), Qt.QueuedConnection)
        worker.log_signal.connect(lambda text, wid=window_id: self._on_log(wid, text), Qt.QueuedConnection)
        worker.log_update.connect(lambda text, wid=window_id: self._on_log_update(wid, text), Qt.QueuedConnection)
        worker.phase_changed.connect(
            lambda phase, wid=window_id: self._on_phase_changed(wid, phase), Qt.QueuedConnection
        )
        worker.tokens_updated.connect(
            lambda tokens, wid=window_id: self._on_tokens_updated(wid, tokens), Qt.QueuedConnection
        )

        worker.start()

    def request_stop(self, window_id: str):
        """停止循环（非阻塞，清理经 loop_stopped 信号异步执行）"""
        session = self._sessions.get(window_id)
        if not session or not session.worker or not session.worker.isRunning():
            return
        session.worker.cancel()
        card = session.running_card or self._running_cards.get(window_id)
        if card:
            card.set_status("⏹ 正在停止...")
        # 兜底：5 秒未停强制收尾
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(5000, lambda wid=window_id: self._force_finish(wid))

    def request_archive(self, window_id: str):
        """用户点击归档 — 跳转归档阶段"""
        session = self._sessions.get(window_id)
        if not session or not session.worker or not session.worker.isRunning():
            return
        card = session.running_card or self._running_cards.get(window_id)
        if card:
            card.set_phase("archiving")
            card.set_status("📦 正在归档...")
            card.hide_archive_button()
            card.hide_stop_button()
        session.worker.request_archive()

    # ================================================================
    #  worker 信号槽（平移自 main_widget）
    # ================================================================

    def _card(self, window_id: str):
        session = self._sessions.get(window_id)
        if session and session.running_card is not None:
            return session.running_card
        return self._running_cards.get(window_id)

    def _on_phase_changed(self, window_id: str, phase: str):
        card = self._card(window_id)
        if card:
            card.set_phase(phase)

    def _on_iteration_started(self, window_id: str, current: int, total: int):
        session = self._sessions.get(window_id)
        card = self._card(window_id)
        if not card or not session or not session.worker:
            return
        progress = session.worker.get_current_progress()
        phase = progress.get("phase", "planning")
        current_step = progress.get("current_step", 0)
        total_steps = progress.get("total_steps", 0)
        if phase == "planning":
            card.set_phase("planning")
            card.set_status(f"📋 第 {current} 轮: 规划中...")
        else:
            card.set_phase("executing")
            if total_steps > 0:
                card.set_status(f"▶ 第 {current} 轮 / 共 {total} 轮 | 步骤 {current_step}/{total_steps}")
            else:
                card.set_status(f"▶ 第 {current} 轮 / 共 {total} 轮")

    def _on_iteration_completed(self, window_id: str, iteration: int, summary: str):
        card = self._card(window_id)
        if card:
            card.append_log(f"第 {iteration} 轮完成: {summary[:40]}")

    def _on_log(self, window_id: str, text: str):
        card = self._card(window_id)
        if card:
            card.append_log(text)

    def _on_log_update(self, window_id: str, text: str):
        card = self._card(window_id)
        if card:
            card.update_log(text)

    def _on_tokens_updated(self, window_id: str, total_tokens: int):
        card = self._card(window_id)
        if card:
            card.update_tokens(total_tokens)

    def _on_progress(self, window_id: str, progress: dict):
        card = self._card(window_id)
        if card:
            card.update_progress_no_token(progress)

    def _on_completed(self, window_id: str, message: str):
        card = self._card(window_id)
        if card:
            card.set_phase("completed")
            card.show_completed(message)
        self._finish(window_id, message)

    def _on_error(self, window_id: str, message: str):
        card = self._card(window_id)
        if card:
            card.show_error(message)
            card.append_log(f"❌ {message[:50]}")
        self._finish(window_id, f"❌ {message}")

    def _on_stopped(self, window_id: str):
        self._finish(window_id, "⏹ 已停止")

    # ================================================================
    #  收尾
    # ================================================================

    def _force_finish(self, window_id: str):
        session = self._sessions.get(window_id)
        if session and session.worker and session.worker.isRunning():
            logger.warning("[AutoLoop] Force cleanup after timeout")
            try:
                session.worker.wait(2000)
            except Exception:
                pass
        self._finish(window_id, "⏹ 强制停止（超时）")

    def _cancel_worker(self, session: _WindowSession):
        try:
            session.worker.cancel()
        except Exception:
            pass

    def _finish(self, window_id: str, message: str):
        """清理窗口会话（防重入）"""
        session = self._sessions.get(window_id)
        if session is None or session.finishing:
            return
        session.finishing = True
        services = session.services or {}

        # 恢复为当前项目配置的工作目录
        services.get("sync_working_directory", lambda: None)()

        card = session.running_card
        if card:
            try:
                card.stop_animation()
                card.hide()
            except RuntimeError, AttributeError:
                pass

        # 保存消息到当前会话
        worker = session.worker
        if worker is not None:
            try:
                messages = worker.get_all_messages()
                if messages:
                    services.get("save_messages_to_session", lambda *_: None)(messages, worker.get_task_prompt())
            except Exception as e:
                logger.warning(f"[AutoLoop] Failed to save messages to session: {e}")

        # 清理 worker
        if worker is not None:
            try:
                worker.quit()
                worker.wait(1000)
            except Exception:
                pass
            try:
                worker.deleteLater()
            except RuntimeError:
                pass
        session.worker = None

        # 解锁 UI
        services.get("exit_exclusive_ui_mode", lambda _s: None)(PLUGIN_ID)
        session.finishing = False

        # 通知用户
        services.get("notify", lambda *_: None)("AutoLoop", message)

    # ================================================================
    #  卸载 / 退出
    # ================================================================

    def shutdown_all(self):
        """插件卸载 / 应用退出：停掉全部窗口的循环"""
        for window_id in list(self._sessions.keys()):
            session = self._sessions.get(window_id)
            if session and session.worker and session.worker.isRunning():
                self._cancel_worker(session)
                try:
                    session.worker.wait(2000)
                except Exception:
                    pass
                self._finish(window_id, "⏹ 插件卸载")
            self._sessions.pop(window_id, None)
        self._running_cards.clear()
