# -*- coding: utf-8 -*-
"""模型能力声明编辑弹窗（P1-9 声明层 UI）。

在「服务商 → 编辑 → 模型列表」每行的齿轮按钮打开，编辑该模型的六项能力声明：

    声明_上下文长度 / 声明_最大输出 / 声明_支持图像 /
    声明_支持思考 / 声明_思考参数 / 声明_思考强度

语义（与 ``model_capabilities.resolve_model_capabilities`` 的 L0 层一致）：
留空 = 未声明，走自动链（插件 per-model 声明 → models.dev → 硬编码 → family
→ 默认）。每个字段右侧标出「自动值 + 来源」，让用户看清自己覆盖的是什么。

弹窗只负责编辑与校验，落盘由调用方（模型列表编辑器）写 ``LLM.ModelOverrides``。
"""

from typing import Any, Dict, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, MaskDialogBase, PrimaryPushButton, PushButton

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

# 三态字段选项：(显示文案, 落盘值)；None = 不声明
_TRI_STATE = (("自动", None), ("是", True), ("否", False))
# 思考参数字段选项（与 MODEL_CAPABILITIES.thinking_param 取值一致）
_THINKING_PARAM = (
    ("自动", None),
    ("thinking", "thinking"),
    ("thinking_budget", "thinking_budget"),
    ("reasoning_effort", "reasoning_effort"),
)

# 自动链生效层 → 展示文案（与模型列表共用同一口径）
from app.core.modelmeta.model_declarations import describe_source

_DECL_KEYS = {
    "context": "声明_上下文长度",
    "max_output": "声明_最大输出",
    "vision": "声明_支持图像",
    "thinking": "声明_支持思考",
    "thinking_param": "声明_思考参数",
    "effort": "声明_思考强度",
}


def _fmt_limit(value: Any) -> str:
    """上下文/输出上限 → 紧凑显示（1024K 而非 1048576）"""
    try:
        num = int(value)
    except TypeError, ValueError:
        return "未知"
    if num <= 0:
        return "未知"
    if num >= 1000:
        return f"{num // 1000}K"
    return str(num)


