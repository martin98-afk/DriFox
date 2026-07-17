# -*- coding: utf-8 -*-
"""ContextUsageStatsCard 浮动卡片 — 统计最近对话的上下文用量图表

功能：
- 最近 14 天会话活跃度柱状图
- 最近 14 天消息量趋势折线图
- 总体统计数据（总会话数、总消息数、平均消息数/会话、压缩率）
- 当前会话信息（消息数、估算 token 数）
- 所有数据异步从 SQLite 数据库读取，不阻塞 UI

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 sqlite3/stdlib 完成
- 基于 .drifox/sessions.db 文件直接读取数据
"""

import sqlite3
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, QPointF, QRectF, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    ScrollArea,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger

# ── 路径常量 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DB_PATH = _PROJECT_ROOT / ".drifox" / "sessions.db"
_USER_DB_PATH = Path.home() / ".drifox" / "sessions.db"


def _find_db() -> Optional[Path]:
    """查找 sessions.db 文件路径（开发环境 → 用户目录兜底）"""
    if _DEV_DB_PATH.exists():
        return _DEV_DB_PATH
    if _USER_DB_PATH.exists():
        return _USER_DB_PATH
    return None


# ── 主题色辅助 ────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    """（已废弃，保留向后兼容）请改用卡片注入的 context 主题色

    根据当前深浅模式返回适配的文字颜色。
    """
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _chart_colors() -> dict:
    """（已废弃，保留向后兼容）请改用卡片注入的 context 主题色

    根据当前深浅模式返回适配的文字颜色。
    """
    dark = isDarkTheme()
    if dark:
        text_color = QColor(255, 255, 255, 200)
        text_secondary = QColor(255, 255, 255, 150)
    else:
        text_color = QColor(30, 30, 30, 220)
        text_secondary = QColor(80, 80, 80, 200)
    return {
        "bar_fill": QColor(98, 160, 234, 200),
        "bar_border": QColor(98, 160, 234),
        "line": QColor(80, 227, 194),
        "line_fill": QColor(80, 227, 194, 60),
        "point": QColor(80, 227, 194),
        "grid": QColor(255, 255, 255, 30),
        "text": text_color,
        "text_secondary": text_secondary,
        "card_bg": QColor(255, 255, 255, 20),
        "accent": QColor(98, 160, 234),
        "accent_fill": QColor(98, 160, 234, 60),
        "warning": QColor(255, 193, 7, 200),
        "success": QColor(80, 227, 194, 200),
        "font_family": "Microsoft YaHei",
        "font_size": 14,
    }


# ── 上下文主题色 → 图表色转换 ────────────────────────────


def _make_chart_colors_from_context(ctx: dict) -> dict:
    """将 context 中的主题 colors 映射为图表组件可用的 QColor 字典

    Args:
        ctx: UIPluginRegistry 注入的上下文（含 colors / is_dark 等字段）

    Returns:
        带 QColor 值的图表配色字典，与 _chart_colors() 输出格式一致
    """
    raw = ctx.get("colors", {})
    is_dark = ctx.get("is_dark", True)

    def _qcolor(key: str, fallback_light: str, fallback_dark: str) -> QColor:
        val = raw.get(key, "")
        if val:
            return QColor(val)
        return QColor(fallback_dark if is_dark else fallback_light)

    accent = _qcolor("accent", "#2878dc", "#62a0ea")
    success = _qcolor("success", "#00a888", "#50e3c2")
    # 文字颜色根据深浅模式动态适配：
    # 深色模式：白色文字（与暗色浮动卡片背景对比）
    # 浅色模式：深色文字（与亮色浮动卡片背景对比）
    if is_dark:
        text_color = QColor(255, 255, 255, 200)
        text_secondary = QColor(255, 255, 255, 150)
    else:
        text_color = QColor(30, 30, 30, 220)
        text_secondary = QColor(80, 80, 80, 200)
    return {
        "bar_fill": accent.lighter(110),
        "bar_border": accent,
        "line": success,
        "line_fill": QColor(success.red(), success.green(), success.blue(), 60),
        "point": success,
        "grid": _qcolor("border", "#cccccc80", "#0000001e"),
        "text": text_color,
        "text_secondary": text_secondary,
        "card_bg": _qcolor("card_bg", "#00000014", "#ffffff14"),
        "accent": accent,
        "accent_fill": QColor(accent.red(), accent.green(), accent.blue(), 60),
        "warning": _qcolor("accent_warm", "#f59e0b", "#ffc107"),
        "success": success,
        "font_family": ctx.get("font_family", "Microsoft YaHei"),
        "font_size": ctx.get("font_size", 14),
    }


# ── Token 快速估算 ────────────────────────────────────────


def _fast_estimate_tokens(text: str) -> int:
    """快速估算文本的 token 数（无需 tiktoken 依赖）

    经验公式（cl100k_base 类分词器近似，覆盖 GPT/DeepSeek/Qwen/Claude）：
    - 中文约 1.2 token/字
    - 英文/代码约 1 token / 4 字符
    ⚠️ 旧实现按「中文 2 字符=1 token」严重低估中文，已修正。
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    non_chinese = len(text) - chinese
    estimated = int(chinese * 1.2 + non_chinese / 4.0)
    return max(1, estimated)


def _estimate_messages_tokens(messages_json: str) -> int:
    """估算整条会话消息 JSON 的 token 数（快速但不精确）"""
    if not messages_json:
        return 0
    if isinstance(messages_json, str) and len(messages_json) > 10:
        return _fast_estimate_tokens(messages_json)
    return 0


# ── SQLite 数据读取 ──────────────────────────────────────


