# 可复用控件 — 图表组件

> 来自 `context-usage-stats` 等项目提炼的 PyQt5 `QPainter` 自绘图表。
> 索引和设计原则见 `widgets.md`。
> 默认配色 `_default_chart_colors()` 见 `widgets-theme.md §3`。

---

## 📑 目录

> 行号对应本文件当前版本，编辑后请更新。

- **一、柱状图 `_BarChartWidget`** — L40–L302
  - 1.1 用途 — L42–L45
  - 1.2 特性 — L46–L54
  - 1.3 完整代码 — L55–L278
  - 1.4 使用示例 — L279–L287
  - 1.5 关键设计点 — L288–L302
- **二、折线图 `_LineChartWidget`** — L303–L589
  - 2.1 用途 — L305–L308
  - 2.2 特性 — L309–L317
  - 2.3 完整代码 — L318–L575
  - 2.4 关键设计点 — L576–L589
- **三、水平柱状图 `_ProjectBarWidget`** — L590–L775
  - 3.1 用途 — L592–L595
  - 3.2 特性 — L596–L603
  - 3.3 完整代码 — L604–L765
  - 3.4 使用示例 — L766–L775
- **四、三个图表的对比与选择** — L776–L784
- **五、共同的设计模式** — L785–L793
- **六、Hover 交互实现模式（所有图表共用）** — L794–L870
  - 6.1 三步实现 — L798–L803
  - 6.2 模板代码 — L804–L831
  - 6.3 各类图表 hover 检测算法 — L832–L839
  - 6.4 高亮效果汇总 — L840–L847
  - 6.5 Tooltip 格式 — L848–L863
  - 6.6 注意点 — L864–L870
- **七、依赖关系** — L871–L880
- **八、注意事项** — L881–L888
## 一、柱状图 `_BarChartWidget`

### 1.1 用途

展示离散数据（如每日会话数、每日消息数）。**全部用 `QPainter` 自绘**，不依赖 QtCharts 等第三方库。

### 1.2 特性

- 自适应宽度（窄宽度时缩小边距和柱宽）
- Y 轴刻度自适应（最大值变化时重算）
- 柱顶值标签（柱体太高时自动移到柱内/柱下）
- X 轴周末高亮（周一、周六、周日显示星期）
- 圆角柱体（`QPainterPath.addRoundedRect`）
- **鼠标悬停交互**：hover 高亮（柱体变亮 + 加粗边框）+ `QToolTip` 显示精确日期和数值

### 1.3 完整代码

```python
from datetime import datetime
from typing import List, Tuple

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget


class _BarChartWidget(QWidget):
    """柱状图组件 — 用于展示每日会话数量"""

    def __init__(self, title: str, data: List[Tuple[str, int]],
                 color_key: str = "bar_fill", parent=None):
        super().__init__(parent)
        self._title = title
        self._data = data
        self._color_key = color_key
        self._colors = _default_chart_colors()
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)  # 启用鼠标追踪，支持 hover 交互
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()  # 触发 paintEvent

    def set_colors(self, colors: dict):
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
                    dt = datetime.strptime(f"2025-{parts[0]}-{parts[1]}", "%Y-%m-%d")
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    date_str = dt.strftime("%m-%d") + f" ({weekdays[dt.weekday()]})"
                else:
                    date_str = label
            except (ValueError, IndexError):
                date_str = label
            from PyQt5.QtWidgets import QToolTip
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

        # ── 自适应边距：窄宽度时缩小边距 ──
        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        margin_top = 34
        margin_bottom = 48 if w >= 400 else 40

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom
        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题（左上角） ──
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(
            QRectF(margin_left, 4, chart_w, 22),
            Qt.AlignLeft | Qt.AlignVCenter, self._title
        )

        # ── 数据范围 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        if max_val == 0:
            max_val = 1

        # ── Y 轴网格 + 刻度标签 ──
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

        # ── 柱体（圆角 + 描边 + 填充） ──
        n = len(self._data)
        bar_width = chart_w / n * (0.65 if w >= 400 else 0.55)
        bar_spacing = chart_w / n
        bar_color = colors.get(self._color_key, colors["bar_fill"])
        border_color = colors.get(
            self._color_key.replace("fill", "border"), colors["bar_border"]
        )

        x_tick_size = max(round(base_font_size * 7 / 14), 6)
        val_font_size = max(round(base_font_size * 9 / 14), 7)

        for i, (label, value) in enumerate(self._data):
            x = margin_left + i * bar_spacing + (bar_spacing - bar_width) / 2
            bar_h = (value / max_val) * chart_h if max_val > 0 else 0
            y = margin_top + chart_h - bar_h

            # 圆角矩形柱体
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

            # X 轴标签（含周末高亮）
            painter.setPen(colors["text_secondary"])
            tick_font = QFont(font_family, x_tick_size)
            painter.setFont(tick_font)
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = datetime.strptime(
                        f"2025-{parts[0]}-{parts[1]}", "%Y-%m-%d"
                    )
                    wd = dt.weekday()
                    if wd in (0, 5, 6):  # 周一、周六、周日高亮
                        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                        display_label = f"{parts[0]}-{parts[1]}\n{weekdays[wd]}"
                    else:
                        display_label = f"{parts[0]}-{parts[1]}"
                else:
                    display_label = label
            except (ValueError, IndexError):
                display_label = label

            painter.drawText(
                QRectF(x - bar_spacing / 2, h - margin_bottom + 4, bar_spacing, 36),
                Qt.AlignCenter, display_label,
            )

            # 柱顶值标签（防裁剪：上方不够则移柱内 / 柱下）
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
                    Qt.AlignCenter, str(value),
                )

        painter.end()
```

