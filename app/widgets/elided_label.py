# -*- coding: utf-8 -*-
"""
ElidedLabel - 自动根据可用宽度省略文本的 QLabel（中间省略）

从 mcp_setting_card._ElidedLabel 提取为共享模块，消除多处重复定义。
"""

import html
import re
from typing import List

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QSizePolicy

# 控制字符 → 空格（单行归一化）。
# 只处理换行/制表/回车这类**会引发换行或异常排版**的字符；不碰普通空格，
# 也不做连续空白折叠（省略测量要跟字符数一一对应，折叠会改变长度）。
_CONTROL_CHARS_PATTERN = re.compile(r"[\r\n\t\v\f]")


def _to_single_line(text: str) -> str:
    """把含控制字符的文本归一成单行（控制字符替成空格，长度不变）

    长度不变是刻意约束：高亮分支要先在归一化文本上定位匹配、再回到原文切分
    并拼 <span>，字符数对齐才能保证下标一致。
    """
    if not text:
        return text
    return _CONTROL_CHARS_PATTERN.sub(" ", text)


class _ElidedLabel(QLabel):
    """自动根据可用宽度省略文本的 QLabel（中间省略），可选支持搜索匹配高亮

    当有搜索高亮且匹配内容落在省略区域时，自动切换为「以匹配为中心」的智能省略，
    确保匹配内容始终可见。

    ⚠️ **单行语义**：本控件按单行渲染（行高由外部会话行/列表行钉死），而原文
    （会话预览、命令描述等）常带 ``\\n``／``\\t`` 等控制字符。若直接交给 QLabel，
    Qt 会真换行渲染，但 :meth:`QFontMetrics.elidedText` 把控制字符当**零宽**处理
    （实测 ``"a\\nb"`` 比 ``"a b"`` 窄 7px），测量与渲染不一致 → 省略结果仍含
    ``\\n`` → 文本被行高截断。因此渲染/测量统一走单行归一化；``_full_text`` 保留
    原文（tooltip 与 setText 回读语义不变）。
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

    def sizeHint(self):
        """sizeHint 返回**完整文本**宽度（受 maximumWidth 封顶），而非当前显示文本宽度。

        若不重写，省略发生后 QLabel.sizeHint() 会缩水到省略文本宽度，布局据此
        收窄本控件 → 永远凑不出足够宽度 → 省略态锁死（棘轮），即使容器后来
        有充足空间也不恢复。以完整文本宽度作为 sizeHint 后：布局优先给足空间，
        只有容器真不够时才省略（兜底），放宽后 resizeEvent 自动恢复。
        """
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(_to_single_line(self._full_text)) + 2  # +2 抗锯齿边缘余量，防压线误省略
        return QSize(min(w, self.maximumWidth()), super().sizeHint().height())

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
            # 未布局：直接显示原文（单行归一化）。无条件强制 PlainText（QLabel
            # 同值设置短路零开销），避免原文含 < > 时 AutoText 误判为富文本
            # （标签被吞/渲染错乱）
            self.setTextFormat(Qt.PlainText)
            single = _to_single_line(self._full_text)
            if self.text() != single:
                super().setText(single)
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
        """标准 ElideMiddle 省略（输入先单行归一化，输出即无控制字符）"""
        fm = self.fontMetrics()
        return fm.elidedText(_to_single_line(self._full_text), Qt.ElideMiddle, width)

    def _elide_highlight_aware(self, width: int) -> str:
        """高亮感知的智能省略 — 确保匹配内容始终可见

        匹配在**归一化后**的文本上做：原文含 ``\\n`` 时，若按原文切分，拼装出的
        省略串仍会带控制字符（渲染换行）。归一化把控制字符换成空格，字符数不变，
        故匹配下标对原文同样成立。
        """
        fm = self.fontMetrics()
        text = _to_single_line(self._full_text)
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