def _get_db_connection() -> Optional[sqlite3.Connection]:
    """获取 SQLite 数据库连接（只读模式）"""
    db_path = _find_db()
    if db_path is None:
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"[ContextUsageStats] 无法打开数据库: {e}")
        return None


def _fetch_session_stats() -> dict:
    """从数据库中读取会话统计数据

    Returns:
        dict with keys:
        - total_sessions: int
        - total_messages: int
        - total_compacted: int (已压缩会话数)
        - daily_sessions: List[Tuple[str, int]] (日期, 会话数), 最近 14 天
        - daily_messages: List[Tuple[str, int]] (日期, 消息数), 最近 14 天
        - daily_tokens: List[Tuple[str, int]] (日期, 估算 token 数), 最近 14 天
        - sessions_per_project: Dict[str, int]
        - avg_messages_per_session: float
        - compaction_rate: float
    """
    result = {
        "total_sessions": 0,
        "total_messages": 0,
        "total_tokens": 0,
        "avg_daily_tokens": 0,
        "total_compacted": 0,
        "daily_sessions": [],
        "daily_messages": [],
        "daily_tokens": [],
        "sessions_per_project": {},
        "avg_messages_per_session": 0.0,
        "compaction_rate": 0.0,
        "error": None,
    }

    conn = _get_db_connection()
    if conn is None:
        result["error"] = "无法连接到数据库"
        return result

    try:
        cursor = conn.cursor()

        # 1. ═══ 基础统计 ═══
        cursor.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(message_count), 0) as msgs, "
            "COALESCE(SUM(context_usage), 0) as tokens "
            "FROM sessions WHERE project NOT LIKE '__archived__%'"
        )
        row = cursor.fetchone()
        total_sessions = row["cnt"] if row else 0
        total_messages = row["msgs"] if row else 0
        total_tokens_known = row["tokens"] if row else 0
        result["total_sessions"] = total_sessions
        result["total_messages"] = total_messages

        # 1b. ═══ Token 总量（全部会话，context_usage=0 的旧会话用 messages 估算） ═══
        total_tokens_estimated = 0
        cursor.execute(
            "SELECT messages FROM sessions "
            "WHERE project NOT LIKE '__archived__%' "
            "AND (context_usage IS NULL OR context_usage = 0) "
            "AND messages IS NOT NULL AND messages != ''"
        )
        for row in cursor.fetchall():
            msg_data = row["messages"]
            if isinstance(msg_data, (str, bytes)):
                total_tokens_estimated += _fast_estimate_tokens(str(msg_data)[:100000])
        result["total_tokens"] = total_tokens_known + total_tokens_estimated

        # 2. ═══ 压缩统计（精确检查 compaction_state 中 active:true） ═══
        cursor.execute(
            "SELECT compaction_state FROM sessions "
            "WHERE project NOT LIKE '__archived__%' "
            "AND instr(compaction_state, 'true') > 0"
        )
        compacted_count = 0
        for row in cursor.fetchall():
            raw = row["compaction_state"]
            if isinstance(raw, str) and '"active":true' in raw:
                compacted_count += 1
        result["total_compacted"] = compacted_count
        result["compaction_rate"] = compacted_count / total_sessions if total_sessions > 0 else 0.0

        # 3. ═══ 按项目统计 ═══
        cursor.execute(
            "SELECT project, COUNT(*) as cnt FROM sessions "
            "WHERE project NOT LIKE '__archived__%' "
            "GROUP BY project ORDER BY cnt DESC"
        )
        for row in cursor.fetchall():
            result["sessions_per_project"][row["project"]] = row["cnt"]

        # 4. ═══ 最近 14 天按日统计（基于 created_at：反映「哪天发起了对话」） ═══
        today = datetime.now()
        date_labels = [(today - timedelta(days=i)).strftime("%m-%d") for i in range(13, -1, -1)]

        daily_sessions_map: Dict[str, int] = {dl: 0 for dl in date_labels}
        daily_messages_map: Dict[str, int] = {dl: 0 for dl in date_labels}
        daily_tokens_map: Dict[str, int] = {dl: 0 for dl in date_labels}

        cursor.execute(
            "SELECT DATE(created_at) as day, COUNT(*) as cnt, "
            "COALESCE(SUM(message_count), 0) as msgs "
            "FROM sessions "
            "WHERE created_at >= date('now', '-13 days') "
            "AND project NOT LIKE '__archived__%' "
            "GROUP BY DATE(created_at) ORDER BY day"
        )
        for row in cursor.fetchall():
            day_str = row["day"]
            if not day_str:
                continue
            try:
                label = datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
                daily_sessions_map[label] = row["cnt"]
                daily_messages_map[label] = row["msgs"]
            except ValueError, TypeError:
                pass

        # 5. ═══ Token 用量 ═══
        # 优先从 context_usage 列读取（精确），对 context_usage=0 的旧会话回退到 messages 估算
        cursor.execute(
            "SELECT DATE(created_at) as day, COALESCE(SUM(context_usage), 0) as total_tokens "
            "FROM sessions "
            "WHERE created_at >= date('now', '-13 days') "
            "AND project NOT LIKE '__archived__%' "
            "AND context_usage > 0 "
            "GROUP BY DATE(created_at) ORDER BY day"
        )
        for row in cursor.fetchall():
            day_str = row["day"]
            if not day_str:
                continue
            try:
                label = datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
                daily_tokens_map[label] = row["total_tokens"]
            except ValueError, TypeError:
                pass

        # 回退：对 context_usage=0 的旧会话，从 messages 估算 token（兼容旧数据）
        cursor.execute(
            "SELECT DATE(created_at) as day, messages "
            "FROM sessions "
            "WHERE created_at >= date('now', '-13 days') "
            "AND project NOT LIKE '__archived__%' "
            "AND (context_usage IS NULL OR context_usage = 0) "
            "AND messages IS NOT NULL AND messages != '' "
            "ORDER BY created_at DESC"
        )
        for row in cursor.fetchall():
            day_str = row["day"]
            if not day_str:
                continue
            try:
                label = datetime.strptime(day_str, "%Y-%m-%d").strftime("%m-%d")
                msg_data = row["messages"]
                if isinstance(msg_data, (str, bytes)):
                    tokens = _fast_estimate_tokens(str(msg_data)[:100000])
                    daily_tokens_map[label] = daily_tokens_map.get(label, 0) + tokens
            except Exception:
                # 兼容旧数据：日期格式异常或 messages 类型异常时跳过该行
                pass

        # 转换为有序列表
        for dl in date_labels:
            result["daily_sessions"].append((dl, daily_sessions_map[dl]))
            result["daily_messages"].append((dl, daily_messages_map[dl]))
            result["daily_tokens"].append((dl, daily_tokens_map[dl]))

        # 6. ═══ 平均消息数 / 日均 token ═══
        result["avg_messages_per_session"] = round(total_messages / total_sessions, 1) if total_sessions > 0 else 0.0

        # 日均 token 基于 14 天窗口（与图表口径一致）
        total_in_window = sum(v for _, v in result["daily_tokens"])
        result["avg_daily_tokens"] = int(round(total_in_window / 14)) if total_in_window > 0 else 0

        conn.close()
    except Exception as e:
        result["error"] = f"{e}"
        logger.error(f"[ContextUsageStats] 数据读取失败: {e}\n{traceback.format_exc()}")
        try:
            conn.close()
        except Exception:
            pass

    return result


