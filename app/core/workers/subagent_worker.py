# -*- coding: utf-8 -*-
"""
子智能体执行器 - 独立运行子智能体任务，避免共享超长上下文
"""

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import orjson
import orjson as json
from loguru import logger
from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal

from app.constants import PARAM_SCHEMA
from app.constants import provider_quota_exclude_keys as QUOTA_EXCLUDE_KEYS
from app.core.message_content import messages_to_responses_input, to_api_message
from app.core.model_capabilities import (
    get_model_capabilities,
    normalize_reasoning_effort,
    resolve_context_limit,
    resolve_max_output_tokens,
)
from app.core.provider_profile import detect_provider_family, get_provider_profile
from app.core.tool_call_parser import smart_parse_arguments
from app.plugins.contracts.loop_policy import LoopDecision, LoopState
from app.tools.result import ToolResult

# ========== 性能优化：预编译正则表达式 ==========
_THINKING_PATTERN = re.compile(r"<think>[\s\S]*?</think>")  # 过滤完整思考块
_TOOL_TAG_PATTERN = re.compile(r"<tool>[\s\S]*?</tool>")  # 过滤工具调用标签
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")  # 验证标识符格式

# ========== 上下文注入预算常量 ==========
CHARS_PER_TOKEN = 4  # 与 HistoryCompactor 保持一致
_CONTEXT_INJECTION_RATIO = 0.6  # 上下文注入最多占 budget 的 60%


class _BoundedTaskDict(dict):
    """有界字典（H1）：超过容量上限时弹出最旧条目，防止 _finished_tasks 无限增长。

    重写 __setitem__，使全部 12+ 处 `self._finished_tasks[task_id] = {...}` 写入点
    自动受容量约束，无需逐个改写。读取（get/items/in/setdefault-on-value）行为不变。
    """

    def __init__(self, maxlen: int = 200):
        super().__init__()
        self._maxlen = maxlen

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        while len(self) > self._maxlen:
            oldest = next(iter(self))  # dict 保序：首个即最旧
            super().__delitem__(oldest)


# 最终总结提示词兕底文案（激活策略无 final_summary_prompt 时使用，内容与原硬编码等价）
_FALLBACK_FINAL_SUMMARY_PROMPT = """

## 已达到最大迭代次数限制，请总结当前执行结果。

请整理并返回以下内容：
1. 已完成的工作
2. 关键发现和结论
3. 重要文件路径和数据
4. 待解决的问题（如有）

直接输出总结内容："""


def _compute_context_budget(llm_config: Dict) -> int:
    """
    计算当前模型的上下文 token 预算。
    逻辑与 HistoryCompactor.get_budget() 保持一致。

    Args:
        llm_config: 模型配置字典

    Returns:
        可用于历史的 token 预算
    """
    if not isinstance(llm_config, dict):
        logger.warning(
            f"[SubAgentExecutor] _compute_context_budget received non-dict llm_config: {type(llm_config).__name__}, using defaults"
        )
        return 96000  # 默认 128k * 0.75

    context_limit = resolve_context_limit(llm_config)
    max_output_tokens = resolve_max_output_tokens(llm_config)

    # O1/O3 模型需要更大的输出预留
    model_name = str(llm_config.get("模型名称", "") or llm_config.get("model", "")).lower()
    reserved = min(800, max_output_tokens)
    if "o1" in model_name or "o3" in model_name:
        reserved = min(max_output_tokens, 32000)

    return max(500, context_limit - reserved)


