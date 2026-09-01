# -*- coding: utf-8 -*-
"""sections.py — 助手中心单列分区组件（对齐 openhanako AgentTab 分区结构）

分区：ProfileSection（名称/模型）→ AboutSection（人格切换，只读）→
MemorySection（记忆传送带）→ ExperienceSection（经验）→ SkillsSection（技能）。

视觉基调（对齐原版纸张风）：细边框卡 + 12px 圆角 + 小字 hint + 大量留白；
去 emoji，统一 FluentIcon / 文字标签。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QFontMetrics
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ComboBox, SwitchButton

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from assistant_hub_manager import AssistantManager

from .assistant_avatar import RoundAvatar

# ── 基础样式辅助 ─────────────────────────────────────


def _elide_lines(text: str, fm, width: int, max_lines: int = 2) -> str:
    """按像素宽度把 text 折行到 max_lines 行，超出部分末行截断加 …。

    中文无空格需逐字累积；QLabel 原生无多行省略，chips 卡描述用。
    """
    if not text:
        return ""
    lines: List[str] = []
    cur = ""
    for ch in text:
        if fm.horizontalAdvance(cur + ch) <= width:
            cur += ch
            continue
        lines.append(cur)
        if len(lines) == max_lines:
            lines[-1] = lines[-1][:-1] + "…"
            return "\n".join(lines)
        cur = ch
    if cur:
        lines.append(cur)
    return "\n".join(lines[:max_lines])


def _hint(text: str, size: int = 10) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
        f"{get_font_family_css()} {font_size_css(size)};"
    )
    return lbl


def _title_label(text: str, size: int = 12) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
        f"{get_font_family_css()} {font_size_css(size)}; font-weight: 600;"
    )
    return lbl


def _card_frame(parent=None) -> QFrame:
    """卡片容器。⚠ 样式用 #objectName 限定：QLabel 是 QFrame 子类，
    裸 QFrame 选择器会污染所有子标签（边框泄漏）。"""
    frame = QFrame(parent)
    frame.setObjectName("hubSectionCard")
    frame.setStyleSheet(
        f"""
        QFrame#hubSectionCard {{
            background: {Colors.CARD_BG.format(alpha=140)};
            border: 1px solid {Colors.BORDER};
            border-radius: 12px;
        }}
        QFrame#hubSectionCard QLabel {{ background: transparent; border: none; }}
    """
    )
    return frame


def _input_style() -> str:
    """中性输入框：不依赖主题 INPUT_*（亮色主题下不发蓝）。"""
    return f"""
        QLineEdit {{
            background: {Colors.CARD_BG.format(alpha=90)};
            border: 1px solid {Colors.BORDER};
            color: {Colors.TEXT_PRIMARY};
            padding: 5px 10px;
            border-radius: 6px;
            {get_font_family_css()} {font_size_css(12)}
        }}
        QLineEdit:focus {{ border-color: {Colors.TEXT_ACCENT}; background: {Colors.CARD_BG.format(alpha=160)}; }}
    """


def _btn_style(danger: bool = False) -> str:
    color = Colors.ERROR if danger else Colors.TEXT_PRIMARY
    return f"""
        QPushButton {{
            color: {color};
            border: 1px solid {Colors.ERROR if danger else Colors.BORDER};
            border-radius: 6px;
            padding: 4px 16px;
            background: transparent;
            {get_font_family_css()} {font_size_css(11)}
        }}
        QPushButton:hover {{
            background: {Colors.HOVER_BG};
        }}
        QPushButton:disabled {{ opacity: 0.4; }}
    """


class _Section(QFrame):
    """分区卡片：标题行（+右侧 context 槽）+ 内容 VBox。

    样式限定 objectName：防止 QFrame 规则波及子 QLabel/QFrame（边框泄漏）。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("hubSectionCard")
        self.setStyleSheet(
            f"""
            QFrame#hubSectionCard {{
                background: {Colors.CARD_BG.format(alpha=140)};
                border: 1px solid {Colors.BORDER};
                border-radius: 12px;
            }}
            QFrame#hubSectionCard QLabel {{ background: transparent; border: none; }}
        """
        )
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(16, 12, 16, 14)
        self._v.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(_title_label(title))
        head.addStretch()
        self._context_slot = head  # set_context 时追加控件
        self._v.addLayout(head)

    def set_context(self, w: QWidget) -> None:
        self._context_slot.addWidget(w)

    def body(self) -> QVBoxLayout:
        return self._v


