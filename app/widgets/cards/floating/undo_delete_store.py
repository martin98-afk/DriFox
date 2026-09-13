# -*- coding: utf-8 -*-
"""撤销删除状态仓库 —— 消息级「删除 / 撤销」的可回退条目栈

替代原先散落在 main_widget 的两套状态：

- 裸 dict ``_undo_delete_cache``（只缓存一步，生命周期靠 6 处手工 ``= {}`` 维持）
- 死代码 ``_undo_delete_stack``（声明为「缓存栈」但全仓零引用）

设计：
- :class:`UndoEntry`：类型化的一次可回退操作（删除单轮 / 撤销到某轮）；
- :class:`UndoDeleteStore`：LIFO 栈，超过 ``max_entries`` 时丢弃**最旧**条目
  —— 最旧的删除早已生效、后续又叠加了新操作，丢弃它不会造成状态不一致，
  只会让用户无法再回退那么远；
- 单一入口：``push`` / ``peek`` / ``pop`` / ``clear``，不再有旁路清空。

``pop`` 是「取出即失效」语义：恢复动作必须先从栈里拿走条目，防止重复恢复
把同一段消息插入两次。

仓库本身不持有 session / widget 引用，纯数据；会话一致性由 ``session_id``
在恢复时校验（校验失败即丢弃该条目并给出用户反馈）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 条目来源：删除单轮（删除的是中间一段）
KIND_DELETE_ROUND = "delete_round"
# 条目来源：撤销到某轮（删除的是该轮及其之后的全部内容）
KIND_UNDO_TO_ROUND = "undo_to_round"

# 卡片主文案模板（按来源区分语义，修掉旧实现两种语义共用一句话的问题）
_LABEL_TEMPLATES = {
    KIND_DELETE_ROUND: "已删除 {count} 条消息",
    KIND_UNDO_TO_ROUND: "已撤销 {count} 条消息",
}


@dataclass
class UndoEntry:
    """一次可回退的消息删除操作（快照 + 回填锚点）

    Attributes:
        session_id: 发生删除时的会话 ID（恢复前校验，防止跨会话误恢复）
        messages: 被移除的消息（canonical 形态，直接回填 session.messages）
        insert_index: 回填到 ``session.messages`` 的下标
        count: 被移除的消息条数（卡片文案展示用）
        kind: :data:`KIND_DELETE_ROUND` 或 :data:`KIND_UNDO_TO_ROUND`
        layout_index: 被移除区域在 ``chat_layout`` 中的起始下标。
            ``None`` 表示移除的是尾部内容，回填时直接追加。
        file_restore_ops: 撤销时一并回滚的文件操作，元素为
            ``{"file_path": str, "content": bytes}``（内容为 AI 编辑后的快照，
            恢复消息时写回磁盘，消除「半恢复」）
        note: 被删内容摘要，用于卡片 tooltip
        created_at: 入栈时间（调试 / 过期诊断用）
    """

    session_id: str
    messages: List[Dict[str, Any]]
    insert_index: int
    count: int
    kind: str
    layout_index: Optional[int] = None
    file_restore_ops: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        """卡片主文案（单一数据源，避免卡片与主程序各写一套）"""
        template = _LABEL_TEMPLATES.get(self.kind, _LABEL_TEMPLATES[KIND_DELETE_ROUND])
        return template.format(count=self.count)

    @property
    def appends_at_tail(self) -> bool:
        """恢复时是否直接追加到对话末尾（撤销到某轮必然是尾部移除）"""
        return self.layout_index is None


class UndoDeleteStore:
    """撤销条目栈（LIFO）"""

    # 栈深上限：超过后丢弃最旧条目。20 步已远超正常使用（连续删 20 轮），
    # 只为兜住「长会话里反复删除」时的内存增长。
    MAX_ENTRIES = 20

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._entries: List[UndoEntry] = []
        self._max_entries = max(1, int(max_entries))

    # ── 写 ──────────────────────────────────────────────

    def push(self, entry: UndoEntry) -> None:
        """记录一次新操作；超上限时丢弃最旧条目"""
        self._entries.append(entry)
        overflow = len(self._entries) - self._max_entries
        if overflow > 0:
            del self._entries[:overflow]

    def pop(self) -> Optional[UndoEntry]:
        """取出最近一次操作（取出即失效，防重复恢复）"""
        if not self._entries:
            return None
        return self._entries.pop()

    def clear(self, reason: str = "") -> int:
        """整体失效（TTL 到期 / 用户关闭 / 会话切换），返回被丢弃的条目数"""
        dropped = len(self._entries)
        if dropped:
            self._entries.clear()
            from loguru import logger

            logger.debug(
                f"[UNDO-STORE] cleared {dropped} entr{'y' if dropped == 1 else 'ies'} reason={reason or 'n/a'}"
            )
        return dropped

    # ── 读 ──────────────────────────────────────────────

    def peek(self) -> Optional[UndoEntry]:
        """只读栈顶（卡片文案渲染用，不改状态）"""
        return self._entries[-1] if self._entries else None

    def entries(self) -> tuple[UndoEntry, ...]:
        """栈内容快照（自底向上，测试用）"""
        return tuple(self._entries)

    @property
    def depth(self) -> int:
        """可回退步数"""
        return len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)
