# -*- coding: utf-8 -*-
"""AI 审核任务：对一次待审批的工具调用发起单轮 LLM 判定

对齐 topic_summary.py 模式：QRunnable + create_api_call_with_retry + http_client。
判定结果经宿主提供的 callback 回传（线程安全桥由宿主负责，约定
callback(result: dict, error: str | None)）；任何失败（模型未配置/调用异常/
解析失败）一律 callback(None, error=...)，由宿主回落人工审批。
本模块不做任何安全决策，只产出「建议」。
"""

import re
from typing import Callable, Optional

import orjson as json
from loguru import logger
from PySide6.QtCore import QRunnable, Slot

from app.core.workers.error_handler import create_api_call_with_retry

# decision 白名单：白名单外一律视为解析失败（宁回落人工，不猜）
_REVIEW_DECISIONS = ("allow", "deny")

# 审核单轮调用超时（秒）：审批链不能复用主对话的长超时——端点挂起时
# openai SDK 默认 600s，叠加重试会让 worker 审批等待循环长时间无响应
APPROVAL_REVIEW_TIMEOUT_SEC = 60

# 文本语义回退（仅 deny 侧）：模型不守 JSON 格式输出短句时兜底判定。
# allow 不做文本回退：自然语言否定前缀（「不能放行」「不可同意」）会让
# allow 误判变成误放行，风险不可接受；deny 误判 = 多弹一次人工审批，无害。
_DENY_TEXT_PATTERNS = (
    "不行",
    "拒绝",
    "禁止",
    "不允许",
    "不能放行",
    "不予放行",
    "危险",
    "不要执行",
    "deny",
)

# allow 侧回退词表：必须同时通过否定词防护（否定前缀 + allow 词 = 误放行风险）
_ALLOW_TEXT_PATTERNS = ("可以", "允许", "放行", "同意", "没问题", "approve", "allow")
_NEGATION_PATTERNS = ("不能", "不可", "不要", "无法", "禁止", "拒绝", "不行", "不允许", "不宜", "不建议")

_PROMPT_TEMPLATE = (
    "你是桌面 AI 助手的安全审核员。主 AI 请求执行下面的工具调用，请判定是否放行。\n\n"
    "工作目录：{workdir}\n"
    "工具名：{tool_name}\n"
    "参数：{arguments}\n"
    "影响面：{impact}\n"
    "{context_block}"
    "判定标准：\n"
    "1. 该操作服务于本次用户目标、且作用范围在允许边界内（工作目录内或用户明确授权的范围）→ allow\n"
    "2. 以下任一情形 → deny：造成不可逆损失、向网络外传数据、改动用户未授权的范围、"
    "明显的破坏性命令。这是典型情形，不限于此。\n"
    "3. 拿不准时一律 deny（宁严勿松）\n\n"
    "输出格式（程序解析，必须严格遵守）：\n"
    "你的回复第一个字符必须是 {{，最后一个字符必须是 }}。\n"
    '格式：{{"decision": "allow 或 deny", "reason": "不超过50字的理由"}}\n'
    "任何解释性文字、前后缀都会导致解析失败并转人工审批。"
)


def _format_context(context: Optional[dict]) -> str:
    """把任务上下文渲染为 prompt 段；无上下文时返回空串（prompt 退回原形态）

    上下文来自 worker 侧 _build_approval_context：user_goal（最近一条用户消息）
    + round_steps（本轮已发起的工具调用，不含结果体）。仅作信息补充，
    不得据此放行——判定仍以判定标准为准。
    """
    if not isinstance(context, dict):
        return ""
    lines = []
    goal = str(context.get("user_goal") or "").strip()
    if goal:
        lines.append(f"用户目标：{goal}")
    steps = context.get("round_steps")
    if isinstance(steps, list) and steps:
        parts = []
        for step in steps[:12]:  # 上限防轨迹过长拖垮 prompt
            if not isinstance(step, dict):
                continue
            name = str(step.get("tool") or "").strip()
            if not name:
                continue
            args = str(step.get("args") or "").strip()
            parts.append(f"{name}({args})" if args else name)
        if parts:
            lines.append("本轮已执行：" + " → ".join(parts))
    if not lines:
        return ""
    return "任务上下文：\n" + "\n".join(lines) + "\n\n"


