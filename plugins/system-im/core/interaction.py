# -*- coding: utf-8 -*-
"""IM 内交互提问的文本协议（纯函数，无 IO）。

Gateway 模式下模型可以调用 ``question`` 工具，但平台是纯文本通道：没有按钮、
没有浮层。这里把 questions 数组渲染成一条 IM 可读的消息，并把用户下一条
回复解析成对当前问题的答案，最终拼成宿主 worker 认得的
``问题「x」的回答：...`` 格式（与 UI 浮层 ``_build_and_emit_answer`` 一致，
下游 ``_parse_question_result`` 可直接解析）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 每题最多展示的选项数（与 question 工具 schema 的上限一致）
MAX_OPTIONS = 4
_SPLIT_RE = re.compile(r"[,，、;；\n]+")


def format_question(
    questions: List[Dict[str, Any]], index: int, total: int
) -> str:
    """渲染第 index 道问题为 IM 消息文本（0 基）。"""
    q = questions[index] if questions else {}
    head = "🙋 需要你补充信息"
    if total > 1:
        head += f"（{index + 1}/{total}）"
    lines = [head + "：", ""]
    header = str(q.get("header") or "").strip()
    if header:
        lines.extend([header, ""])
    lines.append(str(q.get("question") or "请输入你的回答。").strip())
    detail = str(q.get("detail") or "").strip()
    if detail:
        lines.extend(["", detail])

    options = q.get("options") or []
    if options:
        lines.append("")
        for i, opt in enumerate(options[:MAX_OPTIONS], 1):
            label = str((opt or {}).get("label") or "").strip()
            desc = str((opt or {}).get("description") or "").strip()
            lines.append(f"{i}. {label}" + (f" — {desc}" if desc else ""))
        lines.append("")
        if q.get("multiple"):
            lines.append("回复选项序号（多选用逗号分隔），也可以直接输入其他内容。")
        else:
            lines.append("回复选项序号，也可以直接输入其他内容。")
    else:
        lines.extend(["", "请直接回复你的答案。"])
    return "\n".join(lines)


def _match_option(question: Dict[str, Any], token: str) -> Optional[str]:
    """把单个 token 解析成选项 label：支持序号与 label 原文。"""
    token = token.strip()
    if not token:
        return None
    options = question.get("options") or []
    labels = [str((o or {}).get("label") or "").strip() for o in options[:MAX_OPTIONS]]
    labels = [lb for lb in labels if lb]
    if token.isdigit():
        idx = int(token)
        if 1 <= idx <= len(labels):
            return labels[idx - 1]
        return None
    for lb in labels:
        if lb == token:
            return lb
    return None


def parse_answer(question: Dict[str, Any], text: str) -> str:
    """把用户一条回复解析成该题的答案文本。

    有选项时优先匹配序号/label（多选用分隔符拆），匹配不到按自定义文本处理。
    返回格式与 UI 浮层一致：``【选项】；自定义内容``。
    """
    raw = (text or "").strip()
    options = question.get("options") or []
    if not options:
        return raw
    if question.get("multiple"):
        selected: List[str] = []
        custom: List[str] = []
        for token in _SPLIT_RE.split(raw):
            token = token.strip()
            if not token:
                continue
            label = _match_option(question, token)
            if label:
                if label not in selected:
                    selected.append(label)
            else:
                custom.append(token)
        parts = [f"【{lb}】" for lb in selected]
        if custom:
            parts.append("、".join(custom))
        return "；".join(parts) if parts else raw
    label = _match_option(question, raw)
    if label:
        return f"【{label}】"
    return raw


def build_answer_payload(questions: List[Dict[str, Any]], answers: List[str]) -> str:
    """拼成 worker 侧 tool result 用的多题答案文本。"""
    parts = []
    for i, q in enumerate(questions):
        q_text = str((q or {}).get("question") or f"问题{i + 1}")
        ans = answers[i] if i < len(answers) else ""
        parts.append(f"问题「{q_text}」的回答：\n{ans}")
    return "\n---\n".join(parts)