### 1.4 使用示例

```python
# data: List[Tuple[label, value]]，label 是 "MM-DD" 格式
data = [("07-01", 5), ("07-02", 12), ("07-03", 8), ...]
chart = _BarChartWidget("📊 每日会话活跃度", data)
chart.set_colors(self._chart_style)  # 主题色注入
```

### 1.5 关键设计点

| 设计点 | 说明 |
|--------|------|
| 自适应边距 | `if w >= 420` / `if w >= 400` 分支切换边距和柱宽 |
| 防标签裁剪 | 柱顶标签位置判断 `if label_y < margin_top` → 移柱内 |
| 圆角柱体 | `QPainterPath.addRoundedRect(rect, 3, 3)` |
| 周末高亮 | 仅周一/周六/周日显示星期文字 |
| 零值处理 | `if max_val == 0: max_val = 1` 防除零 |
| 主题色覆盖 | `colors.get(self._color_key, colors["bar_fill"])` 优先用注入的色 |
| **鼠标悬停高亮** | `self.setMouseTracking(True)` + `_hovered_index` 追踪，`paintEvent` 中 `bar_color.lighter(130)` 亮色 + 2px 加粗边框 |
| **Tooltip** | `mouseMoveEvent` 中调用 `QToolTip.showText()` 显示 `📊 MM-DD (星期)\n会话数: N` 格式 |

---

## 二、折线图 `_LineChartWidget`

### 2.1 用途

展示连续趋势数据（如每日 token 用量、消息量变化）。比柱状图多了**区域填充**和**数据点圆点**。

### 2.2 特性

- 区域填充（`QPainterPath` 闭合路径）
- 数据点圆点 + 值标签
- **自适应 Y 轴**（顶部留 30% 空间防标签裁剪）
- **标签位置智能调整**：上方不够则放下方，溢出边界则居中
- 折线 `Qt.RoundCap` 圆头
- **鼠标悬停交互**：hover 高亮（数据点放大 + 垂直虚线参考线）+ `QToolTip` 显示精确日期和数值

### 2.3 完整代码

