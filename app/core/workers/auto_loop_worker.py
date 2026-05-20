# -*- coding: utf-8 -*-
"""
AutoLoop Worker — 后台循环工作线程

两阶段任务执行循环：
1. 规划阶段：拆解任务为 N 个步骤，写入 SHARED_TASK_NOTES.md
2. 执行阶段：按步骤执行，每步必须验证

每个迭代创建一个 OpenAIChatWorker，等待其完成后检测完成信号，
更新共享笔记，继续下一轮或停止。

阶段强制机制：
- 规划阶段：tools 仅允许 scan_repo/glob/grep 和写笔记（限制写代码）
- 执行阶段：允许所有工具，但每步必须验证通过才能前进
"""
import re
import time
from typing import Dict, List, Optional, Any, Callable

from PyQt5.QtCore import QThread, pyqtSignal
from loguru import logger

from app.core.auto_loop_config import AutoLoopConfig
from app.core.auto_loop_engine import AutoLoopEngine, LoopState
from app.core.auto_loop_prompt_composer import AutoLoopPromptComposer
from app.core.conversation.core import ConversationCore
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.adapters import AutoLoopConversationAdapter
from app.core.workers import OpenAIChatWorker


# ========== 规划阶段受限工具集 ==========
# PLANNING_TOOLS = {
#     # 扫描工具
#     "scan_repo", "glob", "grep", "list", "read", "websearch", "webfetch", ""
#     # 笔记写入工具
#     "write",
# }