# ── 名称 / 模型分区 ──────────────────────────────────────


class _CenterFlowLayout(QLayout):
    """流式布局：空间不足自动换行，每行水平居中（chips 卡行用）。"""

    def __init__(self, parent=None, spacing: int = 12):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:  # noqa: N802
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientations(Qt.NoOrientation)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, w, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        avail_w = rect.width() - m.left() - m.right()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line: list = []  # [(item, w, h)]
        line_w = 0
        line_h = 0

        def flush_line() -> None:
            nonlocal y
            if not line:
                return
            start_x = x + max(0, (avail_w - line_w) // 2)
            cx = start_x
            for it, iw, ih in line:
                if not test_only:
                    it.setGeometry(QRect(cx, y, iw, ih))
                cx += iw + self._spacing
            y += line_h + self._spacing

        for item in self._items:
            hint = item.sizeHint()
            iw, ih = hint.width(), hint.height()
            add_w = iw if not line else self._spacing + iw
            if line and line_w + add_w > avail_w:
                flush_line()
                line, line_w, line_h = [], 0, 0
                add_w = iw
            line.append((item, iw, ih))
            line_w += add_w
            line_h = max(line_h, ih)
        flush_line()
        return (y - self._spacing) - rect.y() + m.bottom()


# ── 名称 / 模型分区 ──────────────────────────────────────


class ProfileSection(_Section):
    """助手名称 + 记忆整理模型（对话模型跟随系统当前配置，不单独设置；改动即节流保存）。"""

    saveRequested = pyqtSignal(str, str)  # (name, utility_model_key)
    _DEBOUNCE_MS = 600
    _MAX_VISIBLE_ITEMS = 12  # 下拉弹层最大可见条目数，超出滚动

    def __init__(self, parent=None):
        super().__init__("基本信息", parent)
        self._model_keys: List[str] = [""]
        self._suspend_autosave = True  # bind() 期间不触发自动保存

        # 表单整体固定宽度、居中（不占满整行；两行 label 左缘对齐）
        FORM_W = 520
        form_holder = QWidget()
        form_holder.setFixedWidth(FORM_W)
        form = QVBoxLayout(form_holder)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        row1 = QHBoxLayout()
        lbl1 = _title_label("名称", 11)
        lbl1.setFixedWidth(104)  # 与下行 label 同宽 → 控件左缘对齐（须容纳"记忆整理模型"6 字）
        row1.addWidget(lbl1)
        self._name = QLineEdit()
        self._name.setStyleSheet(_input_style())
        self._name.textEdited.connect(self._schedule_autosave)
        row1.addWidget(self._name, 1)
        form.addLayout(row1)

        row2 = QHBoxLayout()
        lbl2 = _title_label("记忆整理模型", 11)
        lbl2.setFixedWidth(104)
        row2.addWidget(lbl2)
        self._utility_model = ComboBox()
        self._utility_model.setMaxVisibleItems(self._MAX_VISIBLE_ITEMS)
        self._utility_model.setStyleSheet(self._combo_style())
        self._utility_model.currentIndexChanged.connect(self._schedule_autosave)
        row2.addWidget(self._utility_model, 1)
        form.addLayout(row2)

        wrap = QHBoxLayout()
        wrap.addStretch()
        wrap.addWidget(form_holder)
        wrap.addStretch()
        self.body().addLayout(wrap)
        self.body().addWidget(
            _hint(
                "对话模型跟随系统当前配置，无需在此设置；记忆整理模型用于记忆编译 / Dream / 经验反思，跟随全局 = 使用当前对话模型。"
            )
        )
        self.body().addWidget(_hint("修改后自动保存。", 9))

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_save)
        self._reload_models()

    def _combo_style(self) -> str:
        """记忆整理模型下拉：透明背景融入卡片（选择器名对齐 bottom_input_area 先例）。"""
        return f"""
            ComboBox {{
                background: transparent;
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 10px;
                {get_font_family_css()} {font_size_css(12)};
            }}
            ComboBox:hover {{
                background: {Colors.HOVER_BG};
                border-color: {Colors.TEXT_ACCENT};
            }}
            ComboBox::drop-down {{ border: none; width: 16px; }}
            ComboBox::down-arrow {{
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {Colors.TEXT_PRIMARY};
                margin-right: 2px;
            }}
        """

    def _schedule_autosave(self, *_a) -> None:
        if self._suspend_autosave:
            return
        self._debounce.start()

    def bind(self, name: str, utility_model: str) -> None:
        self._suspend_autosave = True
        self._name.setText(name)
        keys = self._model_keys
        key = utility_model or ""
        idx = keys.index(key) if key in keys else 0
        self._utility_model.setCurrentIndex(idx)
        self._suspend_autosave = False

    def _reload_models(self) -> None:
        """数据源（对齐 cron-tasks）：main_widget._valid_configs 展开「配置 × 模型列表」。

        复合键 "<config_id>||<model>"——一个服务商可选它模型列表里的任意模型；
        首项「跟随全局」= 空（执行时用当前全局对话模型，选中模型失效自动回退）。
        用 qfluentwidgets.ComboBox：弹层样式/定位/滚动全部由其自管。
        """
        mw = None
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            reg = UIPluginRegistry.get_instance()
            mw = getattr(reg, "_main_widget", None) or next(
                iter(getattr(reg, "_window_main_widgets", {}).values()), None
            )
        except Exception:
            mw = None
        valid = getattr(mw, "_valid_configs", None) if mw is not None else None
        current = getattr(mw, "_current_provider_name", None) if mw is not None else None

        labels: List[str] = ["跟随全局"]
        keys: List[str] = [""]
        if isinstance(valid, dict):
            for key, cfg in valid.items():
                if not isinstance(cfg, dict):
                    continue
                display = str(cfg.get("display_name") or cfg.get("name") or cfg.get("provider_name") or key)
                cur_model = str(cfg.get("模型名称") or "")
                models = [str(m) for m in (cfg.get("模型列表") or []) if str(m).strip()]
                if not models:
                    models = [cur_model] if cur_model else []
                for model in models:
                    label = f"{display} · {model}"
                    if key == current and model == cur_model:
                        label += "（当前）"
                    labels.append(label)
                    keys.append(f"{key}||{model}")
        self._model_keys = keys
        combo = self._utility_model
        combo.clear()
        combo.addItems(labels)
        combo.setCurrentIndex(0)

    def _emit_save(self) -> None:
        keys = getattr(self, "_model_keys", [""])
        ui_ = self._utility_model.currentIndex()
        util_key = keys[ui_] if 0 <= ui_ < len(keys) else ""
        self.saveRequested.emit(self._name.text().strip(), util_key)


