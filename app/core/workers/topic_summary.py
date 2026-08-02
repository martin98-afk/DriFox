# -*- coding: utf-8 -*-
"""
话题摘要任务 - 异步生成话题摘要
"""

import re
from typing import Callable

import orjson as json
from loguru import logger
from openai import OpenAI
from PyQt5.QtCore import QRunnable, pyqtSlot

from app.core.workers.error_handler import create_api_call_with_retry


def _extract_json(text: str) -> dict | None:
    """从文本中提取并解析 JSON

    支持以下格式：
    1. ```json\n{"key": "value"}\n```   (代码块)
    2. 纯 JSON 文本                         ("{"key": "value"}")
    3. 带前/后缀文本的 JSON                  ("说明: {"key": "value"}")
    """
    # 1. 尝试代码块格式（最明确的指令遵循模式）
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block_match:
        try:
            return json.loads(code_block_match.group(1))
        except Exception:
            pass

    # 2. 从文本中提取出第一个 { 到最后一个 } 之间的内容，作为 JSON 候选
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        candidate = text[brace_start : brace_end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 3. 尝试直接解析整个文本（可能本身就是 JSON 字符串）
    try:
        stripped = text.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
    except Exception:
        pass

    return None


class TopicSummaryTask(QRunnable):
    """异步生成话题摘要任务"""

    def __init__(
        self,
        messages: list,
        llm_config: dict,
        callback,
        previous_summary: str = None,
        cancel_check: Callable[[], bool] = None,
    ):
        super().__init__()
        self.messages = messages
        self.llm_config = llm_config
        self.callback = callback
        self.previous_summary = previous_summary
        self.cancel_check = cancel_check or (lambda: False)
        self.setAutoDelete(True)

    def _fallback_user_question(self) -> str:
        """标题生成失败时的回退标题：取第一条真实用户问题，截断 15 字。

        跳过 _hook_event 系统消息与未打标任务邮件（📨 前缀，R3 防御），
        与 get_team_first_question / _build_session_record 的防御对齐，
        确保标题不被邮件污染；无可用用户消息时返回空串。
        """
        for msg in self.messages:
            if msg.get("role") != "user":
                continue
            # 🛡️ R7 修复：team 邮件（_hook_event="TeamMail"）视为真实用户问题
            # → mail-only + LLM 失败时也能回退标题（之前被全过滤误伤返回空串）。
            # 其他 hook 事件（SessionStart 等）仍跳过。
            if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [item.get("text", "") for item in content if item.get("type") == "text"]
                content = "\n".join(texts)
            if not content:
                continue
            # R3 防御：跳过未打标的任务邮件注入路径（旧数据无 _hook_event 的
            # "📨 **来自 [...] 的任务邮件：**" 文本）
            if str(content).startswith("📨 **来自"):
                continue
            return str(content)[:15]
        return ""

    def _emit_result_or_fallback(self, summary: str):
        """发布标题生成结果；结果为空/空白时回退用用户问题作为标题。"""
        if summary and summary.strip():
            self.callback({"topic_summary": summary})
            return
        fallback = self._fallback_user_question()
        if fallback:
            logger.warning("[TopicSummary] 标题生成结果为空，回退用用户问题作为标题")
            self.callback({"topic_summary": fallback})
        else:
            self.callback({"topic_summary": ""})

    def _build_conversation_context(self) -> str:
        """构建带问答对的对话上下文（保留 user 和 assistant 消息）"""
        lines = []
        for msg in self.messages:
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [item.get("text", "") for item in content if item.get("type") == "text"]
                content = "\n".join(texts)
            # 截断单条消息，保留足够上下文
            truncated = content[:300] + ("..." if len(content) > 300 else "")
            if role == "user":
                lines.append(f"用户: {truncated}")
            else:
                lines.append(f"助手: {truncated}")
        return "\n".join(lines)

    @pyqtSlot()
    def run(self):
        try:
            # 开头即检查取消状态，避免无谓的 API 调用
            if self.cancel_check():
                logger.info("[TopicSummary] 任务已取消，跳过生成")
                return

            if not self.messages:
                self.callback({"topic_summary": ""})
                return

            summary_text = self._build_conversation_context()

            # 安全转义 previous_summary 用于 JSON 示例
            safe_prev = self.previous_summary.replace("\\", "\\\\").replace('"', '\\"') if self.previous_summary else ""

            if self.previous_summary:
                prompt = (
                    "你是一个对话标题助手。\n"
                    "最近的用户对话历史：\n"
                    f"{summary_text}\n\n"
                    "根据对话生成标题。\n\n"
                    "【标题要求】\n"
                    "- 不超过15字，体现用户意图\n"
                    '- 如："生成PPT"、"调试bug"、"咨询问题"\n\n'
                    "【标题更新原则】\n"
                    "你不应该仅根据最新一条消息就生成新标题。而是应该：\n"
                    "1. 如果已有标题反映的是本次对话的核心主题，即使最新消息是简单问候，也要保留原标题\n"
                    "2. 只有当对话主题发生实质性转变时才更新标题\n"
                    "3. 如果主题没有实质性转变，topic_summary 填入【之前的标题】原样返回\n\n"
                    "【之前的标题】\n"
                    f"{self.previous_summary}\n\n"
                    "输出JSON（注意：topic_summary 必须是非空字符串，主题不变则填入原标题）：\n"
                    "```json\n"
                    "{\n"
                    f'  "topic_summary": "{safe_prev}"\n'
                    "}\n"
                    "```"
                )
            else:
                prompt = (
                    "你是一个对话标题助手。\n"
                    "最近的用户对话历史：\n"
                    f"{summary_text}\n\n"
                    "根据对话生成标题。\n\n"
                    "【标题要求】\n"
                    "- 不超过15字，体现用户意图\n"
                    '- 如："生成PPT"、"调试bug"、"咨询问题"\n\n'
                    "输出JSON（注意：topic_summary 必须是非空字符串，请生成合适的标题）：\n"
                    "```json\n"
                    "{\n"
                    '  "topic_summary": ""\n'
                    "}\n"
                    "```"
                )

            client = OpenAI(
                api_key=self.llm_config.get("API_KEY", ""),
                base_url=self.llm_config.get("API_URL"),
            )

            def create_task():
                return client.chat.completions.create(
                    model=self.llm_config.get("模型名称", "gpt-4o"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=1500,
                )

            resp = create_api_call_with_retry(client, create_task, cancel_check=self.cancel_check)
            if not resp.choices:
                logger.warning("[TopicSummary] API 返回空 choices，跳过摘要")
                raw_response = ""
            else:
                raw_response = resp.choices[0].message.content.strip()
            result = _extract_json(raw_response)
            if result:
                self._emit_result_or_fallback(result.get("topic_summary", ""))
            else:
                logger.warning("[TopicSummary] LLM 返回内容无法解析为 JSON")
                self._emit_result_or_fallback("")

        except Exception as e:
            logger.exception(f"[TopicSummary] 生成摘要失败: {e}")
            # 标题生成失败 → 回退用用户问题作为标题
            fallback = self._fallback_user_question()
            if fallback:
                logger.warning("[TopicSummary] 生成失败，回退用用户问题作为标题")
                self.callback({"topic_summary": fallback})
            else:
                self.callback(None, error=str(e))