class AutoLoopWorker(QThread):
    """AutoLoop 后台工作线程"""

    # === 进度信号 ===
    iteration_started = pyqtSignal(int, int)  # (current, max)
    iteration_completed = pyqtSignal(int, str)  # (iteration, summary)
    progress_updated = pyqtSignal(dict)  # progress dict
    loop_completed = pyqtSignal(str)  # 完成消息
    loop_error = pyqtSignal(str)  # 错误消息
    loop_stopped = pyqtSignal()  # 用户手动停止
    
    # === 阶段变更信号（用于运行卡 UI）===
    phase_changed = pyqtSignal(str)  # "planning" / "executing" / "completed"

    # === 迭代过程中的消息转发（用于日志显示）===
    log_signal = pyqtSignal(str)  # 日志消息

    # === Token 实时更新信号（直接更新运行卡 UI）===
    tokens_updated = pyqtSignal(int)  # 追加的 token 数量
    
    # === 消息日志列表信号（用于保存到会话）===
    messages_logged = pyqtSignal(list)  # 发送完整的消息日志列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self._config: Optional[AutoLoopConfig] = None
        self._model_config_getter: Optional[Callable[[], Dict]] = None
        self._tool_executor: Optional[Any] = None
        self._tools_schema: Optional[List[Dict]] = None
        self._all_tools_schema: Optional[List[Dict]] = None  # 保留完整工具集
        self._agent_system_prompt_getter: Optional[Callable[[str], str]] = None

        self._is_cancelled = False
        self._engine: Optional[AutoLoopEngine] = None
        self._prompt_composer: Optional[AutoLoopPromptComposer] = None

        # 执行阶段的步骤追踪
        self._last_step = 0  # 上次完成的步骤
        
        # 规划阶段标志：记录首次规划后的状态，防止模型一次性完成
        self._first_planning_done = False
        
        # 汇总的完整消息列表（从每个 ChatWorker 获取）
        self._all_messages: List[Dict] = []
        
        # Worker 同步事件（由 AutoLoopConversationAdapter 管理）

    def _configure_tools_for_phase(self, tools_schema: List[Dict]) -> List[Dict]:
        """根据当前阶段配置工具集"""
        return self._all_tools_schema or tools_schema

    def configure(
            self,
            config: AutoLoopConfig,
            model_config_getter: Callable[[], Dict],
            tool_executor: Any,
            tools_schema: List[Dict],
            agent_system_prompt_getter: Callable[[str], str],
            agent_manager: Any = None,
            permission_check_callback: Callable[[str, dict], str] = None,
            permission_cache: Any = None,
            compactor: Any = None,
    ):
        """配置 worker（应在 start() 前调用）"""
        self._config = config
        self._model_config_getter = model_config_getter
        self._tool_executor = tool_executor
        self._tools_schema = tools_schema
        self._all_tools_schema = tools_schema  # 保存完整工具集
        self._agent_system_prompt_getter = agent_system_prompt_getter
        self._permission_check_callback = permission_check_callback
        self._permission_cache = permission_cache
        self._compactor = compactor

        # ===== ConversationCore + AutoLoopConversationAdapter（统一执行基础设施）=====
        from app.core.conversation.executor import ConversationExecutor
        self._conversation_core = ConversationCore.create(
            get_model_config=model_config_getter,
            agent_manager=agent_manager,
            backend=None,
        )
        conv_config = ConversationConfig(
            permission_strategy=PermissionStrategy.AUTO_ALLOW,
        )
        self._conversation_executor = ConversationExecutor(
            core=self._conversation_core,
            config=conv_config,
            tool_executor=tool_executor,
            agent_manager=agent_manager,
        )
        self._adapter = AutoLoopConversationAdapter(
            core=self._conversation_core,
            executor=self._conversation_executor,
        )

    def cancel(self):
        """取消循环"""
        self._is_cancelled = True
        if self._conversation_executor:
            self._conversation_executor.stop()
        if self._engine:
            self._engine.stop()

    def run(self):
        """主循环 — 两阶段：规划 → 执行"""
        if not self._config or not self._config.task_prompt:
            self.loop_error.emit("未设置任务描述")
            return

        self._is_cancelled = False
        self._engine = AutoLoopEngine(self._config)
        self._prompt_composer = AutoLoopPromptComposer(self._engine)
        self._engine.start()
        self._last_step = 0
        self._first_planning_done = False
        
        # 清空消息列表
        self._all_messages = []
        
        # 发送阶段信号：规划中
        self.phase_changed.emit("planning")
        self.log_signal.emit("📋 进入规划阶段：拆解任务...")

        task_prompt = self._config.task_prompt

        for iteration in range(1, self._config.max_iterations + 1):
            if self._is_cancelled:
                break

            self._engine.iteration = iteration
            self.iteration_started.emit(iteration, self._config.max_iterations)
            self._emit_progress()
            
            # 给主线程时间处理信号并创建 assistant card
            time.sleep(0.2)

            # 构建本轮消息
            messages = self._build_messages(task_prompt, iteration)
            
            # 根据阶段获取对应的工具集
            current_tools = self._configure_tools_for_phase(self._all_tools_schema or self._tools_schema)

            # 创建并运行 worker（通过统一 Executor）
            try:
                self._adapter.reset()

                # 构建包装回调
                wrapped_callbacks = self._make_autoloop_callbacks()

                # 获取模型配置
                llm_config = self._model_config_getter() if self._model_config_getter else {}

                success = self._conversation_executor.execute(
                    messages=messages,
                    llm_config=llm_config,
                    tools=current_tools,
                    callbacks=wrapped_callbacks,
                )
                if not success:
                    self.log_signal.emit("⚠️ Worker 启动失败，重试...")
                    continue

                # 使用 QEventLoop 等待 Worker 完成（Qt 信号需要事件循环才能跨线程投递）
                from PyQt5.QtCore import QEventLoop
                worker = self._conversation_executor.get_current_worker()
                if worker:
                    loop = QEventLoop()
                    worker.finished.connect(loop.quit)
                    loop.exec_()  # 阻塞直到 worker 线程结束
                else:
                    # fallback: 等待 adapter 的事件
                    self._adapter.wait_for_completion(timeout=300)

                response = self._adapter.get_response() or ""
                # 估算 token 用量
                token_usage = max(1, len(response) // 4)
                self._engine.add_tokens(token_usage)
                self._emit_progress()
                    
                # 【新增】强制检查接力文档更新
                if not self._check_relay_doc_updated(iteration):
                    # 未更新接力文档，强制要求更新后才能继续
                    self.log_signal.emit("⚠️【强制】接力文档未更新！正在要求更新...")
                    
                    # 重新构建消息，注入强制更新提示
                    force_messages = self._build_messages(task_prompt, iteration, force_update=True)
                    
                    # 使用统一 Executor 执行强制更新
                    self._adapter.reset()
                    self._conversation_executor.execute(
                        messages=force_messages,
                        llm_config=llm_config,
                        tools=current_tools,
                        callbacks=self._make_autoloop_callbacks(),
                    )
                    from PyQt5.QtCore import QEventLoop
                    worker = self._conversation_executor.get_current_worker()
                    if worker:
                        loop = QEventLoop()
                        worker.finished.connect(loop.quit)
                        loop.exec_()
                    
                    # 再次检查接力文档
                    if self._check_relay_doc_updated(iteration):
                        self.log_signal.emit("✅ 接力文档已更新，继续执行...")
                    else:
                        self.log_signal.emit("⚠️ 接力文档仍未更新，将继续强制要求")
                        # 允许继续（避免死循环），但会在下一轮继续检查
                    
                    self._emit_progress()
                    continue
            except Exception as e:
                logger.error(f"[AutoLoop] Worker error on iteration {iteration}: {e}")
                self.loop_error.emit(f"第{iteration}轮出错: {str(e)}")
                self._engine.increment_consecutive_failures()
                if self._engine.consecutive_failures >= 3:
                    self.loop_error.emit("连续失败 3 次，已停止")
                    return
                self._emit_progress()
                continue

            # 生成摘要
            summary = self._extract_summary(response, iteration)
            self.iteration_completed.emit(iteration, summary)

            # 写入本轮完整日志到独立文件
            timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            log_content = f"""# AutoLoop 轮次 {iteration} 日志

- 时间: {timestamp_str}
- 阶段: {'PLANNING' if self._engine.is_planning_phase() else 'EXECUTING'}
- 当前步骤: {self._engine.current_step} / {self._engine.total_steps}

## 完整响应

{response}
"""
            self._engine.write_round_log(iteration, log_content)

            # ===== 阶段处理 =====
            
            if self._engine.is_planning_phase():
                # --- 规划阶段 ---
                notes = self._engine.read_shared_notes()
                
                # 检测规划是否完成
                planning_done = self._engine.check_planning_complete(response, notes)
                
                if planning_done:
                    self._first_planning_done = True
                    self._engine.enter_execution_phase()
                    current, max_verified, total = self._engine.parse_current_and_next_step(notes)
                    # 从笔记同步已勾选完成的步骤到缓存
                    self._engine.sync_verified_steps_from_notes(notes)
                    self._engine.set_step_progress(current, total)
                    self.log_signal.emit(f"✅ 规划完成！共 {total} 个步骤，{max_verified} 已完成")
                    self.log_signal.emit(f"📋 开始执行步骤 {current}/{total}: {self._get_next_step_preview(notes, current)}")
                    
                    # 发送阶段信号：执行中
                    self.phase_changed.emit("executing")
                    self._emit_progress()
                    continue
                else:
                    # 规划未完成，继续规划
                    self._engine.on_planning_attempt()
                    
                    # 检查：是否写了笔记但没输出 PLANNING_COMPLETE
                    if notes and "## 执行计划" in notes and "PLANNING_COMPLETE" not in response.upper():
                        # 模型写了计划但忘记输出信号，提醒它
                        self.log_signal.emit("📋 检测到已写入计划，请在回复末尾添加 PLANNING_COMPLETE")
                    
                    self.log_signal.emit("📋 继续规划...")
                    self._emit_progress()
                    continue
                    
            else:
                # --- 执行阶段 ---
                notes = self._engine.read_shared_notes()
                
                # 每次执行前从笔记同步已验证步骤
                self._engine.sync_verified_steps_from_notes(notes)
                
                # 解析当前步骤：下一个要执行的、已完成的、总步骤数
                current_step, max_verified, total_steps = self._engine.parse_current_and_next_step(notes)
                
                if total_steps > 0:
                    self._engine.set_step_progress(
                        current_step if (self._engine.current_step == 0 or self._engine.current_step <= max_verified) else self._engine.current_step,
                        total_steps,
                    )
                    
                    # 检测当前步骤是否已完成
                    step_completed = self._check_step_completed(response, notes, self._engine.current_step)
                    
                    if step_completed:
                        self._last_step = self._engine.current_step
                        self.log_signal.emit(f"✓ 步骤 {self._engine.current_step}/{total_steps} 完成")
                        
                        if self._engine.current_step >= total_steps:
                            # 所有步骤完成，输出 DONE
                            self.log_signal.emit("🎉 所有步骤完成！任务结束")
                            self.phase_changed.emit("completed")
                            self.loop_completed.emit("所有计划步骤已完成！🎉")
                            return
                        else:
                            # 前进到下一步
                            self._engine.advance_to_step(self._engine.current_step + 1)
                            self.log_signal.emit(f"📋 执行步骤 {self._engine.current_step}/{total_steps}: {self._get_next_step_preview(notes, self._engine.current_step)}")
                    else:
                        # 步骤未完成，可能需要继续执行或验证
                        # 检查是否有验证失败的情况
                        if "验证失败" in response or "failed" in response.lower():
                            self.log_signal.emit("⚠️ 检测到验证失败，模型应修复后重试")
                
                # 检查完成信号（可能在响应中直接输出 DONE）
                if self._engine.check_completion(response):
                    self.phase_changed.emit("completed")
                    self.loop_completed.emit("任务完成 — 检测到完成信号！🎉")
                    return

            # 检查预算
            budget_reason = self._engine.check_budget()
            if budget_reason:
                self.loop_completed.emit(f"已停止 — {budget_reason}")
                return

            self._emit_progress()

        # 达到最大迭代次数
        if not self._is_cancelled:
            self._engine.state = LoopState.COMPLETED
            self.loop_completed.emit(f"达到最大迭代次数 ({self._config.max_iterations})，已停止")

    # ========== 内部辅助方法（委托给 Engine）==========

    def _check_step_completed(self, response: str, notes: str, step_num: int) -> bool:
        """检测当前步骤是否完成（委托给 Engine）"""
        return self._engine.check_step_completed(response, notes, step_num) if self._engine else False

    def _get_next_step_preview(self, notes: str, step_num: int) -> str:
        """获取下一步骤的预览文本（委托给 Engine）"""
        return self._engine.get_next_step_preview(notes, step_num) if self._engine else f"步骤 {step_num}"

    def _check_relay_doc_updated(self, iteration: int) -> bool:
        """检查接力文档是否已更新（委托给 Engine）"""
        return self._engine.check_relay_doc_updated(iteration) if self._engine else False

    # ========== 消息构建（委托给 PromptComposer）==========

    def _build_messages(self, task_prompt: str, iteration: int, force_update: bool = False) -> List[Dict]:
        """构建本轮对话消息（委托给 PromptComposer）"""
        system_prompt = self._agent_system_prompt_getter("auto_loop") if self._agent_system_prompt_getter else ""
        project_path = self._config.project_path or ""

        if self._prompt_composer:
            return self._prompt_composer.build_messages(
                task_prompt=task_prompt,
                iteration=iteration,
                system_prompt=system_prompt,
                project_path=project_path,
                force_update=force_update,
            )

        # fallback：无 PromptComposer 时使用最简单的消息
        messages = [{"role": "system", "content": system_prompt}]
        messages.append({"role": "user", "content": task_prompt})
        return messages

    def _make_autoloop_callbacks(self) -> Dict[str, Callable]:
        """构建 AutoLoop 的回调包装（日志转发 + 预算检查 + token 追踪）"""
        from app.core.token_estimator import count_messages_tokens

        # 以 Adapter 的回调为基础（包含 finished → 设置 _worker_done_event，作为 QEventLoop 的 fallback）
        callbacks = dict(self._adapter.get_callbacks())

        # 日志转发（覆盖 adapter 的 content_received no-op）
        callbacks["content_received"] = lambda p: self.log_signal.emit(f"生成内容...")
        callbacks["reasoning_content_received"] = lambda p: self.log_signal.emit(f"思考中...")
        callbacks["thinking_started"] = lambda: self.log_signal.emit(f"开始推理")
        callbacks["tool_call_started"] = lambda i, n, a, r: self.log_signal.emit(f"调用工具: {n}")
        callbacks["tool_result_received"] = lambda i, n, a, r: self.log_signal.emit(f"工具完成: {n}")

        # Token 追踪 + 预算检查（在 messages_updated 回调中）
        def on_messages_updated(messages: list):
            # 注意：messages_updated 每轮都发送完整会话消息（含历史），
            # 直接 append 会导致 user 消息重复累积（多轮工具调用时）。
            # 所以这里只做 token 追踪，不追加到 _all_messages。
            # 保存时直接从 ConversationCore 的 SessionManager 读取当前会话消息。
            if self._engine and messages:
                token_count = count_messages_tokens(messages)
                self._engine.add_tokens(token_count)
                self.tokens_updated.emit(self._engine.total_tokens)
                reason = self._engine.check_budget()
                if reason:
                    self.log_signal.emit(f"⚠️ {reason}，正在停止...")
                    self._is_cancelled = True
                    self._conversation_executor.stop()

        callbacks["messages_updated"] = on_messages_updated

        # 错误日志
        orig_error = callbacks.get("error")
        if orig_error:
            def on_error_wrapper(e):
                self.log_signal.emit(f"错误: {e}")
                orig_error(e)
            callbacks["error"] = on_error_wrapper

        return callbacks

    def _extract_summary(self, response: str, iteration: int) -> str:
        """从响应中提取摘要"""
        lines = response.strip().split("\n")
        # 取前 3 行作为摘要
        summary_lines = [l for l in lines if l.strip() and not l.startswith("```")][:3]
        return " | ".join(summary_lines) if summary_lines else f"第{iteration}轮完成"

    def _emit_progress(self):
        """发射进度信号"""
        if self._engine:
            self.progress_updated.emit(self._engine.get_progress())

    def get_all_messages(self) -> List[Dict]:
        """获取所有消息（用于保存到会话）"""
        return self._all_messages.copy()

    # ========== 公共接口（供 main_widget 等外部调用）==========

    def get_current_progress(self) -> dict:
        """获取当前进度信息（替代外部穿透访问 _engine 私有属性）
        
        Returns:
            dict with keys: iteration, max_iterations, current_step, total_steps,
                            total_tokens, phase, state
        """
        if not self._engine:
            return {
                "iteration": 0, "max_iterations": 0,
                "current_step": 0, "total_steps": 0,
                "total_tokens": 0, "phase": "idle", "state": "idle",
            }
        progress = self._engine.get_progress()
        return progress

    def get_task_prompt(self) -> str:
        """获取任务提示（替代外部穿透访问 _config）"""
        return self._config.task_prompt if self._config else ""