# ── 异步工作器 ───────────────────────────────────────────


class _DataWorker(QObject):
    """后台线程执行数据库读取"""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()

    def run(self):
        try:
            data = _fetch_session_stats()
            self.finished.emit(data)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── 工具函数 ──────────────────────────────────────────────


def _format_number(n: int) -> str:
    """格式化大数字，如 1234 → '1.2k'"""
    if n >= 1000000:
        return f"{n / 1000000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _format_pct(v: float) -> str:
    """格式化百分比"""
    return f"{v * 100:.0f}%"


def _parse_mmdd(date_str: str) -> datetime:
    """将 'mm-dd' 转 datetime，自动判断跨年边界（数据来自近 14 天）"""
    try:
        parts = date_str.split("-")
        month, day = int(parts[0]), int(parts[1])
        now = datetime.now()
        dt = datetime(now.year, month, day)
        # 如果算出来比今天晚 >30 天 → 去年底的日期（跨年边界）
        if dt > now + timedelta(days=30):
            dt = datetime(now.year - 1, month, day)
        return dt
    except ValueError, IndexError:
        return datetime.now()


def _short_weekday(date_str: str) -> str:
    """将 '01-15' 转换为 '01-15\n周一' 格式"""
    try:
        dt = _parse_mmdd(date_str)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wd = weekdays[dt.weekday()]
        return f"{date_str}\n{wd}"
    except ValueError:
        return date_str


# ══════════════════════════════════════════════════════════
# 自定义图表组件
# ══════════════════════════════════════════════════════════