def parse_review(raw: str) -> Optional[dict]:
    """从模型输出解析 {"decision","reason"}；decision 白名单外返回 None

    JSON 解析失败时的兜底：短输出（≤50 字）命中拒绝语义词 → deny
    （fail-safe：deny 误判只是多弹人工，allow 绝不从自然语言猜测）。
    """
    if not raw:
        return None
    text = raw.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = m.group(0) if m else text
    try:
        data = json.loads(candidate)
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, dict):
        decision = str(data.get("decision") or "").strip().lower()
        if decision not in _REVIEW_DECISIONS:
            return None
        reason = data.get("reason") or ""
        return {"decision": decision, "reason": str(reason)[:200]}
    # JSON 解析失败 → 短输出文本语义回退（deny 优先；allow 需过否定词防护）
    if len(text) <= 50:
        lowered = text.lower()
        if any(p in lowered for p in _DENY_TEXT_PATTERNS):
            return {"decision": "deny", "reason": text[:50]}
        if any(p in lowered for p in _ALLOW_TEXT_PATTERNS) and not any(n in text for n in _NEGATION_PATTERNS):
            return {"decision": "allow", "reason": text[:50]}
    return None


def _truncate(value, limit: int = 1500) -> str:
    """参数值截断，防超长内容拖垮审核调用"""
    if isinstance(value, (dict, list)):
        try:
            # orjson 无 ensure_ascii 参数（输出恒为 UTF-8 原文，本就是想要的效果）
            value = json.dumps(value).decode("utf-8")
        except Exception:  # noqa: BLE001
            value = str(value)
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def build_review_prompt(
    tool_name: str,
    arguments: dict,
    workdir: str,
    impact: dict,
    context: Optional[dict] = None,
) -> str:
    return _PROMPT_TEMPLATE.format(
        workdir=workdir or "(未知)",
        tool_name=tool_name,
        arguments=_truncate(arguments),
        impact=_truncate(impact),
        context_block=_format_context(context),
    )


class ApprovalReviewTask(QRunnable):
    """单次 AI 审核：构建 prompt → 非流式调用 → 解析 → 回调"""

    def __init__(
        self,
        tool_name: str,
        arguments: dict,
        workdir: str,
        llm_config: dict,
        callback: Callable,
        impact: Optional[dict] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        timeout: int = APPROVAL_REVIEW_TIMEOUT_SEC,
        context: Optional[dict] = None,
    ):
        super().__init__()
        self.tool_name = tool_name
        self.arguments = dict(arguments or {})
        self.workdir = workdir or ""
        self.impact = impact or {}
        self.llm_config = llm_config
        self.callback = callback
        self.cancel_check = cancel_check or (lambda: False)
        self.timeout = timeout
        self.context = context
        self.setAutoDelete(True)

    @Slot()
    def run(self):
        try:
            if self.cancel_check():
                logger.info("[ApprovalReview] 任务已取消")
                self.callback(None, error="cancelled")
                return
            prompt = build_review_prompt(
                self.tool_name, self.arguments, self.workdir, self.impact, context=self.context
            )

            from app.utils.http_client import build_openai_client, chat_completion_text

            # 显式短超时：openai SDK 默认 600s，端点挂起会把审批链拖死
            client = build_openai_client(
                api_key=self.llm_config.get("API_KEY", ""),
                base_url=self.llm_config.get("API_URL"),
                timeout=self.timeout,
            )

            def create_task():
                # 流式发起+聚合：仅流式端点非流式请求会 400（同 topic_summary）。
                # max_tokens 须给足：reasoning 类模型的思考消耗 token 且不进
                # content，上限太小会导致 content 被截空（实测空响应根因）。
                return chat_completion_text(
                    client,
                    model=self.llm_config.get("模型名称", "gpt-4o"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=2000,
                )

            raw = create_api_call_with_retry(
                client,
                create_task,
                # 审批场景收紧：单次短输出调用，失败快速回落人工，
                # 不能继承主对话默认的 15 次 × 5s 重试节奏
                max_retries=1,
                retry_delay=2.0,
                cancel_check=self.cancel_check,
            )
            if not raw or not raw.strip():
                # 空响应单列：与「输出了但格式不对」区分，便于定位
                # （典型：reasoning 模型思考占满 max_tokens / 端点异常）
                logger.warning(
                    "[ApprovalReview] 模型返回空响应，回落人工审批"
                    "（reasoning 类模型可能将 max_tokens 消耗在思考上，可换非思考模型）"
                )
                self.callback(None, error="模型返回空响应")
                return
            result = parse_review(raw)
            if result is None:
                logger.warning(f"[ApprovalReview] 输出无法解析为有效判定: {raw[:200]}")
                self.callback(None, error="模型输出无法解析为有效判定")
            else:
                self.callback(result)
        except Exception as e:  # noqa: BLE001 - 审核失败必须回落人工而非崩溃
            logger.exception(f"[ApprovalReview] 审核调用失败: {e}")
            self.callback(None, error=str(e))
