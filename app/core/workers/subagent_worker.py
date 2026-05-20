# -*- coding: utf-8 -*-
"""
子智能体执行器 - 独立运行子智能体任务，避免共享超长上下文
"""

import orjson as json
import re
import time
from typing import Dict, List, Optional, Any, Callable

from loguru import logger

from app.constants import PARAM_SCHEMA
from app.tools.result import ToolResult
from app.core.store import SubAgentLogStore

from PyQt5.QtCore import QThread, pyqtSignal, QCoreApplication, QObject
from openai import OpenAI

from app.core.provider_profile import get_provider_profile

# ========== 性能优化：预编译正则表达式 ==========
_THINKING_PATTERN = re.compile(r"<think>[\s\S]*?</think>")  # 过滤完整思考块
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")  # 验证标识符格式


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
            is_subagent_call: bool = True,  # 标记是否为被主智能体调用（通过 task_batch）
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
        self._log_store_callback = None  # 日志存储回调
        self._get_history_messages = None  # 获取主智能体历史消息的回调

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
        if self._log_store_callback:
            try:
                self._log_store_callback(self.task_id, self.agent_name, self.task_description, "running", None, None,
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
            agent = self.agent_manager.get_agent(self.agent_name)
            if not agent:
                self.error_occurred.emit(self.task_id, f"Agent not found: {self.agent_name}")
                return

            # 子智能体被调用时 is_subagent_call=True，此时应该使用 subagent_constraints
            # 用于区分"主智能体通过 task_batch 调用子智能体"的情况
            system_prompt = self.agent_manager.get_agent_system_prompt(
                self.agent_name, is_subagent_call=self.is_subagent_call
            )
            tools = self.agent_manager.get_agent_tools_schema(self.agent_name, is_subagent_call=self.is_subagent_call)

            # 【新增】如果 agent 配置了 inherit_history，获取主智能体历史消息
            history_section = ""
            if agent.inherit_history and self._get_history_messages:
                try:
                    history_messages = self._get_history_messages() or []
                    if history_messages:
                        # 根据 inherit_history_count 限制消息数量
                        if agent.inherit_history_count is not None:
                            history_messages = history_messages[-agent.inherit_history_count:]

                        # 格式化历史消息（每条限制在合理长度内，避免上下文过长）
                        history_lines = []
                        max_chars = getattr(agent, 'inherit_history_max_chars', 500) or 500
                        for msg in history_messages:
                            role = msg.get("role", "user")
                            if role == "tool":
                                continue
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = "".join(
                                    c.get("text", "") for c in content if isinstance(c, dict)
                                )
                            if content and role != "user" and role != "assistant":
                                # 截断过长工具内容
                                truncated = content[:max_chars] + ("..." if len(content) > max_chars else "")
                                history_lines.append(f"**{role}**: {truncated}")

                        if history_lines:
                            history_section = "\n\n## 主智能体历史上下文\n" + "\n".join(history_lines)
                except Exception as e:
                    logger.warning(f"[SubAgentExecutor] 获取历史消息失败: {e}")

            messages = [
                {"role": "system", "content": system_prompt + history_section + F"## 父智能体说明\n{self.parent_context}\n\n"},
                {"role": "user", "content": f"## 子任务\n{self.task_description}"}
            ]

            self._add_log("progress", f"开始执行子任务: {self.agent_name}")
            self.progress_updated.emit(self.task_id, f"开始执行子任务: {self.agent_name}")

            try:
                result = self._execute_agent_loop(messages, tools)
            except Exception as e:
                logger.error(f"[SubAgentExecutor] _execute_agent_loop error: {e}")
                result = f"执行出错: {str(e)}"

            if self._is_cancelled:
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
            logger.error(f"[SubAgentExecutor] run() error: {e}")
            self._execution_error = str(e)
            # 任务出错，保存错误状态
            if self._log_store_callback:
                try:
                    self._log_store_callback(self.task_id, self.agent_name, self.task_description, "error", None,
                                             self._execution_error, self._logs, self.get_summary())
                except Exception as e:
                    logger.warning(f"[SubAgentExecutor] 保存错误状态失败: {e}")
            self.error_occurred.emit(self.task_id, f"SubAgent execution error: {str(e)}")

    def _execute_agent_loop(self, messages: List[Dict], tools: List[Dict]) -> str:
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
                response_content, _, _ = self._make_api_call(current_messages, None)
                return self._filter_thinking_content(response_content)

            response_content, tool_calls, reasoning_content = self._make_api_call(
                current_messages, tools
            )
            current_reasoning = reasoning_content

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

    def _make_api_call(self, messages: List[Dict], tools: List[Dict] = None) -> tuple:
        """调用 LLM API（非流式，子智能体后台执行无需流式输出）"""
        api_key = self.llm_config.get("API_KEY", "").strip()
        base_url = self.llm_config.get("API_URL") or None
        model = str(self.llm_config.get("模型名称", "gpt-4o"))

        req_kwargs = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        extra_body = {}

        for cn_key, value in self.llm_config.items():
            if cn_key in ["API_KEY", "API_URL", "模型名称", "系统提示", "启用技能"]:
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
                model, req_kwargs["max_tokens"]
            )

        if extra_body:
            req_kwargs["extra_body"] = extra_body

        client = OpenAI(
            api_key=api_key if api_key else "dummy",
            base_url=base_url,
            timeout=120.0,
        )

        from app.core.retry_helper import create_api_call_with_retry

        def create_completion():
            return client.chat.completions.create(**req_kwargs, tools=tools)

        response = create_api_call_with_retry(client, create_completion)

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

    def _cap_max_output_tokens(self, model: str, requested: int) -> int:
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

        Returns:
            合理的 max_tokens 值
        """
        try:
            requested_int = int(requested)
        except Exception:
            return requested

        profile = get_provider_profile(self.llm_config)

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
                except:
                    arguments = {}

            tool_call_id = tc["id"]

            if tool_name == "question":
                question_text = arguments.get("question", "")
                options = arguments.get("options", [])
                multiple = arguments.get("multiple", False)
                self._question_pending = {
                    "tool_call_id": tool_call_id,
                    "question": question_text,
                    "options": options,
                    "multiple": multiple,
                }
                return None

            self._tool_call_count += 1
            self._add_log("tool_call", tool_name, {"args": arguments})
            self.tool_call_started.emit(self.task_id, tool_name, arguments)
            QCoreApplication.processEvents()

            result = self.tool_executor.execute(tool_name, arguments)
            result_content = str(result) if result else ""
            success = getattr(result, "success", True) if result else False

            self._add_log("tool_result", tool_name, {"result": result_content, "success": success})
            self.tool_result_received.emit(self.task_id, tool_name, result_content, success)
            QCoreApplication.processEvents()

            results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,  # 添加 name 字段与 ChatWorker 一致
                    "arguments": arguments,  # 添加 arguments 字段与 ChatWorker 一致
                    "content": result_content,
                    "success": success,  # 添加 success 字段与 ChatWorker 一致
                    "round_id": f"round_{id(tc)}",  # 添加 round_id 字段与 ChatWorker 一致
                }
            )

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
        self._finished_tasks: Dict[str, Dict] = {}  # task_id -> {"result": str, "error": str}
        self._log_store: Optional[SubAgentLogStore] = None
        # 批次计数：本次启动的任务总数
        self._batch_total = 0
        self._batch_completed = 0
        # 已查询过的任务ID集合，避免重复返回结果浪费上下文
        self._queried_tasks: set = set()
        # 获取主智能体历史消息的回调（由外部设置）
        self._get_history_messages: Optional[Callable[[], List[Dict]]] = None

    def set_history_getter(self, getter: Callable[[], List[Dict]]):
        """设置获取主智能体历史消息的回调"""
        self._get_history_messages = getter

    def set_log_store(self, log_store: SubAgentLogStore):
        """设置日志存储"""
        self._log_store = log_store

    def _save_task_to_store(self, task_id: str, agent_name: str, task_description: str,
                            status: str = "running", result: str = None, error: str = None,
                            logs: List[Dict] = None, summary: Dict = None):
        """保存任务到数据库"""
        if not self._log_store:
            return
        try:
            if logs is None or summary is None:
                executor = self._running_tasks.get(task_id)
                if executor and hasattr(executor, "get_logs"):
                    logs = executor.get_logs()
                if executor and hasattr(executor, "get_summary"):
                    summary = executor.get_summary()
            self._log_store.save_task(task_id, agent_name, task_description, status, result, error, logs or [],
                                      summary or {})
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
    ) -> bool:
        """执行子智能体任务"""
        # 验证 agent 是否存在且是有效的子智能体类型
        agent = self._agent_manager.get_agent(agent_name)
        if not agent:
            error_msg = f"Agent not found: {agent_name}"
            logger.error(f"[SubAgentManager] {error_msg}")
            # 立即触发完成信号，让任务不卡在 running 状态
            self._finished_tasks[task_id] = {"result": "", "error": error_msg, "agent_name": agent_name}
            if on_finished:
                on_finished(error_msg)
            if on_error:
                on_error(error_msg)
            return False

        # 只允许 mode 为 subagent 或 all 的 agent 作为子智能体
        if not agent.is_subagent():
            error_msg = f"Agent '{agent_name}' cannot be used as subagent (mode: {agent.mode})"
            logger.error(f"[SubAgentManager] {error_msg}")
            self._finished_tasks[task_id] = {"result": "", "error": error_msg, "agent_name": agent_name}
            if on_finished:
                on_finished(error_msg)
            if on_error:
                on_error(error_msg)
            return False

        try:
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

            # 设置日志存储回调
            if self._log_store:
                executor.set_log_store_callback(self._save_task_to_store)

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

            # 保存到数据库
            self._save_task_to_store(task_id, agent_name, task_description, "running")

            self.task_started.emit(task_id, agent_name, task_description)

            logger.info(
                f"[SubAgentManager] Started task {task_id} with agent {agent_name}"
            )
            return True

        except Exception as e:
            logger.error(f"[SubAgentManager] Failed to execute task: {e}")
            if on_error:
                on_error(str(e))
            return False

    def cancel_task(self, task_id: str) -> bool:
        """取消子智能体任务"""
        if task_id in self._running_tasks:
            self._running_tasks[task_id].cancel()
            del self._running_tasks[task_id]
            return True
        return False

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

                self._finished_tasks[task_id] = {
                    "result": result,
                    "error": error,
                    "agent_name": agent_name,
                    "task_description": task_description,
                    "logs": logs,
                    "tool_call_count": tool_call_count,
                    "elapsed_seconds": elapsed,
                }

                # 更新数据库
                self._save_task_to_store(task_id, agent_name, task_description, "finished", result, error)

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
                error_msg = f"Task cancelled due to timeout ({timeout_seconds}s)"
                self._finished_tasks[task_id] = {
                    "result": "",
                    "error": error_msg,
                    "agent_name": agent_name,
                    "task_description": task_description,
                    "logs": logs,
                }
                # 更新数据库
                self._save_task_to_store(task_id, agent_name, task_description, "timeout", "", error_msg)
                del self._running_tasks[task_id]
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
        if self._log_store:
            db_task = self._log_store.get_task(task_id)
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
                "status": "running"
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
                "status": "finished"
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

    def get_tasks_status(self, task_ids: List[str]) -> ToolResult:
        """获取指定任务的状态"""
        tasks_info = []
        for tid in task_ids:
            if tid in self._running_tasks:
                executor = self._running_tasks[tid]
                tasks_info.append({
                    "task_id": tid,
                    "status": "running" if executor.isRunning() else "finishing",
                    "agent": executor.agent_name,
                })
            elif tid in self._finished_tasks:
                tasks_info.append({
                    "task_id": tid,
                    "status": "finished",
                    "agent": self._finished_tasks[tid].get("agent_name", ""),
                })
            else:
                tasks_info.append({
                    "task_id": tid,
                    "status": "unknown",
                    "agent": "",
                })
        return ToolResult(True, content={"tasks": tasks_info})

    def get_tasks_status_with_details(self, task_ids: List[str], with_log: bool = False,
                                      with_result: bool = True) -> ToolResult:
        """获取指定任务的详细状态"""
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

            # running 状态可以反复查，完成或失败只能查一次
            if status not in ("running", "unknown"):
                if tid in self._queried_tasks:
                    task_info["_already_queried"] = True
                    task_info["_message"] = "已查询过结果，可通过 id 再次查询"
                    tasks_info.append(task_info)
                    continue
                self._queried_tasks.add(tid)

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

    def get_all_active_tasks_with_details(self, with_log: bool = False, with_result: bool = True) -> ToolResult:
        """获取所有已完成任务的详细状态（避免重复返回）"""
        tasks_info = []

        for task_id, task_info in self._finished_tasks.items():
            if any(t.get("agent") == task_info.get("agent_name", "") for t in tasks_info):
                continue

            task_entry = {
                "task_id": task_id,
                "status": "finished",
                "agent": task_info.get("agent_name", ""),
                "task_description": task_info.get("task_description", ""),
            }

            # 完成或失败只能查一次
            if task_id in self._queried_tasks:
                task_entry["_already_queried"] = True
                task_entry["_message"] = "已查询过结果，可通过 id 再次查询"
                tasks_info.append(task_entry)
                continue

            self._queried_tasks.add(task_id)

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