# ── 关于 Ta（人格切换）────────────────────


class _PersonaChip(QFrame):
    """人格选择卡：40px 圆头像 + 名 + 描述 + tag 方角牌（对齐原版 yuan-chip）。"""

    clicked = pyqtSignal()

    def __init__(self, persona: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("personaChip")  # ⚠ 样式选择器依赖，缺失则无边框
        self.persona_id = persona["id"]
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(118, 150)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 14, 10, 12)
        v.setSpacing(6)
        v.setAlignment(Qt.AlignHCenter)
        self._avatar = RoundAvatar(
            size=44,
            text=persona.get("name") or persona["id"],
            color="#7C3AED",
            image_path=persona.get("avatar_path") or None,
        )
        v.addWidget(self._avatar, 0, Qt.AlignHCenter)
        self._name = QLabel(persona.get("name") or persona["id"])
        self._name.setAlignment(Qt.AlignCenter)
        self._desc = QLabel()
        self._desc.setAlignment(Qt.AlignCenter)
        self._desc.setWordWrap(True)
        self._tag = QLabel(f"[{persona['tag']}]" if persona.get("tag") else "")
        self._tag.setAlignment(Qt.AlignCenter)
        v.addWidget(self._name)
        v.addWidget(self._desc)
        v.addWidget(self._tag)
        self._apply_style()
        # 描述限制 2 行：按像素宽度手动截断（超出加 …），并钉死高度，
        # 防止长描述挤压/遮挡下方 tag（chips 卡固定 118x150）
        fm = QFont(self._desc.font())
        fm.setPixelSize(10)  # 与 _apply_style 中 desc font-size 一致
        metrics = QFontMetrics(fm)
        self._desc.setText(_elide_lines(persona.get("description", ""), metrics, 96, 2))
        self._desc.setFixedHeight(metrics.lineSpacing() * 2)

    def set_avatar_image(self, image_path: str) -> None:
        """换人格头像后轻量刷新本 chip 头像。"""
        self._avatar.set_image(image_path or None)

    def _apply_style(self) -> None:
        border = Colors.TEXT_ACCENT if self._selected else Colors.BORDER
        bg = "rgba(245, 158, 11, 0.07)" if self._selected else "transparent"
        self.setStyleSheet(
            f"""
            QFrame#personaChip {{
                background: {bg};
                border: {"2px solid " + Colors.TEXT_ACCENT if self._selected else "1.5px solid " + border};
                border-radius: 12px;
            }}
            QFrame#personaChip:hover {{ border-color: {Colors.TEXT_ACCENT}; }}
            QFrame#personaChip QLabel {{ background: transparent; border: none; }}
        """
        )
        self._name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        self._desc.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};")
        self._tag.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT if self._selected else Colors.TEXT_MUTED};"
            f"{get_font_family_css()} {font_size_css(9)}; letter-spacing: 1px;"
        )

    def set_selected(self, on: bool) -> None:
        if self._selected != on:
            self._selected = on
            self._apply_style()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self.clicked.emit()


