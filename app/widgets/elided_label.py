# -*- coding: utf-8 -*-
"""
ElidedLabel - 自动根据可用宽度省略文本的 QLabel（中间省略）

从 mcp_setting_card._ElidedLabel 提取为共享模块，消除多处重复定义。

可选 hover 滚动播放：``hover_scroll=True`` 的实例在鼠标悬停、且文本确实
超宽时，暂不省略，改由自绘把全文横向来回滑动（详见类注释）。
"""

import html
import re
from typing import List

from PyQt5.QtCore import QElapsedTimer, QEvent, QPointF, QSize, Qt, QTimer
from PyQt5.QtGui import QFontMetrics, QPainter
from PyQt5.QtWidgets import QLabel, QSizePolicy, QToolTip

# 控制字符 → 空格（单行归一化）。
# 只处理换行/制表/回车这类**会引发换行或异常排版**的字符；不碰普通空格，
# 也不做连续空白折叠（省略测量要跟字符数一一对应，折叠会改变长度）。
_CONTROL_CHARS_PATTERN = re.compile(r"[\r\n\t\v\f]")

# ── hover 滚动播放参数 ──────────────────────────────────
# 悬停后先静置一小段再动：鼠标划过列表时不该满屏跑字
_SCROLL_START_DELAY_MS = 350
# ≈60fps。同一时刻只有鼠标下的那一个标签在滚，单帧只是一次 drawText
_SCROLL_INTERVAL_MS = 16
_SCROLL_SPEED_PX_S = 45.0  # 基准速度（px/s）
_SCROLL_MAX_TRAVERSE_S = 8.0  # 超长文本提速，保证一趟不超过 8 秒
_SCROLL_EDGE_PAUSE_S = 0.9  # 到两端各停一下，首尾读得清


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

    **hover 滚动播放**（``hover_scroll=True``）：鼠标悬停且
    全文宽于可视宽时，省略号让位给自绘的横向滑动（0 → 尾部 → 0 来回），并在
    端点停顿约 0.9s；移出即复位为常规省略。**滚动实例不挂 tooltip**：全文自己会
    滚出来，而 tooltip 会盖住滚动行；另在起滚瞬间调 ``QToolTip.hideText()``
    收掉起滚前就已弹出的那个（其延迟比 350ms 短时会发生）。带搜索高亮（富文本 span）
    时不滚动，自绘纯文本会丢掉高亮区间。
    """

    def __init__(self, text: str = "", parent=None, elide_mode=Qt.ElideMiddle, hover_scroll: bool = False):
        super().__init__(text, parent)
        self._full_text = text
        self._elide_mode = elide_mode  # 省略方式（标题/预览用 Middle，角色名用 Right）
        self._hl_query = ""
        self._hl_queries: List[str] = []  # 多关键字高亮
        self._hl_color = ""
        # ── 全文宽度缓存（避免每拍 horizontalAdvance 全文重测）──
        # 失效点三处缺一不可：setText / setHighlights / FontChange 事件。
        # **不要在 resizeEvent 里清**：文本宽度与控件宽度无关，清掉反而让每次
        # resize 都重测全文；宽度变化已由 _last_elided_w 短路判定覆盖。
        self._full_text_w: int | None = None
        self._last_elided_w: int = -1
        # ── hover 滚动播放状态（定时器懒建：只有真正滚过的实例才持有）──
        self._hover_scroll = hover_scroll
        self._hovering = False
        self._scroll_timer: QTimer | None = None
        self._scroll_delay_timer: QTimer | None = None
        self._scroll_clock = QElapsedTimer()
        self._scroll_offset = 0.0  # 当前左移像素（>=0）
        self._scroll_overflow = 0  # 可滚距离 = 全文宽 - 可视宽
        self._scroll_speed = _SCROLL_SPEED_PX_S
        self._scroll_dir = 1
        self._scroll_pause_left = 0.0
        self._scroll_last_time = 0.0
        if not hover_scroll:  # 滚动实例不挂 tooltip：滚动本身就是全文提示
            self.setToolTip(self._full_text)
        # 防止布局根据文本内容自动扩展宽度，确保宽度由父布局决定
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str):
        """设置完整文本，同时清除高亮"""
        self._full_text = text
        self._full_text_w = None  # 文本变了，全文宽度缓存作废
        self._last_elided_w = -1  # 内容换了，同宽度也要重算
        self._hl_query = ""
        self._hl_queries = []
        self._hl_color = ""
        if not self._hover_scroll:
            self.setToolTip(self._full_text)
        self._reset_scroll()  # 换文本后旧的滚动偏移没有意义
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
        self._full_text_w = None  # 高亮态可能切富文本渲染，全文宽度需重测
        self._last_elided_w = -1  # 渲染分支变了，同宽度也要重算
        self._reset_scroll()  # 富文本高亮与自绘滚动互斥，进高亮态先停滚
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()
        # 注意：不在这里停滚。hover 弹出关闭按钮等会让标题变窄 → 恰好是本控件
        # resize；停掉就再也滚不起来。宽度变化由滚动 tick 重算可滚距离抵消。

    # ── hover 滚动播放 ────────────────────────────────────

    def enterEvent(self, event):
        super().enterEvent(event)
        self._hovering = True
        if not self._hover_scroll_allowed():
            return
        self._ensure_scroll_timers()
        self._scroll_delay_timer.start(_SCROLL_START_DELAY_MS)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hovering = False
        self._reset_scroll()

    def hideEvent(self, event):
        # 折叠/切页时可能收不到 leaveEvent，隐藏即复位，避免留下半截滚动偏移
        super().hideEvent(event)
        self._hovering = False
        self._reset_scroll()

    def event(self, event):
        # 兜底：滚动中吞掉 tooltip 显示请求（外部仍可能给实例 setToolTip）
        if event.type() == QEvent.ToolTip and self._is_scrolling():
            return True
        return super().event(event)

    def _hover_scroll_allowed(self) -> bool:
        """实例开关 + 不与高亮富文本冲突"""
        return self._hover_scroll and not self._hl_queries

    def _scroll_overflow_px(self) -> int:
        """全文宽与可视宽之差（<=0 表示不需要滚动）"""
        return self._full_text_width() - self.width()

    def _full_text_width(self) -> int:
        """全文渲染宽度（单行归一化后），带缓存。

        滚动 tick 每 16ms 调一次，而 horizontalAdvance 对长标题是实打实的
        字形测量；缓存后滚动期零测量。
        """
        if self._full_text_w is None:
            self._full_text_w = self.fontMetrics().horizontalAdvance(
                _to_single_line(self._full_text)
            )
        return self._full_text_w

    def changeEvent(self, event):
        """字体变化时作废全文宽度缓存（字号/字体切换会改变文本宽度）。"""
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._full_text_w = None
            self._last_elided_w = -1  # 宽度未变但字号变了，必须重算一次
            self._update_elided()

    def _is_scrolling(self) -> bool:
        return self._scroll_timer is not None and self._scroll_timer.isActive()

    def _ensure_scroll_timers(self) -> None:
        if self._scroll_timer is not None:
            return
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(_SCROLL_INTERVAL_MS)
        self._scroll_timer.timeout.connect(self._on_scroll_tick)
        self._scroll_delay_timer = QTimer(self)
        self._scroll_delay_timer.setSingleShot(True)
        self._scroll_delay_timer.timeout.connect(self._begin_scroll)

    def _reset_scroll(self) -> None:
        """停止滚动并回到常规省略渲染（幂等，未初始化定时器时直接返回）"""
        if self._scroll_delay_timer is not None:
            self._scroll_delay_timer.stop()
        was_scrolling = self._is_scrolling()
        if self._scroll_timer is not None and was_scrolling:
            self._scroll_timer.stop()
        # 必须重绘：停在 offset=0 的暂停帧也是自绘全文（没有省略号），
        # 只停定时器不重绘会留下这帧，直到别的事件碰巧触发重绘
        if self._scroll_offset or was_scrolling:
            self._scroll_offset = 0.0
            self.update()

    def _begin_scroll(self) -> None:
        """延迟到期后确认确实超宽才起滚（延迟期间布局可能已变宽/变窄）"""
        if not self._hovering or not self._hover_scroll_allowed():
            return
        overflow = self._scroll_overflow_px()
        if overflow <= 0 or not _to_single_line(self._full_text):
            return
        self._scroll_overflow = overflow
        self._scroll_speed = max(_SCROLL_SPEED_PX_S, overflow / _SCROLL_MAX_TRAVERSE_S)
        self._scroll_offset = 0.0
        self._scroll_dir = 1
        self._scroll_pause_left = 0.0
        self._scroll_last_time = 0.0
        self._scroll_clock.restart()
        # 系统 tooltip 延迟可能短于起滚延迟：已经弹出的那个不会自己消失，主动收掉
        QToolTip.hideText()
        self._scroll_timer.start()
        self.update()

    def _on_scroll_tick(self) -> None:
        now = self._scroll_clock.elapsed() / 1000.0
        dt = now - self._scroll_last_time
        self._scroll_last_time = now
        if dt <= 0.0 or dt > 0.5:  # 首帧 / 事件循环卡顿后不追帧，避免瞬移
            return
        if self._scroll_pause_left > 0.0:
            self._scroll_pause_left -= dt
            if self._scroll_pause_left > 0.0:
                return
            dt = -self._scroll_pause_left  # 用停顿时长溢出量继续推进，节奏不断档
            self._scroll_pause_left = 0.0
            self._scroll_dir = -self._scroll_dir

        # 宽度可能在滚动中被改（hover 弹出关闭按钮、侧栏拖拽），随时重算
        overflow = self._scroll_overflow_px()
        if overflow != self._scroll_overflow:
            self._scroll_overflow = overflow
            if overflow <= 0:  # 变宽到不再超宽：直接收工
                self._reset_scroll()
                return
            self._scroll_speed = max(_SCROLL_SPEED_PX_S, overflow / _SCROLL_MAX_TRAVERSE_S)
            self._scroll_offset = min(self._scroll_offset, float(overflow))

        self._scroll_offset += self._scroll_speed * dt * self._scroll_dir
        if self._scroll_offset >= self._scroll_overflow:
            self._scroll_offset = float(self._scroll_overflow)
            self._scroll_pause_left = _SCROLL_EDGE_PAUSE_S
        elif self._scroll_offset <= 0.0:
            self._scroll_offset = 0.0
            self._scroll_pause_left = _SCROLL_EDGE_PAUSE_S
        self.update()

    def paintEvent(self, event):
        if not self._is_scrolling():
            super().paintEvent(event)
            return
        text = _to_single_line(self._full_text)
        if not text:
            return
        # 自绘滚动：取 palette 的 foreground 色，QSS 的 color 会落到 palette 上
        # （实测 label.setStyleSheet("color: rgb(...)") 后 palette().windowText 即该色）
        rect = self.contentsRect()
        fm = self.fontMetrics()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setClipRect(rect)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        baseline = rect.top() + (rect.height() - fm.height()) // 2 + fm.ascent()
        painter.drawText(QPointF(rect.left() - self._scroll_offset, baseline), text)

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
            self._last_elided_w = w
            return

        # 宽度短路：同一宽度重复进入是无意义重算（resize/setText/高亮都可能
        # 连打多次）。宽度的真实变化仍会走到下面的重算，故不是棘轮锁死。
        if w == self._last_elided_w:
            return
        self._last_elided_w = w

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
        """标准省略（默认 ElideMiddle；角色名等前缀有辨识度的文本可改 ElideRight）"""
        fm = self.fontMetrics()
        return fm.elidedText(_to_single_line(self._full_text), self._elide_mode, width)

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