```python
class _LineChartWidget(QWidget):
    """折线图组件 — 用于展示消息量 / token 趋势"""

    def __init__(self, title: str, data: List[Tuple[str, int]],
                 color_key: str = "line", parent=None):
        super().__init__(parent)
        self._title = title
        self._data = data
        self._color_key = color_key
        self._colors = _default_chart_colors()
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)  # 启用鼠标追踪，支持 hover 交互
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
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
                    dt = datetime.strptime(f"2025-{parts[0]}-{parts[1]}", "%Y-%m-%d")
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    date_str = dt.strftime("%m-%d") + f" ({weekdays[dt.weekday()]})"
                else:
                    date_str = label
            except (ValueError, IndexError):
                date_str = label
            from PyQt5.QtWidgets import QToolTip
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

        # 自适应边距（同 _BarChartWidget）
        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        margin_top = 34
        margin_bottom = 48 if w >= 400 else 40
        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom
        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # 标题（同柱状图）
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(
            QRectF(margin_left, 4, chart_w, 22),
            Qt.AlignLeft | Qt.AlignVCenter, self._title
        )

        # ── Y 轴自适应：给顶部留 30% 空间 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        min_val = min(values) if values else 0
        if max_val == min_val:
            max_val = max_val + 1 or 2
            min_val = 0
        top_margin = max_val * 0.3  # ← 关键：防数据点标签被顶部裁剪
        adjusted_max = max_val + top_margin

        # Y 轴网格 + 刻度（用 _format_number 显示紧凑数字）
        painter.setPen(colors["grid"])
        y_ticks = 4
        tick_font_size = max(round(base_font_size * 8 / 14), 7)
        for i in range(y_ticks + 1):
            y = margin_top + chart_h * (1 - i / y_ticks)
            painter.drawLine(QPointF(margin_left, y), QPointF(w - margin_right, y))
            val = int(min_val + (adjusted_max - min_val) * i / y_ticks)
            painter.setPen(colors["text_secondary"])
            tick_font = QFont(font_family, tick_font_size)
            painter.setFont(tick_font)
            painter.drawText(
                QRectF(2, y - 12, margin_left - 8, 24),
                Qt.AlignRight | Qt.AlignVCenter, _format_number(val),
            )
            painter.setPen(colors["grid"])

        # ── 计算数据点坐标 ──
        n = len(self._data)
        if n < 1:
            painter.end()
            return

        line_color = colors.get(self._color_key, colors["line"])
        fill_color = colors.get(f"{self._color_key}_fill", colors["line_fill"])
        point_color = colors.get(f"{self._color_key}_point", colors["point"])

        points: List[QPointF] = []
        for i, (_, value) in enumerate(self._data):
            x = (margin_left + chart_w * i / (n - 1)
                 if n > 1 else margin_left + chart_w / 2)
            ratio = ((value - min_val) / (adjusted_max - min_val)
                     if adjusted_max > min_val else 0.5)
            y = margin_top + chart_h - chart_h * ratio
            points.append(QPointF(x, y))

        # ── 区域填充（闭合路径） ──
        if len(points) >= 2:
            path = QPainterPath()
            path.moveTo(points[0])
            for pt in points[1:]:
                path.lineTo(pt)
            path.lineTo(points[-1].x(), margin_top + chart_h)
            path.lineTo(points[0].x(), margin_top + chart_h)
            path.closeSubpath()
            painter.fillPath(path, fill_color)

        # ── 折线（圆头连接） ──
        pen = QPen(line_color, 2.5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])

        # ── 数据点 + 值标签（智能防裁剪） ──
        val_font_size = max(round(base_font_size * 9 / 14), 7)
        label_w, label_h = 60, 20
        for i, (_, value) in enumerate(self._data):
            pt = points[i]
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

            # 标签位置：上方 → 下方 → 居中
            label_y = pt.y() - label_h - 4
            if label_y < margin_top:
                label_y = pt.y() + 6
                if label_y + label_h > h - margin_bottom + 6:
                    label_y = pt.y() - label_h // 2

            # 标签 X 位置防溢出
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
                    Qt.AlignCenter, _format_number(value),
                )

        # ── X 轴标签（数据多时只显示部分） ──
        x_tick_size = max(round(base_font_size * 7 / 14), 6)
        painter.setPen(colors["text_secondary"])
        tick_font = QFont(font_family, x_tick_size)
        painter.setFont(tick_font)
        for i, (label, _) in enumerate(self._data):
            if n > 10 and i % 2 != 0:  # ← 数据 > 10 个时隔一个显示
                continue
            x = (margin_left + chart_w * i / (n - 1)
                 if n > 1 else margin_left + chart_w / 2)
            # 周末高亮（同柱状图）
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = datetime.strptime(
                        f"2025-{parts[0]}-{parts[1]}", "%Y-%m-%d"
                    )
                    wd = dt.weekday()
                    if wd in (0, 5, 6):
                        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                        display = f"{parts[0]}-{parts[1]}\n{weekdays[wd]}"
                    else:
                        display = label
                else:
                    display = label
            except (ValueError, IndexError):
                display = label

            x_spacing = chart_w / n if n > 0 else chart_w
            painter.drawText(
                QRectF(x - x_spacing, h - margin_bottom + 4, x_spacing * 2, 36),
                Qt.AlignCenter, display,
            )

        painter.end()
```

### 2.4 关键设计点