class AboutSection(_Section):
    """人格选择（chips，含「纯净助手」）。人格只读，只能切换；新增人格走 persona-creator 技能。"""

    personaChangeRequested = pyqtSignal(str)

    def __init__(self, personas: List[dict], current_pid: str, parent=None):
        super().__init__("关于 Ta", parent)
        self.body().addWidget(_hint("点卡片切换人格（内置预设，只读）；新增人格在对话里说「创建新人格」。", 10))

        chips_host = QWidget()
        chips_host.setStyleSheet("background: transparent;")
        chips_row = _CenterFlowLayout(chips_host)
        chips_row.setSpacing(12)
        self._chips_row = chips_row
        self._chips: List[_PersonaChip] = []
        self._personas = personas  # 调用方已包含 none（纯净助手）
        self._current_pid = current_pid
        self.body().addWidget(chips_host)
        self.rebuild_chips(personas)
        self.set_persona(current_pid)

    def set_persona(self, pid: str) -> None:
        """刷新 chips 选中态。"""
        self._current_pid = pid
        for chip in self._chips:
            chip.set_selected(chip.persona_id == pid)

    def rebuild_chips(self, personas: List[dict]) -> None:
        """全量重建人格 chips（人格数据/头像变更后刷新，保留当前选中态）。"""
        self._personas = personas
        while self._chips_row.count():
            item = self._chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._chips = []
        for p in personas:
            chip = _PersonaChip(p)
            chip.clicked.connect(lambda pid=p["id"]: self.personaChangeRequested.emit(pid))
            self._chips_row.addWidget(chip)
            self._chips.append(chip)
        self.set_persona(self._current_pid)


# ── 记忆分区 ────────────────────────────────────────────


