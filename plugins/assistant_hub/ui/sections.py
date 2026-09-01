# -*- coding: utf-8 -*-
"""sections.py — 助手中心单列分区组件（对齐 openhanako AgentTab 分区结构）

分区：ProfileSection（名称/模型）→ AboutSection（人格/身份/AGENTS.md）→
MemorySection（记忆传送带）→ ExperienceSection（经验）→ SkillsSection（技能）。

视觉基调（对齐原版纸张风）：细边框卡 + 12px 圆角 + 小字 hint + 大量留白；
去 emoji，统一 FluentIcon / 文字标签。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import SwitchButton

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from assistant_hub_manager import AssistantManager


# ── 基础样式辅助 ─────────────────────────────────────────


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


def _editor_style() -> str:
    return f"""
        QTextEdit {{
            background: {Colors.CARD_BG.format(alpha=110)};
            border: 1px solid {Colors.BORDER};
            color: {Colors.TEXT_PRIMARY};
            border-radius: 8px;
            padding: 6px;
            {get_font_family_css()} {font_size_css(12)}
        }}
        QTextEdit:focus {{ border-color: {Colors.TEXT_ACCENT}; }}
    """


def _input_style() -> str:
    return f"""
        QLineEdit {{
            background: {Colors.INPUT_BG_START};
            border: 1px solid {Colors.INPUT_BORDER};
            color: {Colors.INPUT_TEXT};
            padding: 5px 10px;
            border-radius: 6px;
            {get_font_family_css()} {font_size_css(12)}
        }}
        QLineEdit:focus {{ border-color: {Colors.TEXT_ACCENT}; }}
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


class NoWheelComboBox(QComboBox):
    """禁滚轮误切换的下拉框；限制弹层最大可见行数。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaxVisibleItems(12)

    def wheelEvent(self, e: "QWheelEvent") -> None:  # noqa: N802
        e.ignore()


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


class ProfileSection(_Section):
    """助手名称 + 聊天模型 + 记忆整理模型。"""

    saveRequested = pyqtSignal(str, str, str)  # (name, chat_model_config_id, utility_model_config_id)

    def __init__(self, parent=None):
        super().__init__("基本信息", parent)
        row1 = QHBoxLayout()
        row1.addWidget(_title_label("名称", 11))
        self._name = QLineEdit()
        self._name.setStyleSheet(_input_style())
        row1.addWidget(self._name, 1)
        self.body().addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(_title_label("对话模型", 11))
        self._chat_model = NoWheelComboBox()
        self._chat_model.setStyleSheet(self._combo_style())
        row2.addWidget(self._chat_model, 1)
        self.body().addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(_title_label("记忆整理模型", 11))
        self._utility_model = NoWheelComboBox()
        self._utility_model.setStyleSheet(self._combo_style())
        row3.addWidget(self._utility_model, 1)
        self.body().addLayout(row3)
        self.body().addWidget(_hint("记忆整理模型用于记忆编译 / Dream / 经验反思；跟随全局 = 使用当前对话模型。"))

        row4 = QHBoxLayout()
        row4.addStretch()
        self._save = QPushButton("保存")
        self._save.setStyleSheet(_btn_style())
        self._save.clicked.connect(self._emit_save)
        row4.addWidget(self._save)
        self.body().addLayout(row4)
        self._reload_models()

    @staticmethod
    def _combo_style() -> str:
        return f"""
            QComboBox {{
                background: {Colors.INPUT_BG_START}; border: 1px solid {Colors.INPUT_BORDER};
                color: {Colors.INPUT_TEXT}; border-radius: 6px; padding: 4px 8px;
                {get_font_family_css()} {font_size_css(11)}
            }}
            QComboBox QAbstractItemView {{
                background: {Colors.CARD_BG_SOLID}; color: {Colors.INPUT_TEXT};
                selection-background-color: {Colors.SELECTED_BG};
            }}
        """

    def _reload_models(self) -> None:
        """数据源（对齐 cron-tasks）：main_widget._valid_configs 展开「配置 × 模型列表」。

        复合键 "<config_id>||<model>"——一个服务商可选它模型列表里的任意模型；
        首项「跟随全局」= 空（执行时用当前全局对话模型，选中模型失效自动回退）。
        双源兜底：UIPluginRegistry._main_widget → 任一窗口 main_widget。
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

        for combo in (self._chat_model, self._utility_model):
            combo.clear()
            combo.addItem("跟随全局", "")
            if not isinstance(valid, dict):
                continue
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
                    combo.addItem(label, f"{key}||{model}")

    def bind(self, name: str, chat_model: str, utility_model: str) -> None:
        self._name.setText(name)
        for combo, val in ((self._chat_model, chat_model), (self._utility_model, utility_model)):
            idx = combo.findData(val or "")
            combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _emit_save(self) -> None:
        self.saveRequested.emit(
            self._name.text().strip(),
            self._chat_model.currentData() or "",
            self._utility_model.currentData() or "",
        )


