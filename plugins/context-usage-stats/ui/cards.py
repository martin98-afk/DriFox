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

import math
import sqlite3
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, QPointF, QRectF, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    ScrollArea,
    StrongBodyLabel,
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
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _chart_colors() -> dict:
    """返回图表颜色方案（跟随主题）"""
    if isDarkTheme():
        return {
            "bar_fill": QColor(98, 160, 234, 200),
            "bar_border": QColor(98, 160, 234),
            "line": QColor(80, 227, 194),
            "line_fill": QColor(80, 227, 194, 60),
            "point": QColor(80, 227, 194),
            "grid": QColor(255, 255, 255, 30),
            "text": QColor(255, 255, 255, 180),
            "text_secondary": QColor(255, 255, 255, 100),
            "card_bg": QColor(255, 255, 255, 20),
            "accent": QColor(98, 160, 234),
            "warning": QColor(255, 193, 7, 200),
            "success": QColor(80, 227, 194, 200),
        }
    return {
        "bar_fill": QColor(40, 120, 220, 180),
        "bar_border": QColor(40, 120, 220),
        "line": QColor(0, 168, 136),
        "line_fill": QColor(0, 168, 136, 40),
        "point": QColor(0, 168, 136),
        "grid": QColor(0, 0, 0, 20),
        "text": QColor(0, 0, 0, 180),
        "text_secondary": QColor(0, 0, 0, 100),
        "card_bg": QColor(0, 0, 0, 8),
        "accent": QColor(40, 120, 220),
        "warning": QColor(245, 158, 11, 200),
        "success": QColor(16, 185, 129, 200),
    }


# ── Token 快速估算 ────────────────────────────────────────