class MemorySection(_Section):
    """记忆传送带 UI：开关 + 状态 + 置顶 + 当下 + Dream + 所有记忆。"""

    toggleMemory = pyqtSignal(bool)
    toggleDreamAuto = pyqtSignal(bool)
    viewToday = pyqtSignal()
    dreamRun = pyqtSignal()
    dreamRestore = pyqtSignal()
    viewAll = pyqtSignal()
    clearAll = pyqtSignal()
    pinAddRequested = pyqtSignal(str)
    pinEdited = pyqtSignal(str, str)  # (pin_id, 新内容)
    pinDeleteRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("记忆", parent)
        self._memory_switch = SwitchButton()
        self._memory_switch.setOnText("开")
        self._memory_switch.setOffText("关")
        self._memory_switch.checkedChanged.connect(self.toggleMemory.emit)
        self.set_context(self._memory_switch)

        # 状态条
        self._status = _hint("记忆状态：暂未编译", 10)
        self.body().addWidget(self._status)

        # 置顶记忆
        self.body().addWidget(_title_label("置顶记忆", 11))
        self.body().addWidget(_hint("你主动告诉助手一定要记住的东西，一条一条管理，可增删改。"))
        self._pin_list = QVBoxLayout()
        self._pin_list.setSpacing(3)
        self.body().addLayout(self._pin_list)
        add_row = QHBoxLayout()
        self._pin_input = QLineEdit()
        self._pin_input.setPlaceholderText("添加一条置顶记忆...")
        self._pin_input.setStyleSheet(_input_style())
        self._pin_input.returnPressed.connect(self._emit_add_pin)
        add_row.addWidget(self._pin_input, 1)
        add_btn = QPushButton("+")
        add_btn.setFixedSize(30, 28)
        add_btn.setStyleSheet(_btn_style())
        add_btn.clicked.connect(self._emit_add_pin)
        add_row.addWidget(add_btn)
        self.body().addLayout(add_row)

        # 当下记忆
        self.body().addWidget(_title_label("当下记忆", 11))
        self.body().addWidget(_hint("助手记住的关于你的、重要的与近期的事。"))
        view_today = QPushButton("查看当下记忆")
        view_today.setStyleSheet(_btn_style())
        view_today.clicked.connect(self.viewToday.emit)
        self.body().addWidget(view_today)

        # Dream
        self.body().addWidget(_title_label("Dream 整理", 11))
        self.body().addWidget(_hint("把重要事实与长期情况整理成一行一条，去重合并并清理过时内容。"))
        dream_row = QHBoxLayout()
        dream_row.addWidget(_title_label("每日自动 Dream", 11))
        dream_row.addStretch()
        self._dream_switch = SwitchButton()
        self._dream_switch.setOnText("开")
        self._dream_switch.setOffText("关")
        self._dream_switch.setChecked(False)
        self._dream_switch.checkedChanged.connect(self.toggleDreamAuto.emit)
        dream_row.addWidget(self._dream_switch)
        self.body().addLayout(dream_row)
        self.body().addWidget(_hint("仅对当前助手生效；默认关闭，每个逻辑日最多运行一次。"))
        btn_row = QHBoxLayout()
        self._dream_btn = QPushButton("整理当下记忆")
        self._dream_btn.setStyleSheet(_btn_style())
        self._dream_btn.clicked.connect(self.dreamRun.emit)
        btn_row.addWidget(self._dream_btn)
        restore_btn = QPushButton("恢复版本")
        restore_btn.setStyleSheet(_btn_style())
        restore_btn.clicked.connect(self.dreamRestore.emit)
        btn_row.addWidget(restore_btn)
        self.body().addLayout(btn_row)
        self._dream_hint = _hint("", 10)
        self.body().addWidget(self._dream_hint)

        # 所有记忆
        self.body().addWidget(_title_label("所有记忆", 11))
        all_row = QHBoxLayout()
        view_all = QPushButton("查看记忆")
        view_all.setStyleSheet(_btn_style())
        view_all.clicked.connect(self.viewAll.emit)
        all_row.addWidget(view_all)
        clear_btn = QPushButton("清除记忆")
        clear_btn.setStyleSheet(_btn_style(danger=True))
        clear_btn.clicked.connect(self.clearAll.emit)
        all_row.addWidget(clear_btn)
        self.body().addLayout(all_row)

    def _emit_add_pin(self) -> None:
        text = self._pin_input.text().strip()
        if not text:
            return
        self._pin_input.clear()
        self.pinAddRequested.emit(text)

    def reload_pins(self, items: List[Tuple[str, str]]) -> None:
        """重建置顶列表：一条一行，行内绑定 pin_id（编辑/删除按 id 上报）。

        UI 永远以盘上数据重绘（宿主写盘后回调本方法），增删改即时可见。
        """
        while self._pin_list.count():
            item = self._pin_list.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for pid, content in items:
            row = QFrame()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(content)
            edit.setStyleSheet(_input_style())
            edit.editingFinished.connect(lambda pid=pid, e=edit: self.pinEdited.emit(pid, e.text().strip()))
            h.addWidget(edit, 1)
            del_btn = QPushButton("×")
            del_btn.setFixedSize(26, 26)
            del_btn.setToolTip("删除这条记忆")
            del_btn.setStyleSheet(_btn_style(danger=True))
            del_btn.clicked.connect(lambda _checked=False, pid=pid: self.pinDeleteRequested.emit(pid))
            h.addWidget(del_btn)
            self._pin_list.addWidget(row)

    def set_memory_enabled(self, on: bool) -> None:
        self._memory_switch.setChecked(on)
        self.setEnabled_all(on)

    def setEnabled_all(self, on: bool) -> None:  # noqa: N802
        """记忆开关关闭时灰置记忆内容（开关本身保持可用）。"""
        for i in range(1, self.body().count()):
            item = self.body().itemAt(i)
            w = item.widget()
            if w is not None:
                w.setVisible(on)
            elif item.layout() is not None:
                for j in range(item.layout().count()):
                    sub = item.layout().itemAt(j)
                    if sub.widget() is not None:
                        sub.widget().setVisible(on)

    def set_dream_auto(self, on: bool) -> None:
        self._dream_switch.setChecked(on)

    def set_status(self, text: str) -> None:
        self._status.setText(f"记忆状态：{text}")

    def set_dream_hint(self, text: str) -> None:
        self._dream_hint.setText(text)

    def set_dream_running(self, running: bool) -> None:
        self._dream_btn.setEnabled(not running)
        self._dream_btn.setText("整理中…" if running else "整理当下记忆")


