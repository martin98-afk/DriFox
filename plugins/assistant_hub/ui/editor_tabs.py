# -*- coding: utf-8 -*-
"""editor_tabs.py — 助手编辑 Tab（身份/提示词/对外人格/头像/记忆/技能）

参考 OpenHanako server/routes/agents.ts 的配置面：
- IdentityTab    → identity.md（身份简介，缺失回落模板）
- PromptTab      → AGENTS.md（智能体提示词/行为准则）
- PublicTab      → AGENTS.public.md（对外人格）
- AvatarTab      → avatars/agent.{png,jpg,...} + 预置剪影
- MemoryTab      → pinned.md + memory/today.md + memory/longterm.md + Dream
- SkillsTab      → skills/*.md（专属技能）
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    SwitchButton,
    TextEdit,
    TransparentToolButton,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from assistant_hub_manager import AssistantManager
from .avatar_picker import AvatarPicker

# ────────────────────────────────────────────────────────────────
# 通用样式
# ────────────────────────────────────────────────────────────────


def _label_style(size: int = 11) -> str:
    return f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(size)};"


def _card_frame(parent=None) -> QFrame:
    frame = QFrame(parent)
    frame.setStyleSheet(
        f"""
        QFrame {{
            background: {Colors.CARD_BG.format(alpha=180)};
            border: 1px solid {Colors.BORDER};
            border-radius: 8px;
        }}
    """
    )
    return frame


def _editor_style() -> str:
    return f"""
        TextEdit, QPlainTextEdit {{
            background: {Colors.CARD_BG.format(alpha=120)};
            border: 1px solid {Colors.BORDER};
            color: {Colors.TEXT_PRIMARY};
            border-radius: 6px;
            padding: 6px;
            {get_font_family_css()} {font_size_css(13)}
        }}
        TextEdit:focus, QPlainTextEdit:focus {{ border-color: {Colors.INFO}; }}
    """


def _line_edit_style() -> str:
    return f"""
        LineEdit, QLineEdit {{
            background: {Colors.INPUT_BG_START};
            border: 1px solid {Colors.INPUT_BORDER};
            color: {Colors.INPUT_TEXT};
            padding: 4px 10px;
            border-radius: 6px;
            {get_font_family_css()} {font_size_css(13)}
        }}
        LineEdit:focus, QLineEdit:focus {{ border-color: {Colors.INFO}; }}
    """


# ────────────────────────────────────────────────────────────────
# Markdown 编辑器（节流自动保存）
# ────────────────────────────────────────────────────────────────


class _MarkdownEditor(QWidget):
    """带保存节流的 Markdown 编辑器

    - 内容变化 → 800ms 防抖 → 调用 save_callback
    - bind() 时 blockSignals 防止初始化触发保存
    """

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self._save_callback = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self._edit = TextEdit(self)
        self._edit.setPlaceholderText(placeholder)
        self._edit.setStyleSheet(_editor_style())
        v.addWidget(self._edit, 1)

        status = QHBoxLayout()
        status.setContentsMargins(2, 0, 2, 0)
        self._chars_label = QLabel("0 字", self)
        self._chars_label.setStyleSheet(_label_style(10))
        status.addWidget(self._chars_label)
        status.addStretch()
        self._save_label = QLabel("", self)
        self._save_label.setStyleSheet(_label_style(10))
        status.addWidget(self._save_label)
        v.addLayout(status)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._do_save)
        self._edit.textChanged.connect(self._on_changed)

    def bind(self, content: str, save_callback) -> None:
        self._save_callback = save_callback
        self._edit.blockSignals(True)
        self._edit.setPlainText(content)
        self._edit.blockSignals(False)
        self._update_chars()

    def _on_changed(self) -> None:
        self._update_chars()
        self._save_label.setText("未保存…")
        self._timer.start()

    def _update_chars(self) -> None:
        n = len(self._edit.toPlainText())
        self._chars_label.setText(f"{n:,} 字")

    def _do_save(self) -> None:
        if self._save_callback is None:
            return
        try:
            self._save_callback(self._edit.toPlainText())
            self._save_label.setText("已保存 ✓")
        except Exception:
            self._save_label.setText("保存失败 ✗")

    def text(self) -> str:
        return self._edit.toPlainText()


# ────────────────────────────────────────────────────────────────
# 身份 / 提示词 / 对外人格
# ────────────────────────────────────────────────────────────────


class IdentityTab(QWidget):
    """身份简介（identity.md）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        hint = QLabel(
            "身份简介注入到系统提示词顶部：写你的人设、性格、目标。"
            "留空则回落内置模板。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_label_style(11))
        v.addWidget(hint)
        self._editor = _MarkdownEditor(placeholder="# 名称\n\n简介…", parent=self)
        v.addWidget(self._editor, 1)

    def bind(self, mgr: AssistantManager, aid: str) -> None:
        content, _ = mgr.read_identity_source(aid)
        self._editor.bind(
            content,
            lambda c, _aid=aid: (mgr.write_identity(_aid, c), mgr.invalidate_context(_aid)),
        )


class PromptTab(QWidget):
    """智能体提示词（AGENTS.md）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        hint = QLabel(
            "行为准则 / 工作流 / 偏好。激活该助手时会**整体替换**当前智能体提示词。"
            "留空则回落内置模板。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_label_style(11))
        v.addWidget(hint)
        self._editor = _MarkdownEditor(placeholder="# 行为准则\n\n- …", parent=self)
        v.addWidget(self._editor, 1)

    def bind(self, mgr: AssistantManager, aid: str) -> None:
        content, _ = mgr.read_agents_md_source(aid)
        self._editor.bind(
            content,
            lambda c, _aid=aid: (mgr.write_agents_md(_aid, c), mgr.invalidate_context(_aid)),
        )


class PublicTab(QWidget):
    """对外人格（AGENTS.public.md）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        hint = QLabel(
            "对外简介：其他 Agent / 插件看到的描述，不暴露内部记忆与细节。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_label_style(11))
        v.addWidget(hint)
        self._editor = _MarkdownEditor(placeholder="# 对外简介\n\n…", parent=self)
        v.addWidget(self._editor, 1)

    def bind(self, mgr: AssistantManager, aid: str) -> None:
        self._editor.bind(
            mgr.read_public_md(aid),
            lambda c, _aid=aid: mgr.write_public_md(_aid, c),
        )


# ────────────────────────────────────────────────────────────────
# 头像
# ────────────────────────────────────────────────────────────────


class AvatarTab(QWidget):
    """头像（预置剪影 / 纯色 / 本地文件）

    - 选中预置头像：拷贝到助手 avatars/ 目录并更新 assistant.yaml
    - 选中纯色：更新 color 字段（无图头像回落色块）
    - 本地上传：写入助手目录
    每次变更都会发 ``changed`` 信号，由 AssistantCardWidget 刷新顶部头像。
    """

    changed = pyqtSignal(str)  # assistant_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._aid: str = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        hint = QLabel(
            "选择或上传头像。把更多头像文件放到插件 icons/avatars/ 目录即可自动出现在预置库。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_label_style(11))
        v.addWidget(hint)
        self._picker = AvatarPicker(parent=self)
        self._picker.avatarSelected.connect(self._on_avatar_selected)
        v.addWidget(self._picker, 1)

    def bind(self, mgr: AssistantManager, aid: str) -> None:
        self._aid = aid
        a = mgr.get(aid)
        if not a:
            return
        ap = mgr.avatar_path(aid)
        self._picker.set_assistant(
            aid=aid,
            color=a.color,
            name=a.name,
            image_path=str(ap) if ap else "",
        )

    def _on_avatar_selected(self, payload: dict) -> None:
        if not self._aid:
            return
        mgr = AssistantManager.get_instance()
        a = mgr.get(self._aid)
        if not a:
            return

        kind = payload.get("kind", "")
        try:
            if kind == "predefined":
                # 拷贝预置头像到助手目录
                src = Path(payload.get("image_path") or "")
                if src.exists() and src.is_file():
                    ext = src.suffix.lstrip(".").lower()
                    if ext == "jpeg":
                        ext = "jpg"
                    data = src.read_bytes()
                    saved = mgr.save_avatar_from_bytes(self._aid, data, ext)
                    if saved:
                        a.avatar_path = str(saved)
                        a.color = payload.get("color") or a.color
                        mgr.update(a)
            elif kind == "color":
                a.color = payload.get("color") or a.color
                a.avatar_path = ""
                mgr.update(a)
            elif kind == "uploaded":
                a.color = payload.get("color") or a.color
                mgr.update(a)

            mgr.invalidate_context(self._aid)
            # 通知卡片刷新顶部头像 + 左侧列表
            self.changed.emit(self._aid)
        except Exception as e:
            from loguru import logger

            logger.warning(f"[assistant_hub] 保存头像失败: {e}")


# ────────────────────────────────────────────────────────────────
# 记忆（置顶 + 当下 + 长期 + Dream）
# ────────────────────────────────────────────────────────────────


class _PinnedRow(QWidget):
    """置顶记忆单行"""

    deleteRequested = pyqtSignal(int)

    def __init__(self, idx: int, content: str, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 1, 4, 1)
        h.setSpacing(6)
        self._idx = idx
        self._edit = LineEdit(self)
        self._edit.setText(content)
        self._edit.setStyleSheet(_line_edit_style())
        self._edit.setPlaceholderText("置顶内容（例如：我的生日是 X 月 X 日）")
        h.addWidget(self._edit, 1)
        del_btn = TransparentToolButton(FluentIcon.DELETE, self)
        del_btn.setFixedSize(24, 24)
        del_btn.clicked.connect(lambda: self.deleteRequested.emit(self._idx))
        h.addWidget(del_btn)

    def content(self) -> str:
        return self._edit.text().strip()

    def set_row_index(self, idx: int) -> None:
        self._idx = idx


class MemoryTab(QWidget):
    """记忆：置顶（pinned.md）+ 当下（today.md）+ 长期（longterm.md）+ Dream"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._aid: str = ""
        self._pinned_rows: List[_PinnedRow] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # ── 置顶记忆 ──
        pinned_box = _card_frame(self)
        pv = QVBoxLayout(pinned_box)
        pv.setContentsMargins(8, 6, 8, 6)
        pv.setSpacing(4)
        phead = QHBoxLayout()
        ptitle = QLabel("📌 置顶记忆", pinned_box)
        ptitle.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        phead.addWidget(ptitle)
        phead.addStretch()
        self._pin_count = QLabel("0 条", pinned_box)
        self._pin_count.setStyleSheet(_label_style(11))
        phead.addWidget(self._pin_count)
        add_btn = QPushButton(FluentIcon.ADD.icon(), "新增", pinned_box)
        add_btn.setFixedHeight(26)
        add_btn.clicked.connect(self._add_pin)
        phead.addWidget(add_btn)
        pv.addLayout(phead)
        phint = QLabel("置顶记忆始终注入 system prompt，永不衰减或被 Dream 覆盖。", pinned_box)
        phint.setStyleSheet(_label_style(10))
        pv.addWidget(phint)
        self._pin_scroll = QScrollArea(pinned_box)
        self._pin_scroll.setWidgetResizable(True)
        self._pin_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._pin_container = QWidget()
        self._pin_layout = QVBoxLayout(self._pin_container)
        self._pin_layout.setContentsMargins(0, 0, 0, 0)
        self._pin_layout.setSpacing(2)
        self._pin_layout.addStretch()
        self._pin_scroll.setWidget(self._pin_container)
        pv.addWidget(self._pin_scroll, 1)
        v.addWidget(pinned_box, 2)

        # ── 当下记忆 ──
        today_box = _card_frame(self)
        tv = QVBoxLayout(today_box)
        tv.setContentsMargins(8, 6, 8, 6)
        tv.setSpacing(4)
        thead = QHBoxLayout()
        ttitle = QLabel("🕐 当下记忆", today_box)
        ttitle.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        thead.addWidget(ttitle)
        thead.addStretch()
        tv.addLayout(thead)
        thint = QLabel("最近会话的临时记忆；Dream 后合并到长期。", today_box)
        thint.setStyleSheet(_label_style(10))
        tv.addWidget(thint)
        self._today_edit = TextEdit(today_box)
        self._today_edit.setStyleSheet(_editor_style())
        self._today_edit.setPlaceholderText("- 今天聊了…\n- 用户偏好…")
        tv.addWidget(self._today_edit, 1)
        v.addWidget(today_box, 2)

        # ── 长期记忆 ──
        lt_box = _card_frame(self)
        lv = QVBoxLayout(lt_box)
        lv.setContentsMargins(8, 6, 8, 6)
        lv.setSpacing(4)
        lhead = QHBoxLayout()
        ltitle = QLabel("📚 长期记忆", lt_box)
        ltitle.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        lhead.addWidget(ltitle)
        lhead.addStretch()
        self._dream_btn = QPushButton("🌙 Dream 一键整理", lt_box)
        self._dream_btn.setFixedHeight(26)
        self._dream_btn.clicked.connect(self._run_dream)
        lhead.addWidget(self._dream_btn)
        self._dream_auto = SwitchButton(lt_box)
        self._dream_auto.setText("自动")
        self._dream_auto.setOnText("开")
        self._dream_auto.setOffText("关")
        self._dream_auto.setToolTip("记忆变更后自动触发 Dream 整理")
        self._dream_auto.checkedChanged.connect(self._on_dream_auto)
        lhead.addWidget(self._dream_auto)
        lv.addLayout(lhead)
        lhint = QLabel("由 Dream 自动整理（gather→atomize→dedupe→optimize→compose→verify）。", lt_box)
        lhint.setStyleSheet(_label_style(10))
        lv.addWidget(lhint)
        self._lt_edit = TextEdit(lt_box)
        self._lt_edit.setStyleSheet(_editor_style())
        self._lt_edit.setPlaceholderText("# 长期记忆\n\n- 事实/偏好/约束/关系…")
        lv.addWidget(self._lt_edit, 1)
        v.addWidget(lt_box, 3)

        # Dream 历史
        self._dream_history = QLabel("（暂无 Dream 记录）", self)
        self._dream_history.setWordWrap(True)
        self._dream_history.setStyleSheet(_label_style(10))
        v.addWidget(self._dream_history, 0)

        # 节流保存（当下/长期共用）
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._do_save_memory)

    def bind(self, aid: str) -> None:
        self._aid = aid
        mgr = AssistantManager.get_instance()
        a = mgr.get(aid)
        # 置顶
        self._reload_pins(mgr.read_pinned(aid))
        # 当下 / 长期（blockSignals 避免初始化触发保存）
        self._today_edit.blockSignals(True)
        self._today_edit.setPlainText(mgr.read_today(aid))
        self._today_edit.blockSignals(False)
        self._lt_edit.blockSignals(True)
        self._lt_edit.setPlainText(mgr.read_longterm(aid))
        self._lt_edit.blockSignals(False)
        # Dream 自动开关
        self._dream_auto.blockSignals(True)
        self._dream_auto.setChecked(bool(a.dream_auto_enabled) if a else False)
        self._dream_auto.blockSignals(False)
        self._refresh_dream_history()
        # 重新连接信号（bind 可能切换助手，旧的连接仍指向旧 aid 的 lambda 但用 self._aid 读取，
        # 这里统一重连保证 handler 是最新）
        try:
            self._today_edit.textChanged.disconnect()
        except TypeError:
            pass
        try:
            self._lt_edit.textChanged.disconnect()
        except TypeError:
            pass
        self._today_edit.textChanged.connect(self._on_memory_changed)
        self._lt_edit.textChanged.connect(self._on_memory_changed)

    # ── 置顶 ──

    def _reload_pins(self, items: List[tuple]) -> None:
        # 清空旧行（保留 stretch）
        while self._pin_layout.count() > 1:
            item = self._pin_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._pinned_rows = []
        if not items:
            empty = QLabel("（暂无置顶，点击「新增」添加）", self._pin_container)
            empty.setStyleSheet(_label_style(11))
            self._pin_layout.insertWidget(0, empty)
        else:
            for i, (_pid, content) in enumerate(items):
                row = _PinnedRow(i, content, self._pin_container)
                row.deleteRequested.connect(self._remove_pin)
                self._pin_layout.insertWidget(i, row)
                self._pinned_rows.append(row)
        self._pin_count.setText(f"{len(items)} 条")

    def _add_pin(self) -> None:
        self._reload_pins([*self._pinned(), ("", "")])

    def _remove_pin(self, idx: int) -> None:
        items = self._pinned()
        if 0 <= idx < len(items):
            items.pop(idx)
            self._reload_pins(items)
            self._persist_pins(items)

    def _pinned(self) -> List[tuple]:
        result = []
        for i, row in enumerate(self._pinned_rows):
            content = row.content()
            if not content:
                continue
            result.append((f"pin-{i}-{int(time.time() * 1000)}", content))
        return result

    def _persist_pins(self, items: List[tuple]) -> None:
        if not self._aid:
            return
        mgr = AssistantManager.get_instance()
        mgr.write_pinned(self._aid, items)
        mgr.invalidate_context(self._aid)

    def _on_memory_changed(self) -> None:
        self._save_timer.start()

    def _do_save_memory(self) -> None:
        if not self._aid:
            return
        mgr = AssistantManager.get_instance()
        today = self._today_edit.toPlainText()
        lt = self._lt_edit.toPlainText()
        mgr.write_today(self._aid, today)
        mgr.write_longterm(self._aid, lt)
        mgr.invalidate_context(self._aid)

    # ── Dream ──

    def _run_dream(self) -> None:
        if not self._aid:
            return
        mgr = AssistantManager.get_instance()
        try:
            result = mgr.run_dream(
                self._aid,
                trigger="manual",
                on_progress=lambda stage, payload: None,
            )
            # Dream 会改写 longterm，刷新编辑器
            self._lt_edit.blockSignals(True)
            self._lt_edit.setPlainText(mgr.read_longterm(self._aid))
            self._lt_edit.blockSignals(False)
            mgr.invalidate_context(self._aid)
            self._refresh_dream_history()
            self._show_info(f"Dream 完成：units={result.get('units', 0)}")
        except Exception as e:
            self._show_error(f"Dream 失败：{e}")

    def _on_dream_auto(self, checked: bool) -> None:
        if not self._aid:
            return
        mgr = AssistantManager.get_instance()
        a = mgr.get(self._aid)
        if a:
            a.dream_auto_enabled = checked
            mgr.update(a)

    def _refresh_dream_history(self) -> None:
        if not self._aid:
            return
        mgr = AssistantManager.get_instance()
        history = mgr.dream_history(self._aid)
        if not history:
            self._dream_history.setText("（暂无 Dream 记录）")
            return
        lines = []
        for e in history[:5]:
            ts = e.get("ts", "?")[:19]
            stages = e.get("stages", {})
            lines.append(
                f"• {ts}  trigger={e.get('trigger')}  "
                f"atomize={stages.get('atomize', 0)}→optimize={stages.get('optimize', 0)}"
            )
        self._dream_history.setText("最近 Dream：\n" + "\n".join(lines))

    # ── 通知 ──

    def _show_info(self, text: str) -> None:
        try:
            InfoBar.success(
                title="记忆",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
                parent=self.window(),
            )
        except Exception:
            pass

    def _show_error(self, text: str) -> None:
        try:
            InfoBar.error(
                title="记忆",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────
# 专属技能
# ────────────────────────────────────────────────────────────────


class SkillsTab(QWidget):
    """专属技能（skills/*.md）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._aid: str = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        head = QHBoxLayout()
        title = QLabel("🛠️ 专属技能", self)
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        head.addWidget(title)
        head.addStretch()
        self._count = QLabel("0 个", self)
        self._count.setStyleSheet(_label_style(11))
        head.addWidget(self._count)
        new_btn = QPushButton(FluentIcon.ADD.icon(), "新建技能", self)
        new_btn.setFixedHeight(26)
        new_btn.clicked.connect(self._new_skill)
        head.addWidget(new_btn)
        v.addLayout(head)

        hint = QLabel(
            "助手专属技能：激活助手后，技能描述会注入到 system prompt，"
            "让助手拥有专有能力（如调用某个工具的固定姿势、领域知识）。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_label_style(11))
        v.addWidget(hint)

        split = QSplitter(Qt.Horizontal, self)
        # 左：技能列表
        list_box = _card_frame(split)
        lv = QVBoxLayout(list_box)
        lv.setContentsMargins(4, 4, 4, 4)
        self._skill_list = QListWidget(list_box)
        self._skill_list.setFrameShape(QFrame.NoFrame)
        self._skill_list.setStyleSheet(
            f"""
            QListWidget {{
                background: transparent; border: none;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QListWidget::item {{
                padding: 3px 6px; border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:hover {{ background: {Colors.HOVER_BG}; }}
            QListWidget::item:selected {{ background: {Colors.SELECTED_BG}; }}
        """
        )
        self._skill_list.currentItemChanged.connect(self._on_skill_selected)
        lv.addWidget(self._skill_list, 1)
        del_btn = QPushButton("删除选中技能", list_box)
        del_btn.setFixedHeight(26)
        del_btn.clicked.connect(self._delete_skill)
        lv.addWidget(del_btn)
        split.addWidget(list_box)

        # 右：技能编辑
        edit_box = _card_frame(split)
        ev = QVBoxLayout(edit_box)
        ev.setContentsMargins(6, 4, 6, 4)
        self._skill_name_label = QLabel("（选择或新建技能）", edit_box)
        self._skill_name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)};"
        )
        ev.addWidget(self._skill_name_label)
        self._skill_edit = _MarkdownEditor(placeholder="# 技能名\n\n技能描述/用法…", parent=edit_box)
        ev.addWidget(self._skill_edit, 1)
        save_btn = QPushButton("保存技能", edit_box)
        save_btn.setFixedHeight(28)
        save_btn.clicked.connect(self._save_skill)
        ev.addWidget(save_btn)
        split.addWidget(edit_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        v.addWidget(split, 1)

    def bind(self, aid: str) -> None:
        self._aid = aid
        self._reload_skills()

    def _reload_skills(self, select: str = "") -> None:
        self._skill_list.blockSignals(True)
        self._skill_list.clear()
        if not self._aid:
            self._count.setText("0 个")
            self._skill_list.blockSignals(False)
            return
        mgr = AssistantManager.get_instance()
        skills = mgr.list_skills(self._aid)
        self._count.setText(f"{len(skills)} 个")
        for sk in skills:
            item = QListWidgetItem(f"{sk['name']}  ({sk['content_chars']} 字)")
            item.setData(Qt.UserRole, sk["name"])
            self._skill_list.addItem(item)
        self._skill_list.blockSignals(False)
        if select:
            for i in range(self._skill_list.count()):
                if self._skill_list.item(i).data(Qt.UserRole) == select:
                    self._skill_list.setCurrentRow(i)
                    break
        elif self._skill_list.count():
            self._skill_list.setCurrentRow(0)
        else:
            self._skill_name_label.setText("（选择或新建技能）")
            self._skill_edit.bind("", None)

    def _on_skill_selected(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None or not self._aid:
            return
        name = current.data(Qt.UserRole)
        mgr = AssistantManager.get_instance()
        content = mgr.read_skill(self._aid, name)
        self._skill_name_label.setText(f"技能：{name}")
        self._skill_edit.bind(content, None)

    def _new_skill(self) -> None:
        from .rename_dialog import RenameDialog

        dlg = RenameDialog(
            title="新建技能",
            hint="输入技能名称（英文/数字/下划线）：",
            default="my-skill",
            parent=self.window(),
        )
        dlg.confirmed.connect(self._create_skill)
        dlg.exec_()

    def _create_skill(self, name: str) -> None:
        if not self._aid:
            return
        import re

        safe = re.sub(r"[^a-zA-Z0-9_\-]+", "-", name).strip("-").lower()
        if not safe:
            return
        mgr = AssistantManager.get_instance()
        if not mgr.read_skill(self._aid, safe):
            mgr.write_skill(self._aid, safe, f"# {safe}\n\n技能描述…")
        self._reload_skills(select=safe)

    def _save_skill(self) -> None:
        if not self._aid:
            return
        item = self._skill_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        mgr = AssistantManager.get_instance()
        mgr.write_skill(self._aid, name, self._skill_edit.text())
        self._reload_skills(select=name)
        self._show_info(f"技能「{name}」已保存")

    def _delete_skill(self) -> None:
        if not self._aid:
            return
        item = self._skill_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        mgr = AssistantManager.get_instance()
        mgr.delete_skill(self._aid, name)
        self._reload_skills()

    def _show_info(self, text: str) -> None:
        try:
            InfoBar.success(
                title="技能",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
                parent=self.window(),
            )
        except Exception:
            pass