def _fast_estimate_tokens(text: str) -> int:
    """快速估算文本的 token 数（无需 tiktoken 依赖）

    经验公式：
    - 1 token ≈ 4 英文/混合字符
    - 1 token ≈ 2 中文字符
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    non_chinese = len(text) - chinese
    estimated = chinese // 2 + non_chinese // 4
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

        # 1. 基础统计
        cursor.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(message_count), 0) as msgs "
            "FROM sessions WHERE project NOT LIKE '__archived__%'"
        )
        row = cursor.fetchone()
        total_sessions = row["cnt"] if row else 0
        total_messages = row["msgs"] if row else 0
        result["total_sessions"] = total_sessions
        result["total_messages"] = total_messages

        # 2. 压缩统计
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM sessions "
            "WHERE compaction_state IS NOT NULL "
            "AND compaction_state != '' "
            "AND compaction_state != '{}' "
            "AND project NOT LIKE '__archived__%'"
        )
        row = cursor.fetchone()
        result["total_compacted"] = row["cnt"] if row else 0
        result["compaction_rate"] = result["total_compacted"] / total_sessions if total_sessions > 0 else 0.0

        # 3. 按项目统计
        cursor.execute(
            "SELECT project, COUNT(*) as cnt FROM sessions "
            "WHERE project NOT LIKE '__archived__%' "
            "GROUP BY project ORDER BY cnt DESC"
        )
        rows = cursor.fetchall()
        for row in rows:
            result["sessions_per_project"][row["project"]] = row["cnt"]

        # 4. 最近 14 天的按日统计（基于 updated_at）
        today = datetime.now()
        date_labels = []
        for i in range(13, -1, -1):
            date_labels.append((today - timedelta(days=i)).strftime("%m-%d"))

        # 初始化每日数据
        daily_sessions_map: Dict[str, int] = {}
        daily_messages_map: Dict[str, int] = {}
        daily_tokens_map: Dict[str, int] = {}
        for dl in date_labels:
            daily_sessions_map[dl] = 0
            daily_messages_map[dl] = 0
            daily_tokens_map[dl] = 0

        # 查询最近 14 天会话数（按 updated_at 日期聚合）
        cursor.execute(
            "SELECT DATE(updated_at) as day, COUNT(*) as cnt, "
            "COALESCE(SUM(message_count), 0) as msgs "
            "FROM sessions "
            "WHERE updated_at >= date('now', '-14 days') "
            "AND project NOT LIKE '__archived__%' "
            "GROUP BY DATE(updated_at) "
            "ORDER BY day"
        )
        rows = cursor.fetchall()
        for row in rows:
            day_str = row["day"]
            if day_str:
                try:
                    dt = datetime.strptime(day_str, "%Y-%m-%d")
                    label = dt.strftime("%m-%d")
                    daily_sessions_map[label] = row["cnt"]
                    daily_messages_map[label] = row["msgs"]
                except ValueError:
                    pass

        # 5. 估算每日 token 量（对最近 14 天内有更新的会话的 messages 字段采样）
        cursor.execute(
            "SELECT session_id, DATE(updated_at) as day, messages, message_count "
            "FROM sessions "
            "WHERE updated_at >= date('now', '-14 days') "
            "AND project NOT LIKE '__archived__%' "
            "AND messages IS NOT NULL AND messages != '' "
            "ORDER BY updated_at DESC"
        )
        rows = cursor.fetchall()
        for row in rows:
            day_str = row["day"]
            if not day_str:
                continue
            try:
                dt = datetime.strptime(day_str, "%Y-%m-%d")
                label = dt.strftime("%m-%d")
                msg_data = row["messages"]
                if isinstance(msg_data, (str, bytes)):
                    tokens = _fast_estimate_tokens(str(msg_data)[:50000])
                    daily_tokens_map[label] += tokens
            except ValueError, TypeError:
                pass

        # 转换为有序列表
        for dl in date_labels:
            result["daily_sessions"].append((dl, daily_sessions_map[dl]))
            result["daily_messages"].append((dl, daily_messages_map[dl]))
            result["daily_tokens"].append((dl, daily_tokens_map[dl]))

        # 6. 平均消息数
        result["avg_messages_per_session"] = round(total_messages / total_sessions, 1) if total_sessions > 0 else 0.0

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


def _short_weekday(date_str: str) -> str:
    """将 '01-15' 转换为 '01-15\n周一' 格式"""
    try:
        dt = datetime.strptime(f"2025-{date_str}", "%Y-%m-%d")
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
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = _chart_colors()
        w = self.width()
        h = self.height()

        # 边距
        margin_left = 40
        margin_right = 16
        margin_top = 32
        margin_bottom = 48

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        painter.setPen(colors["text"])
        title_font = QFont("Microsoft YaHei", 11, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 4, chart_w, 24), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        # ── 计算数据范围 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        if max_val == 0:
            max_val = 1

        # ── Y 轴 ──
        painter.setPen(colors["grid"])
        y_ticks = 4
        for i in range(y_ticks + 1):
            y = margin_top + chart_h * (1 - i / y_ticks)
            painter.drawLine(QPointF(margin_left, y), QPointF(w - margin_right, y))

            val = int(max_val * i / y_ticks)
            painter.setPen(colors["text_secondary"])
            tick_font = QFont("Microsoft YaHei", 8)
            painter.setFont(tick_font)
            painter.drawText(
                QRectF(0, y - 10, margin_left - 4, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                str(val),
            )
            painter.setPen(colors["grid"])

        # ── 柱状图 ──
        n = len(self._data)
        if n == 0:
            painter.end()
            return

        bar_width = chart_w / n * 0.6
        bar_spacing = chart_w / n

        bar_color = colors.get(self._color_key, colors["bar_fill"])
        border_color = colors.get(self._color_key.replace("fill", "border"), colors["bar_border"])

        for i, (label, value) in enumerate(self._data):
            x = margin_left + i * bar_spacing + (bar_spacing - bar_width) / 2
            bar_h = (value / max_val) * chart_h if max_val > 0 else 0
            y = margin_top + chart_h - bar_h

            # 柱体
            rect = QRectF(x, y, bar_width, max(bar_h, 0))
            path = QPainterPath()
            path.addRoundedRect(rect, 3, 3)
            painter.fillPath(path, bar_color)
            painter.setPen(QPen(border_color, 1))
            painter.drawPath(path)

            # X 轴标签
            painter.setPen(colors["text_secondary"])
            tick_font = QFont("Microsoft YaHei", 7)
            painter.setFont(tick_font)
            # 显示 "01-15\n周一" 格式（仅周末/周一标注星期）
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = datetime.strptime(f"2025-{parts[0]}-{parts[1]}", "%Y-%m-%d")
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
                QRectF(x - bar_spacing / 2, h - margin_bottom + 4, bar_spacing, 40),
                Qt.AlignCenter,
                display_label,
            )

            # 值标签（柱顶）
            if value > 0:
                painter.setPen(colors["text"])
                val_font = QFont("Microsoft YaHei", 8, QFont.Bold)
                painter.setFont(val_font)
                painter.drawText(
                    QRectF(x, y - 18, bar_width, 16),
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
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = _chart_colors()
        w = self.width()
        h = self.height()

        margin_left = 40
        margin_right = 16
        margin_top = 32
        margin_bottom = 48

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        painter.setPen(colors["text"])
        title_font = QFont("Microsoft YaHei", 11, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 4, chart_w, 24), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        # ── 计算数据范围 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        min_val = min(values) if values else 0
        if max_val == min_val:
            max_val = max_val + 1 or 2
            min_val = 0
        # 给顶部留 20% 空间
        range_val = max_val - min_val
        top_margin = max_val * 0.2
        adjusted_max = max_val + top_margin

        # ── 网格 + Y 轴 ──
        painter.setPen(colors["grid"])
        y_ticks = 4
        for i in range(y_ticks + 1):
            y = margin_top + chart_h * (1 - i / y_ticks)
            painter.drawLine(QPointF(margin_left, y), QPointF(w - margin_right, y))

            val = int(min_val + (adjusted_max - min_val) * i / y_ticks)
            painter.setPen(colors["text_secondary"])
            tick_font = QFont("Microsoft YaHei", 8)
            painter.setFont(tick_font)
            painter.drawText(
                QRectF(0, y - 10, margin_left - 4, 20),
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
        for i, (_, value) in enumerate(self._data):
            pt = points[i]
            # 圆点
            painter.setPen(Qt.NoPen)
            painter.setBrush(point_color)
            painter.drawEllipse(pt, 4, 4)
            painter.setBrush(Qt.NoBrush)

            # 值标签
            if value > 0:
                painter.setPen(colors["text"])
                val_font = QFont("Microsoft YaHei", 8, QFont.Bold)
                painter.setFont(val_font)
                painter.drawText(
                    QRectF(pt.x() - 20, pt.y() - 22, 40, 18),
                    Qt.AlignCenter,
                    _format_number(value),
                )

        # ── X 轴标签 ──
        painter.setPen(colors["text_secondary"])
        tick_font = QFont("Microsoft YaHei", 7)
        painter.setFont(tick_font)
        for i, (label, _) in enumerate(self._data):
            x = margin_left + chart_w * i / (n - 1) if n > 1 else margin_left + chart_w / 2
            # 标签太多时只显示部分
            if n > 10 and i % 2 != 0:
                continue
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = datetime.strptime(f"2025-{parts[0]}-{parts[1]}", "%Y-%m-%d")
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
                QRectF(x - x_spacing, h - margin_bottom + 4, x_spacing * 2, 40),
                Qt.AlignCenter,
                display,
            )

        painter.end()


class _StatCard(QFrame):
    """单个统计信息卡片"""

    def __init__(self, icon, title: str, value: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._value = value
        self._subtitle = subtitle
        self.setup_ui()

    def setup_ui(self):
        self.setObjectName("statCard")
        self.setStyleSheet(
            "#statCard { background: transparent; border: 1px solid rgba(128,128,128,0.12); "
            "border-radius: 10px; padding: 0px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        # 图标 + 标题行
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        icon_w = IconWidget(self._icon, self)
        icon_w.setFixedSize(16, 16)
        top_row.addWidget(icon_w)

        title_lb = QLabel(self._title, self)
        title_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 11px; background: transparent;")
        top_row.addWidget(title_lb)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        # 值
        val_lb = QLabel(self._value, self)
        val_lb.setStyleSheet(f"color: {_text_color()}; font-size: 22px; font-weight: bold; background: transparent;")
        layout.addWidget(val_lb)

        # 副标题
        if self._subtitle:
            sub_lb = QLabel(self._subtitle, self)
            sub_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 11px; background: transparent;")
            layout.addWidget(sub_lb)


# ══════════════════════════════════════════════════════════
# 主卡片
# ══════════════════════════════════════════════════════════


class ContextUsageStatsCard(QWidget):
    """上下文用量统计浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_DataWorker] = None
        self._stats_data: Optional[dict] = None
        self._setup_ui()

        # 自动加载数据
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(150, self._async_load_data)

    # ── 界面搭建 ──

    def _setup_ui(self):
        self.setMinimumWidth(560)
        self.setMinimumHeight(500)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("ContextUsageStatsCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 头部 ──
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        icon = IconWidget(FluentIcon.HISTORY, header)
        icon.setFixedSize(22, 22)
        hly.addWidget(icon)

        title = StrongBodyLabel("上下文用量统计", header)
        title.setStyleSheet(f"color: {_text_color()}; background: transparent;")
        hly.addWidget(title)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;"
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
        self._empty_lb = StrongBodyLabel("正在加载统计数据…", self)
        self._empty_lb.setAlignment(Qt.AlignCenter)
        self._empty_lb.setStyleSheet(f"color: {_text_color(secondary=True)}; background: transparent;")
        self._empty_lb.setVisible(True)
        root.addWidget(self._empty_lb)

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

        # ── 概要统计卡片行 ──
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)

        total_sessions = data.get("total_sessions", 0)
        total_messages = data.get("total_messages", 0)
        total_compacted = data.get("total_compacted", 0)
        avg_msgs = data.get("avg_messages_per_session", 0.0)
        compact_rate = data.get("compaction_rate", 0.0)

        stat_cards = [
            (
                FluentIcon.CHAT,
                "总会话数",
                str(total_sessions),
                f"其中 {total_compacted} 个已压缩" if total_compacted > 0 else "",
            ),
            (FluentIcon.MESSAGE, "总消息数", _format_number(total_messages), f"平均 {avg_msgs} 条/会话"),
            (FluentIcon.ZIP_FOLDER, "压缩率", _format_pct(compact_rate), f"{total_compacted}/{total_sessions} 个会话"),
            (
                FluentIcon.PEOPLE,
                "项目数",
                str(len(data.get("sessions_per_project", {}))),
                f"共 {total_sessions} 会话分布",
            ),
        ]

        for ic, title, val, sub in stat_cards:
            card = _StatCard(ic, title, val, sub)
            stats_row.addWidget(card)

        stats_widget = QWidget()
        stats_widget.setLayout(stats_row)
        stats_widget.setStyleSheet("background: transparent;")
        self._content_layout.addWidget(stats_widget)

        # ── 项目分布标签 ──
        sessions_per_project = data.get("sessions_per_project", {})
        if sessions_per_project:
            proj_widget = QWidget()
            proj_widget.setStyleSheet("background: transparent;")
            proj_layout = QHBoxLayout(proj_widget)
            proj_layout.setContentsMargins(0, 0, 0, 0)
            proj_layout.setSpacing(8)

            proj_label = QLabel("项目分布:", proj_widget)
            proj_label.setStyleSheet(f"color: {_text_color(secondary=True)}; font-size: 12px; background: transparent;")
            proj_layout.addWidget(proj_label)

            for proj, cnt in sorted(sessions_per_project.items(), key=lambda x: -x[1])[:6]:
                tag = QLabel(f"「{proj}」{cnt}个", proj_widget)
                tag.setStyleSheet(
                    f"color: {_text_color()}; font-size: 11px; background: rgba(128,128,128,0.08); "
                    f"border-radius: 4px; padding: 2px 8px;"
                )
                proj_layout.addWidget(tag)
            proj_layout.addStretch(1)

            self._content_layout.addWidget(proj_widget)

        # ── 会话活跃度柱状图 ──
        daily_sessions = data.get("daily_sessions", [])
        if daily_sessions and any(v for _, v in daily_sessions):
            bar_widget = _BarChartWidget("📊 每日会话活跃度", daily_sessions)
            self._content_layout.addWidget(bar_widget)

        # ── 消息量趋势折线图 ──
        daily_messages = data.get("daily_messages", [])
        if daily_messages and any(v for _, v in daily_messages):
            line_widget = _LineChartWidget("📈 每日消息量趋势", daily_messages, color_key="line")
            self._content_layout.addWidget(line_widget)

        # ── 估算 Token 用量折线图 ──
        daily_tokens = data.get("daily_tokens", [])
        if daily_tokens and any(v for _, v in daily_tokens):
            token_widget = _LineChartWidget("🔤 估算 Token 用量趋势", daily_tokens, color_key="accent")
            self._content_layout.addWidget(token_widget)

        # ── 无数据提示 ──
        if not daily_sessions or not any(v for _, v in daily_sessions):
            empty_hint = QLabel("暂无会话数据，开始对话后将自动生成统计。", self._content)
            empty_hint.setAlignment(Qt.AlignCenter)
            empty_hint.setStyleSheet(
                f"color: {_text_color(secondary=True)}; font-size: 13px; background: transparent; padding: 40px;"
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
