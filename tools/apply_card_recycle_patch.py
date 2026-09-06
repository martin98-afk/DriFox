# -*- coding: utf-8 -*-
"""一次性幂等补丁：让聊天新建的消息卡片进入回收体系（消除对话时内存无限增长）。

背景
----
每张消息卡正文是一个 `CodeWebViewer`（`message_card.py:3946`，继承 QWebEngineView）。
回收函数 `_recycle_out_of_view_batches` 只遍历 `_batch_cards` 且跳过 `None` 项，
而聊天新建的卡片有两重原因进不了这个视野：

  1. `_sync_batch_structures()` 只把 `_batch_cards` **扩展到新长度并填 None**，
     新建的 MessageCard 从不登记进去；
  2. 发消息/流式完成后只推进 `_visible_batch_end`，**从不推进
     `_visible_batch_start`** → active_start 恒为 0、active_end 恒为「全部批次
     + buffer」→ 回收范围为空，一张都不回收。

净效果：每发一条消息永久新增 1~2 个 WebEngine 页面，聊几十轮即达 1GB。

方案
----
  A. 新增 `_register_new_cards_into_batches()`：把 chat_layout 中尚未登记的
     MessageCard 按 `_message_index` 补进 `_batch_cards`（幂等，不重排、不动占位）。
  B. 新增 `_advance_visible_batch_window()`：同时推进 end 与 start，让 active
     窗口真正滑动起来，上方历史卡片才会被回收。
  C. 把 3 处「发 user 消息 / 子智能体回调 / 流式完成」的
     `self._visible_batch_end = len(self._message_batch)` 换成 A + B。

回收链路本身（等高占位 + 滚动补偿 + 跳过流式卡片 + renderer PID LRU kill）
全部复用，不重写。

用法
----
    python tools/apply_card_recycle_patch.py            # 应用
    python tools/apply_card_recycle_patch.py --revert   # 回滚
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _detect_eol(raw: bytes, default: str = "\n") -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf > lf:
        return "\r\n"
    if lf > 0:
        return "\n"
    return default


def write_text_keep_eol(path: str, text: str) -> str:
    """按文件原主流行尾写回（Windows 下裸 open(...,"w") 会把 \\n 静默翻成 \\r\\n）。

    注：原 tools/eol_guard.py 未纳入版本控制且已被清理，此处内联同名实现，
    行为与其 write_text_keep_eol 一致。
    """
    p = Path(path)
    eol = _detect_eol(p.read_bytes())
    p.write_bytes(text.replace("\r\n", "\n").replace("\n", eol).encode("utf-8"))
    return eol

MW = "app/main_widget.py"

# ── 改动 1：新增两个辅助方法（插在 _rebuild_batch_cards_from_layout 之前）──
OLD1 = """        elif new_len < len(self._batch_cards):
            self._batch_cards = self._batch_cards[:new_len]

    def _rebuild_batch_cards_from_layout(self) -> None:"""

NEW1 = """        elif new_len < len(self._batch_cards):
            self._batch_cards = self._batch_cards[:new_len]

    def _register_new_cards_into_batches(self) -> None:
        \"\"\"把 chat_layout 中尚未登记进 _batch_cards 的卡片补齐登记。

        🔧 内存修复（T5）：_sync_batch_structures() 只把 _batch_cards 扩展到新长度
        （填 None），新建的 MessageCard 不会自动登记；而回收函数
        _recycle_out_of_view_batches 只遍历 _batch_cards 且跳过 None 项，导致聊天
        新建的卡片永远进不了回收视野 —— 每张卡正文是一个 CodeWebViewer
        （QWebEngineView），长时间对话会无限累积，聊几十轮即达 1GB。

        幂等：已登记的批次只做去重追加，不重复、不覆盖、不重排、不动占位。
        \"\"\"
        n = len(self._batch_cards)
        if n == 0:
            return
        try:
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                widget = item.widget() if item else None
                if not isinstance(widget, MessageCard):
                    continue
                if getattr(widget, "_is_welcome", False):
                    continue
                if not self._is_widget_alive(widget):
                    continue
                mi = getattr(widget, "_message_index", None)
                if mi is None or not (0 <= mi < n):
                    continue
                if self._batch_cards[mi] is None:
                    self._batch_cards[mi] = [widget]
                elif widget not in self._batch_cards[mi]:
                    self._batch_cards[mi].append(widget)
        except Exception as e:
            logger.debug(f"[Memory] 登记新卡片到批次失败: {e}")

    def _advance_visible_batch_window(self) -> None:
        \"\"\"发新消息/流式完成后推进可见批次窗口（视口在底部语义）。

        🔧 内存修复（T5）：原代码只推进 _visible_batch_end 而不推进
        _visible_batch_start，于是 active_start 恒为 0、active_end 恒为
        「批次总数 + buffer」——回收范围为空，_recycle_out_of_view_batches
        永远不回收任何卡片。补推进 start 后，上方超出缓冲区的历史卡片才会被回收，
        底部新卡片仍在窗口内，不会被误删。
        \"\"\"
        self._visible_batch_end = len(self._message_batch)
        self._visible_batch_start = max(0, self._visible_batch_end - self._initial_visible_batch_count)

    def _rebuild_batch_cards_from_layout(self) -> None:"""

# ── 改动 2：三处调用点换用 A + B（12 空格缩进：16201 / 17098 / 17450）──
# 注意：锚点必须带前导换行 —— 否则 20/24 空格缩进的同名行会被当子串误匹配
OLD2 = """
            self._visible_batch_end = len(self._message_batch)
"""

NEW2 = """            # 🔧 T5：同时推进 start（否则回收范围为空）+ 登记新卡片（否则永不可回收）
            self._advance_visible_batch_window()
            self._register_new_cards_into_batches()
"""

EDITS = [
    (MW, OLD1, NEW1, 1),
    (MW, OLD2, NEW2, 3),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    pairs = [(p, new, old, n) for (p, old, new, n) in EDITS[::-1]] if args.revert else EDITS

    loaded: dict = {}
    for path, old, new, expect in pairs:
        full = ROOT / path
        if path not in loaded:
            loaded[path] = full.read_text(encoding="utf-8")
        got = loaded[path].count(old)
        if got != expect:
            print(f"[FAIL] {path}: 期望命中 {expect} 次，实际 {got} 次")
            print(repr(old[:120]))
            return 1

    for path, old, new, _ in pairs:
        loaded[path] = loaded[path].replace(old, new, 1 if old is OLD1 else 3)

    for path, text in loaded.items():
        used = write_text_keep_eol(str(ROOT / path), text)
        print(f"[OK] {path}  (行尾 {used!r})")

    print("\n已回滚" if args.revert else "\n已应用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