# ── 经验分区 ────────────────────────────────────────────


class ExperienceSection(_Section):
    """经验：开关 + 分类列表 + 反思按钮。"""

    toggleExperience = pyqtSignal(bool)
    viewCategory = pyqtSignal(str)
    reflectRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("经验", parent)
        self._exp_switch = SwitchButton()
        self._exp_switch.setOnText("开")
        self._exp_switch.setOffText("关")
        self._exp_switch.checkedChanged.connect(self.toggleExperience.emit)
        self.set_context(self._exp_switch)

        self._body_wrap = QWidget()
        self._wrap_v = QVBoxLayout(self._body_wrap)
        self._wrap_v.setContentsMargins(0, 0, 0, 0)
        self._wrap_v.setSpacing(6)
        self._hint = _hint("默认关闭。开启后助手可自主回忆/记录工作经验；每日 Dream 后会自动反思整理。")
        self._wrap_v.addWidget(self._hint)
        self._list_area = QVBoxLayout()
        self._wrap_v.addLayout(self._list_area)
        reflect_row = QHBoxLayout()
        reflect_btn = QPushButton("反思整理")
        reflect_btn.setStyleSheet(_btn_style())
        reflect_btn.clicked.connect(self.reflectRequested.emit)
        reflect_row.addWidget(reflect_btn)
        reflect_row.addStretch()
        self._wrap_v.addLayout(reflect_row)
        self.body().addWidget(self._body_wrap)

    def set_enabled(self, on: bool) -> None:
        self._exp_switch.setChecked(on)
        self._body_wrap.setVisible(True)
        self._hint.setText(
            "已启用：助手可在对话中自主回忆/记录经验（recall/record_experience 工具）。"
            if on
            else "已暂停。已有内容会保留，但助手不能读取或记录经验。"
        )

    def reload_categories(self, docs: List[Dict[str, Any]]) -> None:
        while self._list_area.count():
            item = self._list_area.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not docs:
            empty = _hint("（暂无经验分类）")
            self._list_area.addWidget(empty)
            return
        for doc in docs:
            btn = QPushButton(f"{doc['category']}（{doc['count']} 条）")
            btn.setStyleSheet(_btn_style())
            btn.clicked.connect(lambda _c=False, cat=doc["category"]: self.viewCategory.emit(cat))
            self._list_area.addWidget(btn)


# ── 技能分区 ────────────────────────────────────────────


class SkillsSection(_Section):
    """专属技能：列表 + 编辑（复用原 SkillsTab 逻辑的最小化版本）。"""

    skillsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("专属技能", parent)
        self.body().addWidget(_hint("技能是助手的专属知识文件（skills/*.md），可在对话中引用。"))
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        container = QWidget()
        self._list_v = QVBoxLayout(container)
        self._list_v.setContentsMargins(0, 0, 0, 0)
        self._list_v.setSpacing(3)
        self._scroll.setWidget(container)
        self.body().addWidget(self._scroll)

    def reload_skills(self, skills: List[Dict[str, Any]], on_open: Callable[[str], None]) -> None:
        while self._list_v.count():
            item = self._list_v.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not skills:
            self._list_v.addWidget(_hint("（暂无技能）"))
            return
        for sk in skills:
            btn = QPushButton(f"{sk['name']} · {sk.get('description', '')[:30]}（{sk.get('content_chars', 0)} 字）")
            btn.setStyleSheet(_btn_style())
            btn.clicked.connect(lambda _c=False, n=sk["name"]: on_open(n))
            self._list_v.addWidget(btn)
