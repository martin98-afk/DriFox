# -*- coding: utf-8 -*-
"""
子智能体执行器 - 独立运行子智能体任务，避免共享超长上下文
"""
import orjson
import orjson as json
import re
import time
from typing import Dict, List, Optional, Any, Callable

from loguru import logger

from app.constants import PARAM_SCHEMA
from app.tools.result import ToolResult
from app.core.message_content import to_api_message
from app.core.tool_call_parser import smart_parse_arguments

from PyQt5.QtCore import QThread, pyqtSignal, QCoreApplication, QObject
from openai import OpenAI

from app.core.provider_profile import get_provider_profile
from app.core.model_capabilities import resolve_context_limit, resolve_max_output_tokens

# ========== 性能优化：预编译正则表达式 ==========
_THINKING_PATTERN = re.compile(r"<think>[\s\S]*?</think>")  # 过滤完整思考块
_TOOL_TAG_PATTERN = re.compile(r"<tool>[\s\S]*?</tool>")  # 过滤工具调用标签
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")  # 验证标识符格式

# ========== 上下文注入预算常量 ==========
CHARS_PER_TOKEN = 4  # 与 HistoryCompactor 保持一致
_CONTEXT_INJECTION_RATIO = 0.6  # 上下文注入最多占 budget 的 60%


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
        logger.warning(f"[SubAgentExecutor] _compute_context_budget received non-dict llm_config: {type(llm_config).__name__}, using defaults")
        return 96000  # 默认 128k * 0.75

    context_limit = resolve_context_limit(llm_config)
    max_output_tokens = resolve_max_output_tokens(llm_config)

    # O1/O3 模型需要更大的输出预留
    model_name = str(
        llm_config.get("模型名称", "") or llm_config.get("model", "")
    ).lower()
    reserved = min(800, max_output_tokens)
    if "o1" in model_name or "o3" in model_name:
        reserved = min(max_output_tokens, 32000)

    return max(500, context_limit - reserved)