class _BarChartWidget(QWidget):
    """柱状图组件 — 用于展示每日会话数量"""

    def __init__(self, title: str, data: List[Tuple[str, int]], color_key: str = "bar_fill", parent=None):
        super().__init__(parent)
        self._title = title
        self._data = data  # [(label, value), ...]
        self._color_key = color_key
        self._colors = _chart_colors()  # 默认 fallback 配色
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
        """注入外部配色（来自 context），覆盖默认 _chart_colors()"""
        self._colors = colors
        self.update()

    def mouseMoveEvent(self, event):
        """鼠标悬停检测：高亮对应柱体并显示 tooltip"""
        if not self._data:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        w = self.width()
        margin_left = 52 if w >= 420 else 44
        chart_w = w - margin_left - (12 if w >= 400 else 8)

        if chart_w < 10:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        n = len(self._data)
        bar_spacing = chart_w / n
        pos = event.pos()

        i = int((pos.x() - margin_left) / bar_spacing)
        i = max(0, min(i, n - 1))

        bar_x = margin_left + i * bar_spacing
        if bar_x <= pos.x() <= bar_x + bar_spacing:
            self._hovered_index = i
        else:
            self._hovered_index = -1

        self.update()

        if self._hovered_index >= 0:
            label, value = self._data[self._hovered_index]
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    date_str = dt.strftime("%m-%d") + f" ({weekdays[dt.weekday()]})"
                else:
                    date_str = label
            except ValueError, IndexError:
                date_str = label
            QToolTip.showText(event.globalPos(), f"📊 {date_str}\n会话数: {value}", self)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时清除悬停状态"""
        self._hovered_index = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = self._colors
        font_family = colors.get("font_family", "Microsoft YaHei")
        base_font_size = colors.get("font_size", 14)
        w = self.width()
        h = self.height()

        # 自适应边距：窄宽度时缩小边距
        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        margin_top = 34
        margin_bottom = 48 if w >= 400 else 40

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 4, chart_w, 22), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        # ── 计算数据范围 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        if max_val == 0:
            max_val = 1

        # ── Y 轴 ──
        painter.setPen(colors["grid"])
        y_ticks = 4
        tick_font_size = max(round(base_font_size * 8 / 14), 7)
        for i in range(y_ticks + 1):
            y = margin_top + chart_h * (1 - i / y_ticks)
            painter.drawLine(QPointF(margin_left, y), QPointF(w - margin_right, y))

            val = int(max_val * i / y_ticks)
            painter.setPen(colors["text_secondary"])
            tick_font = QFont(font_family, tick_font_size)
            painter.setFont(tick_font)
            painter.drawText(
                QRectF(2, y - 12, margin_left - 8, 24),
                Qt.AlignRight | Qt.AlignVCenter,
                str(val),
            )
            painter.setPen(colors["grid"])

        # ── 柱状图 ──
        n = len(self._data)
        if n == 0:
            painter.end()
            return

        bar_width = chart_w / n * (0.65 if w >= 400 else 0.55)
        bar_spacing = chart_w / n

        bar_color = colors.get(self._color_key, colors["bar_fill"])
        border_color = colors.get(self._color_key.replace("fill", "border"), colors["bar_border"])

        x_tick_size = max(round(base_font_size * 7 / 14), 6)
        val_font_size = max(round(base_font_size * 9 / 14), 7)

        for i, (label, value) in enumerate(self._data):
            x = margin_left + i * bar_spacing + (bar_spacing - bar_width) / 2
            bar_h = (value / max_val) * chart_h if max_val > 0 else 0
            y = margin_top + chart_h - bar_h

            # 柱体
            rect = QRectF(x, y, bar_width, max(bar_h, 0))
            path = QPainterPath()
            path.addRoundedRect(rect, 3, 3)
            if i == self._hovered_index:
                hover_color = QColor(bar_color).lighter(130)
                painter.fillPath(path, hover_color)
                painter.setPen(QPen(QColor(border_color).lighter(150), 2))
            else:
                painter.fillPath(path, bar_color)
                painter.setPen(QPen(border_color, 1))
            painter.drawPath(path)

            # X 轴标签
            painter.setPen(colors["text_secondary"])
            tick_font = QFont(font_family, x_tick_size)
            painter.setFont(tick_font)
            # 显示 "01-15\n周一" 格式（仅周末/周一标注星期）
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    wd = dt.weekday()
                    if wd in (0, 5, 6):  # 周一、周六、周日显示星期
                        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                        display_label = f"{parts[0]}-{parts[1]}\n{weekdays[wd]}"
                    else:
                        display_label = f"{parts[0]}-{parts[1]}"
                else:
                    display_label = label
            except ValueError, IndexError:
                display_label = label

            painter.drawText(
                QRectF(x - bar_spacing / 2, h - margin_bottom + 4, bar_spacing, 36),
                Qt.AlignCenter,
                display_label,
            )

            # 值标签（柱顶/柱内，靠近顶部时自动调整）
            if value > 0 and (w >= 350 or value >= max_val * 0.3):
                painter.setPen(colors["text"])
                val_font = QFont(font_family, val_font_size, QFont.Bold)
                painter.setFont(val_font)
                label_y = y - 20
                if label_y < margin_top:
                    label_y = y + 4
                if label_y + 16 > h - margin_bottom:
                    label_y = y - 12
                painter.drawText(
                    QRectF(x, label_y, bar_width, 16),
                    Qt.AlignCenter,
                    str(value),
                )

        painter.end()


class _LineChartWidget(QWidget):
    """折线图组件 — 用于展示消息量 / token 趋势"""

    def __init__(self, title: str, data: List[Tuple[str, int]], color_key: str = "line", parent=None):
        super().__init__(parent)
        self._title = title
        self._data = data
        self._color_key = color_key
        self._colors = _chart_colors()  # 默认 fallback 配色
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
        """注入外部配色（来自 context），覆盖默认 _chart_colors()"""
        self._colors = colors
        self.update()

    def mouseMoveEvent(self, event):
        """鼠标悬停检测：高亮最近的数据点并显示 tooltip"""
        if not self._data or len(self._data) < 1:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        w = self.width()
        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        chart_w = w - margin_left - margin_right

        if chart_w < 10:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        n = len(self._data)
        pos = event.pos()

        ratio = (pos.x() - margin_left) / chart_w
        i = int(round(ratio * (n - 1)))
        i = max(0, min(i, n - 1))

        pt_x = margin_left + chart_w * i / (n - 1) if n > 1 else margin_left + chart_w / 2

        if abs(pos.x() - pt_x) <= 30:
            self._hovered_index = i
        else:
            self._hovered_index = -1

        self.update()

        if self._hovered_index >= 0:
            label, value = self._data[self._hovered_index]
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    date_str = dt.strftime("%m-%d") + f" ({weekdays[dt.weekday()]})"
                else:
                    date_str = label
            except ValueError, IndexError:
                date_str = label
            QToolTip.showText(event.globalPos(), f"📈 {date_str}\n{_format_number(value)}", self)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时清除悬停状态"""
        self._hovered_index = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = self._colors
        font_family = colors.get("font_family", "Microsoft YaHei")
        base_font_size = colors.get("font_size", 14)
        w = self.width()
        h = self.height()

        # 自适应边距：窄宽度时缩小边距
        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        margin_top = 34
        margin_bottom = 48 if w >= 400 else 40

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 4, chart_w, 22), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        # ── 计算数据范围 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        min_val = min(values) if values else 0
        if max_val == min_val:
            max_val = max_val + 1 or 2
            min_val = 0
        # 给顶部留 30% 空间，避免数据点标签被裁剪
        top_margin = max_val * 0.3
        adjusted_max = max_val + top_margin

        # ── 网格 + Y 轴 ──
        painter.setPen(colors["grid"])
        y_ticks = 4
        for i in range(y_ticks + 1):
            y = margin_top + chart_h * (1 - i / y_ticks)
            painter.drawLine(QPointF(margin_left, y), QPointF(w - margin_right, y))

            val = int(min_val + (adjusted_max - min_val) * i / y_ticks)
            painter.setPen(colors["text_secondary"])
            tick_font_size = max(round(base_font_size * 8 / 14), 7)
            tick_font = QFont(font_family, tick_font_size)
            painter.setFont(tick_font)
            painter.drawText(
                QRectF(2, y - 12, margin_left - 8, 24),
                Qt.AlignRight | Qt.AlignVCenter,
                _format_number(val),
            )
            painter.setPen(colors["grid"])

        # ── 折线图 ──
        n = len(self._data)
        if n < 1:
            painter.end()
            return

        line_color = colors.get(self._color_key, colors["line"])
        fill_color = colors.get(f"{self._color_key}_fill", colors["line_fill"])
        point_color = colors.get(f"{self._color_key}_point", colors["point"])

        points: List[QPointF] = []
        for i, (_, value) in enumerate(self._data):
            x = margin_left + chart_w * i / (n - 1) if n > 1 else margin_left + chart_w / 2
            ratio = (value - min_val) / (adjusted_max - min_val) if adjusted_max > min_val else 0.5
            y = margin_top + chart_h - chart_h * ratio
            points.append(QPointF(x, y))

        # ── 填充区域 ──
        if len(points) >= 2:
            path = QPainterPath()
            path.moveTo(points[0])
            for pt in points[1:]:
                path.lineTo(pt)
            path.lineTo(points[-1].x(), margin_top + chart_h)
            path.lineTo(points[0].x(), margin_top + chart_h)
            path.closeSubpath()
            painter.fillPath(path, fill_color)

        # ── 连线 ──
        pen = QPen(line_color, 2.5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])

        # ── 数据点 + 标签 ──
        val_font_size = max(round(base_font_size * 9 / 14), 7)
        label_w = 60
        label_h = 20
        for i, (_, value) in enumerate(self._data):
            pt = points[i]
            # 圆点
            painter.setPen(Qt.NoPen)
            if i == self._hovered_index:
                painter.setBrush(point_color.lighter(150))
                painter.drawEllipse(pt, 6, 6)
                # 绘制垂直参考线
                painter.setPen(QPen(colors["text_secondary"], 1, Qt.DashLine))
                painter.drawLine(QPointF(pt.x(), margin_top), QPointF(pt.x(), h - margin_bottom))
            else:
                painter.setBrush(point_color)
                painter.drawEllipse(pt, 3, 3)
            painter.setBrush(Qt.NoBrush)

            # 值标签：先尝试放在数据点上方
            label_y = pt.y() - label_h - 4
            if label_y < margin_top:
                # 上方不够则放下方
                label_y = pt.y() + 6
                if label_y + label_h > h - margin_bottom + 6:
                    # 上下都不够则在数据点正中央对齐
                    label_y = pt.y() - label_h // 2
            # 计算标签 x 位置，确保在 widget 边界内
            label_x = pt.x() - label_w / 2
            if label_x < 2:
                label_x = 2
            elif label_x + label_w > w - 2:
                label_x = w - 2 - label_w
            if value > 0:
                painter.setPen(colors["text"])
                val_font = QFont(font_family, val_font_size, QFont.Bold)
                painter.setFont(val_font)
                painter.drawText(
                    QRectF(label_x, label_y, label_w, label_h),
                    Qt.AlignCenter,
                    _format_number(value),
                )

        # ── X 轴标签 ──
        x_tick_size = max(round(base_font_size * 7 / 14), 6)
        painter.setPen(colors["text_secondary"])
        tick_font = QFont(font_family, x_tick_size)
        painter.setFont(tick_font)
        for i, (label, _) in enumerate(self._data):
            x = margin_left + chart_w * i / (n - 1) if n > 1 else margin_left + chart_w / 2
            # 标签太多时只显示部分
            if n > 10 and i % 2 != 0:
                continue
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    wd = dt.weekday()
                    if wd in (0, 5, 6):
                        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                        display = f"{parts[0]}-{parts[1]}\n{weekdays[wd]}"
                    else:
                        display = label
                else:
                    display = label
            except ValueError, IndexError:
                display = label

            x_spacing = chart_w / n if n > 0 else chart_w
            painter.drawText(
                QRectF(x - x_spacing, h - margin_bottom + 4, x_spacing * 2, 36),
                Qt.AlignCenter,
                display,
            )

        painter.end()


