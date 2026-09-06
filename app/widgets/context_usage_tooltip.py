# -*- coding: utf-8 -*-
"""
上下文占比条 Tooltip — WorkBuddy 风格

替代原生 QToolTip 纯文本，渲染：
  - 总占用 / 预算 / 占比（按占比变色）
  - 单条堆叠比例条（各类型上下文占比）
  - 图例：色块 + 类型名 + token 数 + 百分比
  - 压缩上下文分解（若启用）
  - 缓存统计（若命中）

纯自绘 + 布局混合，深色主题自适应。
"""

from typing import Dict, List

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import _get_global_font, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css


def _parse_color(value, fallback: str = "#212126") -> QColor:
    """将主题色字符串解析为 QColor。

    QColor 的字符串构造函数只认 #rrggbb / #aarrggbb / 具名色，
    不认 CSS 的 rgba()/rgb() 写法；主题里大量使用 rgba(r,g,b,a)，
    直接丢给 QColor 会解析失败退化为黑色。此处手动解析。
    """
    if isinstance(value, QColor):
        return value
    s = str(value or "").strip()
    try:
        if s.startswith("#"):
            c = QColor(s)
            if c.isValid():
                return c
        elif s.lower().startswith(("rgba(", "rgb(")):
            inner = s[s.index("(") + 1 : s.rindex(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                r, g, b = (int(round(float(parts[i]))) for i in range(3))
                a = 255
                if len(parts) >= 4:
                    av = float(parts[3])
                    a = int(round(av * 255)) if av <= 1 else int(round(av))
                return QColor(
                    max(0, min(255, r)),
                    max(0, min(255, g)),
                    max(0, min(255, b)),
                    max(0, min(255, a)),
                )
        else:
            c = QColor(s)
            if c.isValid():
                return c
    except Exception:
        pass
    fc = QColor(fallback)
    return fc if fc.isValid() else QColor(33, 33, 38, 246)


class _StackedBar(QWidget):
    """单条堆叠比例条（带圆角裁剪），末端显示百分比文字"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self._segments: List[tuple] = []  # [(fraction, color_str), ...]
        self._track = QColor(255, 255, 255, 28)
        self._text_on_fill = Qt.white
        self._text_on_empty = QColor(255, 255, 255, 180)
        self._percent = 0

    def set_theme_colors(
        self,
        track_color: QColor,
        text_on_fill: QColor = Qt.white,
        text_on_empty: QColor = QColor(255, 255, 255, 180),
    ):
        """设置主题感知的轨道底色和文字颜色"""
        self._track = track_color
        self._text_on_fill = text_on_fill
        self._text_on_empty = text_on_empty
        # 主动触发重绘（仅 set_segments 也调用 update，但若仅切主题时分段不变则不会重绘）
        self.update()

    def set_segments(self, segments: List[tuple], percent: int = 0):
        """设置堆叠分段与整体百分比，百分比会在条末端显示"""
        self._segments = segments or []
        self._percent = max(0, min(100, int(percent or 0)))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        rect = self.rect()
        r = rect.height() / 2
        w_total = rect.width()

        # 裁剪到圆角矩形，使分段看起来是一条圆角条
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(rect), r, r)
        painter.setClipPath(clip)

        # 背景轨道（代表整个上下文预算）
        painter.setBrush(self._track)
        painter.drawRoundedRect(rect, r, r)

        # 各分段按「占整个上下文预算」的比例绘制，空闲部分留为轨道
        x = 0
        for frac, color in self._segments:
            if frac <= 0:
                continue
            if x >= w_total:
                break
            w = int(w_total * frac)
            if x + w > w_total:
                w = w_total - x
            if w <= 0:
                continue
            painter.setBrush(QColor(color))
            painter.drawRect(int(x), rect.top(), w, rect.height())
            x += w

        painter.setClipping(False)

        # — 百分比文字（最右侧） —
        # 计算填充区域宽度，用于决定文字颜色（白 vs 半透明白）
        fill_w = int(w_total * self._percent / 100.0)
        if self._percent > 0:
            fill_w = max(fill_w, int(r * 2))  # 最少显示圆头

        font = QFont(_get_global_font(), scale_font_size(10))
        font.setWeight(QFont.Bold)
        painter.setFont(font)

        pct_text = f"{self._percent}%"
        fm = QFontMetrics(font)
        text_w = fm.width(pct_text)
        text_x = w_total - text_w - 6  # 距右边缘 6px
        text_y = int((rect.height() + fm.ascent()) / 2) - 1

        # 数字落在填充区上用 fill 色，否则用 empty 色（随主题自适应）
        if text_x < fill_w and self._percent > 0:
            painter.setPen(self._text_on_fill)
        else:
            painter.setPen(self._text_on_empty)
        painter.drawText(text_x, text_y, pct_text)
        painter.setPen(Qt.NoPen)


class ContextBreakdownTooltip(QWidget):
    """上下文占比浮动卡片"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._data: Dict = {}
        self._load_theme_colors()
        self._build_ui()

    def _load_theme_colors(self):
        """从当前主题读取卡片背景/边框/文字色（跟随主题，替代硬编码黑底）"""
        try:
            from app.utils.design_tokens import current_theme

            theme = current_theme()
        except Exception:
            theme = {}
        self._card_bg = theme.get("card_bg_solid") or "rgba(33, 33, 38, 0.96)"
        self._border = theme.get("border") or "#3d3d3d"
        self._text_primary = theme.get("text_primary") or "#ffffff"
        self._text_secondary = theme.get("text_secondary") or "rgba(255, 255, 255, 0.5)"

    # ---------- UI 构建 ----------
    def _build_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(8)

        # 标题 + 占比
        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel("上下文占用")
        self._title.setStyleSheet(f"color: {self._text_primary}; font-weight: 600; {get_font_family_css()} {font_size_css(13)}")
        self._pct = QLabel("")
        self._pct.setStyleSheet(f"color: {self._text_primary}; font-weight: 600; {get_font_family_css()} {font_size_css(13)}")
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(self._pct)
        self._layout.addLayout(header)

        # 占用数值行
        self._usage = QLabel("")
        self._usage.setStyleSheet(f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}")
        self._layout.addWidget(self._usage)

        # 堆叠比例条
        self._bar = _StackedBar(self)
        self._layout.addWidget(self._bar)

        # 图例容器（动态重建）
        self._legend_host = QWidget(self)
        self._legend_layout = QVBoxLayout(self._legend_host)
        self._legend_layout.setContentsMargins(0, 0, 0, 0)
        self._legend_layout.setSpacing(5)
        self._layout.addWidget(self._legend_host)

        # 附加信息（压缩 / 缓存）
        self._extras = QLabel("")
        self._extras.setStyleSheet(f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}")
        self._extras.setTextFormat(Qt.PlainText)
        self._layout.addWidget(self._extras)

    # ---------- 数据更新 ----------
    def set_data(self, data: Dict):
        self._data = data or {}
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh()  # show 后 isVisible=True，完整跑一次真实刷新（覆盖前置 rebuild 被短路）

    def _refresh(self):
        # 隐藏时短路：流式期间 set_usage 2Hz 调 set_data → _refresh，tooltip 隐藏时
        # 13 处 setStyleSheet + legend rebuild 全是无效工作。显示前 _show_tooltip()
        # 会主动调 _rebuild_tooltip() 补齐数据，短路不丢数据、不丢主题同步。
        if not self.isVisible():
            return
        data = self._data
        # 跟随主题：每次显示前重新读取主题色并回填静态控件样式
        self._load_theme_colors()
        # 触发 paintEvent 重绘卡片背景/边框（_card_bg / _border 在 _load_theme_colors 中更新）
        self.update()
        self._title.setStyleSheet(f"color: {self._text_primary}; font-weight: 600; {get_font_family_css()} {font_size_css(13)}")
        self._usage.setStyleSheet(f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}")
        self._extras.setStyleSheet(f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}")

        # 空状态：未选择会话 / 模型（刚进入软件时常见），给出友好引导而非空白
        if data.get("empty"):
            self._pct.setText("—")
            self._pct.setStyleSheet(f"color: {self._text_primary}; font-weight: 700; {get_font_family_css()} {font_size_css(13)}")
            self._usage.setText("未选择会话或模型")
            # 空状态也更新进度条主题色
            try:
                from app.utils.theme_manager import theme_manager
                _is_light = theme_manager.is_light_theme()
            except Exception:
                _is_light = False
            self._bar.set_theme_colors(
                QColor(0, 0, 0, 40) if _is_light else QColor(255, 255, 255, 28),
                _parse_color(self._text_primary, "#1a1a1a") if _is_light else Qt.white,
                _parse_color(self._text_secondary, "rgba(0,0,0,0.4)") if _is_light else QColor(255, 255, 255, 180),
            )
            self._bar.set_segments([], 0)
            self._rebuild_legend([], 0)
            self._extras.setText("")
            self._extras.setVisible(False)
            self.adjustSize()
            return

        used = int(data.get("used_tokens", 0) or 0)
        budget = int(data.get("budget_tokens", 0) or 0)
        percent = int(data.get("percent", 0) or 0)
        ring_color = data.get("ring_color", "#5aa9ff")
        breakdown: List[Dict] = data.get("breakdown", []) or []

        # 占比文字（变色）
        self._pct.setText(f"{percent}%")
        self._pct.setStyleSheet(f"color: {ring_color}; font-weight: 700; {get_font_family_css()} {font_size_css(13)}")

        budget_txt = f"{budget:,}" if budget else "—"
        self._usage.setText(f"已用 {used:,} tokens · 预算 {budget_txt}")

        # ── 进度条主题色（轨道底色 + 百分比文字色） ──
        try:
            from app.utils.theme_manager import theme_manager
            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False
        if _is_light:
            _bar_track = QColor(0, 0, 0, 40)  # 浅色模式用深色半透明轨道
            _bar_text_fill = _parse_color(self._text_primary, "#1a1a1a")
            _bar_text_empty = _parse_color(self._text_secondary, "rgba(0,0,0,0.4)")
        else:
            _bar_track = QColor(255, 255, 255, 28)  # 深色模式用白色半透明轨道
            _bar_text_fill = Qt.white
            _bar_text_empty = QColor(255, 255, 255, 180)
        self._bar.set_theme_colors(_bar_track, _bar_text_fill, _bar_text_empty)

        # 堆叠条：按「占整个上下文预算」的比例绘制，剩余部分显示为空闲轨道
        if breakdown and budget > 0:
            segments = [
                (b.get("tokens", 0) / budget, b.get("color", "#888888")) for b in breakdown
            ]
        elif breakdown:
            # 无预算信息时退化为：按已用上下文内部占比
            total = sum(b.get("tokens", 0) for b in breakdown) or 1
            segments = [(b.get("tokens", 0) / total, b.get("color", "#888888")) for b in breakdown]
        else:
            # 无明细：按占比画单色条
            frac = max(0.0, min(1.0, used / budget)) if budget else 0.0
            segments = [(frac, ring_color), (max(0.0, 1.0 - frac), "rgba(255,255,255,0.12)")]
        self._bar.set_segments(segments, percent)

        # 图例
        self._rebuild_legend(breakdown, budget)

        # 附加信息
        self._rebuild_extras(data)

        self.adjustSize()

    def _rebuild_legend(self, breakdown: List[Dict], budget: int):
        # 清空旧图例
        while self._legend_layout.count():
            item = self._legend_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not breakdown:
            return

        for b in breakdown:
            tokens = int(b.get("tokens", 0) or 0)
            color = b.get("color", "#888888")
            label = b.get("label", "")
            # 百分比按「占整个上下文预算」计算，与堆叠条一致
            pct = (tokens / budget * 100) if budget else 0.0

            row = QWidget(self)
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(0, 0, 0, 0)
            rlay.setSpacing(8)

            swatch = QLabel(row)
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background: {color}; border-radius: 2px;")

            name = QLabel(label, row)
            name.setStyleSheet(f"color: {self._text_primary}; {get_font_family_css()} {font_size_css(11)}")

            val = QLabel(f"{tokens:,}", row)
            val.setStyleSheet(f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}")

            pc = QLabel(f"{pct:.0f}%", row)
            pc.setStyleSheet(f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}")
            pc.setFixedWidth(38)
            pc.setAlignment(Qt.AlignRight)

            rlay.addWidget(swatch)
            rlay.addWidget(name)
            rlay.addStretch(1)
            rlay.addWidget(val)
            rlay.addWidget(pc)

            self._legend_layout.addWidget(row)

    def _rebuild_extras(self, data: Dict):
        lines: List[str] = []

        compaction = data.get("compaction") or {}
        if compaction.get("active"):
            normal = int(data.get("normal_tokens", 0) or 0)
            compacted = int(data.get("compacted_tokens", 0) or 0)
            total = normal + compacted
            if total > 0:
                lines.append(
                    f"压缩上下文: {compacted:,} tokens ({compacted / total * 100:.0f}%) · "
                    f"普通: {normal:,} tokens ({normal / total * 100:.0f}%)"
                )
            lines.append(
                f"压缩条数: {compaction.get('summarized_count', 0)} · "
                f"保留条数: {compaction.get('kept_count', 0)}"
            )
            note = str(compaction.get("note", "") or "").strip()
            if note:
                lines.append(note)

        # 工具结果截断节省（S2）：显示实际发送口径下的截断收益
        pruned = int(data.get("pruned_tokens", 0) or 0)
        if pruned > 0:
            if lines:
                lines.append("")
            lines.append(f"工具结果截断节省: {pruned:,} tokens")
        # 缓存统计：压缩为单行核心指标（命中率 + 节省成本），去掉冗余明细
        cache = data.get("cache") or {}
        has_cache = cache.get("hit_rate", 0) > 0 or cache.get("cost_savings", 0) > 0
        if has_cache:
            if lines:
                lines.append("")
            parts = []
            if cache.get("hit_rate", 0) > 0:
                parts.append(f"缓存命中率 {cache.get('hit_rate', 0):.1%}")
            if cache.get("cost_savings", 0) > 0:
                parts.append(f"节省 ${cache.get('cost_savings', 0):.3f}")
            if parts:
                lines.append(" · ".join(parts))

        self._extras.setText("\n".join(lines))
        self._extras.setVisible(bool(lines))

    # ---------- 绘制卡片背景（圆角 + 边框） ----------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        bg = _parse_color(self._card_bg, "#212126")
        painter.setBrush(bg)
        r = 8
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), r, r)

        # 边框
        painter.setPen(_parse_color(self._border, "#3d3d3d"))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), r, r)
        painter.setPen(Qt.NoPen)