class SubAgentExecutor(QThread):
    """子智能体执行器 - 独立线程运行子智能体任务"""

    finished_with_result = pyqtSignal(str, str)  # task_id, result
    error_occurred = pyqtSignal(str, str)  # task_id, error
    progress_updated = pyqtSignal(str, str)  # task_id, message
    tool_call_started = pyqtSignal(str, str, dict)  # task_id, tool_name, args
    tool_result_received = pyqtSignal(str, str, str, bool)  # task_id, tool_name, result, success

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
            max_iterations: int = 30,  # 最大迭代次数，默认 30
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
        self.max_iterations = max_iterations  # 最大迭代次数限制
        self._is_cancelled = False
        self._pending_answer = None
        self._last_result = None
        self._execution_error = None
        self._start_time: Optional[float] = None  # Unix timestamp, 访问用 @property
        # 日志存储: [{"type": "progress"|"thinking"|"ai_response"|"tool_call"|"tool_result"|"finish", "content": str, "timestamp": float}]
        self._logs: List[Dict] = []
        self._tool_call_count = 0
        self._log_store_callback = None  # 日志存储回调
        self._get_history_messages = None  # 获取主智能体历史消息的回调

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
        budget_ratio = getattr(
            agent, 'inherit_history_budget_ratio', _CONTEXT_INJECTION_RATIO
        ) or _CONTEXT_INJECTION_RATIO
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
                content_text = "".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
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
        log_entry = {
            "type": log_type,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            log_entry.update(extra)
        self._logs.append(log_entry)
        # 实时保存到数据库
        log_callback = getattr(self, '_log_store_callback', None)
        if log_callback:
            try:
                log_callback(self.task_id, self.agent_name, self.task_description, "running", None, None,
                             self._logs, self.get_summary())
            except Exception as e:
                logger.warning(f"[SubAgentExecutor] 实时保存日志失败: {e}")

    def get_logs(self) -> List[Dict]:
        """获取所有日志"""
        return self._logs.copy()

    def get_summary(self) -> dict:
        """获取任务摘要"""
        import time
        elapsed = int(time.time() - self._start_time) if self._start_time else 0
        return {
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "task_description": self.task_description,
            "tool_call_count": self._tool_call_count,
            "elapsed_seconds": elapsed,
            "result": self._last_result,
            "error": self._execution_error,
        }

    def run(self):
        import time
        self._start_time = time.time()

        try:
            # 防御：确保 llm_config 是 dict
            if not isinstance(self.llm_config, dict):
                logger.warning(f"[SubAgentExecutor] run() llm_config is not a dict: {type(self.llm_config).__name__}={self.llm_config!r}")
                self.llm_config = {}

            agent = self.agent_manager.get_agent(self.agent_name)
            if not agent:
                self.error_occurred.emit(self.task_id, f"Agent not found: {self.agent_name}")
                return

            # 子智能体被调用时 is_subagent_call=True，此时应该使用 subagent_constraints
            # 用于区分"主智能体通过 subagent_para 调用子智能体"的情况
            system_prompt = self.agent_manager.get_agent_system_prompt(
                self.agent_name, is_subagent_call=self.is_subagent_call
            )
            tools = self.agent_manager.get_agent_tools_schema(self.agent_name, is_subagent_call=self.is_subagent_call)

            # 基于 context budget 构建主智能体历史上下文注入（返回消息对象列表）
            inherited_messages = []
            if agent.inherit_history and getattr(self, '_get_history_messages', None):
                try:
                    history_messages = self._get_history_messages() or []
                    if history_messages:
                        # 根据 inherit_history_count 限制消息数量
                        if agent.inherit_history_count is not None:
                            history_messages = history_messages[-agent.inherit_history_count:]

                        inherited_messages = self._build_inherited_context(
                            agent, history_messages
                        )
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
                logger.warning(f"[SubAgentExecutor] Task {self.task_id} ({self.agent_name}) cancelled, emitting error_occurred to notify DAG")
                if self._log_store_callback:
                    try:
                        self._log_store_callback(self.task_id, self.agent_name, self.task_description, "cancelled",
                                                 "", "Task cancelled", self._logs, self.get_summary())
                    except Exception as e:
                        logger.warning(f"[SubAgentExecutor] 保存取消状态失败: {e}")
                self.error_occurred.emit(self.task_id, "Task cancelled")
                return

            # 直接使用执行结果（迭代结束时已自动总结）
            self._last_result = result if result else "无执行结果"

            # 任务完成，保存最终状态
            if self._log_store_callback:
                try:
                    self._log_store_callback(self.task_id, self.agent_name, self.task_description, "finished",
                                             self._last_result, None, self._logs, self.get_summary())
                except Exception as e:
                    logger.warning(f"[SubAgentExecutor] 保存完成状态失败: {e}")

            self.finished_with_result.emit(self.task_id, self._last_result)

        except Exception as e:
            logger.exception(f"[SubAgentExecutor] run() error: {e}")
            self._execution_error = str(e)
            # 任务出错，保存错误状态
            if self._log_store_callback:
                try:
                    self._log_store_callback(self.task_id, self.agent_name, self.task_description, "error", None,
                                             self._execution_error, self._logs, self.get_summary())
                except Exception as e:
                    logger.warning(f"[SubAgentExecutor] 保存错误状态失败: {e}")
            self.error_occurred.emit(self.task_id, f"SubAgent execution error: {str(e)}")

    def _execute_agent_loop(self, messages: List[Dict], tools: List[Dict], llm_config: Dict) -> str:
        """执行子智能体对话循环"""
        current_messages = messages.copy()
        response_content = ""
        current_reasoning = ""  # DeepSeek V4 thinking mode
        iteration_count = 0

        while not self._is_cancelled:
            if self._is_cancelled:
                return ""

            # 检查是否达到最大迭代次数（触发强制结束并总结）
            iteration_count += 1
            if iteration_count > self.max_iterations:
                self._add_log("progress", f"已达到最大迭代次数 ({self.max_iterations})，强制结束并总结")
                final_summary_prompt = self._build_final_summary_prompt()
                current_messages.append({"role": "user", "content": final_summary_prompt})
                response_content, _, _ = self._make_api_call(current_messages, None, llm_config)
                return self._filter_thinking_content(response_content)

            response_content, tool_calls, reasoning_content = self._make_api_call(
                current_messages, tools, llm_config
            )
            current_reasoning = reasoning_content

            # 记录大模型生成内容（thinking + ai_response），排除工具调用结果
            if current_reasoning:
                self._add_log("thinking", current_reasoning)
            if response_content:
                self._add_log("ai_response", response_content)

            if self._is_cancelled:
                return ""

            # 没有工具调用，直接返回结果（提前有结果了）
            if not tool_calls:
                return self._filter_thinking_content(response_content)

            # DeepSeek V4 thinking mode: 需要传递 reasoning_content
            assistant_msg = {
                "role": "assistant",
                "content": response_content,
                "tool_calls": tool_calls,
            }
            if current_reasoning:
                assistant_msg["reasoning_content"] = current_reasoning
            current_messages.append(assistant_msg)

            tool_results = self._execute_tools(tool_calls)

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

            current_messages.extend(tool_results)
            QCoreApplication.processEvents()
            time.sleep(0.2)

        return self._filter_thinking_content(response_content)

    def _build_final_summary_prompt(self) -> str:
        """构建最终总结提示"""
        return """

## 已达到最大迭代次数限制，请总结当前执行结果。

请整理并返回以下内容：
1. 已完成的工作
2. 关键发现和结论
3. 重要文件路径和数据
4. 待解决的问题（如有）

直接输出总结内容："""

    def _filter_thinking_content(self, content: str) -> str:
        """过滤掉思考内容，只保留纯回复"""
        if not content:
            return content
        return _THINKING_PATTERN.sub("", content)

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
            logger.warning(f"[SubAgentExecutor] _make_api_call received non-dict config: {type(config).__name__}={config!r}, falling back to empty config")
            config = {}
        api_key = config.get("API_KEY", "").strip()
        base_url = config.get("API_URL") or None
        model = str(config.get("模型名称", "gpt-4o"))

        req_kwargs = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        extra_body = {}

        for cn_key, value in config.items():
            if cn_key in {"API_KEY", "API_URL", "API_BASE", "认证方式", "模型名称",
                          "系统提示", "启用技能",
                          "name", "provider_name", "config_id", "display_name",
                          "_suffix_index", "备注", "获取地址", "模型列表"}:
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
            req_kwargs["max_tokens"] = self._cap_max_output_tokens(
                model, req_kwargs["max_tokens"], config
            )

        if extra_body:
            req_kwargs["extra_body"] = extra_body

        client = OpenAI(
            api_key=api_key if api_key else "dummy",
            base_url=base_url,
            timeout=120.0,
        )

        from app.core.workers.error_handler import create_api_call_with_retry

        def create_completion():
            return client.chat.completions.create(**req_kwargs, tools=tools)

        response = create_api_call_with_retry(
            client, create_completion,
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
                tool_calls_found.append({
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,  # 已是 JSON 字符串
                    },
                })

        return response_content, tool_calls_found, reasoning_content

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

    def _execute_tools(self, tool_calls: List[Dict]) -> Optional[List[Dict]]:
        """执行工具调用"""
        if not tool_calls or not self.tool_executor:
            return []

        results = []
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
                        logger.warning(f"[SubAgent] ⚠️ JSON 解析失败且无法修复, tool={tool_name}, preview='{arguments[:200]}'")
                        arguments = {}

            tool_call_id = tc["id"]

            if tool_name == "question":
                # 子智能体不需要 question 工具
                return None

            self._tool_call_count += 1
            self.tool_call_started.emit(self.task_id, tool_name, arguments)
            QCoreApplication.processEvents()

            result = self.tool_executor.execute(tool_name, arguments)
            result_content = str(result) if result else ""
            success = getattr(result, "success", True) if result else False

            self.tool_result_received.emit(self.task_id, tool_name, result_content, success)
            QCoreApplication.processEvents()

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
            results.append(to_api_message(raw_result) or raw_result)

        return results


class SubAgentManager(QObject):
    """子智能体管理器 - 管理子智能体任务分发"""

    task_started = pyqtSignal(str, str, str)  # task_id, agent_name, task_description
    task_finished = pyqtSignal(str, str)  # task_id, result
    batch_finished = pyqtSignal()  # 批次内所有任务都完成时触发

    def __init__(self, agent_manager, tool_executor, get_llm_config: Callable):
        super().__init__()
        self._agent_manager = agent_manager
        self._tool_executor = tool_executor
        self._get_llm_config = get_llm_config
        self._running_tasks: Dict[str, SubAgentExecutor] = {}
        self._finished_tasks: Dict[str, Dict] = {}  # task_id -> {"result": str, "error": str, "session_id": str}
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

    def _save_task_to_store(self, task_id: str, agent_name: str, task_description: str,
                            status: str = "running", result: str = None, error: str = None,
                            logs: List[Dict] = None, summary: Dict = None,
                            session_id: str = ""):
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
                task_id, agent_name, task_description, status, result, error,
                logs or [], summary or {},
                session_id=session_id or self._current_session_id,
            )
        except Exception as e:
            logger.error(f"[SubAgentManager] 保存任务到数据库失败: {e}")

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
            self._finished_tasks[task_id] = {"result": "", "error": error_msg, "agent_name": agent_name, "session_id": task_session_id}
            if on_finished:
                on_finished(error_msg)
            if on_error:
                on_error(error_msg)
            return False

        # 只允许 mode 为 subagent 或 all 的 agent 作为子智能体
        if not agent.is_subagent():
            error_msg = f"Agent '{agent_name}' cannot be used as subagent (mode: {agent.mode})"
            logger.error(f"[SubAgentManager] {error_msg}")
            self._finished_tasks[task_id] = {"result": "", "error": error_msg, "agent_name": agent_name, "session_id": task_session_id}
            if on_finished:
                on_finished(error_msg)
            if on_error:
                on_error(error_msg)
            return False

        try:
            if llm_config is None:
                llm_config = self._get_llm_config()
            elif not isinstance(llm_config, dict):
                # 防御：非 dict 的 llm_config（可能从 main_widget 传入的异常值）
                logger.warning(f"[SubAgentManager] execute_task llm_config is not a dict: {type(llm_config).__name__}={llm_config!r}, falling back to default")
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
                                content = "".join(
                                    c.get("text", "") for c in content if isinstance(c, dict)
                                )
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

            # 获取最大迭代次数（从 agent 配置的 steps 获取）
            max_iterations = agent.steps if agent.steps else 30

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

            if on_finished:
                executor.finished_with_result.connect(on_finished)
            if on_error:
                executor.error_occurred.connect(on_error)
            if on_progress:
                executor.progress_updated.connect(on_progress)

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

            logger.info(
                f"[SubAgentManager] Started task {task_id} with agent {agent_name}"
            )
            return True

        except Exception as e:
            logger.error(f"[SubAgentManager] Failed to execute task: {e}")
            if on_error:
                on_error(str(e))
            return False

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
            node_map[up["from"]]["_status"] in ("failed", "skipped")
            for up in dag_state["upstream_results"][nid]
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
                context_parts.append(
                    f"### {up['from']} ({up_node.get('agent', '')})\n{result_text}"
                )
        if node.get("context"):
            context_parts.append(node["context"])
        full_context = "\n\n".join(context_parts) if context_parts else ""

        llm_config = self._get_llm_config()
        if not isinstance(llm_config, dict):
            llm_config = {}

        agent_name = node.get("agent", "")
        task_description = node.get("description", "")

        agent = self._agent_manager.get_agent(agent_name)
        max_iterations = agent.steps if agent and agent.steps else 30

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
        # 原因：PyQt5 对在嵌套信号上下文（_start_dag_node 被 _check_dag_downstream
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
        完全相同的连接时机，消除了 PyQt5 对嵌套信号上下文中创建 lambda 的潜在
        行为差异（这种差异会导致 DAG 回调被静默丢弃而 UI 回调正常工作）。
        """
        # 只处理属于 DAG 的任务（在 _task_to_dag 中有记录的）
        if not hasattr(self, '_task_to_dag') or task_id not in self._task_to_dag:
            return
        dag_id, nid = self._task_to_dag[task_id]

        # 获取 executor（此时一定在 _running_tasks 中，因为 _start_dag_node 在
        # task_started.emit 之前已经 self._running_tasks[task_id] = executor）
        executor = self._running_tasks.get(task_id)
        if not executor:
            logger.warning(f"[DAG] _on_dag_task_started_slot: executor not found for task_id={task_id[:8]}")
            return

        # 连接 DAG 回调 —— 与 UI 回调完全相同的连接时机和方式
        executor.finished_with_result.connect(
            lambda tid, result: self._safe_dag_node_finished(dag_id, nid, tid, result)
        )
        executor.error_occurred.connect(
            lambda tid, error: self._safe_dag_node_error(dag_id, nid, tid, error)
        )
        logger.info(f"[DAG] 🔗 DAG callbacks connected for nid={nid} (via task_started slot)")

    def _safe_dag_node_finished(self, dag_id: str, nid: str, task_id: str, result: str):
        """
        DAG 完成回调的安全包装 —— 关键作用：
        1. 在 lambda 边界上 100% 捕获异常，**不让任何异常抛给 PyQt5**。
           PyQt5 在某些版本下，如果 slot 抛异常会自动 disconnect，
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
        """DAG 错误回调的安全包装（防 PyQt5 异常自动断开连接）"""
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
        logger.info(f"[DAG] 🔥 _on_dag_node_finished ENTERED: dag_id={dag_id}, nid={nid}, task_id={task_id[:8]}, has_dag_state={dag_id in getattr(self, '_dag_states', {})}")
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
        logger.info(f"[DAG] 节点完成: dag_id={dag_id}, nid={nid}, status={node['_status']}, has_downstream={bool(dag_state['adj'].get(nid))}")

        # 检查下游节点（用 try/except 包裹，防止因下游节点启动异常导致本节点状态和 all_done 检查被跳过）
        try:
            self._check_dag_downstream(dag_id, nid)
        except Exception as e:
            logger.error(f"[DAG] _on_dag_node_finished: 检查下游节点失败: {e}", exc_info=True)

        # 检查是否全部完成
        all_done = all(
            node_map[nid]["_status"] in ("completed", "failed", "skipped", "cancelled")
            for nid in node_map
        )
        if all_done:
            # DAG 整体完成，清理 _task_to_dag 映射
            if hasattr(self, '_task_to_dag'):
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
        all_done = all(
            node_map[nid]["_status"] in ("completed", "failed", "skipped", "cancelled")
            for nid in node_map
        )
        if all_done:
            # DAG 整体完成，清理 _task_to_dag 映射
            if hasattr(self, '_task_to_dag'):
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
            f"[DAG] 检查下游: dag_id={dag_id}, nid={nid}, "
            f"downstream_nodes={adj_neighbors}, adj_keys={list(adj.keys())}"
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
            {"name": "pending",   "itemStyle": {"color": "#FFC107", "borderColor": "#d4a020", "borderWidth": 2, "shadowBlur": 8, "shadowColor": "rgba(255,193,7,0.4)"}},
            {"name": "running",   "itemStyle": {"color": "#2196F3", "borderColor": "#1976D2", "borderWidth": 2, "shadowBlur": 8, "shadowColor": "rgba(33,150,243,0.4)"}},
            {"name": "completed", "itemStyle": {"color": "#4CAF50", "borderColor": "#388E3C", "borderWidth": 2, "shadowBlur": 8, "shadowColor": "rgba(76,175,80,0.4)"}},
            {"name": "failed",    "itemStyle": {"color": "#F44336", "borderColor": "#D32F2F", "borderWidth": 2, "shadowBlur": 8, "shadowColor": "rgba(244,67,54,0.4)"}},
            {"name": "skipped",   "itemStyle": {"color": "#9E9E9E", "borderColor": "#757575", "borderWidth": 2, "shadowBlur": 8, "shadowColor": "rgba(158,158,158,0.4)"}},
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
            echarts_nodes.append({
                "id": nid,
                "name": f"{nid} ({agent})",
                "symbolSize": 50,
                "category": status_map.get(status, 4),
                "draggable": True,
                "description": desc[:100],
            })

        echarts_edges = []
        for e in edges:
            echarts_edges.append({
                "source": e["from"],
                "target": e["to"],
                "lineStyle": {"width": 2, "opacity": 0.6, "curveness": 0.15},
            })

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
            "series": [{
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
            }],
        }

        return json.dumps(chart_config, option=orjson.OPT_INDENT_2).decode('utf-8')

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
        if not hasattr(self, '_task_to_dag'):
            return
        info = self._task_to_dag.pop(task_id, None)
        if not info:
            return
        dag_id, nid = info
        if not hasattr(self, '_dag_states') or dag_id not in self._dag_states:
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

                task_session_id = getattr(executor, '_task_session_id', self._current_session_id)
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
                self._save_task_to_store(task_id, agent_name, task_description, "finished", result, error, session_id=task_session_id)

                # 【安全网】如果这个 task 是一个 DAG 节点，但 DAG 回调没触发（节点还是 running），
                # 手动触发 DAG cascade（否则 DAG 永远卡在第一层）
                if hasattr(self, '_task_to_dag') and task_id in self._task_to_dag:
                    dag_id, nid = self._task_to_dag[task_id]
                    dag_state = getattr(self, '_dag_states', {}).get(dag_id)
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
                task_session_id = getattr(executor, '_task_session_id', self._current_session_id)
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
                self._save_task_to_store(task_id, agent_name, task_description, "timeout", "", error_msg, session_id=task_session_id)
                del self._running_tasks[task_id]
                # 【关键修复】通知 DAG：被超时清理的任务如果属于某个 DAG 节点，标记为 failed 并级联
                self._notify_dag_task_failed(task_id, error_msg)
                cleaned.append(task_id)

        return cleaned

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
                "session_id": getattr(executor, '_task_session_id', ''),
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
            results.append({
                "task_id": task_id,
                "summary": executor.get_summary(),
                "logs": executor.get_logs(),
                "status": "running"
            })

        # 收集已完成的任务
        for task_id, task_info in self._finished_tasks.items():
            results.append({
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
                "status": "finished"
            })

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
                    task_session = getattr(executor, '_task_session_id', '')
                    if task_session != effective_session:
                        continue
                tasks_info.append({
                    "task_id": tid,
                    "status": "running" if executor.isRunning() else "finishing",
                    "agent": executor.agent_name,
                })
            elif tid in self._finished_tasks:
                task_info = self._finished_tasks[tid]
                task_session = task_info.get("session_id", "")
                # 会话隔离：只返回当前会话的任务
                # 注意: 当 task_session 为空（旧记录/边缘情况）时，也视为不属于当前会话
                if effective_session and task_session != effective_session:
                    continue
                tasks_info.append({
                    "task_id": tid,
                    "status": "finished",
                    "agent": task_info.get("agent_name", ""),
                })
            # 其他情况（unknown）不返回，隐藏不存在或不属于当前会话的任务
        return ToolResult(True, content={"tasks": tasks_info})

    def get_tasks_status_with_details(self, task_ids: List[str], with_log: bool = False,
                                      with_result: bool = True, session_id: str = None) -> ToolResult:
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
                tasks_info.append({
                    "task_id": tid,
                    "status": "unknown",
                    "agent": "",
                })
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

    def get_all_active_tasks_with_details(self, with_log: bool = False, with_result: bool = True, session_id: str = None) -> ToolResult:
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
                ) if status == "running" else "",
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
            tasks_info.append({
                "task_id": task_id,
                "status": "running" if executor.isRunning() else "finishing",
                "agent": executor.agent_name,
            })
        return ToolResult(True, content={"tasks": tasks_info})