class _StatCard(QFrame):
    """单个统计信息卡片"""

    def __init__(self, icon, title: str, value: str, subtitle: str = "", extra_info: str = "", parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._value = value
        self._subtitle = subtitle
        self._extra_info = extra_info  # 副标题下的额外小字（用于 "日均 X" 等补充信息）
        self._colors = _chart_colors()  # 默认 fallback 配色
        self.setup_ui()

    def set_colors(self, colors: dict):
        """注入外部配色（来自 context），覆盖默认 _text_color()"""
        self._colors = colors
        self._apply_card_style()

    def _apply_card_style(self):
        """根据当前 colors 刷新样式（含 font_family / font_size）"""
        tc = self._colors.get("text", QColor(255, 255, 255, 180))
        tcs = self._colors.get("text_secondary", QColor(255, 255, 255, 100))
        font_family = self._colors.get("font_family", "Microsoft YaHei")
        base_font_size = self._colors.get("font_size", 14)
        text_color = f"rgba({tc.red()},{tc.green()},{tc.blue()},{tc.alpha()})"
        text_sec = f"rgba({tcs.red()},{tcs.green()},{tcs.blue()},{tcs.alpha()})"
        val_size = max(round(base_font_size * 22 / 14), 16)
        sub_size = max(round(base_font_size * 11 / 14), 9)
        extra_size = max(round(base_font_size * 10 / 14), 8)
        for child in self.findChildren(QLabel):
            obj_name = child.objectName()
            if obj_name == "statValue":
                child.setStyleSheet(
                    f"color: {text_color}; font-size: {val_size}px; font-weight: bold; "
                    f"font-family: '{font_family}'; background: transparent;"
                )
            elif obj_name == "statSub":
                child.setStyleSheet(
                    f"color: {text_sec}; font-size: {sub_size}px; "
                    f"font-family: '{font_family}'; background: transparent;"
                )
            elif obj_name == "statExtra":
                child.setStyleSheet(
                    f"color: {text_sec}; font-size: {extra_size}px; opacity: 0.85; "
                    f"font-family: '{font_family}'; background: transparent;"
                )
            else:
                child.setStyleSheet(
                    f"color: {text_sec}; font-size: {sub_size}px; "
                    f"font-family: '{font_family}'; background: transparent;"
                )

    def setup_ui(self):
        self.setObjectName("statCard")
        self.setStyleSheet(
            "#statCard { background: transparent; border: 1px solid rgba(128,128,128,0.12); "
            "border-radius: 10px; padding: 0px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        # 用当前 colors（默认 fallback）确定字号
        font_family = self._colors.get("font_family", "Microsoft YaHei")
        base_font_size = self._colors.get("font_size", 14)
        val_size = max(round(base_font_size * 22 / 14), 16)
        sub_size = max(round(base_font_size * 11 / 14), 9)

        # 从 colors 取色，fallback 到 _text_color()
        tc = self._colors.get("text", QColor(255, 255, 255, 180))
        tcs = self._colors.get("text_secondary", QColor(255, 255, 255, 100))
        text_color = f"rgba({tc.red()},{tc.green()},{tc.blue()},{tc.alpha()})"
        text_sec = f"rgba({tcs.red()},{tcs.green()},{tcs.blue()},{tcs.alpha()})"

        # 图标 + 标题行
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        icon_w = IconWidget(self._icon, self)
        icon_w.setFixedSize(16, 16)
        top_row.addWidget(icon_w)

        title_lb = QLabel(self._title, self)
        title_lb.setObjectName("statTitle")
        title_lb.setStyleSheet(
            f"color: {text_sec}; font-size: {sub_size}px; font-family: '{font_family}'; background: transparent;"
        )
        top_row.addWidget(title_lb)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        # 值
        val_lb = QLabel(self._value, self)
        val_lb.setObjectName("statValue")
        val_lb.setStyleSheet(
            f"color: {text_color}; font-size: {val_size}px; font-weight: bold; "
            f"font-family: '{font_family}'; background: transparent;"
        )
        layout.addWidget(val_lb)

        # 副标题
        if self._subtitle:
            sub_lb = QLabel(self._subtitle, self)
            sub_lb.setObjectName("statSub")
            sub_lb.setStyleSheet(
                f"color: {text_sec}; font-size: {sub_size}px; font-family: '{font_family}'; background: transparent;"
            )
            layout.addWidget(sub_lb)

        # 额外小字（副标题之下，比副标题更小的字号，用于补充信息如"日均 token"）
        if self._extra_info:
            extra_size = max(round(base_font_size * 10 / 14), 8)
            extra_lb = QLabel(self._extra_info, self)
            extra_lb.setObjectName("statExtra")
            extra_lb.setStyleSheet(
                f"color: {text_sec}; font-size: {extra_size}px; opacity: 0.85; "
                f"font-family: '{font_family}'; background: transparent;"
            )
            layout.addWidget(extra_lb)


class _ProjectBarWidget(QWidget):
    """项目分布水平柱状图"""

    def __init__(self, data: List[Tuple[str, int]], title: str = "📁 项目分布", parent=None):
        super().__init__(parent)
        self._data = data
        self._title = title
        self._colors = _chart_colors()  # 默认 fallback 配色
        self.setMinimumHeight(160)
        self.setMaximumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
        """注入外部配色（来自 context）"""
        self._colors = colors
        self.update()

    def mouseMoveEvent(self, event):
        """鼠标悬停检测：高亮对应项目柱体并显示 tooltip"""
        if not self._data:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        h = self.height()
        margin_top = 28  # title_h(24) + 4
        margin_bottom = 8
        chart_h = h - margin_top - margin_bottom

        if chart_h < 10:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        n = len(self._data)
        pos = event.pos()
        row_h = chart_h / n

        i = int((pos.y() - margin_top) / row_h)
        i = max(0, min(i, n - 1))

        row_y = margin_top + i * row_h
        if row_y <= pos.y() <= row_y + row_h:
            self._hovered_index = i
        else:
            self._hovered_index = -1

        self.update()

        if self._hovered_index >= 0:
            label, value = self._data[self._hovered_index]
            QToolTip.showText(event.globalPos(), f"📁 {label}\n会话数: {value}", self)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时清除悬停状态"""
        self._hovered_index = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = self._colors
        font_family = colors.get("font_family", "Microsoft YaHei")
        base_font_size = colors.get("font_size", 14)
        w = self.width()
        h = self.height()

        # 标题区域
        title_h = 24
        margin_left = 16
        margin_right = 16
        margin_top = title_h + 4
        margin_bottom = 8

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 2, chart_w, title_h), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        n = len(self._data)
        if n == 0:
            painter.end()
            return

        max_val = max(v for _, v in self._data)
        if max_val == 0:
            max_val = 1

        row_h = chart_h / n
        bar_h = max(row_h * 0.6, 14)
        bar_h = min(bar_h, 28)

        label_font_size = max(round(base_font_size * 10 / 14), 8)
        val_font_size = max(round(base_font_size * 9 / 14), 8)

        for i, (label, value) in enumerate(self._data):
            y = margin_top + i * row_h + (row_h - bar_h) / 2

            # 标签
            painter.setPen(colors["text"])
            label_font = QFont(font_family, label_font_size)
            painter.setFont(label_font)
            display_label = label if len(label) <= 12 else label[:11] + "…"
            painter.drawText(
                QRectF(margin_left, y, 80, bar_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                display_label,
            )

            # 柱条
            bar_w = (value / max_val) * (chart_w - 80 - 60) if max_val > 0 else 0
            bar_x = margin_left + 84

            bar_color = QColor(colors["accent"])
            bar_color.setAlpha(180)
            path = QPainterPath()
            path.addRoundedRect(QRectF(bar_x, y + 2, max(bar_w, 2), bar_h - 4), 4, 4)
            if i == self._hovered_index:
                hover_color = QColor(bar_color).lighter(140)
                painter.fillPath(path, hover_color)
                painter.setPen(QPen(QColor(colors["accent"]).lighter(150), 2))
            else:
                painter.fillPath(path, bar_color)
                painter.setPen(QPen(colors["accent"], 1))
            painter.drawPath(path)

            # 数值
            painter.setPen(colors["text"])
            val_font = QFont(font_family, val_font_size, QFont.Bold)
            painter.setFont(val_font)
            painter.drawText(
                QRectF(bar_x + max(bar_w, 2) + 6, y, 50, bar_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                str(value),
            )

        painter.end()


# ══════════════════════════════════════════════════════════
# 主卡片
# ══════════════════════════════════════════════════════════


class ContextUsageStatsCard(QWidget):
    """上下文用量统计浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_DataWorker] = None
        self._stats_data: Optional[dict] = None
        self._chart_style: Optional[dict] = None
        self._header_icon: Optional[IconWidget] = None
        self._setup_ui()

        # 首次显示时由 show_card 触发加载，__init__ 不再自动加载

    # ── 拉模型上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）

        卡片在 show_card / 需要刷新主题时自行调用 provider 获取最新上下文，
        不再依赖外部推送。
        """
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：用最新上下文刷新主题色 + 加载数据"""
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._async_load_data()
        self.setVisible(True)

    def _apply_plugin_icon(self):
        """从上下文获取插件图标并更新头部图标"""
        if self._context_provider is None or self._header_icon is None:
            return
        try:
            from PyQt5.QtGui import QIcon

            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme, "")
            if icon_path:
                self._header_icon.setIcon(QIcon(icon_path))
        except Exception:
            pass

    def _apply_latest_theme(self):
        """从上下文提供函数拉取最新主题色并应用到所有子组件"""
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
            self._chart_style = _make_chart_colors_from_context(ctx)
        except Exception:
            self._chart_style = _chart_colors()

        cs = self._chart_style
        # 刷新头部标题颜色
        font_family = cs.get("font_family", "Microsoft YaHei")
        font_size = cs.get("font_size", 14)
        tc = cs.get("text", QColor(255, 255, 255, 180))
        tcs = cs.get("text_secondary", QColor(255, 255, 255, 100))
        text_color = f"rgba({tc.red()},{tc.green()},{tc.blue()},{tc.alpha()})"
        text_sec = f"rgba({tcs.red()},{tcs.green()},{tcs.blue()},{tcs.alpha()})"

        # 更新所有 QLabel 子控件的样式
        for child in self.findChildren(QLabel):
            try:
                obj_name = child.objectName()
                current = child.styleSheet()
                # 保留独立的 font-weight 设定（如 statValue 的 bold）
                weight_part = ""
                if "font-weight" in current:
                    weight_part = "font-weight: bold;"
                child.setStyleSheet(
                    f"color: {text_color}; font-size: {font_size}px; "
                    f"font-family: '{font_family}'; {weight_part}"
                    "background: transparent;"
                )
            except RuntimeError:
                pass

    # ── 界面搭建 ──

    def _setup_ui(self):
        self.setMinimumWidth(300)
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("ContextUsageStatsCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 初始配色（后续 _apply_latest_theme 覆盖）
        # 浮动卡片背景偏暗，强制初始文字用白色
        ff = "Microsoft YaHei"
        fs = 14
        text_color_default = "rgba(255,255,255,200)"
        text_sec_default = "rgba(255,255,255,150)"

        # ── 头部 ──
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        icon = IconWidget(FluentIcon.HISTORY, header)
        icon.setFixedSize(22, 22)
        hly.addWidget(icon)
        self._header_icon = icon

        title = QLabel("上下文用量统计", header)
        title.setStyleSheet(
            f"color: {text_color_default}; font-size: {fs}px; font-weight: bold; "
            f"font-family: '{ff}'; background: transparent;"
        )
        hly.addWidget(title)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {text_sec_default}; font-size: {fs}px; font-family: '{ff}'; background: transparent;"
        )
        hly.addWidget(self._status_lb)
        hly.addStretch(1)

        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新数据")
        self._refresh_btn.clicked.connect(self._async_load_data)
        hly.addWidget(self._refresh_btn)

        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # ── 滚动内容 ──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(16, 12, 16, 12)
        self._content_layout.setSpacing(16)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        # ── 加载中占位 ──
        self._empty_lb = QLabel("正在加载统计数据…", self)
        self._empty_lb.setAlignment(Qt.AlignCenter)
        self._empty_lb.setStyleSheet(f"color: {text_sec_default}; font-family: '{ff}'; background: transparent;")
        self._empty_lb.setVisible(True)
        root.addWidget(self._empty_lb)

    # ── 高度模式 ──

    def sizeHint(self):
        """与 SystemCardFrame proportional 模式一致：返回窗口高度的 85%"""
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):
        """显示时安装窗口 resize 事件过滤器，窗口缩放时通知容器重新展开"""
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        """监听窗口 resize，触发 updateGeometry → CardContainer 重算高度"""
        from PyQt5.QtCore import QEvent

        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    # ── 数据加载 ──

    def _async_load_data(self):
        """后台线程异步加载数据"""
        self._set_loading(True)
        self._cleanup_worker()

        worker = _DataWorker()
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_data_loaded)
        worker.error.connect(self._on_load_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._worker, self._worker_thread = worker, thread
        thread.start()

    def _on_data_loaded(self, data: dict):
        """数据加载完成"""
        self._stats_data = data
        self._set_loading(False)

        if data.get("error"):
            self._empty_lb.setText(f"数据加载失败: {data['error'][:60]}")
            self._empty_lb.setVisible(True)
            return

        self._render_stats(data)

    def _on_load_error(self, err: str):
        """数据加载出错"""
        self._set_loading(False)
        self._empty_lb.setText(f"数据读取异常: {err[:60]}")
        self._empty_lb.setVisible(True)

    def _set_loading(self, loading: bool):
        """设置加载状态"""
        self._refresh_btn.setEnabled(not loading)
        if loading:
            self._status_lb.setText("读取中…")
            self._empty_lb.setVisible(True)
        else:
            self._status_lb.setText("")
            self._empty_lb.setVisible(False)

    # ── 渲染数据 ──

    def _render_stats(self, data: dict):
        """渲染所有统计数据"""
        # 清空旧内容
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # ── 概要统计卡片（紧凑单行） ──
        total_sessions = data.get("total_sessions", 0)
        total_messages = data.get("total_messages", 0)
        total_tokens = data.get("total_tokens", 0)
        avg_daily_tokens = data.get("avg_daily_tokens", 0)
        avg_msgs = data.get("avg_messages_per_session", 0.0)

        # 计算平均值
        avg_daily = round(total_sessions / 14, 1) if total_sessions > 0 else 0.0

        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)

        stat_cards = [
            # 总 token 数放在第一位（最重要），日均 token 作为额外小字显示在副标题之下
            (
                FluentIcon.FONT,
                "总 token 数",
                _format_number(total_tokens),
                f"日均 {_format_number(avg_daily_tokens)}（近 14 天）",
                "",
            ),
            (FluentIcon.CHAT, "总会话数", str(total_sessions), f"平均 {avg_daily} 次/天", ""),
            (FluentIcon.MESSAGE, "总消息数", _format_number(total_messages), f"平均 {avg_msgs} 条/会话", ""),
        ]

        for ic, title, val, sub, extra in stat_cards:
            card = _StatCard(ic, title, val, sub, extra_info=extra)
            if self._chart_style:
                card.set_colors(self._chart_style)
            stats_row.addWidget(card)

        stats_widget = QWidget()
        stats_widget.setLayout(stats_row)
        stats_widget.setStyleSheet("background: transparent;")
        self._content_layout.addWidget(stats_widget)

        # 注入配色到子图表
        cs = self._chart_style

        # ── 估算 Token 用量折线图 ──
        daily_tokens = data.get("daily_tokens", [])
        if daily_tokens and any(v for _, v in daily_tokens):
            token_widget = _LineChartWidget("🔤 估算 Token 用量趋势", daily_tokens, color_key="accent")
            if cs:
                token_widget.set_colors(cs)
            self._content_layout.addWidget(token_widget)

        # ── 消息量趋势折线图 ──
        daily_messages = data.get("daily_messages", [])
        if daily_messages and any(v for _, v in daily_messages):
            line_widget = _LineChartWidget("📈 每日消息量趋势", daily_messages, color_key="accent")
            if cs:
                line_widget.set_colors(cs)
            self._content_layout.addWidget(line_widget)

        # ── 会话活跃度柱状图 ──
        daily_sessions = data.get("daily_sessions", [])
        if daily_sessions and any(v for _, v in daily_sessions):
            bar_widget = _BarChartWidget("📊 每日会话活跃度", daily_sessions)
            if cs:
                bar_widget.set_colors(cs)
            self._content_layout.addWidget(bar_widget)

        # ── 项目分布柱状图（水平） ──
        sessions_per_project = data.get("sessions_per_project", {})
        if sessions_per_project:
            sorted_projects = sorted(sessions_per_project.items(), key=lambda x: -x[1])[:8]
            proj_bar = _ProjectBarWidget(sorted_projects)
            if cs:
                proj_bar.set_colors(cs)
            self._content_layout.addWidget(proj_bar)

        # ── 无数据提示 ──
        if not daily_sessions or not any(v for _, v in daily_sessions):
            empty_hint = QLabel("暂无会话数据，开始对话后将自动生成统计。", self._content)
            empty_hint.setAlignment(Qt.AlignCenter)
            font_family = cs.get("font_family", "Microsoft YaHei") if cs else "Microsoft YaHei"
            tcs2 = cs.get("text_secondary", QColor(255, 255, 255, 100)) if cs else QColor(255, 255, 255, 100)
            empty_color = f"rgba({tcs2.red()},{tcs2.green()},{tcs2.blue()},{tcs2.alpha()})"
            empty_hint.setStyleSheet(
                f"color: {empty_color}; font-family: '{font_family}'; font-size: 13px; "
                "background: transparent; padding: 40px;"
            )
            self._content_layout.addWidget(empty_hint)

    # ── 关闭 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _cleanup_worker(self):
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()
