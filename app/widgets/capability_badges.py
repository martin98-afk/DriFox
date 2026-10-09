# -*- coding: utf-8 -*-
"""模型能力徽章组件（P2-13 抽取）。

抽取动机：同一套「能力徽章」在两个地方各写了一份——
- `model_selector_card.ModelItem._setup_ui` 里的三徽章段（胶囊 QLabel + 配色）
- `main_widget` 模型按钮 tooltip 的能力文本拼装

两处对「什么算能力」的判定迟早会漂移，故抽出：
- `ModelCapabilityBadges`：可复用组件（caps dict 直收，无数据类别自动跳过）
- `build_capability_tooltip`：纯函数（无 Qt 依赖，可直接单测）
- `_lighten_hex` / `cap_badge_colors`：随迁的色彩辅助

**边界**：成本（cost）相关逻辑不在此模块——`_format_cost_number` 仍留在
`model_selector_card.py`（`main_widget` 跨文件 import 它，迁移会破坏既有引用）。
"""

from typing import Any, Dict, FrozenSet, List, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

# 徽章类别标识（供 show 掩码筛选）
BADGE_THINKING = "thinking"
BADGE_EFFORT = "effort"
BADGE_VISION = "vision"

# 默认全显
ALL_BADGES: FrozenSet[str] = frozenset({BADGE_THINKING, BADGE_EFFORT, BADGE_VISION})


def _lighten_hex(hex_color: str, amount: float) -> str:
    """将 6 位 hex 颜色向白色提亮 amount（0~1），用于深色底上的标签文字更醒目。"""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def cap_badge_colors() -> Tuple[str, str, str, str, str, str]:
    """能力徽章配色（开关思考 / 多模态 / 思考强度）。

    深色主题下整体提亮——文字向白提亮、背景 alpha 略增，避免颜色在深底上显得过深；
    浅色主题保持主题 token 原值（深色文字配合浅底，避免亮字看不清）。
    返回：(think_text, think_bg, vision_text, vision_bg, effort_text, effort_bg)
    """
    Colors.refresh()
    from app.utils.theme_manager import theme_manager

    if theme_manager.is_light_theme():
        return (
            Colors.TAG_ORANGE_TEXT,
            "rgba(255,179,102,0.18)",
            Colors.TAG_ACCENT_TEXT,
            "rgba(102,198,255,0.18)",
            Colors.TAG_PURPLE_TEXT,
            "rgba(179,136,255,0.15)",
        )
    return (
        _lighten_hex(Colors.TAG_ORANGE_TEXT, 0.22),
        "rgba(255,179,102,0.28)",
        _lighten_hex(Colors.TAG_ACCENT_TEXT, 0.22),
        "rgba(102,198,255,0.28)",
        _lighten_hex(Colors.TAG_PURPLE_TEXT, 0.22),
        "rgba(179,136,255,0.24)",
    )


def effort_values_of(caps: Dict[str, Any]) -> List[str]:
    """思考强度可选值（models.dev reasoning_effort values，如 ["high","max"]）"""
    values = (caps or {}).get("reasoning_effort_values")
    return [str(v) for v in values] if values else []


def build_capability_tooltip(caps: Dict[str, Any]) -> str:
    """把能力 caps 组装成 tooltip 能力段文本（纯函数，无 Qt 依赖）。

    只负责「能力」部分（开关思考 / 思考强度 / 多模态），**不含成本与模型名**——
    那两段由调用方按自己的版式拼（模型选择卡是单行简要、模型按钮是「·」分隔）。

    Args:
        caps: 模型能力 dict（get_model_capabilities 的返回值）

    Returns:
        空格分隔的能力短语，如 ``"开关思考 思考强度: high/max 多模态"``；无能力返回空串。
    """
    caps = caps or {}
    parts: List[str] = []
    if caps.get("supports_thinking"):
        parts.append("开关思考")
    effort_values = effort_values_of(caps)
    if effort_values:
        parts.append(f"思考强度: {'/'.join(effort_values)}")
    if caps.get("supports_vision"):
        parts.append("多模态")
    return "  ".join(parts)


class ModelCapabilityBadges(QWidget):
    """能力徽章行（开关思考 / 思考强度 / 多模态）。

    - ``caps`` 直收模型能力 dict；无数据的类别自动跳过（不占位）
    - ``show`` 掩码可只显部分徽章（默认全显）；调用方的「是否有 trailing 内容」
      判定必须与掩码保持一致，否则名称列固定宽度会算错、对齐错乱
    """

    def __init__(
        self,
        caps: Dict[str, Any],
        show: FrozenSet[str] = ALL_BADGES,
        parent: QWidget = None,
    ):
        super().__init__(parent)
        self._caps = caps or {}
        self._show = show
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        think_text, think_bg, vision_text, vision_bg, effort_text_c, effort_bg = cap_badge_colors()

        if BADGE_THINKING in self._show and self._caps.get("supports_thinking"):
            self.think_label = self._make_badge("开关思考", think_text, think_bg, "支持思考开关", layout)

        effort_values = effort_values_of(self._caps)
        if BADGE_EFFORT in self._show and effort_values:
            effort_text = "/".join(effort_values)
            self.effort_label = self._make_badge(
                "思考强度", effort_text_c, effort_bg, f"支持的思考强度: {effort_text}", layout
            )

        if BADGE_VISION in self._show and self._caps.get("supports_vision"):
            self.vision_label = self._make_badge("多模态", vision_text, vision_bg, "支持多模态输入", layout)

    def _make_badge(self, text: str, text_color: str, bg_color: str, tip: str, layout: QHBoxLayout) -> QLabel:
        """构造能力徽章（文字胶囊，替换 emoji）"""
        lbl = QLabel(text, self)
        lbl.setStyleSheet(
            f"color: {text_color};"
            f"background-color: {bg_color};"
            f"border-radius: 4px; padding: 0 6px 0 6px;"
            f"font-weight: 600;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        lbl.setFixedHeight(18)
        lbl.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        lbl.setToolTip(tip)
        layout.addWidget(lbl, 0, Qt.AlignVCenter)
        return lbl

    def has_any_badge(self) -> bool:
        """当前掩码 + 数据下是否真的渲染了徽章（供调用方对齐判定复用）"""
        return any(
            getattr(self, attr, None) is not None
            for attr in ("think_label", "effort_label", "vision_label")
        )
