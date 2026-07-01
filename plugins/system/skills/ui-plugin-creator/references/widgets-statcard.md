# 可复用控件 — 统计卡片 `_StatCard`

> 来自 `context-usage-stats` 等项目提炼的 PyQt5 自绘控件。
> 索引和设计原则见 `widgets.md`。

---

## 一、统计卡片 `_StatCard`

### 1.1 用途

紧凑显示一个数字指标（图标 + 标题 + 大数字 + 副标题 + 可选补充小字）。最常用于"第一行概要卡片"。

### 1.2 特性

- 支持 5 层信息：图标、标题、值、副标题、**额外小字**
- 字号随 `colors["font_size"]` 等比例缩放
- 主题色动态注入（拉模型）
- 浮动卡片背景偏暗时强制白色文字（避免 qfluentwidgets 主题状态不同步导致黑字）

### 1.3 完整代码

```python
# -*- coding: utf-8 -*-
"""_StatCard — 多层级统计信息卡片"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import IconWidget


class _StatCard(QFrame):
    """单个统计信息卡片：图标 + 标题 + 值 + 副标题 + 可选补充小字"""

    def __init__(self, icon, title: str, value: str, subtitle: str = "",
                 extra_info: str = "", parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._value = value
        self._subtitle = subtitle
        self._extra_info = extra_info  # 副标题下的额外小字（用于"日均 X"等补充信息）
        self._colors = _default_card_colors()  # 默认 fallback 配色
        self.setup_ui()

    def set_colors(self, colors: dict):
        """注入外部配色（来自 context）"""
        self._colors = colors
        self._apply_card_style()

    def _apply_card_style(self):
        """根据当前 colors 刷新所有 QLabel 样式"""
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

        font_family = self._colors.get("font_family", "Microsoft YaHei")
        base_font_size = self._colors.get("font_size", 14)
        val_size = max(round(base_font_size * 22 / 14), 16)
        sub_size = max(round(base_font_size * 11 / 14), 9)
        extra_size = max(round(base_font_size * 10 / 14), 8)

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
            f"color: {text_sec}; font-size: {sub_size}px; font-family: '{font_family}'; "
            f"background: transparent;"
        )
        top_row.addWidget(title_lb)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        # 值（大字）
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
                f"color: {text_sec}; font-size: {sub_size}px; "
                f"font-family: '{font_family}'; background: transparent;"
            )
            layout.addWidget(sub_lb)

        # 额外小字（副标题之下，用于"日均 X"等补充信息，字号小于副标题）
        if self._extra_info:
            extra_lb = QLabel(self._extra_info, self)
            extra_lb.setObjectName("statExtra")
            extra_lb.setStyleSheet(
                f"color: {text_sec}; font-size: {extra_size}px; opacity: 0.85; "
                f"font-family: '{font_family}'; background: transparent;"
            )
            layout.addWidget(extra_lb)


def _default_card_colors() -> dict:
    """默认 fallback 配色（无上下文时使用）"""
    return {
        "text": QColor(255, 255, 255, 200),
        "text_secondary": QColor(255, 255, 255, 150),
        "font_family": "Microsoft YaHei",
        "font_size": 14,
    }
```

### 1.4 使用示例

```python
from qfluentwidgets import FluentIcon

# 单卡片
card = _StatCard(
    FluentIcon.CHAT,           # 图标
    "总会话数",                  # 标题
    "283",                     # 大字数值
    "平均 20.2 次/天",          # 副标题
    "日均 398k（近 14 天）",     # 额外小字（可选）
)

# 主题色注入（拉模型）
card.set_colors(self._chart_style)

# 横向并排：QHBoxLayout + addStretch(1) 自动均分
row = QHBoxLayout()
row.setSpacing(8)
for card in cards:
    row.addWidget(card)
```

### 1.5 注意事项

- **`extra_info` 字号小于 `subtitle`**（10/14 vs 11/14），并加 `opacity: 0.85` 制造视觉层次
- 浮动卡片背景偏暗时，`_default_card_colors()` 强制返回白色文字（避免 qfluentwidgets 主题状态不同步导致黑字）
- `_apply_card_style` 通过 `findChildren(QLabel)` + `objectName` 区分控件，所以**改 setStyleSheet 时必须保留 objectName**
- 当卡片宽度变窄时，建议根据实际可用宽度调整卡片标题长度（如"总 token"代替"总 token 数"）

### 1.6 推荐用法：把最重要的指标放第一张

```python
# 第一行：3 张 _StatCard，最重要的指标放最左边
stat_cards = [
    (FluentIcon.FONT, "总 token 数", str(total_tokens), "累计消耗",
     f"日均 {_format_number(avg_daily_tokens)}（近 14 天）"),  # 额外小字
    (FluentIcon.CHAT, "总会话数", str(total_sessions), f"平均 {avg_daily} 次/天"),
    (FluentIcon.MESSAGE, "总消息数", _format_number(total_messages),
     f"平均 {avg_msgs} 条/会话"),
]

stat_row = QHBoxLayout()
stat_row.setSpacing(8)
for ic, title, val, sub, *extra in stat_cards:
    card = _StatCard(ic, title, val, sub, extra_info=extra[0] if extra else "")
    if self._chart_style:
        card.set_colors(self._chart_style)
    stat_row.addWidget(card)
```