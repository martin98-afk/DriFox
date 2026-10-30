# -*- coding: utf-8 -*-
"""内置 tier（order 18）：超长 JSON 工具结果结构骨架化。

字符截断（tool_prune，order 20）对 JSON 是破坏性的：切在字符串中间会得到
非法 JSON，切在数组中间会让模型误判「就这么多」。本层先把结构保住：

  - 对象：保留全部键名（键数超限时保留前 N 个 + 省略计数）
  - 数组：保留前 4 项 + 后 2 项，中间插入 {"_omitted": 数量}
  - 长字符串：保留前 200 字符 + 总长度标注
  - 嵌套深度上限 6 层

输出仍是合法 JSON，模型能看出「这是被省略的列表」而不是「数据就这些」，
也能据此决定要不要重新调用工具缩小范围。

非 JSON 内容一律不动，交给 tool_prune 按文本截断。

cache_impact=none：纯函数、幂等（骨架化结果再跑一次不再变化），
同一结果每轮投影字节一致，前缀稳定。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from app.plugins.contracts.context_policy import ALL_STAGES, CACHE_NONE, TierOutcome
from app.plugins.sdk import cfg_int, count_messages_tokens

# 触发骨架化的 JSON 正文长度（字符）
_DEFAULT_MAX_CHARS = 8000
# 单字符串保留长度
_STR_KEEP = 200
# 数组保留头尾
_LIST_HEAD = 4
_LIST_TAIL = 2
# 对象键数上限
_MAX_KEYS = 30
# 嵌套深度上限
_MAX_DEPTH = 6


# 判断内容是否以 JSON 容器开头（避免 lstrip 复制大字符串）
_JSON_START_RE = re.compile(r"\s*[\{\[]")


def _shrink(value: Any, depth: int = 0) -> Any:
    """递归骨架化：返回值仍是可 JSON 序列化的结构。"""
    if depth >= _MAX_DEPTH:
        if isinstance(value, (dict, list)):
            return f"<{type(value).__name__} 深度超限，共 {len(value)} 项>"
        return value
    if isinstance(value, str):
        if len(value) <= _STR_KEEP:
            return value
        return value[:_STR_KEEP] + f"…(共 {len(value)} 字符)"
    if isinstance(value, dict):
        # 已骨架化的对象：带省略标记且键数在上限内，原样返回（保证幂等）
        if "_omitted_keys" in value and len(value) <= _MAX_KEYS + 1:
            return value
        items = list(value.items())
        out: Dict[str, Any] = {}
        for k, v in items[:_MAX_KEYS]:
            out[str(k)] = _shrink(v, depth + 1)
        if len(items) > _MAX_KEYS:
            out["_omitted_keys"] = len(items) - _MAX_KEYS
        return out
    if isinstance(value, list):
        # 已骨架化的数组：头 + {"_omitted"} + 尾，原样返回（保证幂等）
        if (
            len(value) == _LIST_HEAD + _LIST_TAIL + 1
            and isinstance(value[_LIST_HEAD], dict)
            and set(value[_LIST_HEAD]) == {"_omitted"}
        ):
            return value
        if len(value) <= _LIST_HEAD + _LIST_TAIL:
            return [_shrink(v, depth + 1) for v in value]
        head = [_shrink(v, depth + 1) for v in value[:_LIST_HEAD]]
        tail = [_shrink(v, depth + 1) for v in value[-_LIST_TAIL:]]
        return head + [{"_omitted": len(value) - _LIST_HEAD - _LIST_TAIL}] + tail
    return value


def shape_json_text(text: str) -> str:
    """骨架化 JSON 文本；非 JSON 或无法缩短时原样返回。"""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        data = json.loads(stripped)
    except Exception:
        return text
    try:
        shaped = json.dumps(_shrink(data), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return text
    return shaped if len(shaped) < len(text) else text


class ToolResultShapeTier:
    id = "tool_result_shape"
    label = "JSON 结果骨架化"
    order = 18
    # 设置页细项行副标题（component_items 走 AST 静态读取，仅支持字面量）
    description = "超长 JSON 工具结果保留结构骨架（键名、数组头尾、长串截断），输出仍是合法 JSON，避免字符截断切碎结构；非 JSON 内容不动"
    stages = frozenset(ALL_STAGES)
    cache_impact = CACHE_NONE

    def _limit(self) -> int:
        return max(1000, cfg_int("json_shape_max_chars", _DEFAULT_MAX_CHARS))

    def should_apply(self, view) -> bool:
        limit = self._limit()
        for m in view.messages:
            if m.get("role") != "tool":
                continue
            c = m.get("content")
            if isinstance(c, str) and len(c) > limit and _JSON_START_RE.match(c):
                return True
        return False

    def apply(self, view) -> TierOutcome:
        limit = self._limit()
        result: List[Dict[str, Any]] = []
        changed = 0
        for m in view.messages:
            c = m.get("content")
            if m.get("role") == "tool" and isinstance(c, str) and len(c) > limit:
                new_content = shape_json_text(c)
                if new_content != c:
                    m = {**m, "content": new_content}
                    changed += 1
            result.append(m)
        if changed == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无超长 JSON")
        try:
            saved = max(0, view.used_tokens - count_messages_tokens(result))
        except Exception:
            saved = 0
        return TierOutcome(messages=result, saved_tokens=saved, note=f"骨架化 {changed} 条 JSON 结果")


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolResultShapeTier())
