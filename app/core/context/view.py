# -*- coding: utf-8 -*-
"""上下文投影对象 — cascade 的输入/输出载体。

ContextView 是「给模型看的那一份上下文」的临时视图。tier 只读取它、
只产出新 messages 列表，不原地修改 —— 保证 tier 幂等、可热替换、可观测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.infra.token_estimator import count_messages_tokens, per_message_tokens

def _msg_fingerprint(msg: Any) -> Tuple:
    """单条消息的轻量内容指纹：仅覆盖 token 计算消费的字段。

    用于 replace_messages 的增量对齐。单向保守：指纹相等 ⇒ per_message_tokens
    结果必相等（指纹不等也可能 token 相等——只多算不错算）。
    tuple 内直接持有原 str/list 引用（不复制内容），比较走 ==（str 为 memcmp
    级），远廉于 tiktoken 重编码。覆盖字段与 _compute_message_tokens 一致：
    role / content(None|str|list[type,text]) / reasoning_content / tool_calls
    (name+arguments) / tool_call_id。
    """
    if not isinstance(msg, dict):
        return (None,)
    content = msg.get("content")
    if content is None:
        c_sig = None
    elif isinstance(content, str):
        c_sig = content
    elif isinstance(content, list):
        c_sig = tuple(
            (it.get("type"), it.get("text", "")) if isinstance(it, dict) else None
            for it in content
        )
    else:
        c_sig = str(content)
    tool_calls = msg.get("tool_calls")
    if tool_calls and isinstance(tool_calls, list):
        tc_sig = tuple(
            (
                (tc.get("function") or {}).get("name") or "",
                (tc.get("function") or {}).get("arguments") or "",
            )
            if isinstance(tc, dict)
            else None
            for tc in tool_calls
        )
    else:
        tc_sig = None
    return (
        msg.get("role", ""),
        c_sig,
        msg.get("reasoning_content"),
        tc_sig,
        msg.get("tool_call_id"),
    )

@dataclass
class TierStat:
    """单层执行记录（供 UI / 日志展示每层省了多少、是否破坏 cache 前缀）"""

    tier_id: str
    label: str
    before_tokens: int = 0
    after_tokens: int = 0
    saved_tokens: int = 0
    cache_impact: str = ""
    note: str = ""
    skipped: bool = False  # True = 抛错被跳过

@dataclass
class ContextView:
    """一次上下文投影的完整状态。"""

    messages: List[Dict[str, Any]]
    budget: int = 0
    target_tokens: int = 0
    hard_limit: int = 0  # 模型窗口硬上限（0=未知）；紧急层据此判断是否已到极限
    llm_config: Dict[str, Any] = field(default_factory=dict)
    stage: str = "send"
    stats: List[TierStat] = field(default_factory=list)
    compaction_state: Dict[str, Any] = field(default_factory=dict)
    compaction_cache: Dict[str, Any] = field(default_factory=dict)
    failure_counts: Dict[str, int] = field(default_factory=dict)

    # used_tokens 惰性估算 + per-message 增量数组：与 messages 平行的指纹/单条
    # token 数组，replace_messages 按「共同前缀+后缀」对齐，仅重算被改条目，
    # 未改条目直接复用 —— 消除 tier 每层 applied 后的下层整表重算（tier 产出
    # 新列表使 count_messages_tokens 的 is 身份缓存必然 MISS）。
    _used_cache: Optional[int] = field(default=None, repr=False, compare=False)
    _msg_fps: List[Tuple] = field(default_factory=list, repr=False, compare=False)
    _per_msg_tokens: List[int] = field(default_factory=list, repr=False, compare=False)

    @property
    def used_tokens(self) -> int:
        if self._used_cache is None:
            try:
                self._rebuild_tokens()
            except Exception:
                self._used_cache = 0
        return self._used_cache or 0

    def replace_messages(self, new_messages: List[Dict[str, Any]]) -> None:
        """唯一允许的 messages 写入口：增量维护 token 数组并同步 used 缓存。

        增量对齐（共同前缀+后缀）：指纹相等的条目直接复用旧单条 token，仅重算
        被改条目（追加/替换/删头等 tier 常见形态各只付被改部分的成本）。对齐
        任何异常回退全量重算，语义与旧实现（count_messages_tokens 整表）一致。
        """
        old_msgs = self.messages
        self.messages = new_messages
        try:
            self._refresh_tokens_incremental(old_msgs)
        except Exception:
            # 兜底：与旧语义完全一致（全量重算，失败计 0）
            self._msg_fps = []
            self._per_msg_tokens = []
            try:
                self._used_cache = count_messages_tokens(new_messages)
            except Exception:
                self._used_cache = 0

    def _rebuild_tokens(self) -> None:
        """全量建档：指纹 + 单条 token 与 messages 逐一对齐，并刷新 used 缓存。

        per_message_tokens 与 count_messages_tokens([msg]) 公开等价（见其
        docstring），逐条累加 == 整表计算（tools/ratio 两参 view 语义下均为
        缺省：无 tools、ratio=1.0）。
        """
        msgs = self.messages
        self._msg_fps = [_msg_fingerprint(m) for m in msgs]
        self._per_msg_tokens = [per_message_tokens(m) for m in msgs]
        self._used_cache = max(0, sum(self._per_msg_tokens))

    def _refresh_tokens_incremental(self, old_msgs: List[Dict[str, Any]]) -> None:
        """前缀+后缀对齐的增量重算：被改条目才付 token 成本。

        old 数组无效（首次/长度失配，含外部绕过 replace_messages 改写 messages
        的防御）时退化为全量建档。对齐判据是指纹相等；中间段新条目按位重算。
        """
        new_msgs = self.messages
        old_fps = self._msg_fps
        if not old_fps or len(old_fps) != len(old_msgs):
            self._rebuild_tokens()
            return
        n_cmp = min(len(old_msgs), len(new_msgs))
        pre = 0
        while pre < n_cmp and _msg_fingerprint(new_msgs[pre]) == old_fps[pre]:
            pre += 1
        suf = 0
        while suf < n_cmp - pre and _msg_fingerprint(new_msgs[-1 - suf]) == old_fps[-1 - suf]:
            suf += 1
        new_len = len(new_msgs)
        new_fps: List[Tuple] = []
        new_tokens: List[int] = []
        for i, m in enumerate(new_msgs):
            if i < pre:
                new_fps.append(old_fps[i])
                new_tokens.append(self._per_msg_tokens[i])
            elif i >= new_len - suf:
                j = len(old_msgs) - (new_len - i)
                new_fps.append(old_fps[j])
                new_tokens.append(self._per_msg_tokens[j])
            else:
                new_fps.append(_msg_fingerprint(m))
                new_tokens.append(per_message_tokens(m))
        self._msg_fps = new_fps
        self._per_msg_tokens = new_tokens
        self._used_cache = max(0, sum(new_tokens))

    def record(self, stat: TierStat) -> None:
        self.stats.append(stat)

    def note_failure(self, tier_id: str) -> int:
        """连续无收益计数 +1，返回当前值"""
        n = self.failure_counts.get(tier_id, 0) + 1
        self.failure_counts[tier_id] = n
        return n

    def clear_failure(self, tier_id: str) -> None:
        self.failure_counts.pop(tier_id, None)