# ── 关于 Ta（人格 / 身份 / AGENTS.md）────────────────────


class AboutSection(_Section):
    """人格选择（chips + 无横幅）+ 身份简介 + AGENTS.md。"""

    personaChangeRequested = pyqtSignal(str)
    personaManageRequested = pyqtSignal()
    saveRequested = pyqtSignal(str, str)  # (identity, agents_md)

    def __init__(self, personas: List[dict], current_pid: str, parent=None):
        super().__init__("关于 Ta", parent)
        self.body().addWidget(_hint("人格是助手的潜意识，以此为基础搭建你独一无二的伙伴。", 10))
        chips_row = QHBoxLayout()
        chips_row.setSpacing(10)
        chips_row.addStretch()
        self._chip_buttons: List[QPushButton] = []
        self._personas = [p for p in personas if p["id"] != "none"]
        for p in self._personas:
            btn = self._make_chip(p)
            chips_row.addWidget(btn)
        chips_row.addStretch()
        self.body().addLayout(chips_row)

        manage_row = QHBoxLayout()
        manage_row.addStretch()
        manage_btn = QPushButton("管理人格…")
        manage_btn.setStyleSheet(_btn_style())
        manage_btn.clicked.connect(self.personaManageRequested.emit)
        manage_row.addWidget(manage_btn)
        manage_row.addStretch()
        self.body().addLayout(manage_row)

        # 「无」横幅
        self._none_banner = QPushButton()
        self._none_banner.setFixedHeight(46)
        self._none_banner.setCursor(Qt.PointingHandCursor)
        self._none_banner.setStyleSheet(self._banner_style(False))
        self._none_banner.setText("无　· 不附加人格底座，纯净助手")
        self._none_banner.clicked.connect(lambda: self.personaChangeRequested.emit("none"))
        banner_wrap = QHBoxLayout()
        banner_wrap.addStretch()
        banner_wrap.addWidget(self._none_banner, 0)
        banner_wrap.addStretch()
        self.body().addLayout(banner_wrap)

        # 身份
        self.body().addWidget(_title_label("身份简介", 11))
        self._identity = QTextEdit()
        self._identity.setFixedHeight(72)
        self._identity.setStyleSheet(_editor_style())
        self._identity.setPlaceholderText(
            "# {{agentName}}\n\n{{userName}}的个人助手。感性与理性兼备，既有温度也有判断力。"
        )
        self.body().addWidget(self._identity)
        self.body().addWidget(
            _hint("简短描述助手是谁、擅长什么。其他助手通过这段文字认识 Ta；支持 {{userName}} / {{agentName}} 变量。")
        )
        # AGENTS.md
        self.body().addWidget(_title_label("AGENTS.md", 11))
        self._agents_md = QTextEdit()
        self._agents_md.setFixedHeight(180)
        self._agents_md.setStyleSheet(_editor_style())
        self._agents_md.setPlaceholderText("# 人格定义\n\n- 你是一个有温度的存在…")
        self.body().addWidget(self._agents_md)
        self.body().addWidget(_hint("行为准则 / 工作流 / 偏好。激活该助手时会整体替换当前智能体提示词。"))
        row = QHBoxLayout()
        row.addStretch()
        save = QPushButton("保存")
        save.setStyleSheet(_btn_style())
        save.clicked.connect(
            lambda: self.saveRequested.emit(self._identity.toPlainText(), self._agents_md.toPlainText())
        )
        row.addWidget(save)
        row.addStretch()
        self.body().addLayout(row)
        self.set_persona(current_pid)

    @staticmethod
    def _banner_style(selected: bool) -> str:
        accent = Colors.TEXT_ACCENT
        border = ("2px solid " + accent) if selected else "1px solid rgba(255,255,255,0.25)"
        return f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(20, 22, 30, 235), stop:1 rgba(38, 34, 28, 235));
                border: {border};
                border-radius: 10px;
                color: #fff;
                text-align: left;
                padding-left: 16px;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QPushButton:hover {{ border-color: rgba(255,255,255,0.5); }}
        """

    def _make_chip(self, p: dict) -> QPushButton:
        """人格 chip 卡：头像 + 名 + 描述 + tag 小牌（纵向）。"""
        btn = QPushButton()
        btn.setFixedSize(104, 116)
        btn.setCursor(Qt.PointingHandCursor)
        tag_line = f"\n[{p['tag']}]" if p.get("tag") else ""
        btn.setText(f"{p['name']}\n{p.get('description', '')}{tag_line}")
        btn.setStyleSheet(self._chip_style(False))
        btn.clicked.connect(lambda: self.personaChangeRequested.emit(p["id"]))
        btn.setToolTip(p.get("prompt_preview", ""))
        self._chip_buttons.append(btn)
        btn._persona_id = p["id"]  # type: ignore[attr-defined]
        return btn

    @staticmethod
    def _chip_style(selected: bool) -> str:
        border = Colors.TEXT_ACCENT if selected else Colors.BORDER
        bg = "rgba(245, 158, 11, 0.08)" if selected else "transparent"
        color = Colors.TEXT_PRIMARY if selected else Colors.TEXT_MUTED
        border_line = f"2px solid {border}" if selected else f"1px solid {border}"
        return f"""
            QPushButton {{
                background: {bg};
                border: {border_line};
                border-radius: 10px;
                color: {color};
                {get_font_family_css()} {font_size_css(11)}
            }}
            QPushButton:hover {{ border-color: {Colors.TEXT_ACCENT}; color: {Colors.TEXT_PRIMARY}; }}
        """

    def set_persona(self, pid: str) -> None:
        """刷新 chips / 横幅选中态。"""
        for btn in self._chip_buttons:
            sel = getattr(btn, "_persona_id", "") == pid
            btn.setStyleSheet(self._chip_style(sel))
        self._none_banner.setStyleSheet(self._banner_style(pid == "none"))

    def bind_texts(self, identity: str, agents_md: str) -> None:
        self._identity.setPlainText(identity)
        self._agents_md.setPlainText(agents_md)


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
    pinsChanged = pyqtSignal(list)

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
        self.body().addWidget(_hint("你主动告诉助手一定要记住的东西，也可以手动编辑与添加。"))
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
        pins = self.pins()
        pins.append(text)
        self.pinsChanged.emit(pins)

    def pins(self) -> List[str]:
        out = []
        for i in range(self._pin_list.count()):
            row = self._pin_list.itemAt(i)
            w = row.widget() if row else None
            edit = w.findChild(QLineEdit) if w else None
            if edit is not None and edit.text().strip():
                out.append(edit.text().strip())
        return out

    def reload_pins(self, pins: List[str]) -> None:
        """重建置顶列表（pin 行：输入框 + 删除按钮）。"""
        while self._pin_list.count():
            item = self._pin_list.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for text in pins:
            row = QFrame()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(text)
            edit.setStyleSheet(_input_style())
            edit.editingFinished.connect(lambda e=edit: self.pinsChanged.emit(self.pins()))
            h.addWidget(edit, 1)
            del_btn = QPushButton("×")
            del_btn.setFixedSize(26, 26)
            del_btn.setStyleSheet(_btn_style(danger=True))

            def _remove(_checked=False, w=row, e=edit):
                if self._pin_list.indexOf(w) >= 0:
                    pins = self.pins()
                    pins.remove(e.text().strip()) if e.text().strip() in pins else None
                    self.pinsChanged.emit(pins)

            del_btn.clicked.connect(_remove)
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