class SubAgentExecutor(QThread):
    """子智能体执行器 - 独立线程运行子智能体任务"""

    finished_with_result = Signal(str, str)  # task_id, result
    error_occurred = Signal(str, str)  # task_id, error
    progress_updated = Signal(str, str)  # task_id, message
    tool_call_started = Signal(str, str, dict)  # task_id, tool_name, args
    tool_result_received = Signal(str, str, str, bool)  # task_id, tool_name, result, success
    token_usage_updated = Signal(str, int, int, int)  # task_id, prompt_tokens, completion_tokens, total_tokens
    thinking_received = Signal(str, str)  # task_id, reasoning_content
    # ★ T24：ask 行为权限请求（task_id, tool_name, arguments）→ 主线程弹窗
    permission_requested = Signal(str, str, dict)

    def __init__(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        llm_config: Dict,
        agent_manager: Any,
        tool_executor: Any = None,
        parent_context: str = "",
        is_subagent_call: bool = True,  # 标记是否为被主智能体调用（通过 subagent_para）
        max_iterations: Optional[
            int
        ] = None,  # 轮数上限（per-agent steps 优先）；None=走激活策略（默认 subagent 策略 30）
        hook_policy_id: Optional[str] = None,  # 子智能体域 hook 策略插件 id（plugins/system/hook_policies/）
    ):
        super().__init__()
        self.task_id = task_id
        self.agent_name = agent_name
        self.task_description = task_description
        self.llm_config = llm_config
        self.agent_manager = agent_manager
        self.tool_executor = tool_executor
        self.parent_context = parent_context
        self.is_subagent_call = is_subagent_call  # 传递给提示词构建
        self.max_iterations = max_iterations  # 轮数上限（None=走激活策略）
        # 子智能体域 hook 策略：默认 None → 走 plugins/system/hook_policies/ 的
        # "subagent_default"（仅工具级 + Stop + PluginChanged）。可显式传 id 覆盖。
        self._hook_policy_id = hook_policy_id
        self._hook_policy_obj = None  # 懒解析缓存
        self._is_cancelled = False
        self._pending_answer = None
        self._last_result = None
        self._execution_error = None
        # ★ T24：ask 权限等待（主线程弹窗响应后 set event，超时按拒绝）
        self._permission_event = threading.Event()
        self._permission_allow = False
        self._permission_answered = False
        self._start_time: Optional[float] = None  # Unix timestamp, 访问用 @property
        self._last_activity_time: float = time.time()  # 最后活跃时间戳（日志/API/工具），供外部 stall 检测
        # 日志存储: [{"type": "progress"|"thinking"|"ai_response"|"tool_call"|"tool_result"|"finish", "content": str, "timestamp": float}]
        self._logs: List[Dict] = []
        self._tool_call_count = 0
        self._log_store_callback = None  # 日志存储回调
        self._get_history_messages = None  # 获取主智能体历史消息的回调
        # Token 用量追踪
        self._total_prompt_tokens: int = 0  # 累计 prompt tokens
        self._total_completion_tokens: int = 0  # 累计 completion tokens
        self._total_tokens: int = 0  # 累计总 tokens（用于计费统计）
        self._peak_total_tokens: int = 0  # 单次 API 调用的峰值总 tokens（反映上下文窗口压力）

    @property
    def total_tokens(self) -> int:
        """获取累计 token 总数（供 UI 显示）"""
        return self._total_tokens

    @property
    def start_time(self) -> Optional[float]:
        """获取任务开始时间戳（供 SubAgentManager 超时检测使用）"""
        return self._start_time

    @property
    def tool_call_count(self) -> int:
        """获取工具调用次数（供 SubAgentManager 使用）"""
        return self._tool_call_count

    @property
    def last_result(self) -> Optional[str]:
        """获取最终结果（供 SubAgentManager 使用）"""
        return self._last_result

    @property
    def execution_error(self) -> Optional[str]:
        """获取执行错误（供 SubAgentManager 使用）"""
        return self._execution_error

    def get_last_activity_time(self) -> float:
        """获取最后活跃时间戳（供 SubAgentManager stall 检测使用）"""
        return self._last_activity_time

    def set_log_store_callback(self, callback):
        """设置日志存储回调"""
        self._log_store_callback = callback

    def set_history_getter(self, getter: callable):
        """
        设置获取历史消息的回调。

        Args:
            getter: callable, 返回 List[Dict]，每个 dict 包含 role/content
        """
        self._get_history_messages = getter

    def cancel(self):
        self._is_cancelled = True

    def provide_answer(self, answer: str):
        self._pending_answer = answer

    def _build_inherited_context(self, agent, history_messages: List[Dict]) -> List[Dict]:
        """
        基于 context budget 构建主智能体历史上下文注入。

        策略：
        1. 获取模型 context budget（token 数）
        2. 按比例计算可用 token
        3. 从最新消息向前填充，保持每条消息完整（不截断内容）
        4. 将消息作为原始 message 对象返回（保留 role/content），
           以支持 API provider 的 prompt caching
        5. 超出 budget 时整条消息丢弃（不截断内容）

        Args:
            agent: Agent 配置对象
            history_messages: 主智能体的历史消息列表

        Returns:
            保留原始格式的消息列表（List[Dict]），无内容时返回空列表
        """
        if not history_messages:
            return []

        # 获取预算比例（优先从 agent 配置读取）
        budget_ratio = (
            getattr(agent, "inherit_history_budget_ratio", _CONTEXT_INJECTION_RATIO) or _CONTEXT_INJECTION_RATIO
        )
        # 确保比例在合理范围内
        budget_ratio = max(0.1, min(0.8, float(budget_ratio)))

        # 计算可用 token 预算
        budget_tokens = _compute_context_budget(self.llm_config)
        max_context_tokens = int(budget_tokens * budget_ratio)
        if max_context_tokens <= 0:
            return []

        # 从最新消息向前填充，保持消息完整
        selected_messages = []
        remaining_tokens = max_context_tokens

        for msg in reversed(history_messages):
            role = msg.get("role", "user")
            # 跳过 tool 角色消息（工具结果很长且与 assistant 消息隐含关联）
            if role == "tool":
                continue

            # 预估消息的 token 数
            content = msg.get("content", "")
            if isinstance(content, list):
                # multimodal 内容（图文混合），只从 text 部分估算
                content_text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            elif isinstance(content, str):
                content_text = content
            else:
                content_text = str(content) if content else ""

            # 无内容的消息跳过
            if not content_text and not isinstance(content, list):
                continue

            # token 数估算（1 token ≈ 4 字符，+4 角色开销）
            msg_tokens = max(1, len(content_text) // CHARS_PER_TOKEN) + 4

            if msg_tokens <= remaining_tokens:
                # 完整消息放得下，保留原始格式
                clean_msg = {"role": role}
                if isinstance(content, list):
                    clean_msg["content"] = content  # 保留 multimodal 原始格式
                elif isinstance(content, str):
                    clean_msg["content"] = content
                else:
                    clean_msg["content"] = str(content) if content else ""
                selected_messages.append(clean_msg)
                remaining_tokens -= msg_tokens
            else:
                # 放不下则停止（不截断内容）
                break

        # 翻转回正序
        selected_messages.reverse()
        return selected_messages

    def _add_log(self, log_type: str, content: str, extra: dict = None):
        """记录日志"""
        import time

        now = time.time()
        log_entry = {
            "type": log_type,
            "content": content,
            "timestamp": now,
        }
        if extra:
            log_entry.update(extra)
        self._logs.append(log_entry)
        # 每次输出日志都刷新活跃时间（供外部 stall 检测）
        self._last_activity_time = now
        # 实时保存到数据库
        log_callback = getattr(self, "_log_store_callback", None)
        if log_callback:
            try:
                log_callback(
                    self.task_id,
                    self.agent_name,
                    self.task_description,
                    "running",
                    None,
                    None,
                    self._logs,
                    self.get_summary(),
                )
            except Exception as e:
                logger.warning(f"[SubAgentExecutor] 实时保存日志失败: {e}")

    def get_logs(self) -> List[Dict]:
        """获取所有日志"""
        return self._logs.copy()

    def get_summary(self) -> dict:
        """获取任务摘要"""
        import time

        elapsed = int(time.time() - self._start_time) if self._start_time else 0
        # 从 llm_config 提取模型名称
        _model = ""
        if isinstance(self.llm_config, dict):
            _model = str(self.llm_config.get("模型名称", "") or self.llm_config.get("model", "") or "")
        # token 显示（使用单次 API 调用的峰值，反映上下文窗口压力而非累计计费）
        _ctx_display = ""
        if self._peak_total_tokens > 0:
            t = self._peak_total_tokens
            _ctx_display = f"{t / 1000:.1f}K tokens" if t >= 1000 else f"{t} tokens"
        return {
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "task_description": self.task_description,
            "tool_call_count": self._tool_call_count,
            "elapsed_seconds": elapsed,
            "result": self._last_result,
            "error": self._execution_error,
            "model_name": _model,
            "context_usage": _ctx_display,
            "total_tokens": self._peak_total_tokens,
        }

    def run(self):
        import time

        self._start_time = time.time()

        try:
            # 防御：确保 llm_config 是 dict
            if not isinstance(self.llm_config, dict):
                logger.warning(
                    f"[SubAgentExecutor] run() llm_config is not a dict: {type(self.llm_config).__name__}={self.llm_config!r}"
                )
                self.llm_config = {}

            agent = self.agent_manager.get_agent(self.agent_name)
            if not agent:
                self.error_occurred.emit(self.task_id, f"Agent not found: {self.agent_name}")
                return

            # 子智能体被调用时 is_subagent_call=True，此时应该使用 subagent_constraints
            # 用于区分"主智能体通过 subagent_para 调用子智能体"的情况
            # 【修复】补 extra_context（project_root/project_name），
            # 让 read_project_notes / inject_skills_content 能正确注入项目笔记和技能配置
            _sub_workdir = ""
            _sub_project_name = ""
            try:
                if self.tool_executor and hasattr(self.tool_executor, "get_workdir"):
                    _sub_workdir = self.tool_executor.get_workdir() or ""
            except Exception:
                pass
            try:
                from app.utils.config import Settings

                _sub_project_name = Settings.get_instance().llm_project_name.value or "DriFoxx"
            except Exception:
                pass
            system_prompt = self.agent_manager.get_agent_system_prompt(
                self.agent_name,
                is_subagent_call=self.is_subagent_call,
                extra_context={
                    "project_root": _sub_workdir,
                    "project_name": _sub_project_name,
                },
            )
            # 传入当前窗口的 builtin_tools 实例，确保 is_in_team 检查使用正确窗口的 team_window_id
            _bt = self.tool_executor._builtin_tools if self.tool_executor else None
            tools = self.agent_manager.get_agent_tools_schema(
                self.agent_name, is_subagent_call=self.is_subagent_call, builtin_tools=_bt
            )
            # ★ T24（按产品指示修订）：**不做** controller schema 过滤——
            # 工具定义保持静态完整（prompt 缓存稳定），禁用/询问全部由
            # 执行层 _check_ui_tool_permission 实时控制（UI 调整立即生效）。

            # 基于 context budget 构建主智能体历史上下文注入（返回消息对象列表）
            inherited_messages = []
            if agent.inherit_history and getattr(self, "_get_history_messages", None):
                try:
                    history_messages = self._get_history_messages() or []
                    if history_messages:
                        # 根据 inherit_history_count 限制消息数量
                        if agent.inherit_history_count is not None:
                            history_messages = history_messages[-agent.inherit_history_count :]

                        inherited_messages = self._build_inherited_context(agent, history_messages)
                except Exception as e:
                    logger.warning(f"[SubAgentExecutor] 获取历史消息失败: {e}")

            # 过滤父智能体上下文中的 <tool> 和 <think> 标签，避免污染子智能体的工具调用格式
            sanitized_context = _TOOL_TAG_PATTERN.sub("", self.parent_context)
            sanitized_context = _THINKING_PATTERN.sub("", sanitized_context)

            # 构建子智能体消息列表（4层结构，实现 prompt caching）
            messages = []
            # Layer 1: 继承的主智能体消息（原始 message 格式，完整保留 role/content）
            #          相同前缀可被 API provider 缓存，提升后续调用性能
            messages.extend(inherited_messages)
            # Layer 2: 父智能体说明
            messages.append({"role": "system", "content": f"## 父智能体说明\n{sanitized_context}"})
            # Layer 3: 子智能体提示词（作为最后一条 system 消息，权重最高）
            messages.append({"role": "system", "content": system_prompt})
            # Layer 4: 子任务
            messages.append({"role": "user", "content": f"## 子任务\n{self.task_description}"})

            self._add_log("progress", f"开始执行子任务: {self.agent_name}")
            self.progress_updated.emit(self.task_id, f"开始执行子任务: {self.agent_name}")

            try:
                result = self._execute_agent_loop(messages, tools, self.llm_config)
            except Exception as e:
                logger.error(f"[SubAgentExecutor] _execute_agent_loop error: {e}")
                result = f"执行出错: {str(e)}"

            if self._is_cancelled:
                # 【关键修复】被取消时也要发射错误信号，让 DAG 知道节点结束了
                # 不然 DAG 会永远卡在等这个节点的完成回调上
                self._execution_error = "Task cancelled"
                logger.warning(
                    f"[SubAgentExecutor] Task {self.task_id} ({self.agent_name}) cancelled, emitting error_occurred to notify DAG"
                )
                if self._log_store_callback:
                    try:
                        self._log_store_callback(
                            self.task_id,
                            self.agent_name,
                            self.task_description,
                            "cancelled",
                            "",
                            "Task cancelled",
                            self._logs,
                            self.get_summary(),
                        )
                    except Exception as e:
                        logger.warning(f"[SubAgentExecutor] 保存取消状态失败: {e}")
                self.error_occurred.emit(self.task_id, "Task cancelled")
                return

            # 直接使用执行结果（迭代结束时已自动总结）
            self._last_result = result if result else "无执行结果"

            # 任务完成，保存最终状态
            if self._log_store_callback:
                try:
                    self._log_store_callback(
                        self.task_id,
                        self.agent_name,
                        self.task_description,
                        "finished",
                        self._last_result,
                        None,
                        self._logs,
                        self.get_summary(),
                    )
                except Exception as e:
                    logger.warning(f"[SubAgentExecutor] 保存完成状态失败: {e}")

            self.finished_with_result.emit(self.task_id, self._last_result)

        except Exception as e:
            logger.exception(f"[SubAgentExecutor] run() error: {e}")
            self._execution_error = str(e)
            # 任务出错，保存错误状态
            if self._log_store_callback:
                try:
                    self._log_store_callback(
                        self.task_id,
                        self.agent_name,
                        self.task_description,
                        "error",
                        None,
                        self._execution_error,
                        self._logs,
                        self.get_summary(),
                    )
                except Exception as e:
                    logger.warning(f"[SubAgentExecutor] 保存错误状态失败: {e}")
            self.error_occurred.emit(self.task_id, f"SubAgent execution error: {str(e)}")

    def _execute_agent_loop(self, messages: List[Dict], tools: List[Dict], llm_config: Dict) -> str:
        """执行子智能体对话循环"""
        current_messages = messages.copy()
        response_content = ""
        current_reasoning = ""  # DeepSeek V4 thinking mode
        iteration_count = 0
        # LoopPolicy：轮数上限（per-agent steps 优先，激活策略兜底，默认 30）
        round_limit = self._resolve_round_limit()

        while not self._is_cancelled:
            if self._is_cancelled:
                return ""

            # 每次迭代开始前刷新活跃时间（即将调 API，算作活跃）
            self._last_activity_time = time.time()

            # LoopPolicy：轮数上限判定 + 策略门控（默认 subagent 策略到限即停，
            # 插件策略可改判 CONTINUE 放行进入正常轮次）
            iteration_count += 1
            if round_limit is not None and iteration_count > round_limit:
                _lp_state = LoopState(round_count=iteration_count)
                if self._loop_policy().should_continue(_lp_state) is not LoopDecision.CONTINUE:
                    self._add_log("progress", f"已达到最大迭代次数 ({round_limit})，强制结束并总结")
                    final_summary_prompt = self._final_summary_prompt()
                    current_messages.append({"role": "user", "content": final_summary_prompt})
                    # 强制续命前也触发 PreAssistantMessage（与正常轮次一致）
                    self._trigger_hook_sync("PreAssistantMessage", current_messages)
                    response_content, _, _ = self._make_api_call(current_messages, None, llm_config)
                    return self._filter_thinking_content(response_content)

            # ====== PreAssistantMessage hook：每次 API 调用前触发 ======
            # 同步触发，hook 输出追加到 current_messages，让 LLM 在下一轮请求中看到
            self._trigger_hook_sync("PreAssistantMessage", current_messages)

            response_content, tool_calls, reasoning_content = self._make_api_call(current_messages, tools, llm_config)
            current_reasoning = reasoning_content

            # ====== PostAssistantMessage hook：assistant 响应后触发 ======
            # 注入 response_content（_make_api_call 拿到的纯文本）作为上下文，
            # 让 hook 能基于最近一次回复做检查（如敏感词、协议格式等）
            self._trigger_hook_sync(
                "PostAssistantMessage",
                current_messages,
                extra_context={"assistant_response": response_content or ""},
            )

            # 记录大模型生成内容（thinking + ai_response），排除工具调用结果
            if current_reasoning:
                self._add_log("thinking", current_reasoning)
                # 实时推送到 UI（浮动卡片可即时显示思考内容）
                self.thinking_received.emit(self.task_id, current_reasoning)
            if response_content:
                self._add_log("ai_response", response_content)

            if self._is_cancelled:
                return ""

            # 没有工具调用，直接返回结果（提前有结果了）
            if not tool_calls:
                # 【修复】将 reasoning_content 以 <think> 标签嵌入结果文本，
                # 前端 _inject_think_cards 会自动渲染为可折叠思考块
                if current_reasoning:
                    return f"<think>{current_reasoning}</think>\n\n{response_content}"
                return response_content

            # DeepSeek V4 thinking mode: 需要传递 reasoning_content
            assistant_msg = {
                "role": "assistant",
                "content": response_content,
                "tool_calls": tool_calls,
            }
            if current_reasoning:
                assistant_msg["reasoning_content"] = current_reasoning
            elif self._requires_reasoning_content(llm_config):
                # 上游要求 thinking mode 下 tool_calls assistant 必须带 reasoning_content 字段（可为空串）
                assistant_msg["reasoning_content"] = ""
            current_messages.append(assistant_msg)

            tool_results, hook_messages = self._execute_tools(tool_calls)

            if tool_results is None:
                while self._pending_answer is None and not self._is_cancelled:
                    time.sleep(0.1)

                if self._is_cancelled:
                    return ""

                current_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": self._question_pending["tool_call_id"],
                        "content": self._pending_answer,
                    }
                )
                self._pending_answer = None
                continue

            # 先 extend tool_results（纯 role="tool" 消息，紧跟在 assistant(tool_calls) 之后），
            # 再 extend hook_messages（role="user" 消息，避免插入 tool_calls 和 tool 之间导致 2013 错误）
            current_messages.extend(tool_results)
            current_messages.extend(hook_messages)
            QCoreApplication.processEvents()
            time.sleep(0.2)

        # 【修复】将 reasoning_content 以 <think> 标签嵌入结果文本，
        # 前端 _inject_think_cards 会自动渲染为可折叠思考块
        if current_reasoning:
            return f"<think>{current_reasoning}</think>\n\n{response_content}"
        return response_content

    # ===== LoopPolicy 接入（scope="subagent"）=====

    _loop_policy_obj = None  # 懒解析缓存（激活策略）

    def _loop_policy(self):
        """当前激活的子智能体循环策略（scope=subagent，默认 subagent 策略兜底）"""
        if self._loop_policy_obj is None:
            from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

            self._loop_policy_obj = LoopPolicyRegistry.get_instance().get_active("subagent")
        return self._loop_policy_obj

    # ===== HookPolicy 接入（scope="subagent"）=====

    def _hook_policy_obj_resolve(self):
        """当前激活的子智能体 hook 触发策略对象

        优先级：_hook_policy_id 显式 id > 默认 scope=subagent 的激活策略
        （默认 plugins/system/hook_policies/subagent_default.py，仅工具级 + Stop +
        PluginChanged）。Registry 未加载时回退到内置 SubagentDefaultHookPolicy（保持
        现状行为：仅工具级 + Stop + PluginChanged）。
        """
        if self._hook_policy_obj is not None:
            return self._hook_policy_obj
        try:
            from app.plugins.registries.hook_policy_registry import HookPolicyRegistry
            from app.plugins.contracts.hook_policy import SCOPE_SUBAGENT

            registry = HookPolicyRegistry.get_instance()
            self._hook_policy_obj = registry.get_active(SCOPE_SUBAGENT)
            if self._hook_policy_id and self._hook_policy_obj.id != self._hook_policy_id:
                if registry.set_active(self._hook_policy_id, SCOPE_SUBAGENT):
                    self._hook_policy_obj = registry.get_active(SCOPE_SUBAGENT)
        except Exception as exc:
            logger.warning(f"[SubAgent] HookPolicy resolve 异常，回退内置默认: {exc!r}")
            from app.plugins.contracts.hook_policy import (
                HookDecision,
                HookEvent,
                PluginChangedEvent,
                PostToolUseEvent,
                PreToolUseEvent,
                StopEvent,
            )

            class _FallbackSubagent:
                id = "subagent_default"
                scope = "subagent"

                def should_trigger(self, event: HookEvent) -> HookDecision:
                    if isinstance(event, (PreToolUseEvent, PostToolUseEvent, StopEvent, PluginChangedEvent)):
                        return HookDecision.TRIGGER
                    return HookDecision.SKIP

            self._hook_policy_obj = _FallbackSubagent()
        return self._hook_policy_obj

    def _should_run_hook(self, event) -> bool:
        """按 _hook_policy_obj 判定给定事件是否触发"""
        try:
            policy = self._hook_policy_obj_resolve()
            return policy.should_trigger(event).value == "trigger"
        except Exception:
            return True  # 异常保守放行

    def _resolve_round_limit(self) -> Optional[int]:
        """轮数上限：per-agent steps（max_iterations）显式声明优先，否则激活策略兜底（默认 30）。"""
        if self.max_iterations is not None:
            return self.max_iterations
        try:
            return self._loop_policy().max_rounds(self.llm_config or {})
        except Exception as exc:
            logger.warning(f"[SubAgentExecutor] LoopPolicy max_rounds 调用异常，回退默认 30: {exc!r}")
            return 30

    def _final_summary_prompt(self) -> str:
        """最终总结提示词：激活策略提供（SubagentLoopPolicy），异常时回退内置文案"""
        try:
            fn = getattr(self._loop_policy(), "final_summary_prompt", None)
            if callable(fn):
                prompt = fn()
                if isinstance(prompt, str) and prompt:
                    return prompt
        except Exception as exc:
            logger.warning(f"[SubAgentExecutor] LoopPolicy final_summary_prompt 调用异常，用内置文案: {exc!r}")
        return _FALLBACK_FINAL_SUMMARY_PROMPT

    def _filter_thinking_content(self, content: str) -> str:
        """过滤掉思考内容，只保留纯回复"""
        if not content:
            return content
        return _THINKING_PATTERN.sub("", content)

    def _serialize_for_api(self, messages: List[Dict], llm_config: Dict = None):
        """序列化单入口（Phase C）：adapter flags → serializer_id → 序列化器 → SerializeResult。

        与 chat_worker._serialize_for_api 对称；supports_vision 默认 True（subagent 无视觉注入）。
        """
        from app.plugins.contracts.message_serializer import SerializeContext
        from app.plugins.registries.serializer_registry import SerializerRegistry

        config = llm_config if llm_config is not None else getattr(self, "llm_config", None)
        flags = self._protocol_flags(config)
        serializer = SerializerRegistry.get_instance().resolve(flags.serializer_id)
        return serializer.serialize(messages, SerializeContext(flags=flags))

    def _protocol_flags(self, llm_config: Dict = None):
        """经 ModelAdapterRegistry 解析完整协议开关（含 serializer_id，冷启动防御同 Task 1）"""
        from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

        registry = ModelAdapterRegistry.get_instance()
        config = llm_config if llm_config is not None else getattr(self, "llm_config", None)
        adapter = registry.resolve(config or {})
        if adapter is None:
            adapter = self._resolve_adapter_with_warmup(registry, config or {})
        if adapter is None:
            raise RuntimeError(
                "未注册任何 ModelAdapter 插件（含系统插件 openai），请确认 plugins/system/model_adapters/ 已启用"
            )
        return adapter.protocol_flags(config or {})

    def _requires_reasoning_content(self, llm_config: Dict) -> bool:
        """thinking 模式下，兼容要求 tool-call assistant 保留 reasoning_content 字段的 provider。

        通过 ModelAdapterRegistry 解析（系统插件 openai 兜底，可被插件覆盖）。
        每调用解析一次 ——该方法每轮对话仅调用数次，开销可忽略；
        不缓存避免 worker 生命周期与热重载不一致。
        """
        from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

        registry = ModelAdapterRegistry.get_instance()
        adapter = registry.resolve(llm_config or {})
        if adapter is None:
            adapter = self._resolve_adapter_with_warmup(registry, llm_config)
        if adapter is None:
            raise RuntimeError(
                "未注册任何 ModelAdapter 插件（含系统插件 openai），请确认 plugins/system/model_adapters/ 已启用"
            )
        return adapter.protocol_flags(llm_config or {}).requires_reasoning_content

    def _resolve_adapter_with_warmup(self, registry, llm_config: Dict) -> Optional[Any]:
        """冷启动兜底：注册表为空时幂等加载系统插件再 resolve（同 chat_worker._adapter_flags 防御）"""
        try:
            if registry.adapters():
                return None
            from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

            warmup_runtime_components()
        except Exception as e:
            logger.warning(f"[SubAgentWorker] 冷启动 ModelAdapter 加载失败: {e}")
            return None
        return registry.resolve(llm_config or {})

    # ========== Hook 集成（让子智能体也能应用所有 hook） ==========
    # 设计目标：与 chat_worker 对齐，让子智能体也能触发/消费以下 hook：
    #   - PreAssistantMessage / PostAssistantMessage：子智能体自己同步触发
    #   - PreToolUse / PostToolUse：tool_executor.execute() 已触发，消息进 backend 队列，
    #     此处只需消费对应队列
    # 所有 hook context 都会注入 current_role="subagent" + agent_name，
    # 让 hook 脚本能识别当前执行角色并按需分支。

    def _build_hook_context(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构造 hook context：基础字段（current_role/agent_name/task_id/workdir）+ 调用方扩展"""
        ctx: Dict[str, Any] = {
            "agent_name": self.agent_name,
            "current_role": "subagent",  # 关键：让 hook 知道当前是子智能体
            "is_subagent_call": self.is_subagent_call,
            "task_id": self.task_id,
            "task_description": self.task_description,
        }
        # project_root：用于 read_project_notes 等需要 workdir 的 hook
        try:
            if self.tool_executor and hasattr(self.tool_executor, "get_workdir"):
                ctx["project_root"] = self.tool_executor.get_workdir() or ""
        except Exception:
            pass
        if extra:
            ctx.update(extra)
        return ctx

    def _trigger_hook_sync(
        self,
        event_name: str,
        current_messages: List[Dict],
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """同步触发 hook 并把每条 hook 输出追加到 current_messages（in-place）

        与 chat_worker._trigger_worker_hook 行为一致：
        - 使用 trigger_event(sync) 同步执行 hook
        - 用 _make_hook_message 包装成 user 消息（role=user，与 Claude Code 官方行为对齐）
        - 直接 append 到 current_messages，下次 API 调用时 LLM 即可看到

        HookPolicy 接入（scope=subagent）：按 _hook_policy_id 解析策略，
        默认 SubagentDefaultHookPolicy（仅工具级 + Stop + PluginChanged）。
        PreAssistantMessage/PostAssistantMessage 走 subagent 自注入（不在 hook_policy 范围）。
        """
        try:
            backend = getattr(self.tool_executor, "_backend", None) if self.tool_executor else None
            if not backend or not backend.hook_manager:
                return

            ctx = self._build_hook_context(extra=extra_context)

            # HookPolicy 拦截：构造具体事件类 + should_trigger 判定
            from app.plugins.contracts.hook_policy import (
                PostAssistantMessageEvent,
                PreAssistantMessageEvent,
                PluginChangedEvent,
                PostToolUseEvent,
                PreToolUseEvent,
                StopEvent,
            )

            is_team = bool(ctx.get("is_team_member", False))
            role = ctx.get("current_role", "subagent")
            if event_name == "PreAssistantMessage":
                ev = PreAssistantMessageEvent(message=ctx.get("message", ""), is_team_member=is_team)
            elif event_name == "PostAssistantMessage":
                ev = PostAssistantMessageEvent(message=ctx.get("message", ""), is_team_member=is_team)
            elif event_name == "PreToolUse":
                ev = PreToolUseEvent(
                    tool_name=ctx.get("tool_name", ""),
                    tool_args=ctx.get("tool_args", {}) if isinstance(ctx.get("tool_args"), dict) else {},
                    tool_call_id=ctx.get("tool_call_id", ""),
                    current_role=role,
                    is_subagent_call=True,
                    is_team_member=is_team,
                )
            elif event_name == "PostToolUse":
                ev = PostToolUseEvent(
                    tool_name=ctx.get("tool_name", ""),
                    tool_result=ctx.get("tool_result"),
                    tool_call_id=ctx.get("tool_call_id", ""),
                    current_role=role,
                    is_subagent_call=True,
                    is_team_member=is_team,
                    success=ctx.get("success", True),
                )
            elif event_name == "Stop":
                ev = StopEvent(reason=ctx.get("reason", "completed"), is_team_member=is_team)
            elif event_name == "PluginChanged":
                ev = PluginChangedEvent(
                    action=ctx.get("action", ""),
                    plugin_name=ctx.get("plugin_name", ""),
                    diff=ctx.get("diff", {}),
                    sub_actions=ctx.get("sub_actions", []),
                )
            else:
                ev = None
            if ev is not None and not self._should_run_hook(ev):
                return

            # PreAssistantMessage / PostAssistantMessage：注入上下文使用量信息
            # 让 hook（如 context_auto_compact）能检测当前 token 占比
            if event_name in ("PreAssistantMessage", "PostAssistantMessage"):
                try:
                    from app.core.token_estimator import count_messages_tokens as _count
                    from app.core.model_capabilities import resolve_context_limit as _resolve_limit

                    token_count = _count(current_messages)
                    token_limit = 0
                    llm_config = getattr(self, "llm_config", None)
                    if llm_config:
                        token_limit = _resolve_limit(llm_config)
                    ctx["token_count"] = token_count
                    ctx["token_limit"] = token_limit
                    if token_count > 0 and token_limit > 0:
                        ctx["token_ratio"] = token_count / token_limit
                    else:
                        ctx["token_ratio"] = 0.0
                except Exception:
                    pass

            # 取最新 user 消息作为 current_message（用于 matcher 匹配）
            cur_msg = ""
            for m in reversed(current_messages):
                if m.get("role") == "user":
                    c = m.get("content", "")
                    if isinstance(c, str):
                        cur_msg = c
                    break

            # 记录 trigger_event 前的队列大小，用于后续精确 drain
            _q = getattr(backend, "_hook_message_queue", None)
            qsize_before = _q.qsize() if _q is not None else 0

            results = backend.hook_manager.trigger_event(
                event_name,
                context=ctx,
                current_message=cur_msg,
                trigger_async=False,
            )

            # 🛡️ 精确排出同步执行中 _execute_hook 通过 on_hook_finished 入队的消息，
            # 避免 _inject_pending_hook_messages（_drain_hook_queues）重复注入。
            # ★ 只排出本轮 trigger_event 新增的消息，不误伤其他路径放入的消息。
            if _q is not None:
                qsize_after = _q.qsize()
                to_drain = qsize_after - qsize_before
                for _ in range(to_drain):
                    try:
                        _q.get_nowait()
                    except Exception:
                        break
                if to_drain > 0:
                    logger.debug(
                        f"[SubAgent] Drained {to_drain} msg(s) from hook queue after sync trigger_event({event_name})"
                    )

            # 收集成功执行的 hook 输出，注入 messages
            # ★ 只注入标记为 add_to_context=true 的 hook 结果
            from app.core.backend import _make_hook_message

            injected = 0
            for r in results:
                if r.success and r.output and r.add_to_context:
                    msg = _make_hook_message(event_name, r.output, r.status_message)
                    current_messages.append(msg)
                    injected += 1
            if injected:
                logger.debug(f"[SubAgent] Hook '{event_name}' injected {injected} msg(s) into messages")
        except Exception as e:
            logger.debug(f"[SubAgent] Hook '{event_name}' trigger failed: {e}")

    def _drain_hook_queues(self) -> List[Dict[str, Any]]:
        """消费 backend 的 _pre_tool_message_queue 和 _hook_message_queue

        tool_executor.execute() 内部已同步触发 PreToolUse/PostToolUse，
        对应消息已分别进 _pre_tool_message_queue 和 _hook_message_queue。
        这里把两个队列的消息全取出来，由调用方按需插入到 tool_result 之前/之后。
        """
        msgs: List[Dict[str, Any]] = []
        try:
            backend = getattr(self.tool_executor, "_backend", None) if self.tool_executor else None
            if not backend:
                return msgs
            for q_attr in ("_pre_tool_message_queue", "_hook_message_queue"):
                q = getattr(backend, q_attr, None)
                if q is None:
                    continue
                while True:
                    try:
                        msgs.append(q.get_nowait())
                    except Exception:
                        break
        except Exception as e:
            logger.debug(f"[SubAgent] drain hook queues failed: {e}")
        return msgs

    def _parse_tool_arguments_json(self, raw_arguments: Any):
        if isinstance(raw_arguments, dict):
            return raw_arguments, ""

        text = str(raw_arguments or "")
        if not text.strip():
            return {}, ""

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, str(exc)

        if not isinstance(parsed, dict):
            return None, f"expected JSON object, got {type(parsed).__name__}"

        return parsed, ""

    def _make_api_call(self, messages: List[Dict], tools: List[Dict] = None, llm_config: Dict = None) -> tuple:
        """调用 LLM API（非流式，子智能体后台执行无需流式输出）"""
        # 使用传入的 llm_config 或回退到 self.llm_config
        config = llm_config if llm_config is not None else self.llm_config
        # 防御：确保 config 是 dict（传递给子智能体的配置可能被意外覆盖为字符串）
        if not isinstance(config, dict):
            logger.warning(
                f"[SubAgentExecutor] _make_api_call received non-dict config: {type(config).__name__}={config!r}, falling back to empty config"
            )
            config = {}
        api_key = config.get("API_KEY", "").strip()
        base_url = config.get("API_URL") or None
        model = str(config.get("模型名称", "gpt-4o"))

        req_kwargs = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        # 添加会话标识（帮助服务商区分不同会话的缓存 key / 用量监控）
        task_session_id = getattr(self, "_task_session_id", None)
        if task_session_id:
            req_kwargs["user"] = task_session_id

        extra_body = {}

        for cn_key, value in config.items():
            if cn_key in {
                "API_KEY",
                "API_URL",
                "API_BASE",
                "认证方式",
                "模型名称",
                "系统提示",
                "启用技能",
                "name",
                "provider_name",
                "config_id",
                "display_name",
                "_suffix_index",
                "备注",
                "获取地址",
                "模型列表",
            }:
                continue
            if cn_key in QUOTA_EXCLUDE_KEYS():
                continue

            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key and _VALID_IDENTIFIER_PATTERN.match(cn_key):
                en_key = cn_key
            if not en_key:
                continue
            elif en_key in ["temperature", "max_tokens", "top_p"]:
                req_kwargs[en_key] = value
            else:
                extra_body[en_key] = value

        if "max_tokens" in req_kwargs:
            req_kwargs["max_tokens"] = self._cap_max_output_tokens(model, req_kwargs["max_tokens"], config)

        # 处理思考模式
        thinking_mode = config.get("思考模式")
        if thinking_mode is not None:
            caps = get_model_capabilities(model)
            t_param = None
            enable_value = "enabled"
            if caps:
                t_param = caps.get("thinking_param")
                enable_value = caps.get("thinking_enable_value", "enabled")
            if not t_param:
                profile = get_provider_profile(config)
                t_param = profile.get("thinking_param")

            if thinking_mode is True:
                if t_param == "thinking":
                    extra_body["thinking"] = {"type": enable_value}
                    extra_body.pop("reasoning_effort", None)
                    extra_body.pop("thinking_budget", None)
                elif t_param == "thinking_budget":
                    budget = config.get("思考预算", 4096)
                    extra_body["thinking_budget"] = budget
                    extra_body.pop("reasoning_effort", None)
                    extra_body.pop("thinking", None)
                elif t_param == "reasoning_effort":
                    if "reasoning_effort" not in extra_body:
                        # 等级经 normalize 强制校验：保存值不在该模型可选值中时
                        # 回退中间配置，防止无效值发到 API
                        extra_body["reasoning_effort"] = normalize_reasoning_effort(
                            config.get("思考等级", "medium"), caps.get("reasoning_effort_values")
                        )
                    extra_body.pop("thinking", None)
                    extra_body.pop("thinking_budget", None)
            else:  # False - 关闭思考
                extra_body["thinking"] = {"type": "disabled"}
                extra_body.pop("thinking_budget", None)
                extra_body.pop("reasoning_effort", None)

        if extra_body:
            req_kwargs["extra_body"] = extra_body

        from app.utils.http_client import build_openai_client

        client = build_openai_client(
            api_key=api_key,
            base_url=base_url,
            timeout=120.0,
        )

        # GPT-5.x 系列走 Responses API（chat/completions 不透传 reasoning，思考在
        # /v1/responses 的 reasoning item 中返回）
        if self._use_responses_api(config):
            try:
                return self._make_responses_api_call(client, messages, tools, config)
            except Exception as e:
                # 网关不支持 responses 时回退 chat/completions
                logger.warning(f"[SubAgentWorker] Responses API 调用失败，回退 chat/completions: {e}")
                if self._is_cancelled:
                    return "", [], ""

        from app.core.workers.error_handler import create_api_call_with_retry

        def create_completion():
            return client.chat.completions.create(**req_kwargs, tools=tools)

        response = create_api_call_with_retry(
            client,
            create_completion,
            cancel_check=lambda: self._is_cancelled,
        )

        # 防御性检查：某些模型可能返回空 choices
        if not response.choices:
            logger.warning("[SubAgentWorker] API 返回空 choices，跳过工具执行")
            return "", [], ""

        # 非流式：直接读取响应
        message = response.choices[0].message
        response_content = self._filter_thinking_content(message.content or "")
        reasoning_content = getattr(message, "reasoning_content", "") or ""

        # 提取工具调用
        tool_calls_found = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls_found.append(
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,  # 已是 JSON 字符串
                        },
                    }
                )

        # 追踪 token 用量
        try:
            usage = response.usage
            if usage:
                pt = getattr(usage, "prompt_tokens", 0) or 0
                ct = getattr(usage, "completion_tokens", 0) or 0
                tt = getattr(usage, "total_tokens", 0) or 0
                self._total_prompt_tokens += pt
                self._total_completion_tokens += ct
                self._total_tokens += tt
                if tt > self._peak_total_tokens:
                    self._peak_total_tokens = tt
                self.token_usage_updated.emit(self.task_id, pt, ct, tt)
        except Exception:
            pass

        return response_content, tool_calls_found, reasoning_content

    # ========== Responses API（GPT-5.x 系列）==========

    def _use_responses_api(self, llm_config: Dict = None) -> bool:
        """GPT-5.x 系列走 Responses API（chat/completions 不透传 reasoning）。"""
        config = llm_config if llm_config is not None else self.llm_config
        try:
            if not isinstance(config, dict):
                return False
            override = config.get("使用ResponsesAPI")
            if override is not None:
                return bool(override)
            model = str(config.get("模型名称", "") or "").lower()
            return model.startswith("gpt-5")
        except Exception:
            return False

    @staticmethod
    def _responses_tools(tools: List[Dict]) -> List[Dict]:
        """chat/completions 工具格式 → Responses API 扁平格式。"""
        out: List[Dict] = []
        for t in tools or []:
            if not isinstance(t, dict):
                continue
            func = t.get("function") or {}
            if not isinstance(func, dict) or not func.get("name"):
                continue
            out.append(
                {
                    "type": "function",
                    "name": func.get("name"),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        return out

    def _make_responses_api_call(self, client, messages, tools, config) -> tuple:
        """非流式 Responses API 调用：解析 output 数组 → (content, tool_calls, reasoning)。"""
        result = self._serialize_for_api(messages, config)
        input_items, instructions = result.input_items, result.instructions
        model = str(config.get("模型名称", "") or "")
        kwargs: Dict[str, Any] = {
            "model": model,
            "input": input_items,
            "stream": False,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = self._responses_tools(tools)

        thinking_mode = config.get("思考模式")
        if thinking_mode is True:
            kwargs["reasoning"] = {"effort": config.get("思考等级", "medium")}
        elif thinking_mode is False:
            kwargs["reasoning"] = {"effort": "none"}

        max_tokens = config.get("最大Token")
        if max_tokens is not None:
            kwargs["max_output_tokens"] = self._cap_max_output_tokens(model, max_tokens, config)

        task_session_id = getattr(self, "_task_session_id", None)
        if task_session_id:
            kwargs["user"] = task_session_id

        logger.info(f"[SubAgentWorker][ResponsesAPI] model={model} 使用 Responses API")
        response = client.responses.create(**kwargs)
        return self._parse_responses_output(response)

    def _parse_responses_output(self, response) -> tuple:
        """解析非流式 Responses 对象 output 数组。

        Returns:
            (content, tool_calls, reasoning)
        """
        reasoning_parts: List[str] = []
        content_parts: List[str] = []
        tool_calls: List[Dict] = []

        def _item_get(item, key, default=None):
            if isinstance(item, dict):
                return item.get(key, default)
            return getattr(item, key, default)

        for item in getattr(response, "output", None) or []:
            itype = _item_get(item, "type", "")
            if itype == "reasoning":
                # summary 数组：OpenCode Go 网关提供思考摘要
                summary = _item_get(item, "summary", None) or []
                for s in summary:
                    if isinstance(s, dict):
                        text = s.get("text", "")
                    else:
                        text = getattr(s, "text", "") or ""
                    if text:
                        reasoning_parts.append(text)
                # 兜底：完整思考内容（部分网关放 content/raw）
                raw = _item_get(item, "content", None) or _item_get(item, "raw", None)
                if isinstance(raw, str) and raw:
                    reasoning_parts.append(raw)
            elif itype == "message":
                for part in _item_get(item, "content", None) or []:
                    if _item_get(part, "type", "") == "output_text":
                        text = _item_get(part, "text", "") or ""
                        if text:
                            content_parts.append(text)
            elif itype == "function_call":
                call_id = _item_get(item, "call_id", "") or _item_get(item, "id", "")
                name = _item_get(item, "name", "") or ""
                arguments = _item_get(item, "arguments", "") or "{}"
                if call_id and name:
                    tool_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    )

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        if content:
            content = self._filter_thinking_content(content)

        # 追踪 token 用量
        try:
            usage = getattr(response, "usage", None)
            if usage:
                pt = getattr(usage, "input_tokens", 0) or 0
                ct = getattr(usage, "output_tokens", 0) or 0
                tt = getattr(usage, "total_tokens", 0) or 0
                self._total_prompt_tokens += pt
                self._total_completion_tokens += ct
                self._total_tokens += tt
                if tt > self._peak_total_tokens:
                    self._peak_total_tokens = tt
                self.token_usage_updated.emit(self.task_id, pt, ct, tt)
        except Exception:
            pass

        return content, tool_calls, reasoning

    def _cap_max_output_tokens(self, model: str, requested: int, llm_config: Dict = None) -> int:
        """
        计算 max_tokens 的合理上限。

        核心原则：
        - 用户明确设置的 max_tokens 应被尊重，provider 默认值不再作为硬上限
        - 仅对已知的模型特定限制做软提示（不强制截断）
        - 对于 write/edit 等工具调用场景，需要足够的输出 token
          来生成完整的 arguments JSON（含长 content）

        Args:
            model: 模型名称
            requested: 用户配置中请求的 max_tokens
            llm_config: LLM 配置字典（优先使用）

        Returns:
            合理的 max_tokens 值
        """
        try:
            requested_int = int(requested)
        except Exception:
            return requested

        config = llm_config if llm_config is not None else self.llm_config
        profile = get_provider_profile(config)

        # 1. 如果用户没有设置或设置值 <= 0，使用 provider 默认值
        if requested_int <= 0:
            return int(profile.get("max_output_tokens", 8192))

        # 2. 获取绝对上限（防止用户设置极端值）
        absolute_limit = int(profile.get("absolute_limit", 65536))

        # 3. 针对特定模型系列的软限制（仅当用户设置值超出时才生效）
        family = profile.get("family", "")
        model_name = (model or "").lower()

        if family == "openai":
            if "gpt-4-turbo" in model_name:
                # GPT-4-Turbo 实际限制 4096，超出会报错
                if requested_int > 4096:
                    return 4096
        elif family == "anthropic":
            # Claude 系列上限一般为 8192
            pass
        elif family == "minimax":
            pass  # MiniMax 支持高输出

        # 4. 只做绝对上限保护（避免明显错误的极值）
        return min(requested_int, absolute_limit)

    def _execute_tools(self, tool_calls: List[Dict]) -> tuple:
        """执行工具调用

        返回 (tool_results, hook_messages) 元组。
        - tool_results: 纯 role="tool" 消息列表，可安全 extend 到 assistant(tool_calls) 之后
        - hook_messages: PreToolUse/PostToolUse 的 role="user" 消息列表，
          应在所有 tool_results 被 extend 之后再 extend，避免插入 assistant(tool_calls)
          和 tool 消息之间导致 API 2013 错误

        同步执行所有 tool_executor.execute()，并在末尾消费 backend 的 hook 队列：
        - _pre_tool_message_queue → PreToolUse 消息（role="user"）
        - _hook_message_queue    → PostToolUse 消息（role="user"）
        """
        if not tool_calls or not self.tool_executor:
            return [], []

        tool_results = []
        hook_messages = []
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            arguments = tc["function"]["arguments"]

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    parsed = smart_parse_arguments(arguments, tool_name)
                    if parsed is not None:
                        arguments = parsed
                        logger.info(f"[SubAgent] ✓ JSON 智能修复成功: tool={tool_name}")
                    else:
                        logger.warning(
                            f"[SubAgent] ⚠️ JSON 解析失败且无法修复, tool={tool_name}, preview='{arguments[:200]}'"
                        )
                        arguments = {}

            tool_call_id = tc["id"]

            # 交互式工具（UI 弹窗，metadata["interactive"]=True）：子智能体不执行
            try:
                from app.tools.registry import ToolRegistry

                _interactive = ToolRegistry.get_instance().is_interactive(tool_name)
            except Exception:
                _interactive = False
            if _interactive:
                return None, []

            # ★ T24 方案 B：UI 工具权限检查（执行前）
            # UI 调整（ToolPermissionController）对子智能体结构性生效：
            # - deny：跳过执行，回填失败 ToolResult（保持 tool_call_id 与消息顺序）
            # - ask：emit 信号桥接主线程弹窗，允许才执行，拒绝/超时回填失败
            _ui_permission = self._check_ui_tool_permission(tool_name, arguments)
            _ui_denied = False
            if _ui_permission == "ask":
                _ui_denied = not self._ask_permission(tool_name, arguments)
                if _ui_denied:
                    logger.info(f"[SubAgent] 工具 {tool_name} 被用户拒绝（ask）")
            elif _ui_permission == "deny":
                _ui_denied = True
                logger.info(f"[SubAgent] 工具 {tool_name} 已被 UI 禁用（deny），跳过执行")

            self._tool_call_count += 1
            self.tool_call_started.emit(self.task_id, tool_name, arguments)
            self._add_log("tool_call", tool_name, {"args": arguments})
            QCoreApplication.processEvents()

            # 工具执行也算活跃（避免 stall 检测器误杀）
            self._last_activity_time = time.time()

            if _ui_denied:
                # 跳过执行，回填失败 ToolResult（保持 tool_call_id 与消息顺序）
                result = ToolResult(False, error=f"工具 {tool_name} 已被禁用或拒绝")
            else:
                # tool_executor.execute() 内部已同步触发 PreToolUse 和 PostToolUse，
                # 消息分别进 backend 的 _pre_tool_message_queue / _hook_message_queue
                # 传入 hook_context 覆盖默认角色，防止 subagent 工具调用误用 primary 角色
                result = self.tool_executor.execute(
                    tool_name,
                    arguments,
                    hook_context={
                        "current_role": "subagent",
                        "is_subagent_call": True,
                    },
                )
            result_content = str(result) if result else ""
            success = getattr(result, "success", True) if result else False

            self.tool_result_received.emit(self.task_id, tool_name, result_content, success)
            self._add_log("tool_result", tool_name, {"result": result_content, "success": success})
            QCoreApplication.processEvents()

            # 消费 PreToolUse 队列（role="user" 消息），放入 hook_messages 而非 tool_results
            pretool_msgs = self._drain_pretool_queue()
            if pretool_msgs:
                hook_messages.extend(pretool_msgs)

            raw_result = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": result_content,
                "arguments": arguments,
                "success": success,
                "round_id": f"round_{id(tc)}",
                "diff": getattr(result, "diff", None) if result else None,
                "anchors": getattr(result, "anchors", None) if result else None,
            }
            # 用 to_api_message 标准化：仅保留 role/tool_call_id/name/content，避免非标字段混淆API
            serialized = self._serialize_for_api([raw_result]).messages
            tool_results.append(serialized[0] if serialized else raw_result)

            # 消费 PostToolUse 队列（role="user" 消息），放入 hook_messages 而非 tool_results
            posttool_msgs = self._drain_posttool_queue()
            if posttool_msgs:
                hook_messages.extend(posttool_msgs)

        return tool_results, hook_messages

    def _check_ui_tool_permission(self, tool_name: str, arguments: dict = None) -> str:
        """UI 工具权限检查（T24 执行层唯一控制点）— 返回 "allow" / "deny" / "ask"。

        优先级（对齐产品指示）：
        a) UI controller toggles：用户显式关闭的工具（toggles False）→ UI 为准
           （behavior=deny → deny；ask → 弹窗询问），覆盖模板 allow
        b) UI 开启/默认（toggles True）→ 模板 PermissionResolver 判定：
           模板 deny → 执行层拦截（返回 deny，schema 已静态化不再移除）
           模板 allow/ask → 执行（子智能体无模板 ask 弹窗机制，视为允许）
        c) 团队工具无条件放行

        check_name 归一化（mcp__server__tool → tool）；无 controller（API 模式）
        回退 Settings.tool_toggles。
        """
        from app.tools.registry import ToolRegistry

        if tool_name in ToolRegistry.get_instance().team_only_tools():
            return "allow"
        check_name = tool_name
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            check_name = parts[2] if len(parts) > 2 else tool_name

        controller = None
        backend = getattr(self.tool_executor, "_backend", None) if self.tool_executor else None
        if backend is not None:
            controller = getattr(backend, "tool_permission_controller", None)

        policies: Dict[str, str] = {}
        if controller is not None:
            toggles = controller.get_toggles()
            behavior = controller.get_behavior()
        else:
            from app.utils.config import Settings

            settings = Settings.get_instance()
            toggles = dict(settings.tool_toggles.value)
            behavior = settings.tool_off_behavior.value
            policies = dict(settings.tool_permission_policy.value)

        is_enabled = toggles.get(check_name, True)
        if not is_enabled:
            # per-tool 关闭策略优先，缺失回退全局 behavior（与 UI 引擎 _check_tool_permission 同口径）
            from app.core.tool_permission_controller import resolve_tool_off_policy

            return resolve_tool_off_policy(check_name, controller, policies, behavior)

        # ★ T28：UI 显式开启（用户调整过该工具）→ UI 为准，放行（覆盖模板 deny）
        if controller is not None and controller.is_user_modified(check_name):
            return "allow"

        # 未调整（默认开启）→ 模板 PermissionResolver（模板 deny 执行层拦截）
        try:
            agent = self.agent_manager.get_agent(self.agent_name) if self.agent_manager else None
            if agent is not None:
                from app.core.agent import PermissionResolver

                resolver = PermissionResolver(agent.permission, {}, agent.tools)
                # 权限参数适配（与 UI 引擎/AGENT_CONFIG 同口径）：
                # bash 用 command、read 用 filePath...（registry metadata 驱动）
                try:
                    mode, arg = ToolRegistry.get_instance().permission_resolve_args(tool_name, arguments or {})
                except Exception:
                    mode, arg = "plain", ""
                if mode == "task":
                    check_result = resolver.resolve_task(arg)
                elif arg:
                    check_result = resolver.resolve(tool_name, arg)
                else:
                    check_result = resolver.resolve(tool_name)
                if check_result == "deny":
                    return "deny"
        except Exception as e:
            logger.debug(f"[SubAgent] 模板权限解析失败，放行: {e}")
        return "allow"

    def _ask_permission(self, tool_name: str, arguments: dict, timeout: float = 30.0) -> bool:
        """ask 行为：emit 信号桥接主线程弹窗，等待用户决策（超时按拒绝）。

        主线程连接 permission_requested 后弹窗 → respond_permission(allow)
        → 本方法返回结果。用户 30s 无响应按拒绝处理（不阻塞子智能体任务）。
        """
        self._permission_event.clear()
        self._permission_allow = False
        self._permission_answered = False
        try:
            self.permission_requested.emit(self.task_id, tool_name, arguments)
        except Exception as e:
            logger.warning(f"[SubAgent] permission_requested emit failed: {e}")
            return False
        self._permission_event.wait(timeout)
        return self._permission_allow

    def respond_permission(self, allow: bool):
        """主线程响应权限询问结果（ask 分支继续执行/拒绝）。"""
        self._permission_allow = bool(allow)
        self._permission_answered = True
        self._permission_event.set()

    def _drain_pretool_queue(self) -> List[Dict[str, Any]]:
        """仅消费 backend 的 _pre_tool_message_queue（PreToolUse 消息）"""
        msgs: List[Dict[str, Any]] = []
        try:
            backend = getattr(self.tool_executor, "_backend", None) if self.tool_executor else None
            if not backend:
                return msgs
            q = getattr(backend, "_pre_tool_message_queue", None)
            if q is None:
                return msgs
            while True:
                try:
                    msgs.append(q.get_nowait())
                except Exception:
                    break
        except Exception as e:
            logger.debug(f"[SubAgent] drain pretool queue failed: {e}")
        return msgs

    def _drain_posttool_queue(self) -> List[Dict[str, Any]]:
        """仅消费 backend 的 _hook_message_queue（PostToolUse 消息）"""
        msgs: List[Dict[str, Any]] = []
        try:
            backend = getattr(self.tool_executor, "_backend", None) if self.tool_executor else None
            if not backend:
                return msgs
            q = getattr(backend, "_hook_message_queue", None)
            if q is None:
                return msgs
            while True:
                try:
                    msgs.append(q.get_nowait())
                except Exception:
                    break
        except Exception as e:
            logger.debug(f"[SubAgent] drain posttool queue failed: {e}")
        return msgs


class SubAgentManager(QObject):
    """子智能体管理器 - 管理子智能体任务分发"""

    task_started = Signal(str, str, str)  # task_id, agent_name, task_description
    task_finished = Signal(str, str)  # task_id, result
    batch_finished = Signal()  # 批次内所有任务都完成时触发
    # ★ T24：子智能体 ask 权限请求转发（window_id, task_id, tool_name, arguments）→ 主线程弹窗
    permission_requested = Signal(str, str, str, dict)

    def __init__(self, agent_manager, tool_executor, get_llm_config: Callable):
        super().__init__()
        self._agent_manager = agent_manager
        self._tool_executor = tool_executor
        self._get_llm_config = get_llm_config
        self._running_tasks: Dict[str, SubAgentExecutor] = {}
        # H1：有界字典，防止 _finished_tasks 无限增长（进程生命周期内巨漏）；上限 200，超出弹最旧
        self._finished_tasks = _BoundedTaskDict(
            maxlen=200
        )  # task_id -> {"result": str, "error": str, "session_id": str}
        self._session_store = None  # 使用 SessionStore 替代 SubAgentLogStore
        # 批次计数：本次启动的任务总数
        self._batch_total = 0
        self._batch_completed = 0
        # 当前批次的任务ID集合，用于回调时只通知本批次完成的任务
        self._batch_task_ids: set = set()
        # 已查询过的任务ID集合（按 session 隔离），避免重复返回结果浪费上下文
        # Dict[session_id, Set[task_id]]
        self._queried_tasks: Dict[str, set] = {}
        # 获取主智能体历史消息的回调（由外部设置）
        self._get_history_messages: Optional[Callable[[], List[Dict]]] = None
        # 当前会话 ID（用于隔离不同会话的子智能体任务）
        self._current_session_id: str = ""

        # ========== 日志活力度 Stall 检测 ==========
        self._stall_timeout: int = 300  # 日志静默超时秒数（默认 5 分钟）
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(10000)  # 每 10 秒检查一次
        self._stall_timer.timeout.connect(self._check_stalled_tasks)

        # DAG 回调延后连接：监听自己的 task_started，在此时连接 DAG 回调到 executor
        # （与 UI 回调 _on_sub_agent_task_started 完全相同的连接时机）
        self.task_started.connect(self._on_dag_task_started_slot)

    def set_current_session_id(self, session_id: str):
        """设置当前会话 ID，用于会话隔离"""
        self._current_session_id = session_id

    def set_history_getter(self, getter: Callable[[], List[Dict]]):
        """设置获取主智能体历史消息的回调"""
        self._get_history_messages = getter

    def set_session_store(self, session_store):
        """设置会话存储（使用 SessionStore 统一管理）"""
        self._session_store = session_store

    def _save_task_to_store(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        status: str = "running",
        result: str = None,
        error: str = None,
        logs: List[Dict] = None,
        summary: Dict = None,
        session_id: str = "",
    ):
        """保存任务到数据库（通过 SessionStore），携带会话 ID 以实现会话隔离"""
        if not self._session_store:
            return
        try:
            if logs is None or summary is None:
                executor = self._running_tasks.get(task_id)
                if executor and hasattr(executor, "get_logs"):
                    logs = executor.get_logs()
                if executor and hasattr(executor, "get_summary"):
                    summary = executor.get_summary()
            self._session_store.save_subagent_task(
                task_id,
                agent_name,
                task_description,
                status,
                result,
                error,
                logs or [],
                summary or {},
                session_id=session_id or self._current_session_id,
            )
        except Exception as e:
            logger.error(f"[SubAgentManager] 保存任务到数据库失败: {e}")

    # ========== 日志活力度 Stall 检测 ==========

    def start_stall_detector(self):
        """启动 stall 检测定时器（默认在 SubAgentManager 初始化时未启动，由外部调用）"""
        if not self._stall_timer.isActive():
            self._stall_timer.start()
            logger.info("[SubAgentManager] Stall 检测器已启动")

    def stop_stall_detector(self):
        """停止 stall 检测定时器"""
        if self._stall_timer.isActive():
            self._stall_timer.stop()
            logger.info("[SubAgentManager] Stall 检测器已停止")

    def set_stall_timeout(self, seconds: int):
        """设置日志静默超时阈值（最少 30 秒）"""
        self._stall_timeout = max(30, seconds)
        logger.info(f"[SubAgentManager] Stall 超时已设置为 {self._stall_timeout}s")

    def _check_stalled_tasks(self):
        """
        检查所有运行中任务的最后活跃时间，如果日志静默超过 stall_timeout 则 cancel。
        由 QTimer 定时触发（默认每 10 秒）。
        """
        import time

        now = time.time()
        for task_id in list(self._running_tasks.keys()):
            executor = self._running_tasks.get(task_id)
            if not executor or not executor.isRunning():
                continue

            last_activity = executor.get_last_activity_time()
            if not last_activity:
                continue

            idle_time = now - last_activity
            if idle_time <= self._stall_timeout:
                continue

            # 检测到 stall（日志静默超时）
            error_msg = f"Task stalled: no log activity for {int(idle_time)}s (timeout={self._stall_timeout}s)"
            logger.warning(
                f"[SubAgentManager] ⚠️ Task {task_id[:8]} ({executor.agent_name}) "
                f"stalled for {int(idle_time)}s, cancelling"
            )

            # 1. Cancel executor
            executor.cancel()

            # 2. 写入 finished_tasks
            agent_name = executor.agent_name
            task_description = executor.task_description
            logs = executor.get_logs()
            task_session_id = getattr(executor, "_task_session_id", self._current_session_id)
            self._finished_tasks[task_id] = {
                "result": "",
                "error": error_msg,
                "agent_name": agent_name,
                "task_description": task_description,
                "session_id": task_session_id,
                "logs": logs,
            }

            # 3. 保存数据库（status="stalled"）
            self._save_task_to_store(
                task_id,
                agent_name,
                task_description,
                "stalled",
                "",
                error_msg,
                logs,
                session_id=task_session_id,
            )

            # 4. 通知 DAG（如果有）
            self._notify_dag_task_failed(task_id, error_msg)

            # 5. 通知 UI
            try:
                self.task_finished.emit(task_id, "")
            except Exception as e:
                logger.error(f"[SubAgentManager] task_finished.emit 失败 (stalled path): {e}")

            # 6. 从 running_tasks 移除（避免 get_finished_tasks 再处理一次）
            #    注意：executor 线程可能还在运行（卡在 API 调用中），
            #    但已经从管理器角度"移除"了，后续 finished_with_result 回调
            #    会因 task_id 不在 running_tasks 而被安全忽略。
            if task_id in self._running_tasks:
                del self._running_tasks[task_id]

    def _dispatch_executor_finished(self, task_id: str, result: str):
        """executor finished_with_result → 外部 on_finished 回调（queued 回主线程后执行）"""
        executor = self._running_tasks.get(task_id)
        cb = getattr(executor, "_cb_finished", None) if executor else None
        if cb:
            try:
                cb(task_id, result)
            except Exception as e:
                logger.error(f"[SubAgentManager] on_finished 回调异常: task={task_id[:8]}: {e}", exc_info=True)

    def _dispatch_executor_error(self, task_id: str, error: str):
        """executor error_occurred → 外部 on_error 回调（queued 回主线程后执行）"""
        executor = self._running_tasks.get(task_id)
        cb = getattr(executor, "_cb_error", None) if executor else None
        if cb:
            try:
                cb(task_id, error)
            except Exception as e:
                logger.error(f"[SubAgentManager] on_error 回调异常: task={task_id[:8]}: {e}", exc_info=True)

    def _dispatch_executor_progress(self, task_id: str, message: str):
        """executor progress_updated → 外部 on_progress 回调（queued 回主线程后执行）"""
        executor = self._running_tasks.get(task_id)
        cb = getattr(executor, "_cb_progress", None) if executor else None
        if cb:
            try:
                cb(task_id, message)
            except Exception as e:
                logger.error(f"[SubAgentManager] on_progress 回调异常: task={task_id[:8]}: {e}", exc_info=True)

    def execute_task(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        parent_context: str = "",
        on_finished: Callable[[str], None] = None,
        on_error: Callable[[str], None] = None,
        on_progress: Callable[[str], None] = None,
        executor_ref: Dict = None,
        share_context: bool = False,  # 是否共享主智能体上下文
        session_id: str = "",  # 所属会话 ID（任务创建时锁定，避免跨会话覆盖）
        llm_config: Dict = None,  # 可选：预解析的 LLM 配置（支持覆盖模型）
    ) -> bool:
        """执行子智能体任务

        Args:
            session_id: 所属会话 ID。任务创建时即锁定该值，
                        后续回调不再读取全局 _current_session_id，
                        避免同一窗口内切换会话后异步回调用错 session_id。
            llm_config: 预解析的 LLM 配置（可选）。传入时跳过内部 _get_llm_config() 调用，
                        用于 --model=xxx 覆盖模型/服务商的场景。
        """
        # 在任务创建时即锁定 session_id，不依赖后续的全局状态
        task_session_id = session_id or self._current_session_id

        # 验证 agent 是否存在且是有效的子智能体类型
        agent = self._agent_manager.get_agent(agent_name)
        if not agent:
            error_msg = f"Agent not found: {agent_name}"
            logger.error(f"[SubAgentManager] {error_msg}")
            # 立即触发完成信号，让任务不卡在 running 状态
            self._finished_tasks[task_id] = {
                "result": "",
                "error": error_msg,
                "agent_name": agent_name,
                "session_id": task_session_id,
            }
            if on_error:
                on_error(error_msg)
            return False

        # 只允许 mode 为 subagent 或 all 的 agent 作为子智能体
        if not agent.is_subagent():
            error_msg = f"Agent '{agent_name}' cannot be used as subagent (mode: {agent.mode})"
            logger.error(f"[SubAgentManager] {error_msg}")
            self._finished_tasks[task_id] = {
                "result": "",
                "error": error_msg,
                "agent_name": agent_name,
                "session_id": task_session_id,
            }
            if on_error:
                on_error(error_msg)
            return False

        try:
            if llm_config is None:
                llm_config = self._get_llm_config()
            elif not isinstance(llm_config, dict):
                # 防御：非 dict 的 llm_config（可能从 main_widget 传入的异常值）
                logger.warning(
                    f"[SubAgentManager] execute_task llm_config is not a dict: {type(llm_config).__name__}={llm_config!r}, falling back to default"
                )
                llm_config = self._get_llm_config()
            if not llm_config:
                if on_error:
                    on_error("No LLM config available")
                return False

            # 如果启用共享上下文，将获取完整的上下文信息
            full_context = parent_context
            if share_context and self._get_history_messages:
                try:
                    history_messages = self._get_history_messages() or []
                    if history_messages:
                        # 格式化历史消息
                        history_lines = []
                        max_chars = 1000  # 完整上下文字符限制
                        for msg in history_messages:
                            role = msg.get("role", "user")
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                            if content:
                                truncated = content[:max_chars] + ("..." if len(content) > max_chars else "")
                                history_lines.append(f"**{role}**: {truncated}")

                        if history_lines:
                            history_section = "\n\n## 主智能体完整上下文\n" + "\n".join(history_lines)
                            if parent_context:
                                full_context = f"{parent_context}{history_section}"
                            else:
                                full_context = history_section.lstrip("\n\n")
                except Exception as e:
                    logger.warning(f"[SubAgentManager] 获取上下文失败: {e}")

            # 轮数上限：agent.steps 显式声明优先（None=激活策略兜底，默认 subagent 策略 30）
            max_iterations = agent.steps

            executor = SubAgentExecutor(
                task_id=task_id,
                agent_name=agent_name,
                task_description=task_description,
                llm_config=llm_config,
                agent_manager=self._agent_manager,
                tool_executor=self._tool_executor,
                parent_context=full_context,
                is_subagent_call=True,  # 标记为被主智能体调用
                max_iterations=max_iterations,  # 传递最大迭代次数限制
            )
            # 在 executor 上存储 session_id，供后续回调使用
            executor._task_session_id = task_session_id

            # 设置日志存储回调（用 lambda 默认参数在定义时锁定 session_id，避免闭包 late-binding）
            if self._session_store:
                executor.set_log_store_callback(
                    lambda *args, _sid=task_session_id: self._save_task_to_store(*args, session_id=_sid)
                )

            # 【新增】设置历史消息获取回调
            if self._get_history_messages:
                executor.set_history_getter(self._get_history_messages)

            if executor_ref is not None:
                executor_ref["executor"] = executor

            # ⚠️ 外部回调不能直接 connect（lambda/closure 的 receiver 归属 sender=executor，
            # 而 executor 在 ChatWorker 子线程创建 → queued 投到无事件循环的线程 → 永不执行）。
            # 统一存到 executor 上，由 Manager 的 bound method（receiver=主线程）分发。
            executor._cb_finished = on_finished
            executor._cb_error = on_error
            executor._cb_progress = on_progress
            executor.finished_with_result.connect(self._dispatch_executor_finished)
            executor.error_occurred.connect(self._dispatch_executor_error)
            executor.progress_updated.connect(self._dispatch_executor_progress)

            # ★ T24：转发子智能体 ask 权限请求到主线程（带 window_id 供多窗口定位弹窗）
            executor.permission_requested.connect(self._forward_permission_request)

            self._running_tasks[task_id] = executor
            executor.start()

            # 批次计数：本次启动的任务数
            self._batch_total += 1
            self._batch_task_ids.add(task_id)

            # 保存到数据库（传入锁定的 task_session_id）
            self._save_task_to_store(task_id, agent_name, task_description, "running", session_id=task_session_id)

            try:
                self.task_started.emit(task_id, agent_name, task_description)
            except Exception as e:
                logger.error(f"[SubAgentManager] task_started.emit failed: {e}", exc_info=True)

            logger.info(f"[SubAgentManager] Started task {task_id} with agent {agent_name}")
            return True

        except Exception as e:
            logger.error(f"[SubAgentManager] Failed to execute task: {e}")
            if on_error:
                on_error(str(e))
            return False

    def _forward_permission_request(self, task_id: str, tool_name: str, arguments: dict):
        """转发 executor 的权限请求到主线程（SubAgentManager → main_widget）。

        携带 window_id（来自 executor 所在窗口的 backend），供多窗口场景
        精确定位弹窗归属。
        """
        executor = self._running_tasks.get(task_id)
        window_id = ""
        if executor is not None and executor.tool_executor is not None:
            backend = getattr(executor.tool_executor, "_backend", None)
            if backend is not None:
                window_id = getattr(backend, "_window_id", "") or ""
        self.permission_requested.emit(window_id, task_id, tool_name, arguments)

    def respond_permission(self, task_id: str, allow: bool):
        """主线程响应用户决策 → 转交 executor（ask 分支继续/拒绝）。"""
        executor = self._running_tasks.get(task_id)
        if executor is not None:
            executor.respond_permission(allow)
        else:
            logger.warning(f"[SubAgentManager] respond_permission: task {task_id} 不存在")

    def execute_dag(self, nodes: List[Dict], edges: List[Dict], session_id: str = "") -> ToolResult:
        """
        执行 DAG 工作流（异步）。验证 DAG 后立即返回 ECharts 节点图，
        后台按拓扑顺序执行，全部完成后回调通知。

        Args:
            nodes: [{"id": str, "agent": str, "description": str, "context": str}]
            edges: [{"from": str, "to": str}]
            session_id: 会话 ID

        Returns:
            ToolResult: success=True, echarts=节点图JSON
        """
        import uuid

        # 1. 验证 DAG
        node_map = {n["id"]: dict(n) for n in nodes}
        for edge in edges:
            if edge["from"] not in node_map:
                return ToolResult(False, error=f"节点 '{edge['from']}' 不存在")
            if edge["to"] not in node_map:
                return ToolResult(False, error=f"节点 '{edge['to']}' 不存在")

        # 构建邻接表 + 入度表
        adj = {nid: [] for nid in node_map}
        in_degree = {nid: 0 for nid in node_map}
        for edge in edges:
            adj[edge["from"]].append(edge["to"])
            in_degree[edge["to"]] += 1

        # 环检测
        queue = [nid for nid in node_map if in_degree[nid] == 0]
        sorted_count = 0
        while queue:
            nid = queue.pop(0)
            sorted_count += 1
            for neighbor in adj[nid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        if sorted_count != len(node_map):
            return ToolResult(False, error="DAG 中存在环，请检查 edges 定义")

        # 2. 初始化 DAG 状态
        task_session_id = session_id or self._current_session_id
        dag_id = str(uuid.uuid4())

        # 为每个节点生成 task_id
        for n in nodes:
            nid = n["id"]
            node_map[nid]["_task_id"] = str(uuid.uuid4())
            node_map[nid]["_status"] = "pending"
            node_map[nid]["_result"] = ""
            node_map[nid]["_error"] = ""

        # 存储 DAG 状态到管理器
        dag_state = {
            "dag_id": dag_id,
            "node_map": node_map,
            "adj": adj,
            "in_degree": {nid: 0 for nid in node_map},  # 重新计算
            "upstream_results": {nid: [] for nid in node_map},
            "session_id": task_session_id,
        }
        for edge in edges:
            dag_state["in_degree"][edge["to"]] += 1

        if not hasattr(self, "_dag_states"):
            self._dag_states: Dict[str, Dict] = {}
        self._dag_states[dag_id] = dag_state

        # 【新增】建立 task_id → (dag_id, nid) 映射，让 cleanup_dead_tasks / cancel_task
        # 在清理 task 时能反向通知对应的 DAG 节点，避免 DAG 永远卡在"等下游"
        if not hasattr(self, "_task_to_dag"):
            self._task_to_dag: Dict[str, tuple] = {}
        for nid in node_map:
            tid = node_map[nid]["_task_id"]
            self._task_to_dag[tid] = (dag_id, nid)

        # 3. 设置批次计数器（所有 DAG 节点计入同一批次）
        all_task_ids = [node_map[nid]["_task_id"] for nid in node_map]
        self._batch_total += len(all_task_ids)
        self._batch_task_ids.update(all_task_ids)

        # 4. 启动入度为0的节点
        ready_nodes = [nid for nid in node_map if dag_state["in_degree"][nid] == 0]
        logger.info(f"[DAG] 初始启动: dag_id={dag_id}, total_nodes={len(nodes)}, ready={ready_nodes}, edges={edges}")
        for nid in ready_nodes:
            self._start_dag_node(dag_id, nid)

        # 5. 生成 ECharts 节点图并立即返回
        echarts_json = self._build_dag_echarts_json(nodes, edges, node_map)
        # 附带每个节点的 task_id，方便 LLM 通过 subagent_status 查询单个节点结果
        nodes_info = [
            {
                "id": n["id"],
                "task_id": node_map[n["id"]]["_task_id"],
                "agent": n["agent"],
            }
            for n in nodes
        ]
        return ToolResult(
            True,
            content={
                "dag_id": dag_id,
                "status": "running",
                "total": len(nodes),
                "nodes": nodes_info,
            },
            echarts=echarts_json,
        )

    def _start_dag_node(self, dag_id: str, nid: str):
        """启动 DAG 中的单个节点"""
        dag_state = self._dag_states[dag_id]
        node_map = dag_state["node_map"]
        node = node_map[nid]
        task_id = node["_task_id"]
        task_session_id = dag_state["session_id"]

        # 检查上游是否有失败的节点（failed 或 skipped 都级联跳过）
        upstream_failed = any(
            node_map[up["from"]]["_status"] in ("failed", "skipped") for up in dag_state["upstream_results"][nid]
        )
        if upstream_failed:
            node["_status"] = "skipped"
            node["_error"] = "上游节点执行失败，跳过"
            # 跳过的节点也需要触发完成信号，让批次计数器工作
            self._finished_tasks[task_id] = {
                "result": "",
                "error": "上游节点执行失败，跳过",
                "agent_name": node.get("agent", ""),
                "task_description": node.get("description", ""),
                "session_id": task_session_id,
            }
            try:
                self.task_finished.emit(task_id, "")
            except Exception as e:
                logger.error(f"[DAG] task_finished.emit 失败 (upstream failed path): {e}")
            # 跳过节点也要检查下游
            self._check_dag_downstream(dag_id, nid)
            return

        # 构建 context：自动注入上游结果
        context_parts = []
        if dag_state["upstream_results"][nid]:
            context_parts.append("## 上游节点结果")
            for up in dag_state["upstream_results"][nid]:
                up_node = node_map[up["from"]]
                result_text = up_node.get("_result", "") or "(无输出)"
                context_parts.append(f"### {up['from']} ({up_node.get('agent', '')})\n{result_text}")
        if node.get("context"):
            context_parts.append(node["context"])
        full_context = "\n\n".join(context_parts) if context_parts else ""

        llm_config = self._get_llm_config()
        if not isinstance(llm_config, dict):
            llm_config = {}

        agent_name = node.get("agent", "")
        task_description = node.get("description", "")

        agent = self._agent_manager.get_agent(agent_name)
        # 轮数上限：agent.steps 显式声明优先（None=激活策略兜底，默认 subagent 策略 30）
        max_iterations = agent.steps if agent else None

        executor = SubAgentExecutor(
            task_id=task_id,
            agent_name=agent_name,
            task_description=task_description,
            llm_config=llm_config,
            agent_manager=self._agent_manager,
            tool_executor=self._tool_executor,
            parent_context=full_context,
            is_subagent_call=True,
            max_iterations=max_iterations,
        )
        executor._task_session_id = task_session_id

        if self._session_store:
            executor.set_log_store_callback(
                lambda *args, _sid=task_session_id: self._save_task_to_store(*args, session_id=_sid)
            )
        if self._get_history_messages:
            executor.set_history_getter(self._get_history_messages)

        # 节点完成回调
        # 【第N次修复】不在 _start_dag_node 中直接连接 finished_with_result，
        # 而是延后到 task_started 信号处理过程中连接（与 UI 回调完全一致）。
        # 原因：PySide6 对在嵌套信号上下文（_start_dag_node 被 _check_dag_downstream
        # 调用，_check_dag_downstream 被 finished_with_result 信号处理器调用）中
        # 创建的 lambda 连接可能有微妙行为差异，导致 callback 被静默丢弃。
        # 通过在这里只记录元数据，在 _on_dag_task_started_slot 中真正连接，
        # 确保与 UI 回调完全相同的连接时序。
        pass  # ← 实际连接在 _on_dag_task_started_slot 中完成
        # 节点出错回调（补充 finished_with_result 的缺失路径）
        # 同理，error 回调也在 task_started 处理中进行连接
        pass  # ← 实际连接在 _on_dag_task_started_slot 中完成

        self._running_tasks[task_id] = executor
        node["_status"] = "running"
        executor.start()

        logger.info(f"[DAG] 节点已启动: dag_id={dag_id}, nid={nid}, agent={node.get('agent')}, task_id={task_id}")

        # 【关键修复】将 _save_task_to_store 和 task_started.emit 都包裹在 try/except 中
        # 如果其中任何一个抛异常（比如 UI 回调 _on_sub_agent_task_started 失败），
        # executor 已经在后台运行，异常冒泡会让 execute_dag 整体失败，
        # 导致 LLM 收到错误而不会继续等待 DAG 完成。
        # 但 executor 已经在子线程中运行了，它的 finished_with_result 迟早会发射。
        # 所以这里必须吞噬异常，让 DAG 的正常流程不被破坏。
        try:
            self._save_task_to_store(task_id, agent_name, task_description, "running", session_id=task_session_id)
        except Exception as e:
            logger.error(f"[DAG] _save_task_to_store 失败: {e}", exc_info=True)
        try:
            self.task_started.emit(task_id, agent_name, task_description)
        except Exception as e:
            logger.error(f"[DAG] task_started.emit 失败 (UI 回调异常): {e}", exc_info=True)

    def _on_dag_task_started_slot(self, task_id: str, agent_name: str, task_description: str):
        """
        当 task_started 信号发射时（_start_dag_node 末尾），在此完成 DAG 回调的连接。

        关键设计：不直接在 _start_dag_node 中连接 DAG 回调，而是延后到
        task_started 的信号处理过程中。这与 UI 回调（_on_sub_agent_task_started）
        完全相同的连接时机，消除了 PySide6 对嵌套信号上下文中创建 lambda 的潜在
        行为差异（这种差异会导致 DAG 回调被静默丢弃而 UI 回调正常工作）。
        """
        # 只处理属于 DAG 的任务（在 _task_to_dag 中有记录的）
        if not hasattr(self, "_task_to_dag") or task_id not in self._task_to_dag:
            return
        dag_id, nid = self._task_to_dag[task_id]

        # 获取 executor（此时一定在 _running_tasks 中，因为 _start_dag_node 在
        # task_started.emit 之前已经 self._running_tasks[task_id] = executor）
        executor = self._running_tasks.get(task_id)
        if not executor:
            logger.warning(f"[DAG] _on_dag_task_started_slot: executor not found for task_id={task_id[:8]}")
            return

        # 连接 DAG 回调 —— ⚠️ 必须连接 bound method（receiver=self=Manager，主线程），
        # 禁止连接裸 lambda：lambda 的 receiver 归属 sender（executor），而 executor
        # 在 ChatWorker 子线程创建（thread affinity 在子线程），emit 时 Auto 判定
        # queued 到子线程 —— 工作线程无 Qt 事件循环，回调永不执行（DAG 永远卡在等节点）。
        # bound method receiver=Manager（主线程）→ queued 必投主线程事件循环。
        # dag_id/nid 通过 task_id 反查 _task_to_dag（信号参数自带 task_id）。
        executor.finished_with_result.connect(self._on_dag_executor_finished)
        executor.error_occurred.connect(self._on_dag_executor_error)
        logger.info(f"[DAG] 🔗 DAG callbacks connected for nid={nid} (via task_started slot)")

    def _dag_route(self, task_id: str):
        """按 task_id 反查 DAG 路由信息（dag_id, nid）；不在映射中返回 None"""
        return getattr(self, "_task_to_dag", {}).get(task_id)

    def _on_dag_executor_finished(self, task_id: str, result: str):
        """executor finished_with_result → DAG 节点完成（queued 回主线程后执行）"""
        route = self._dag_route(task_id)
        if not route:
            return
        dag_id, nid = route
        self._safe_dag_node_finished(dag_id, nid, task_id, result)

    def _on_dag_executor_error(self, task_id: str, error: str):
        """executor error_occurred → DAG 节点失败（queued 回主线程后执行）"""
        route = self._dag_route(task_id)
        if not route:
            return
        dag_id, nid = route
        self._safe_dag_node_error(dag_id, nid, task_id, error)

    def _safe_dag_node_finished(self, dag_id: str, nid: str, task_id: str, result: str):
        """
        DAG 完成回调的安全包装 —— 关键作用：
        1. 在 lambda 边界上 100% 捕获异常，**不让任何异常抛给 PySide6**。
           PySide6 在某些版本下，如果 slot 抛异常会自动 disconnect，
           这会让下游节点永远不被启动。
        2. 添加诊断日志，确认这个 lambda 真的被发射信号触发了。
        """
        logger.info(f"[DAG] 🔥 DAG finished callback FIRED: nid={nid}, task_id={task_id[:8]}")
        try:
            self._on_dag_node_finished(dag_id, nid, task_id, result)
        except BaseException as e:
            logger.error(
                f"[DAG] ❌ _on_dag_node_finished 抛异常被吞噬: nid={nid}, err={e}",
                exc_info=True,
            )

    def _safe_dag_node_error(self, dag_id: str, nid: str, task_id: str, error: str):
        """DAG 错误回调的安全包装（防 PySide6 异常自动断开连接）"""
        logger.info(f"[DAG] 🔥 DAG error callback FIRED: nid={nid}, task_id={task_id[:8]}, error={error[:50]}")
        try:
            self._on_dag_node_error(dag_id, nid, task_id, error)
        except BaseException as e:
            logger.error(
                f"[DAG] ❌ _on_dag_node_error 抛异常被吞噬: nid={nid}, err={e}",
                exc_info=True,
            )

    def _on_dag_node_finished(self, dag_id: str, nid: str, task_id: str, result: str):
        """DAG 节点执行完成（正常路径）

        注意：不在此处 emit task_finished，由 _on_sub_agent_task_started 连接的
        UI 回调路径（finished_with_result → _on_sub_agent_finished → task_finished）
        统一触发批次数和 _finished_tasks 写入，避免双重计数。
        """
        # 【关键诊断】在 dag_state 检查之前先记录，证明这个方法被实际调用了
        logger.info(
            f"[DAG] 🔥 _on_dag_node_finished ENTERED: dag_id={dag_id}, nid={nid}, task_id={task_id[:8]}, has_dag_state={dag_id in getattr(self, '_dag_states', {})}"
        )
        dag_state = self._dag_states.get(dag_id)
        if not dag_state:
            logger.warning(f"[DAG] _on_dag_node_finished: dag_state 已为空! dag_id={dag_id}, nid={nid}")
            return
        node_map = dag_state["node_map"]
        node = node_map[nid]

        # 更新节点状态
        executor = self._running_tasks.get(task_id)
        error = getattr(executor, "_execution_error", None) if executor else None
        node["_status"] = "failed" if error else "completed"
        node["_result"] = result
        node["_error"] = error or ""
        logger.info(
            f"[DAG] 节点完成: dag_id={dag_id}, nid={nid}, status={node['_status']}, has_downstream={bool(dag_state['adj'].get(nid))}"
        )

        # 检查下游节点（用 try/except 包裹，防止因下游节点启动异常导致本节点状态和 all_done 检查被跳过）
        try:
            self._check_dag_downstream(dag_id, nid)
        except Exception as e:
            logger.error(f"[DAG] _on_dag_node_finished: 检查下游节点失败: {e}", exc_info=True)

        # 检查是否全部完成
        all_done = all(node_map[nid]["_status"] in ("completed", "failed", "skipped", "cancelled") for nid in node_map)
        if all_done:
            # DAG 整体完成，清理 _task_to_dag 映射
            if hasattr(self, "_task_to_dag"):
                for n in node_map.values():
                    tid = n.get("_task_id", "")
                    if tid:
                        self._task_to_dag.pop(tid, None)
            self._dag_states.pop(dag_id, None)

    def _on_dag_node_error(self, dag_id: str, nid: str, task_id: str, error: str):
        """DAG 节点执行出错（error_occurred 路径）

        当 executor 遇到未预期异常（agent 不存在、LLM 配置无效等），
        只 emit error_occurred 而不 emit finished_with_result。
        此方法确保：
        1. 节点状态标记为 failed
        2. 写入 _finished_tasks（UI 回调不会触发）
        3. 触发 task_finished，让批次计数器和 UI 更新
        4. 级联跳过下游节点
        5. 检查 DAG 是否全部完成
        """
        dag_state = self._dag_states.get(dag_id)
        if not dag_state:
            logger.warning(f"[DAG] _on_dag_node_error: dag_state 已为空! dag_id={dag_id}, nid={nid}, error={error}")
            return
        node_map = dag_state["node_map"]
        node = node_map[nid]

        node["_status"] = "failed"
        node["_result"] = ""
        node["_error"] = error or "节点执行失败"
        logger.info(f"[DAG] 节点出错: dag_id={dag_id}, nid={nid}, error={error}")

        # 写入 _finished_tasks（此路径没有 UI 回调，必须手动写）
        self._finished_tasks[task_id] = {
            "result": "",
            "error": error or "节点执行失败",
            "agent_name": node.get("agent", ""),
            "task_description": node.get("description", ""),
            "session_id": dag_state["session_id"],
        }

        # 触发 task_finished（让批次计数器和 UI 紧凑卡片更新）
        try:
            self.task_finished.emit(task_id, "")
        except Exception as e:
            logger.error(f"[DAG] task_finished.emit 失败 (_on_dag_node_error path): {e}")

        # 级联跳过下游节点（用 try/except 包裹，防止因异常导致状态检查和 cleanup 被跳过）
        try:
            self._check_dag_downstream(dag_id, nid)
        except Exception as e:
            logger.error(f"[DAG] _on_dag_node_error: 级联跳过下游节点失败: {e}", exc_info=True)

        # 检查是否全部完成
        all_done = all(node_map[nid]["_status"] in ("completed", "failed", "skipped", "cancelled") for nid in node_map)
        if all_done:
            # DAG 整体完成，清理 _task_to_dag 映射
            if hasattr(self, "_task_to_dag"):
                for n in node_map.values():
                    tid = n.get("_task_id", "")
                    if tid:
                        self._task_to_dag.pop(tid, None)
            self._dag_states.pop(dag_id, None)

    def _check_dag_downstream(self, dag_id: str, nid: str):
        """DAG 节点完成后，检查并启动下游节点"""
        dag_state = self._dag_states.get(dag_id)
        if not dag_state:
            logger.warning(f"[DAG] _check_dag_downstream: dag_state 已为空! dag_id={dag_id}, nid={nid}")
            return
        adj = dag_state.get("adj", {})
        adj_neighbors = list(adj.get(nid, []))
        logger.info(
            f"[DAG] 检查下游: dag_id={dag_id}, nid={nid}, downstream_nodes={adj_neighbors}, adj_keys={list(adj.keys())}"
        )
        if not adj_neighbors:
            return

        in_degree = dag_state["in_degree"]
        upstream_results = dag_state["upstream_results"]
        node_map = dag_state["node_map"]
        task_session_id = dag_state.get("session_id", "")

        for neighbor in adj_neighbors:
            try:
                # 【关键修复】整个邻居处理逻辑都用 try/except 包裹，
                # 否则若 upstream_results/in_degree 中缺 key（比如 adj 包含未注册的 nid），
                # 异常会一路冒到 _on_dag_node_finished，被静默吞噬，导致
                # 下游节点既不在 _running_tasks 也不在 _finished_tasks，呈现"unknown"
                upstream_results[neighbor].append({"from": nid})
                in_degree[neighbor] -= 1
                new_degree = in_degree[neighbor]
                logger.info(f"[DAG]   下游 {neighbor}: in_degree -> {new_degree}")
                if new_degree == 0:
                    logger.info(f"[DAG]   启动下游节点: {neighbor}")
                    self._start_dag_node(dag_id, neighbor)
            except KeyError as e:
                # 邻接表和入度表数据不一致：adj 有这个 neighbor，但
                # upstream_results / in_degree 中没有。可能是 LLM 生成的 edges
                # 包含 nodes 中不存在的 id（理论上被 execute_dag 校验拦截，但兜底）
                logger.error(
                    f"[DAG] ❌ 下游 {neighbor} 数据不一致 (KeyError: {e})，"
                    f"adj={list(adj.keys())}, in_degree_keys={list(in_degree.keys())}",
                    exc_info=True,
                )
                if neighbor in node_map:
                    node = node_map[neighbor]
                    node["_status"] = "failed"
                    node["_error"] = f"数据不一致: KeyError {e}"
                    task_id = node.get("_task_id", "")
                    if task_id:
                        self._finished_tasks[task_id] = {
                            "result": "",
                            "error": node["_error"],
                            "agent_name": node.get("agent", ""),
                            "task_description": node.get("description", ""),
                            "session_id": task_session_id,
                        }
                        try:
                            self.task_finished.emit(task_id, "")
                        except Exception as e:
                            logger.error(f"[DAG] task_finished.emit 失败 (KeyError cascade): {e}")
                    # 跳过该节点后继续级联它的下游
                    try:
                        self._check_dag_downstream(dag_id, neighbor)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(
                    f"[DAG] ❌ 启动/处理下游节点 {neighbor} 失败: {e}",
                    exc_info=True,
                )
                if neighbor in node_map:
                    node = node_map[neighbor]
                    node["_status"] = "failed"
                    node["_error"] = f"启动失败: {e}"
                    task_id = node.get("_task_id", "")
                    if task_id:
                        self._finished_tasks[task_id] = {
                            "result": "",
                            "error": node["_error"],
                            "agent_name": node.get("agent", ""),
                            "task_description": node.get("description", ""),
                            "session_id": task_session_id,
                        }
                        try:
                            self.task_finished.emit(task_id, "")
                        except Exception as e:
                            logger.error(f"[DAG] task_finished.emit 失败 (Exception cascade): {e}")
                    # 跳过该节点后继续级联它的下游
                    try:
                        self._check_dag_downstream(dag_id, neighbor)
                    except Exception:
                        pass

    def _build_dag_echarts_json(self, nodes: List[Dict], edges: List[Dict], node_map: Dict) -> str:
        """
        根据 DAG 生成 ECharts 力导向图 JSON。

        节点颜色按状态区分：
        - pending:   #FFC107 (黄)
        - running:   #2196F3 (蓝)
        - completed: #4CAF50 (绿)
        - failed:    #F44336 (红)
        - skipped:   #9E9E9E (灰)
        """
        status_categories = [
            {
                "name": "pending",
                "itemStyle": {
                    "color": "#FFC107",
                    "borderColor": "#d4a020",
                    "borderWidth": 2,
                    "shadowBlur": 8,
                    "shadowColor": "rgba(255,193,7,0.4)",
                },
            },
            {
                "name": "running",
                "itemStyle": {
                    "color": "#2196F3",
                    "borderColor": "#1976D2",
                    "borderWidth": 2,
                    "shadowBlur": 8,
                    "shadowColor": "rgba(33,150,243,0.4)",
                },
            },
            {
                "name": "completed",
                "itemStyle": {
                    "color": "#4CAF50",
                    "borderColor": "#388E3C",
                    "borderWidth": 2,
                    "shadowBlur": 8,
                    "shadowColor": "rgba(76,175,80,0.4)",
                },
            },
            {
                "name": "failed",
                "itemStyle": {
                    "color": "#F44336",
                    "borderColor": "#D32F2F",
                    "borderWidth": 2,
                    "shadowBlur": 8,
                    "shadowColor": "rgba(244,67,54,0.4)",
                },
            },
            {
                "name": "skipped",
                "itemStyle": {
                    "color": "#9E9E9E",
                    "borderColor": "#757575",
                    "borderWidth": 2,
                    "shadowBlur": 8,
                    "shadowColor": "rgba(158,158,158,0.4)",
                },
            },
        ]
        status_map = {c["name"]: i for i, c in enumerate(status_categories)}

        echarts_nodes = []
        for n in nodes:
            nid = n["id"]
            node_info = node_map[nid]
            status = node_info["_status"]
            agent = n.get("agent", "")
            desc = n.get("description", "")
            # 用 agent 名作为节点显示名，tooltip 显示完整描述
            echarts_nodes.append(
                {
                    "id": nid,
                    "name": f"{nid} ({agent})",
                    "symbolSize": 50,
                    "category": status_map.get(status, 4),
                    "draggable": True,
                    "description": desc[:100],
                }
            )

        echarts_edges = []
        for e in edges:
            echarts_edges.append(
                {
                    "source": e["from"],
                    "target": e["to"],
                    "lineStyle": {"width": 2, "opacity": 0.6, "curveness": 0.15},
                }
            )

        chart_config = {
            "title": {
                "text": "子智能体工作流",
                "left": "center",
                "textStyle": {"fontSize": 16, "fontWeight": "bold", "color": "#ccc"},
            },
            "tooltip": {
                "trigger": "item",
                "formatter": "{b}",
            },
            "series": [
                {
                    "type": "graph",
                    "layout": "force",
                    "symbolSize": 50,
                    "roam": True,
                    "draggable": True,
                    "focusNodeAdjacency": True,
                    "edgeSymbol": ["none", "arrow"],
                    "edgeSymbolSize": [0, 8],
                    "label": {
                        "show": True,
                        "position": "bottom",
                        "fontSize": 11,
                        "fontWeight": "bold",
                        "color": "#ccc",
                        "offset": [0, 6],
                    },
                    "lineStyle": {
                        "color": "source",
                        "curveness": 0.15,
                        "width": 1.5,
                        "opacity": 0.6,
                    },
                    "force": {
                        "repulsion": 500,
                        "edgeLength": [80, 200],
                        "layoutAnimation": True,
                        "friction": 0.1,
                        "gravity": 0.05,
                    },
                    "categories": status_categories,
                    "data": echarts_nodes,
                    "links": echarts_edges,
                    "emphasis": {
                        "focus": "adjacency",
                        "lineStyle": {"width": 3},
                    },
                    "blur": {"opacity": 0.2},
                    "animation": True,
                    "animationDuration": 1000,
                    "animationEasing": "cubicOut",
                }
            ],
        }

        return json.dumps(chart_config, option=orjson.OPT_INDENT_2).decode("utf-8")

    def cancel_task(self, task_id: str) -> bool:
        """取消子智能体任务"""
        if task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            self._notify_dag_task_failed(task_id, "Task cancelled by user")
            del self._running_tasks[task_id]
            return True
        return False

    def _notify_dag_task_failed(self, task_id: str, error_msg: str):
        """
        当一个 task 被取消/超时清理时，反向通知对应的 DAG 节点

        否则如果 executor 在 _is_cancelled 后没来得及发射信号就被从 _running_tasks 移除，
        DAG 会永远卡在"等这个节点完成"。
        """
        if not hasattr(self, "_task_to_dag"):
            return
        info = self._task_to_dag.pop(task_id, None)
        if not info:
            return
        dag_id, nid = info
        if not hasattr(self, "_dag_states") or dag_id not in self._dag_states:
            return
        dag_state = self._dag_states[dag_id]
        node = dag_state.get("node_map", {}).get(nid)
        if not node or node.get("_status") != "running":
            return

        logger.warning(
            f"[DAG] 任务 {task_id} (nid={nid}, dag_id={dag_id}) 已被清理但节点仍是 running，"
            f"标记为 failed 并级联: {error_msg}"
        )
        node["_status"] = "failed"
        node["_error"] = error_msg
        task_session_id = dag_state.get("session_id", "")
        # 写入 _finished_tasks 以便 subagent_status 能查到
        if task_id not in self._finished_tasks:
            self._finished_tasks[task_id] = {
                "result": "",
                "error": error_msg,
                "agent_name": node.get("agent", ""),
                "task_description": node.get("description", ""),
                "session_id": task_session_id,
            }
        # 触发 task_finished 让批次计数器能继续
        try:
            self.task_finished.emit(task_id, "")
        except Exception as e:
            logger.error(f"[DAG] task_finished.emit 失败 (_notify_dag path): {e}")
        # 级联：触发该节点的下游处理
        try:
            self._check_dag_downstream(dag_id, nid)
        except Exception as e:
            logger.error(f"[DAG] 通知任务失败时级联异常: {e}", exc_info=True)

    def get_running_tasks(self) -> List[str]:
        """获取正在运行的任务ID列表"""
        return list(self._running_tasks.keys())

    def get_finished_tasks(self) -> List[str]:
        """获取已完成的任务ID列表（清理已完成的从running列表）"""
        finished = []
        for task_id in list(self._running_tasks.keys()):
            executor = self._running_tasks[task_id]
            if executor.isFinished():
                # 通过公共属性获取结果
                result = executor.last_result or ""
                error = executor.execution_error or ""
                agent_name = executor.agent_name
                task_description = executor.task_description
                logs = executor.get_logs()
                tool_call_count = executor.tool_call_count
                elapsed = int(time.time() - executor.start_time) if executor.start_time else 0

                task_session_id = getattr(executor, "_task_session_id", self._current_session_id)
                # 如果 _on_sub_agent_task_finished 已经写入过，避免覆盖已有字段
                if task_id not in self._finished_tasks:
                    self._finished_tasks[task_id] = {
                        "result": result,
                        "error": error,
                        "agent_name": agent_name,
                        "task_description": task_description,
                        "session_id": task_session_id,
                        "logs": logs,
                        "tool_call_count": tool_call_count,
                        "elapsed_seconds": elapsed,
                    }
                else:
                    # 更新关键字段，保留 session_id 等已有数据
                    self._finished_tasks[task_id].setdefault("session_id", task_session_id)
                    self._finished_tasks[task_id].setdefault("logs", logs)
                    self._finished_tasks[task_id].setdefault("tool_call_count", tool_call_count)
                    self._finished_tasks[task_id].setdefault("elapsed_seconds", elapsed)
                    # 总是更新 result/error（_on_sub_agent_task_finished 可能拿到更准的数据）
                    if result is not None:
                        self._finished_tasks[task_id]["result"] = result
                    if error is not None:
                        self._finished_tasks[task_id]["error"] = error

                # 更新数据库（传入锁定的 session_id）
                self._save_task_to_store(
                    task_id, agent_name, task_description, "finished", result, error, session_id=task_session_id
                )

                # 【安全网】如果这个 task 是一个 DAG 节点，但 DAG 回调没触发（节点还是 running），
                # 手动触发 DAG cascade（否则 DAG 永远卡在第一层）
                if hasattr(self, "_task_to_dag") and task_id in self._task_to_dag:
                    dag_id, nid = self._task_to_dag[task_id]
                    dag_state = getattr(self, "_dag_states", {}).get(dag_id)
                    if dag_state and dag_state.get("node_map", {}).get(nid, {}).get("_status") == "running":
                        logger.warning(
                            f"[DAG] ⚠️ get_finished_tasks 发现 DAG 节点 {nid} 已完成但节点状态仍是 running，"
                            f"手动触发 _on_dag_node_finished (task_id={task_id[:8]})"
                        )
                        try:
                            self._on_dag_node_finished(dag_id, nid, task_id, result)
                        except BaseException as e:
                            logger.error(f"[DAG] get_finished_tasks 手动触发 DAG 完成失败: {e}", exc_info=True)

                del self._running_tasks[task_id]
                finished.append(task_id)
        return finished

    def cleanup_dead_tasks(self, timeout_seconds: int = 300) -> List[str]:
        """
        清理卡死的任务（运行时间超过 timeout_seconds 的任务）

        Returns: 已清理的任务ID列表
        """
        import time

        cleaned = []
        now = time.time()

        for task_id in list(self._running_tasks.keys()):
            executor = self._running_tasks[task_id]
            start_time = executor.start_time

            if start_time and (now - start_time) > timeout_seconds:
                logger.warning(f"[SubAgentManager] Task {task_id} dead for {now - start_time}s, cancelling")
                executor.cancel()
                agent_name = executor.agent_name
                task_description = executor.task_description
                logs = executor.get_logs()
                task_session_id = getattr(executor, "_task_session_id", self._current_session_id)
                error_msg = f"Task cancelled due to timeout ({timeout_seconds}s)"
                self._finished_tasks[task_id] = {
                    "result": "",
                    "error": error_msg,
                    "agent_name": agent_name,
                    "task_description": task_description,
                    "session_id": task_session_id,
                    "logs": logs,
                }
                # 更新数据库（传入锁定的 session_id）
                self._save_task_to_store(
                    task_id, agent_name, task_description, "timeout", "", error_msg, session_id=task_session_id
                )
                del self._running_tasks[task_id]
                # 【关键修复】通知 DAG：被超时清理的任务如果属于某个 DAG 节点，标记为 failed 并级联
                self._notify_dag_task_failed(task_id, error_msg)
                cleaned.append(task_id)

        return cleaned

    def cancel_all(self):
        """取消所有运行中的子智能体任务 + 停止 Stall 检测器

        用于窗口关闭时清理当前窗口的所有子智能体任务，防止线程泄漏。
        """
        # 先停止 stall 检测器，避免在取消过程中触发额外的回调
        self.stop_stall_detector()

        # 取消所有运行中的任务
        for task_id in list(self._running_tasks.keys()):
            try:
                executor = self._running_tasks[task_id]
                agent_name = executor.agent_name
                task_description = executor.task_description
                logs = executor.get_logs()
                task_session_id = getattr(executor, "_task_session_id", self._current_session_id)

                # 标记取消（设置 _is_cancelled 标志，线程在下一次检查点退出）
                executor.cancel()

                # 写入 finished_tasks（与 _check_stalled_tasks / cancel_task 一致）
                error_msg = "Task cancelled: window closed"
                self._finished_tasks[task_id] = {
                    "result": "",
                    "error": error_msg,
                    "agent_name": agent_name,
                    "task_description": task_description,
                    "session_id": task_session_id,
                    "logs": logs,
                }
                # 保存到数据库
                self._save_task_to_store(
                    task_id,
                    agent_name,
                    task_description,
                    "cancelled",
                    "",
                    error_msg,
                    logs,
                    session_id=task_session_id,
                )
                # 通知 DAG（如果有）
                self._notify_dag_task_failed(task_id, error_msg)
                # 发送完成信号让 UI 知道
                try:
                    self.task_finished.emit(task_id, "")
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[SubAgentManager] cancel_all: 取消任务 {task_id} 时出错: {e}")

        self._running_tasks.clear()

    def get_task_result(self, task_id: str) -> Dict:
        """获取指定任务的执行结果"""
        return self._finished_tasks.get(task_id, {"result": "", "error": ""})

    def get_task_logs(self, task_id: str) -> Dict:
        """
        获取指定任务的完整日志和摘要。

        Returns:
            Dict: {
                "summary": {...},  # 任务摘要
                "logs": [...],      # 日志列表
                "found": bool       # 是否找到任务
            }
        """
        # 先从数据库获取
        if self._session_store:
            db_task = self._session_store.get_subagent_task(task_id)
            if db_task:
                summary = db_task.get("summary", {})
                # 确保 task_description 在 summary 中
                if not summary.get("task_description"):
                    summary["task_description"] = db_task.get("task_description", "")
                # 确保 task_id 在 summary 中
                if not summary.get("task_id"):
                    summary["task_id"] = task_id
                return {
                    "task_id": task_id,
                    "summary": summary,
                    "logs": db_task.get("logs", []),
                    "found": True,
                    "status": db_task.get("status", "unknown"),
                    "agent_name": db_task.get("agent_name", ""),
                    "task_description": db_task.get("task_description", ""),
                    "session_id": db_task.get("session_id", ""),
                    "result": db_task.get("result", ""),
                    "error": db_task.get("error", ""),
                }

        # 清理并检查内存中的任务
        self.get_finished_tasks()

        # 检查运行中的任务
        if task_id in self._running_tasks:
            executor = self._running_tasks[task_id]
            return {
                "summary": executor.get_summary(),
                "logs": executor.get_logs(),
                "found": True,
                "status": "running",
                "session_id": getattr(executor, "_task_session_id", ""),
            }

        # 检查已完成的任务（内存）
        if task_id in self._finished_tasks:
            task_info = self._finished_tasks[task_id]
            return {
                "summary": {
                    "task_id": task_id,
                    "agent_name": task_info.get("agent_name", ""),
                    "task_description": task_info.get("task_description", ""),
                    "result": task_info.get("result", ""),
                    "error": task_info.get("error", ""),
                    "tool_call_count": task_info.get("tool_call_count", 0),
                    "elapsed_seconds": task_info.get("elapsed_seconds", 0),
                },
                "logs": task_info.get("logs", []),
                "found": True,
                "status": "finished",
                "session_id": task_info.get("session_id", ""),
            }

        return {"summary": {}, "logs": [], "found": False, "status": "unknown"}

    def get_all_task_logs(self) -> List[Dict]:
        """
        获取所有任务的日志（运行中和已完成的）。

        Returns:
            List[Dict]: 每个任务的日志信息列表
        """
        results = []
        self.get_finished_tasks()  # 先清理

        # 收集运行中的任务
        for task_id, executor in self._running_tasks.items():
            results.append(
                {
                    "task_id": task_id,
                    "summary": executor.get_summary(),
                    "logs": executor.get_logs(),
                    "status": "running",
                }
            )

        # 收集已完成的任务
        for task_id, task_info in self._finished_tasks.items():
            results.append(
                {
                    "task_id": task_id,
                    "summary": {
                        "task_id": task_id,
                        "agent_name": task_info.get("agent_name", ""),
                        "task_description": task_info.get("task_description", ""),
                        "result": task_info.get("result", ""),
                        "error": task_info.get("error", ""),
                        "tool_call_count": task_info.get("tool_call_count", 0),
                        "elapsed_seconds": task_info.get("elapsed_seconds", 0),
                    },
                    "logs": task_info.get("logs", []),
                    "status": "finished",
                }
            )

        return results

    def get_tasks_status(self, task_ids: List[str], session_id: str = None) -> ToolResult:
        """获取指定任务的状态（会话隔离）

        Args:
            session_id: 可选，传入时只返回属于该会话的任务
        """
        effective_session = session_id if session_id else self._current_session_id
        tasks_info = []
        for tid in task_ids:
            if tid in self._running_tasks:
                executor = self._running_tasks[tid]
                # 会话隔离：检查 running 任务的 session
                if effective_session:
                    task_session = getattr(executor, "_task_session_id", "")
                    if task_session != effective_session:
                        continue
                tasks_info.append(
                    {
                        "task_id": tid,
                        "status": "running" if executor.isRunning() else "finishing",
                        "agent": executor.agent_name,
                    }
                )
            elif tid in self._finished_tasks:
                task_info = self._finished_tasks[tid]
                task_session = task_info.get("session_id", "")
                # 会话隔离：只返回当前会话的任务
                # 注意: 当 task_session 为空（旧记录/边缘情况）时，也视为不属于当前会话
                if effective_session and task_session != effective_session:
                    continue
                tasks_info.append(
                    {
                        "task_id": tid,
                        "status": "finished",
                        "agent": task_info.get("agent_name", ""),
                    }
                )
            # 其他情况（unknown）不返回，隐藏不存在或不属于当前会话的任务
        return ToolResult(True, content={"tasks": tasks_info})

    def get_tasks_status_with_details(
        self, task_ids: List[str], with_log: bool = False, with_result: bool = True, session_id: str = None
    ) -> ToolResult:
        """获取指定任务的详细状态（按 task_id 精确查询，无会话限制）

        与 get_all_active_tasks_with_details 不同，本方法用于按 explicit task_id
        查询特定任务，因此不执行会话隔离。只要 task_id 存在就能查到结果。

        Args:
            session_id: 保留参数（不再用于会话隔离），兼容历史调用方
        """
        tasks_info = []
        for tid in task_ids:
            task_data = self.get_task_logs(tid)
            if not task_data.get("found"):
                tasks_info.append(
                    {
                        "task_id": tid,
                        "status": "unknown",
                        "agent": "",
                    }
                )
                continue

            status = task_data.get("status", "unknown")
            task_info = {
                "task_id": tid,
                "status": status,
                "agent": task_data.get("summary", {}).get("agent_name", task_data.get("agent_name", "")),
            }

            # 显式按 ID 查询始终返回完整结果（不应用 _already_queried 限制）
            # _already_queried 仅在 get_all_active_tasks_with_details 无条件返回时生效

            # running / finishing：任务还没结束，注入 _hint 提醒调用方不要重复查询
            if status in ("running"):
                summary = task_data.get("summary", {}) or {}
                elapsed = summary.get("elapsed_seconds", 0) or 0
                tool_calls = summary.get("tool_call_count", 0) or 0
                task_info["elapsed_seconds"] = elapsed
                task_info["tool_call_count"] = tool_calls
                task_info["_hint"] = (
                    f"⏳ 该任务还在后台运行中（已用时 {elapsed}s，已调用 {tool_calls} 次工具）。"
                    "**请勿重复调用 subagent_status 等待结果**——"
                    "任务完成后系统会自动通过 `[后台任务状态]` 用户消息通知，"
                    "届时再调用本工具获取详细结果。"
                )

            # 是否包含结果
            if with_result:
                result = task_data.get("result") or task_data.get("summary", {}).get("result", "")
                error = task_data.get("error") or task_data.get("summary", {}).get("error", "")
                if result:
                    task_info["result"] = result
                if error:
                    task_info["error"] = error

            # 是否包含日志
            if with_log:
                logs = task_data.get("logs", [])
                if logs:
                    task_info["logs"] = logs

            tasks_info.append(task_info)

        return ToolResult(True, content={"tasks": tasks_info})

    def get_all_active_tasks_with_details(
        self, with_log: bool = False, with_result: bool = True, session_id: str = None
    ) -> ToolResult:
        """获取当前会话任务的详细状态（会话隔离），同时包含运行中与已完成。

        Args:
            with_log: 是否包含执行日志
            with_result: 是否包含执行结果
            session_id: 会话 ID，None 时使用 _current_session_id

        Returns:
            ToolResult: content={"tasks": [...]}
                - finished 任务：按"会话内只查一次"策略返回完整 result/error
                - running/finishing 任务：附带 _hint 提醒调用方不要重复查询
        """
        target_session = session_id or self._current_session_id
        tasks_info = []

        for task_id, task_info in self._finished_tasks.items():
            # 会话隔离：只返回当前会话的任务
            task_session = task_info.get("session_id", "")
            # 注意: 当 task_session 为空（旧记录/边缘情况）时，也视为不属于当前会话
            if target_session and task_session != target_session:
                continue

            # 跳过当前正在运行的任务（它们应该由 _running_tasks 处理）
            if task_id in self._running_tasks:
                continue

            task_entry = {
                "task_id": task_id,
                "session_id": task_session,
                "status": "finished",
                "agent": task_info.get("agent_name", ""),
                "task_description": task_info.get("task_description", ""),
            }

            # 完成或失败只能查一次（按 session 隔离）
            session_queried = self._queried_tasks.get(target_session, set())
            if task_id in session_queried:
                task_entry["_already_queried"] = True
                task_entry["_message"] = "已查询过结果，可通过 id 再次查询"
                tasks_info.append(task_entry)
                continue

            session_queried.add(task_id)
            self._queried_tasks[target_session] = session_queried

            if with_result:
                result = task_info.get("result", "") or ""
                error = task_info.get("error", "") or ""
                if result:
                    task_entry["result"] = result
                if error:
                    task_entry["error"] = error
            if with_log:
                logs = task_info.get("logs", [])
                if logs:
                    task_entry["logs"] = logs

            tasks_info.append(task_entry)

        # 补充：运行中的任务也一并返回，附带 _hint 提醒调用方不要重复轮询
        for task_id, executor in self._running_tasks.items():
            # 会话隔离
            if target_session:
                task_session = getattr(executor, "_task_session_id", "")
                if task_session != target_session:
                    continue

            try:
                summary = executor.get_summary() or {}
            except Exception:
                summary = {}
            elapsed = summary.get("elapsed_seconds", 0) or 0
            tool_calls = summary.get("tool_call_count", 0) or 0
            status = "running" if executor.isRunning() else "finishing"

            task_entry = {
                "task_id": task_id,
                "session_id": getattr(executor, "_task_session_id", ""),
                "status": status,
                "agent": summary.get("agent_name", ""),
                "task_description": summary.get("task_description", ""),
                "elapsed_seconds": elapsed,
                "tool_call_count": tool_calls,
                "_hint": (
                    f"⏳ 该任务还在后台运行中（已用时 {elapsed}s，已调用 {tool_calls} 次工具）。"
                    "**请勿重复调用 subagent_status 等待结果**——"
                    "任务完成后系统会自动通过 `[后台任务状态]` 用户消息通知，"
                    "届时再调用本工具获取详细结果。"
                )
                if status == "running"
                else "",
            }

            if with_log:
                try:
                    logs = executor.get_logs()
                except Exception:
                    logs = []
                if logs:
                    task_entry["logs"] = logs

            tasks_info.append(task_entry)

        return ToolResult(True, content={"tasks": tasks_info})

    def get_all_active_tasks(self) -> ToolResult:
        """获取所有活跃任务"""
        tasks_info = []
        for task_id, executor in self._running_tasks.items():
            tasks_info.append(
                {
                    "task_id": task_id,
                    "status": "running" if executor.isRunning() else "finishing",
                    "agent": executor.agent_name,
                }
            )
        return ToolResult(True, content={"tasks": tasks_info})
