# -*- coding: utf-8 -*-
"""
ElidedLabel - 自动根据可用宽度省略文本的 QLabel（中间省略）

从 mcp_setting_card._ElidedLabel 提取为共享模块，消除多处重复定义。
"""

import html
from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QSizePolicy


class _ElidedLabel(QLabel):
    """自动根据可用宽度省略文本的 QLabel（中间省略），可选支持搜索匹配高亮

    当有搜索高亮且匹配内容落在省略区域时，自动切换为「以匹配为中心」的智能省略，
    确保匹配内容始终可见。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self._hl_query = ""
        self._hl_queries: List[str] = []  # 多关键字高亮
        self._hl_color = ""
        self.setToolTip(self._full_text)
        # 防止布局根据文本内容自动扩展宽度，确保宽度由父布局决定
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str):
        """设置完整文本，同时清除高亮"""
        self._full_text = text
        self._hl_query = ""
        self._hl_queries = []
        self._hl_color = ""
        self.setToolTip(self._full_text)
        self._update_elided()

    def setHighlight(self, query: str, color: str):
        """设置搜索高亮：匹配部分用 <span> 高亮

        Args:
            query: 搜索关键词（不区分大小写匹配）
            color: 高亮颜色（CSS 格式，如 "#FF6600"）
        """
        self._hl_query = query
        self._hl_queries = [query]  # 单关键字时列表只有一项
        self._hl_color = color
        self._update_elided()

    def setHighlights(self, queries: List[str], color: str):
        """设置多关键字搜索高亮：每个匹配部分都用 <span> 高亮

        Args:
            queries: 搜索关键词列表（不区分大小写匹配）
            color: 高亮颜色
        """
        self._hl_queries = list(queries)
        self._hl_query = queries[0] if queries else ""  # 主查询用于省略锚定
        self._hl_color = color
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    # ── 核心省略逻辑 ──────────────────────────────────────

    def _update_elided(self):
        w = self.width()
        if w <= 0:
            # 未布局：直接显示原文。无条件强制 PlainText（QLabel 同值设置短路
            # 零开销），避免原文含 < > 时 AutoText 误判为富文本（标签被吞/渲染错乱）
            self.setTextFormat(Qt.PlainText)
            if self.text() != self._full_text:
                super().setText(self._full_text)
            return

        # 决定使用哪种省略策略
        if self._hl_query and self._hl_color:
            elided = self._elide_highlight_aware(w)
        else:
            elided = self._plain_elide(w)

        if elided is None:  # 不需要省略
            return

        # 应用高亮（如果 elided 中有匹配）
        if self._hl_queries and self._hl_color:
            # 对所有高亮 query 依次查找，合并重叠区间后一次性构建 HTML
            lower_elided = elided.lower()
            spans = []
            for q in self._hl_queries:
                if not q:
                    continue
                lower_q = q.lower()
                idx = lower_elided.find(lower_q)
                if idx >= 0:
                    spans.append((idx, idx + len(q)))
            # 合并重叠/相邻区间
            if spans:
                spans.sort()
                merged = [spans[0]]
                for s in spans[1:]:
                    if s[0] <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], s[1]))
                    else:
                        merged.append(s)
                # 从原文一次构建：普通部分 escape，匹配部分加 <span>
                parts = []
                pos = 0
                for start, end in merged:
                    if pos < start:
                        parts.append(html.escape(elided[pos:start]))
                    parts.append(
                        f'<span style="color: {self._hl_color}; font-weight: bold;">'
                        f"{html.escape(elided[start:end])}</span>"
                    )
                    pos = end
                if pos < len(elided):
                    parts.append(html.escape(elided[pos:]))
                html_text = "".join(parts)
                # 高亮 HTML 含 <span>，无条件按富文本渲染（span 颜色才生效）
                self.setTextFormat(Qt.RichText)
                if self.text() != html_text:
                    super().setText(html_text)
                return

        # 无高亮/未匹配 → 纯文本
        # 原文可能含 < > 等字符：无条件强制 PlainText 字面显示，避免 AutoText
        # 误判为富文本（标签被吞/渲染错乱）；省略测量基于原文宽度，与渲染一致
        self.setTextFormat(Qt.PlainText)
        if self.text() != elided:
            super().setText(elided)

    def _plain_elide(self, width: int) -> str:
        """标准 ElideMiddle 省略"""
        fm = self.fontMetrics()
        return fm.elidedText(self._full_text, Qt.ElideMiddle, width)

    def _elide_highlight_aware(self, width: int) -> str:
        """高亮感知的智能省略 — 确保匹配内容始终可见"""
        fm = self.fontMetrics()
        text = self._full_text
        query = self._hl_query.lower()

        # 若全文可见，无需省略
        if fm.horizontalAdvance(text) <= width:
            return text

        # 在原文中找匹配位置（不区分大小写）
        match_start = text.lower().find(query)
        if match_start < 0:
            return self._plain_elide(width)

        match_end = match_start + len(query)
        actual_match = text[match_start:match_end]
        match_w = fm.horizontalAdvance(actual_match)

        # 极端情况：匹配文本本身超宽
        if match_w > width:
            return fm.elidedText(actual_match, Qt.ElideRight, width)

        # 策略 1：尝试 ElideLeft（匹配靠右时生效）
        elided_left = fm.elidedText(text, Qt.ElideLeft, width)
        if actual_match.lower() in elided_left.lower():
            return elided_left

        # 策略 2：尝试 ElideRight（匹配靠左时生效）
        elided_right = fm.elidedText(text, Qt.ElideRight, width)
        if actual_match.lower() in elided_right.lower():
            return elided_right

        # 策略 3：匹配在中间 → 构建 match-centered 省略
        return self._build_match_centered(text, match_start, match_end, width)

    def _build_match_centered(self, text: str, match_start: int, match_end: int, width: int) -> str:
        """从匹配位置向两侧扩展，构建以匹配为中心的省略文本"""
        fm = self.fontMetrics()
        ellipsis = "…"
        ellipsis_w = fm.horizontalAdvance(ellipsis)

        prefix = text[:match_start]
        actual_match = text[match_start:match_end]
        suffix = text[match_end:]

        match_w = fm.horizontalAdvance(actual_match)
        # 保留空间：匹配 + 左右各一个省略号
        available = width - match_w - 2 * ellipsis_w
        if available < 0:
            # 连两个省略号都放不下，只保留一个
            available = width - match_w - ellipsis_w
            if available < 0:
                return fm.elidedText(actual_match, Qt.ElideRight, width)

        # 从两侧贪婪地添加字符
        left_pad = ""
        right_pad = ""
        left_w = 0
        right_w = 0

        # 右侧优先（文件扩展名/末尾信息通常更重要）
        for ch in suffix:
            ch_w = fm.horizontalAdvance(ch)
            if right_w + ch_w <= available:
                right_pad += ch
                right_w += ch_w
            else:
                break

        # 剩余空间给左侧
        left_avail = available - right_w
        for ch in reversed(prefix):
            ch_w = fm.horizontalAdvance(ch)
            if left_w + ch_w <= left_avail:
                left_pad = ch + left_pad
                left_w += ch_w
            else:
                break

        # 组装结果
        clip_left = left_pad != prefix
        clip_right = right_pad != suffix

        result = ""
        if clip_left:
            result += ellipsis
        result += left_pad + actual_match + right_pad
        if clip_right:
            result += ellipsis

        # 保险：最终检查宽度，超了就从右往左截
        while fm.horizontalAdvance(result) > width and (left_pad or right_pad):
            if right_pad:
                right_pad = right_pad[:-1]
            elif left_pad:
                left_pad = left_pad[:-1]
            result = ""
            if clip_left:
                result += ellipsis
            result += left_pad + actual_match + right_pad
            if clip_right:
                result += ellipsis

        return result