| 设计点 | 说明 |
|--------|------|
| 顶部 30% 留白 | `top_margin = max_val * 0.3` 防止最大值附近的标签被裁剪 |
| 区域填充闭合路径 | `path.lineTo(..., margin_top + chart_h)` 把路径延伸到 X 轴 |
| 标签三段式防裁剪 | 上方 → 下方 → 数据点中央 |
| 数据稀疏显示 | `n > 10 and i % 2 != 0: continue` 避免 X 轴标签重叠 |
| 紧凑数字 | Y 轴和值标签都调用 `_format_number`（见 `widgets-utils.md`） |
| **鼠标悬停高亮** | `self.setMouseTracking(True)` + `_hovered_index` 追踪，hover 时数据点放大到 6px + 亮色，并绘制垂直虚线参考线 |
| **Tooltip** | `mouseMoveEvent` 中调用 `QToolTip.showText()` 显示 `📈 MM-DD (星期)\n{value}` 格式 |

---

## 三、水平柱状图 `_ProjectBarWidget`

### 3.1 用途

展示分类数据的占比（如各项目会话数排行、文件类型分布）。**水平布局**比垂直柱状图更适合"长标签 + 数值"。

### 3.2 特性

- 每行：标签 + 水平柱 + 数值
- 标签超长截断（>12 字加 `…`）
- 圆角柱体
- 行高自适应（min 14, max 28）
- **鼠标悬停交互**：hover 高亮（柱条变亮 + 加粗边框）+ `QToolTip` 显示完整项目名和数值

### 3.3 完整代码

```python
class _ProjectBarWidget(QWidget):
    """水平柱状图 — 用于分类数据排行（如项目分布）"""

    def __init__(self, data: List[Tuple[str, int]],
                 title: str = "📁 项目分布", parent=None):
        super().__init__(parent)
        self._data = data
        self._title = title
        self._colors = _default_chart_colors()
        self.setMinimumHeight(160)
        self.setMaximumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)  # 启用鼠标追踪，支持 hover 交互
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
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
            from PyQt5.QtWidgets import QToolTip
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

        # 标题区
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

        # 标题
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(
            QRectF(margin_left, 2, chart_w, title_h),
            Qt.AlignLeft | Qt.AlignVCenter, self._title
        )

        n = len(self._data)
        max_val = max(v for _, v in self._data)
        if max_val == 0:
            max_val = 1

        row_h = chart_h / n
        bar_h = max(row_h * 0.6, 14)
        bar_h = min(bar_h, 28)  # 行高限制，避免极端情况

        label_font_size = max(round(base_font_size * 10 / 14), 8)
        val_font_size = max(round(base_font_size * 9 / 14), 8)

        for i, (label, value) in enumerate(self._data):
            y = margin_top + i * row_h + (row_h - bar_h) / 2

            # 标签（超长截断）
            painter.setPen(colors["text"])
            label_font = QFont(font_family, label_font_size)
            painter.setFont(label_font)
            display_label = label if len(label) <= 12 else label[:11] + "…"
            painter.drawText(
                QRectF(margin_left, y, 80, bar_h),
                Qt.AlignLeft | Qt.AlignVCenter, display_label,
            )

            # 水平柱（圆角）
            bar_w = (value / max_val) * (chart_w - 80 - 60) if max_val > 0 else 0
            bar_x = margin_left + 84
            bar_color = QColor(colors["accent"])
            bar_color.setAlpha(180)
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(bar_x, y + 2, max(bar_w, 2), bar_h - 4), 4, 4
            )
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
                Qt.AlignLeft | Qt.AlignVCenter, str(value),
            )

        painter.end()
```

### 3.4 使用示例

```python
data = sorted(sessions_per_project.items(), key=lambda x: -x[1])[:8]
chart = _ProjectBarWidget(data, title="📁 项目分布")
chart.set_colors(self._chart_style)
```

---

## 四、三个图表的对比与选择

| 场景 | 推荐控件 | 数据示例 |
|------|----------|----------|
| 离散计数（每天几条） | `_BarChartWidget` | 每日会话数、每日消息数 |
| 连续趋势（有累加感） | `_LineChartWidget` | token 用量趋势、消息量变化 |
| 分类排行（长标签） | `_ProjectBarWidget` | 项目会话数排行、文件类型分布 |
| 占比展示（饼图替代） | `_ProjectBarWidget` | 各模型调用占比 |

## 五、共同的设计模式

1. **构造参数**：`title: str` + `data: List[Tuple[str, int]]` + `color_key: str`（可选）
2. **set_data / set_colors**：修改数据或主题色后必须 `self.update()` 触发 `paintEvent`
3. **自适应宽度**：所有 `paintEvent` 用 `if w >= 420` / `if w >= 400` 分支切换边距和字号
4. **周末高亮**：所有图表的 X 轴周一/周六/周日显示星期
5. **零值保护**：`if max_val == 0: max_val = 1` 防除零
6. **依赖 `_format_number`**：Y 轴和值标签统一用紧凑数字格式（详见 `widgets-utils.md`）