def _fmt_bool(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def _fmt_effort(values: Any) -> str:
    if isinstance(values, (list, tuple)) and values:
        return "/".join(str(v) for v in values)
    return "未知"


class ModelCapabilityEditDialog(MaskDialogBase):
    """单模型能力声明编辑弹窗。

    Usage::

        dialog = ModelCapabilityEditDialog("hy4-preview", "CodeBuddy", parent=self.window())
        dialog.exec_()
        if dialog.saved:
            declared = dialog.declared   # 只含已声明键；空 dict = 全部自动
    """

    DEFAULT_WIDTH = 560
    MIN_HEIGHT = 320

    def __init__(self, model_name: str, provider_name: str = "", parent=None):
        super().__init__(parent)
        self.model_name = str(model_name or "")
        self.provider_name = str(provider_name or "")
        self.saved = False
        self.declared: Dict[str, Any] = {}
        self._auto: Dict[str, Any] = {}
        self._sources: Dict[str, Any] = {}
        self._load_auto_caps()
        self._init_ui()

    # ── 数据 ───────────────────────────────────────────────

    def _load_auto_caps(self):
        """查自动链生效值 + 来源（不含声明层，供占位提示）"""
        try:
            from app.core.modelmeta.model_capabilities import resolve_model_capabilities

            sources: Dict[str, Any] = {}
            cfg = {"模型名称": self.model_name, "provider_name": self.provider_name}
            self._auto = resolve_model_capabilities(cfg, self.provider_name, sources) or {}
            self._sources = sources
        except Exception:
            self._auto = {}
            self._sources = {}

    def _load_declared(self):
        """读当前已保存的声明（来自 ModelOverrides）"""
        try:
            from app.core.modelmeta.model_declarations import read_declarations
            from app.utils.config import Settings

            overrides = Settings.llm_model_overrides.value or {}
            return read_declarations(overrides, self.provider_name, self.model_name)
        except Exception:
            return {}

    def _source_text(self, field: str) -> str:
        """字段来源提示文案：自动值 + 生效层"""
        source = self._sources.get(field)
        if source is None:
            return ""
        label = describe_source(source) or str(source)
        return f"自动: {label}"

    # ── UI ─────────────────────────────────────────────────

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("capabilityDialogWidget")
        self.widget.setStyleSheet(f"""
            #capabilityDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)

        title = BodyLabel(self.widget)
        title.setText(f"模型能力：{self.model_name}")
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(16)}; font-weight: 600;"
        )
        layout.addWidget(title)

        hint = BodyLabel(self.widget)
        hint.setText(f"服务商: {self.provider_name or '未指定'} · 留空 = 跟随自动识别，右侧显示当前自动值与来源")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)};"
        )
        layout.addWidget(hint)
        layout.addSpacing(4)

        declared = self._load_declared()

        # 上下文长度 / 最大输出：整数输入，空 = 自动
        self.context_edit = self._add_int_row(
            layout,
            "上下文长度",
            "context",
            declared.get(_DECL_KEYS["context"]),
            _fmt_limit(self._auto.get("context_limit")),
        )
        self.max_output_edit = self._add_int_row(
            layout,
            "最大输出",
            "max_output_tokens",
            declared.get(_DECL_KEYS["max_output"]),
            _fmt_limit(self._auto.get("max_output_tokens")),
        )
        # 支持多模态 / 支持思考：三态
        self.vision_combo = self._add_choice_row(
            layout,
            "支持多模态",
            "supports_vision",
            _TRI_STATE,
            declared.get(_DECL_KEYS["vision"]),
            _fmt_bool(self._auto.get("supports_vision")),
        )
        self.thinking_combo = self._add_choice_row(
            layout,
            "支持思考",
            "supports_thinking",
            _TRI_STATE,
            declared.get(_DECL_KEYS["thinking"]),
            _fmt_bool(self._auto.get("supports_thinking")),
        )
        # 思考参数字段：决定请求体带哪个字段
        self.param_combo = self._add_choice_row(
            layout,
            "思考参数字段",
            "thinking_param",
            _THINKING_PARAM,
            declared.get(_DECL_KEYS["thinking_param"]),
            str(self._auto.get("thinking_param") or "未知"),
        )
        # 思考强度等级：逗号分隔文本
        self.effort_edit = self._add_text_row(
            layout,
            "思考强度等级",
            "reasoning_effort_values",
            declared.get(_DECL_KEYS["effort"]),
            _fmt_effort(self._auto.get("reasoning_effort_values")),
        )

        layout.addSpacing(6)
        self.error_label = BodyLabel(self.widget)
        self.error_label.setText("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            f"color: {Colors.ERROR}; background: transparent; {get_font_family_css()} {font_size_css(11)};"
        )
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        reset_btn = PushButton("全部重置为自动", self.widget)
        reset_btn.setCursor(Qt.PointingHandCursor)
        reset_btn.setStyleSheet(f"""
            PushButton {{
                background-color: transparent;
                color: {Colors.TEXT_MUTED};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 14px;
                {font_size_css(12)}
            }}
            PushButton:hover {{
                background-color: {Colors.HOVER_BG};
                color: {Colors.TEXT_PRIMARY};
            }}
        """)
        reset_btn.clicked.connect(self._on_reset)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)

        cancel_btn = PushButton("取消", self.widget)
        cancel_btn.setStyleSheet(f"""
            PushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 20px;
                {font_size_css(12)}
            }}
            PushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.BORDER_ACCENT};
            }}
        """)
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)

        save_btn = PrimaryPushButton("保存", self.widget)
        save_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background-color: {Colors.INFO};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 20px;
                {font_size_css(12)}
            }}
            PrimaryPushButton:hover {{
                background-color: {Colors.SEND_BTN_END};
            }}
        """)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        # MaskDialogBase 会把 widget 拉伸到全屏 → 摘出来按内容定尺寸并居中
        self.layout().removeWidget(self.widget)
        self.widget.setParent(self)
        self.widget.setMinimumWidth(self.DEFAULT_WIDTH)
        self.widget.setMaximumWidth(self.DEFAULT_WIDTH + 120)
        self.widget.setMinimumHeight(self.MIN_HEIGHT)
        self.widget.adjustSize()
        self._center_widget()

    def _add_int_row(self, layout, label: str, field: str, current, auto_text: str):
        """整数输入行（上下文长度 / 最大输出）；返回输入框"""
        return self._add_row(layout, label, field, LineEdit(self.widget), current, auto_text)

    def _add_text_row(self, layout, label: str, field: str, current, auto_text: str):
        """自由文本输入行（思考强度等级）；返回输入框"""
        return self._add_row(layout, label, field, LineEdit(self.widget), current, auto_text, is_text=True)

    def _add_choice_row(self, layout, label: str, field: str, options: Tuple, current, auto_text: str):
        """下拉选项行；返回下拉框"""
        combo = ComboBox(self.widget)
        for text, value in options:
            combo.addItem(text, userData=value)
        idx = 0
        for i, (_text, value) in enumerate(options):
            if current is not None and value == current:
                idx = i
                break
        combo.setCurrentIndex(idx)
        combo.setFixedHeight(32)
        return self._add_row(layout, label, field, combo, current, auto_text)

    def _add_row(
        self, layout, label: str, field: str, control: QWidget, current, auto_text: str, is_text: bool = False
    ):
        """一行：标签 + 控件 + 自动值提示"""
        row = QWidget(self.widget)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        name = BodyLabel(row)
        name.setText(label)
        name.setFixedWidth(96)
        name.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; {get_font_family_css()} {font_size_css(12)};"
        )
        lay.addWidget(name)

        if isinstance(control, LineEdit):
            control.setPlaceholderText(f"自动（当前 {auto_text}）；留空跟随自动")
            control.setFixedHeight(32)
            control.setStyleSheet(f"""
                LineEdit {{
                    background-color: {Colors.INPUT_BG_START};
                    border: 1px solid {Colors.INPUT_BORDER};
                    color: {Colors.INPUT_TEXT};
                    padding: 4px 10px;
                    border-radius: 6px;
                    {get_font_family_css()} {font_size_css(12)}
                }}
                LineEdit:focus {{ border-color: {Colors.INFO}; }}
            """)
            if current is not None:
                text = _fmt_effort(current) if is_text else str(current)
                control.setText(text)
        lay.addWidget(control, 1)

        src = BodyLabel(row)
        src.setText(self._source_text(field))
        src.setFixedWidth(96)
        src.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(10)};"
        )
        lay.addWidget(src)
        layout.addWidget(row)
        return control

    def _center_widget(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()

    # ── 交互 ───────────────────────────────────────────────

    def _read_int(self, edit: LineEdit, label: str):
        """读整数输入：空 → None（自动）；非法 → 抛 ValueError"""
        text = edit.text().strip()
        if not text:
            return None
        try:
            value = int(text.replace(",", "").replace("_", ""))
        except ValueError:
            raise ValueError(f"{label} 需要填正整数（留空 = 自动）") from None
        if value <= 0:
            raise ValueError(f"{label} 需要填正整数（留空 = 自动）")
        return value

    def _show_error(self, message: str):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _on_save(self):
        declared: Dict[str, Any] = {}
        try:
            context = self._read_int(self.context_edit, "上下文长度")
            max_output = self._read_int(self.max_output_edit, "最大输出")
        except ValueError as exc:
            self._show_error(str(exc))
            return
        if context is not None:
            declared[_DECL_KEYS["context"]] = context
        if max_output is not None:
            declared[_DECL_KEYS["max_output"]] = max_output

        vision = self.vision_combo.currentData()
        if vision is not None:
            declared[_DECL_KEYS["vision"]] = bool(vision)

        thinking = self.thinking_combo.currentData()
        if thinking is not None:
            declared[_DECL_KEYS["thinking"]] = bool(thinking)

        thinking_param = self.param_combo.currentData()
        if thinking_param:
            declared[_DECL_KEYS["thinking_param"]] = str(thinking_param)

        effort_text = self.effort_edit.text().strip()
        if effort_text:
            levels = [p.strip() for p in effort_text.replace("，", ",").replace("、", ",").split(",")]
            levels = [p for p in levels if p]
            if not levels:
                self._show_error("思考强度等级请用逗号分隔，如 low,medium,high")
                return
            declared[_DECL_KEYS["effort"]] = levels

        self.declared = declared
        self.saved = True
        self.accept()

    def _on_reset(self):
        """清空全部声明（回到自动链），作为一次保存生效"""
        self.declared = {}
        self.saved = True
        self.accept()