## 六、Hover 交互实现模式（所有图表共用）

所有三个图表组件使用**相同的 hover 交互模式**：

### 6.1 三步实现

1. **启用鼠标追踪**：`__init__` 中添加 `self.setMouseTracking(True)`
2. **追踪悬停索引**：`mouseMoveEvent` 中计算鼠标位置对应的数据索引，存到 `self._hovered_index`
3. **视觉反馈**：`paintEvent` 中判断 `if i == self._hovered_index` 绘制高亮效果

### 6.2 模板代码

```python
# ── 在 __init__ 末尾 ──
self.setMouseTracking(True)
self._hovered_index = -1

# ── 新增 mouseMoveEvent ──
def mouseMoveEvent(self, event):
    if not self._data:
        self._hovered_index = -1
        self.update()
        super().mouseMoveEvent(event)
        return

    # 1. 根据鼠标位置计算索引（具体算法因图表类型而异）
    # 2. 更新 self._hovered_index
    # 3. 调用 self.update() 触发重绘
    # 4. 调用 QToolTip.showText() 显示详细信息
    super().mouseMoveEvent(event)

# ── 新增 leaveEvent ──
def leaveEvent(self, event):
    self._hovered_index = -1
    self.update()
    super().leaveEvent(event)
```

### 6.3 各类图表 hover 检测算法

| 图表类型 | 检测维度 | 算法 | 阈值 |
|----------|----------|------|------|
| `_BarChartWidget` | X 轴 | `i = int((pos.x() - margin_left) / bar_spacing)` | 落在柱体 slot 范围内 |
| `_LineChartWidget` | X 轴 | `i = int(round((pos.x() - margin_left) / chart_w * (n-1)))` | `abs(dx) <= 30px` |
| `_ProjectBarWidget` | Y 轴 | `i = int((pos.y() - margin_top) / row_h)` | 落在行范围内 |

### 6.4 高亮效果汇总

| 图表类型 | 默认状态 | Hover 状态 |
|----------|----------|------------|
| `_BarChartWidget` | 柱体正常填充 + 1px 边框 | `bar_color.lighter(130)` 亮色 + 2px 加粗边框 |
| `_LineChartWidget` | 数据点 3px 圆点 | 数据点放大到 6px + 亮色 + 垂直虚线参考线 |
| `_ProjectBarWidget` | 柱条正常填充 + 1px 边框 | `bar_color.lighter(140)` 亮色 + 2px 加粗边框 |

### 6.5 Tooltip 格式

```
# 柱状图
📊 01-15 (周三)
会话数: 5

# 折线图
📈 01-15 (周三)
12.5k

# 水平柱状图
📁 project_name
会话数: 15
```

### 6.6 注意点

- **`QToolTip.showText(event.globalPos(), text, self)`**：第三个参数 `self` 将 tooltip 关联到当前 widget，widget 隐藏/销毁时 tooltip 自动消失
- **`leaveEvent` 必须重置 `_hovered_index = -1`**：否则鼠标离开后高亮仍停留在最后一个悬停位置
- **`super().mouseMoveEvent(event)`**：必须在末尾调用，确保 Qt 事件链完整
- **导入路径**：`QToolTip` 从 `PyQt5.QtWidgets` 导入，可在方法体内局部 import 避免顶层依赖

## 七、依赖关系

```
widgets-charts.md
├─ 依赖 PyQt5.QtCore / QtGui / QtWidgets（含 QToolTip）
├─ 依赖 qfluentwidgets（仅 _ProjectBarWidget 间接通过 paintEvent）
├─ 依赖 widgets-utils.md: _format_number
└─ 依赖 widgets-theme.md: _default_chart_colors()
```

## 八、注意事项

- **`QPainter` 不结束会卡 UI**：每个提前 `return` 前必须 `painter.end()`
- **QColor 字符串 alpha**：`"#ffffff1e"` 末尾 `1e` 是 alpha 00~ff
- **主题色不同步**：浮动卡片场景固定白色文字（见 `widgets-theme.md`）
- **超大数据值**：`_LineChartWidget` 的 `top_margin` 会让小值在底部聚集，注意调节
- **Hover 性能**：`mouseMoveEvent` 频繁触发，避免在其中做耗时操作；只计算索引 + 调用 `update()`，具体绘制在 `paintEvent` 中完成
- **QToolTip 局部导入**：建议在 `mouseMoveEvent` 方法体内 `from PyQt5.QtWidgets import QToolTip`，避免顶层导入污染