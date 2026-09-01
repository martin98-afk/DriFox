# -*- coding: utf-8 -*-
"""assistant_card.py — 助手中心主页面（full 容器浮动卡 · 单列滚动版）

复刻 openhanako AgentTab 的单列结构（DriFox 化）：

    ┌────────────────────────────────────┐
    │        ArcCardStack 弧形卡片堆叠     │
    │   名称 + ★主助手 + 操作行（重命名等）  │
    │   基本信息（对话模型 / 记忆整理模型）    │
    │   关于 Ta（人格 chips / 身份 / AGENTS.md）│
    │   记忆（传送带 + Dream + 版本）        │
    │   经验（工具型 + 反思）               │
    │   专属技能                          │
    └────────────────────────────────────┘

宿主契约：widget_class=AssistantCardWidget、refresh_style()（主题切换回调）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import InfoBar, InfoBarPosition, MaskDialogBase

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from assistant_hub_manager import AssistantManager

from .arc_stack import ArcCardStack
from .assistant_avatar import RoundAvatar
from .overlays import DreamRevisionOverlay, PersonaManageDialog, TextViewOverlay, _dialog_btn
from .rename_dialog import RenameDialog
from .sections import (
    AboutSection,
    ExperienceSection,
    MemorySection,
    ProfileSection,
    SkillsSection,
    _btn_style,
)


def _confirm_dialog(parent, title: str, text: str) -> bool:
    """统一 Mask 风格确认弹窗（替代 QMessageBox，系统字体+主题色）。"""
    from PyQt5.QtGui import QColor

    class _Dialog(MaskDialogBase):
        def __init__(self):
            super().__init__(parent)
            self._yes = False
            self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
            self.setClosableOnMaskClicked(True)
            self.setDraggable(True)
            self.setMaskColor(QColor(0, 0, 0, 76))
            self.widget.setObjectName("hubConfirm")
            self.widget.setStyleSheet(
                f"#hubConfirm {{ background: {Colors.CARD_BG_SOLID}; border: 1px solid {Colors.BORDER};"
                f"border-radius: 12px; }}"
            )
            self.setFixedSize(640, 420)
            v = QVBoxLayout(self.widget)
            v.setContentsMargins(24, 20, 24, 18)
            v.setSpacing(10)
            t = QLabel(title)
            t.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
                f"{get_font_family_css()} {font_size_css(14)}; font-weight: 600;"
            )
            v.addWidget(t)
            body = QLabel(text)
            body.setWordWrap(True)
            body.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; border: none;"
                f"{get_font_family_css()} {font_size_css(12)};"
            )
            v.addWidget(body)
            v.addStretch()
            row = QHBoxLayout()
            row.addStretch()
            cancel = _dialog_btn("取消")
            cancel.clicked.connect(self.reject)
            ok = _dialog_btn("确定", primary=True)
            ok.clicked.connect(self._yes_accept)
            row.addWidget(cancel)
            row.addWidget(ok)
            v.addLayout(row)
            x = max(0, (self.width() - self.widget.width()) // 2)
            y = max(0, (self.height() - self.widget.height()) // 2)
            self.widget.move(x, y)

        def _yes_accept(self):
            self._yes = True
            self.accept()

    dlg = _Dialog()
    dlg.exec_()
    return dlg._yes


class AssistantCardWidget(QWidget):
    """助手中心主卡片（单列滚动页）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mgr = AssistantManager.get_instance()
        self._active_aid: str = ""
        self._dream_running = False
        self._build_ui()
        self._reload_all()

    # ══════════════════════════════════════════════════
    #  UI 构建
    # ══════════════════════════════════════════════════

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        outer.addWidget(scroll)

        content = QWidget()
        self._content_v = QVBoxLayout(content)
        self._content_v.setContentsMargins(24, 8, 24, 24)
        self._content_v.setSpacing(14)

        # 1. 弧形卡片堆叠：占满整行（不放进窄容器，助手多也能全展示）
        self._stack = ArcCardStack()
        self._stack.selectionChanged.connect(self._on_select)
        self._stack.createRequested.connect(self._on_create)
        self._content_v.addWidget(self._stack)

        inner = QWidget()
        inner.setMaximumWidth(900)
        self._inner_v = QVBoxLayout(inner)
        self._inner_v.setContentsMargins(0, 0, 0, 0)
        self._inner_v.setSpacing(14)

        # 2. 名称行 + 操作（头像可点击更换；当前助手 accent 高亮 + 徽章）
        name_row = QHBoxLayout()
        self._avatar = RoundAvatar(size=44, text="?", color="#7C3AED", parent=self)
        self._avatar.setCursor(Qt.PointingHandCursor)
        self._avatar.setToolTip("点击更换头像")
        self._avatar.installEventFilter(self)
        name_row.addWidget(self._avatar)
        self._name_label = QLabel("(未选择)")
        self._name_label.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; background: transparent; border: none;"
            f"{get_font_family_css()} 20px; font-weight: 700;"
        )
        name_row.addWidget(self._name_label)
        self._active_badge = QLabel("当前助手")
        self._active_badge.setStyleSheet(
            f"""
            QLabel {{
                color: {Colors.TEXT_ACCENT}; background: rgba(245, 158, 11, 0.12);
                border: 1px solid {Colors.TEXT_ACCENT}; border-radius: 9px;
                padding: 1px 10px;
                {get_font_family_css()} {font_size_css(10)}
            }}
        """
        )
        name_row.addWidget(self._active_badge)
        self._primary_badge = QLabel("★ 主助手")
        self._primary_badge.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: {Colors.HOVER_BG};"
            f"border: 1px solid {Colors.BORDER}; border-radius: 9px; padding: 1px 10px;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        self._primary_badge.hide()
        name_row.addWidget(self._primary_badge)
        name_row.addStretch()
        for text, handler, danger in (
            ("设为主助手", self._on_set_primary, False),
            ("重命名", self._on_rename, False),
            ("删除此助手", self._on_delete, True),
        ):
            btn = QPushButton(text)
            btn.setStyleSheet(_btn_style(danger=danger))
            btn.clicked.connect(handler)
            name_row.addWidget(btn)
        self._inner_v.addLayout(name_row)

        # 3-6. 分区
        self._profile = ProfileSection()
        self._profile.saveRequested.connect(self._on_profile_save)
        self._inner_v.addWidget(self._profile)

        self._about = AboutSection(self._persona_items(), "")
        self._about.personaChangeRequested.connect(self._on_persona_change)
        self._about.personaManageRequested.connect(self._on_persona_manage)
        self._about.saveRequested.connect(self._on_about_save)
        self._inner_v.addWidget(self._about)

        self._memory = MemorySection()
        self._memory.toggleMemory.connect(self._on_memory_toggle)
        self._memory.toggleDreamAuto.connect(self._on_dream_auto_toggle)
        self._memory.viewToday.connect(self._on_view_today)
        self._memory.dreamRun.connect(self._on_dream_run)
        self._memory.dreamRestore.connect(self._on_dream_restore)
        self._memory.viewAll.connect(self._on_view_all)
        self._memory.clearAll.connect(self._on_clear_all)
        self._memory.pinsChanged.connect(self._on_pins_changed)
        self._inner_v.addWidget(self._memory)

        self._experience = ExperienceSection()
        self._experience.toggleExperience.connect(self._on_experience_toggle)
        self._experience.viewCategory.connect(self._on_view_experience)
        self._experience.reflectRequested.connect(self._on_reflect)
        self._inner_v.addWidget(self._experience)

        self._skills = SkillsSection()
        self._inner_v.addWidget(self._skills)

        self._inner_v.addStretch(1)

        center = QHBoxLayout()
        center.addStretch()
        center.addWidget(inner)
        center.addStretch()
        self._content_v.addLayout(center, 1)
        scroll.setWidget(content)

    # ══════════════════════════════════════════════════
    #  数据绑定
    # ══════════════════════════════════════════════════

    def _persona_items(self) -> List[dict]:
        try:
            reg = self._mgr.persona_registry()
            items = []
            for p in reg.list_all():
                if p.id == "none":
                    continue  # none 走横幅
                ap = reg.avatar_path(p.id)
                items.append(
                    {
                        "id": p.id,
                        "name": p.name,
                        "description": p.description,
                        "tag": p.tag,
                        "avatar_path": str(ap or ""),
                    }
                )
            return items
        except Exception:
            return []

    def eventFilter(self, obj, event):
        """头像点击 → 更换头像。"""
        from PyQt5.QtCore import QEvent

        if obj is self._avatar and event.type() == QEvent.MouseButtonPress:
            self._on_change_avatar()
            return True
        return super().eventFilter(obj, event)

    def _on_change_avatar(self):
        """弹出头像选择器（预置/纯色/上传），确定后落盘（Mask 风格）。

        ⚠ 内层类方法里的 self 是 dialog 实例——mgr/aid 必须先捕获为局部变量。
        """
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        from PyQt5.QtGui import QColor

        from .avatar_picker import AvatarPicker

        mgr, aid = self._mgr, self._active_aid  # 局部捕获（闭包内禁用 self._mgr）
        name, color = a.name or a.id, a.color

        class _AvatarDialog(MaskDialogBase):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
                self.setClosableOnMaskClicked(True)
                self.setDraggable(True)
                self.setMaskColor(QColor(0, 0, 0, 76))
                self.widget.setObjectName("hubAvatarDialog")
                self.widget.setStyleSheet(
                    f"#hubAvatarDialog {{ background: {Colors.CARD_BG_SOLID};"
                    f"border: 1px solid {Colors.BORDER}; border-radius: 14px; }}"
                )
                self.setFixedSize(780, 620)
                v = QVBoxLayout(self.widget)
                v.setContentsMargins(20, 16, 20, 16)
                picker = AvatarPicker(assistant_id=aid, parent=self.widget)
                ap = mgr.avatar_path(aid)
                picker.set_assistant(
                    aid=aid,
                    color=color,
                    name=name,
                    image_path=str(ap) if ap else "",
                )
                v.addWidget(picker, 1)
                self._picker = picker  # 确定后外部取选择结果
                row = QHBoxLayout()
                row.addStretch()
                cancel = _dialog_btn("取消")
                cancel.clicked.connect(self.reject)
                ok = _dialog_btn("确定", primary=True)
                ok.clicked.connect(self.accept)
                row.addWidget(cancel)
                row.addWidget(ok)
                v.addLayout(row)
                x = max(0, (self.width() - self.widget.width()) // 2)
                y = max(0, (self.height() - self.widget.height()) // 2)
                self.widget.move(x, y)

        dlg = _AvatarDialog(self.window())
        if not dlg.exec_():
            return
        sel = dlg._picker.get_selection()
        if sel.get("image_path"):
            try:
                data = Path(sel["image_path"]).read_bytes()
                ext = Path(sel["image_path"]).suffix.lstrip(".") or "png"
                self._mgr.save_avatar_from_bytes(a.id, data, ext)
            except Exception as e:
                self._notify_error(f"头像保存失败: {e}")
                return
        else:
            self._mgr.clear_avatar(a.id)
            a.color = sel.get("color") or a.color
            self._mgr.update(a)
        self._reload_all(select_aid=a.id)
        self._notify("头像已更新")

    def _reload_all(self, select_aid: str = "") -> None:
        assistants = self._mgr.list_assistants()
        self._stack.set_assistants(
            [
                {
                    "id": a.id,
                    "name": a.name or a.id,
                    "color": a.color,
                    "avatar_path": str(self._mgr.avatar_path(a.id) or ""),
                }
                for a in assistants
            ]
        )
        primary = next((a.id for a in assistants if a.primary), "")
        self._stack.set_primary(primary)
        if select_aid and self._mgr.has(select_aid):
            self._bind_editor(select_aid)
        elif self._active_aid and self._mgr.has(self._active_aid):
            self._bind_editor(self._active_aid)
        elif assistants:
            self._bind_editor(assistants[0].id)
        else:
            self._show_empty()

    def _show_empty(self) -> None:
        self._active_aid = ""
        self._name_label.setText("(无助手)")
        self._avatar.set_text("?")

    def _bind_editor(self, aid: str) -> None:
        self._active_aid = aid
        a = self._mgr.get(aid)
        if not a:
            return
        ap = self._mgr.avatar_path(aid)
        self._avatar.set_text(a.name or a.id)
        self._avatar.set_color(a.color)
        self._avatar.set_image(str(ap) if ap else None)
        self._name_label.setText(a.name or a.id)
        self._primary_badge.setVisible(a.primary)
        self._stack.set_selected(aid)

        aid_capture = aid

        def _do_bind() -> None:
            mgr = self._mgr
            self._profile.bind(a.name or a.id, a.model or "", a.utility_model or "")
            self._about.set_persona(a.yuan)
            identity, _ = mgr.read_identity_source(aid_capture)
            agents_md, _ = mgr.read_agents_md_source(aid_capture)
            self._about.bind_texts(identity, agents_md)
            self._memory.set_memory_enabled(a.memory_enabled)
            self._memory.set_dream_auto(a.dream_auto_enabled)
            self._memory.reload_pins([c for _pid, c in mgr.read_pinned(aid_capture)])
            self._memory.set_status(self._memory_status(aid_capture))
            self._memory.set_dream_hint("每日自动 Dream 已开启" if a.dream_auto_enabled else "每日自动 Dream 未开启")
            self._experience.set_enabled(a.experience_enabled)
            self._experience.reload_categories(mgr.experience_list(aid_capture))
            self._skills.reload_skills(mgr.list_skills(aid_capture), self._on_view_skill)

        QTimer.singleShot(0, _do_bind)

    def _memory_status(self, aid: str) -> str:
        try:
            text = self._mgr.compiled_memory(aid)
            return f"已编译 {len(text)} 字" if text else "暂无记忆"
        except Exception:
            return "暂无记忆"

    # ══════════════════════════════════════════════════
    #  选择 / 创建 / 删除 / 主助手 / 重命名
    # ══════════════════════════════════════════════════

    def _on_select(self, aid: str) -> None:
        if aid and self._mgr.has(aid):
            self._bind_editor(aid)

    def _on_create(self) -> None:
        dlg = RenameDialog(
            title="新建助手",
            hint="输入助手名称（例如：小助手 / 翻译官 / 代码审查员）：",
            default="",
            parent=self.window(),
        )
        dlg.confirmed.connect(self._do_create)
        dlg.exec_()

    def _do_create(self, name: str) -> None:
        a = self._mgr.create(name=name)
        self._reload_all(select_aid=a.id)
        self._notify(f"助手「{a.name}」已创建")

    def _on_delete(self) -> None:
        if not self._active_aid:
            return
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        ret = _confirm_dialog(
            self.window(),
            "删除助手",
            f"确定删除助手「{a.name}」？\n其身份、提示词、记忆、头像将一并删除。\n（该操作不可撤销）",
        )
        if not ret:
            return
        if self._mgr.delete(a.id):
            self._active_aid = ""
            self._reload_all()
            self._notify(f"助手「{a.name}」已删除")
        else:
            self._notify_error("至少保留一个助手，无法删除")

    def _on_rename(self) -> None:
        a = self._mgr.get(self._active_aid) if self._active_aid else None
        if not a:
            return
        dlg = RenameDialog(title="重命名助手", hint="新的名称：", default=a.name, parent=self.window())
        dlg.confirmed.connect(self._do_rename)
        dlg.exec_()

    def _do_rename(self, name: str) -> None:
        a = self._mgr.get(self._active_aid)
        if a is None or not name:
            return
        a.name = name
        self._mgr.update(a)
        self._mgr.invalidate_context(a.id)
        self._reload_all(select_aid=a.id)

    def _on_set_primary(self) -> None:
        if not self._active_aid:
            return
        if self._mgr.set_primary(self._active_aid):
            self._reload_all(select_aid=self._active_aid)
            self._notify("已设为主助手")

    # ══════════════════════════════════════════════════
    #  基本信息 / 关于 Ta
    # ══════════════════════════════════════════════════

    def _on_profile_save(self, name: str, chat_model: str, utility_model: str) -> None:
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        if name:
            a.name = name
        a.model = chat_model
        a.utility_model = utility_model
        self._mgr.update(a)
        self._mgr.invalidate_context(a.id)
        self._notify("已保存")
        self._reload_all(select_aid=a.id)

    def _on_persona_change(self, pid: str) -> None:
        a = self._mgr.get(self._active_aid)
        if not a or a.yuan == pid:
            return
        a.yuan = pid
        self._mgr.update(a)
        self._mgr.invalidate_context(a.id)
        self._about.set_persona(pid)
        self._notify(f"人格已切换：{'无（纯净助手）' if pid == 'none' else pid}")

    def _on_persona_manage(self) -> None:
        reg = self._mgr.persona_registry()
        current = self._mgr.get(self._active_aid).yuan if self._active_aid else ""
        dlg = PersonaManageDialog(reg, current, parent=self.window())
        dlg.exec_()
        self._reload_all(select_aid=self._active_aid)

    def _on_about_save(self, identity: str, agents_md: str) -> None:
        aid = self._active_aid
        if not aid:
            return
        self._mgr.write_identity(aid, identity)
        self._mgr.write_agents_md(aid, agents_md)
        self._mgr.invalidate_context(aid)
        self._notify("人格已保存")

    # ══════════════════════════════════════════════════
    #  记忆
    # ══════════════════════════════════════════════════

    def _on_memory_toggle(self, on: bool) -> None:
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        a.memory_enabled = bool(on)
        self._mgr.update(a)
        self._mgr.invalidate_context(a.id)
        self._memory.setEnabled_all(bool(on))

    def _on_dream_auto_toggle(self, on: bool) -> None:
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        a.dream_auto_enabled = bool(on)
        self._mgr.update(a)
        self._memory.set_dream_hint("每日自动 Dream 已开启" if on else "每日自动 Dream 未开启")

    def _on_pins_changed(self, pins: List) -> None:
        aid = self._active_aid
        if not aid:
            return
        items = [(f"pin-{i}-{abs(hash(t)) % 100000}", t) for i, t in enumerate(pins)]
        self._mgr.write_pinned(aid, items)
        self._mgr.invalidate_context(aid)

    def _on_view_today(self) -> None:
        if not self._active_aid:
            return
        path = self._mgr.memory_dir(self._active_aid) / "today.md"
        text = path.read_text(encoding="utf-8") if Path(path).exists() else ""
        TextViewOverlay("当下记忆", text or "（暂无当下记忆）", parent=self.window()).exec_()

    def _on_view_all(self) -> None:
        if not self._active_aid:
            return
        text = self._mgr.compiled_memory(self._active_aid)
        TextViewOverlay("所有记忆", text or "（暂无记忆）", parent=self.window()).exec_()

    def _on_dream_run(self) -> None:
        if not self._active_aid or self._dream_running:
            return
        aid = self._active_aid
        self._dream_running = True
        self._memory.set_dream_running(True)

        def _worker():
            try:
                result = self._mgr.dream_start(aid, "manual")
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            QTimer.singleShot(0, lambda: self._dream_done(result))

        threading.Thread(target=_worker, daemon=True).start()

    def _dream_done(self, result: dict) -> None:
        self._dream_running = False
        self._memory.set_dream_running(False)
        if result.get("ok"):
            self._memory.set_status(self._memory_status(self._active_aid))
            self._notify("Dream 整理完成" + ("（内容无变化）" if not result.get("changed") else ""))
        else:
            self._notify_error(f"Dream 失败：{result.get('error', '未知错误')}")

    def _on_dream_restore(self) -> None:
        if not self._active_aid:
            return
        aid = self._active_aid
        revisions = self._mgr.dream_revisions(aid)
        if not revisions:
            self._notify_error("暂无 Dream 版本")
            return

        def _preview(rid: str) -> str:
            try:
                p = self._mgr.dream_dir(aid) / "revisions" / f"{rid}.json"
                if not p.exists():
                    return "（无法读取版本内容）"
                before = (json.loads(p.read_text(encoding="utf-8")) or {}).get("before") or {}
                parts = [f"# 事实\n{before.get('facts', '')}", f"# 长期\n{before.get('longterm', '')}"]
                for d in before.get("daily") or []:
                    parts.append(f"# {d.get('date')}\n{d.get('body', '')}")
                return "\n\n".join(parts)
            except Exception:
                return "（无法读取版本内容）"

        dlg = DreamRevisionOverlay(
            revisions,
            preview_fn=_preview,
            restore_fn=lambda rid: self._mgr.dream_restore(aid, rid),
            parent=self.window(),
        )
        dlg.exec_()
        self._memory.set_status(self._memory_status(aid))

    def _on_clear_all(self) -> None:
        if not self._active_aid:
            return
        aid = self._active_aid
        ret = _confirm_dialog(self.window(), "清除记忆", "确定清除该助手的全部记忆（置顶记忆保留）？\n该操作不可撤销。")
        if not ret:
            return
        mem = self._mgr.memory_dir(aid)
        for name in ("today.md", "longterm.md", "facts.md", "memory.md"):
            p = mem / name
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        self._mgr.invalidate_context(aid)
        self._memory.set_status(self._memory_status(aid))
        self._notify("记忆已清除")

    # ══════════════════════════════════════════════════
    #  经验 / 技能
    # ══════════════════════════════════════════════════

    def _on_experience_toggle(self, on: bool) -> None:
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        a.experience_enabled = bool(on)
        self._mgr.update(a)
        self._experience.set_enabled(bool(on))

    def _on_view_experience(self, category: str) -> None:
        if not self._active_aid:
            return
        aid = self._active_aid
        text = self._mgr.experience_read(aid, category)

        def _save(content: str):
            lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
            doc_path = self._mgr.experience_dir(aid) / f"{category}.md"
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
            try:
                self._mgr._core_experience().rebuild_index(self._mgr.assistant_dir(aid))
            except Exception:
                pass

        TextViewOverlay(f"经验 · {category}", text, editable=True, on_save=_save, parent=self.window()).exec_()
        self._experience.reload_categories(self._mgr.experience_list(aid))

    def _on_reflect(self) -> None:
        if not self._active_aid or self._dream_running:
            return
        aid = self._active_aid
        self._notify("反思进行中…")

        def _worker():
            r = self._mgr.experience_reflect(aid)
            msg = (
                f"反思完成：新增 {r.get('added', 0)} 条经验"
                if r.get("added")
                else f"反思完成：暂无新经验 {('(' + r.get('error', '') + ')') if r.get('error') else ''}"
            )
            QTimer.singleShot(0, lambda: self._notify(msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_view_skill(self, name: str) -> None:
        if not self._active_aid:
            return
        text = self._mgr.read_skill(self._active_aid, name)
        TextViewOverlay(
            f"技能 · {name}",
            text,
            editable=True,
            on_save=lambda c: self._mgr.write_skill(self._active_aid, name, c),
            parent=self.window(),
        ).exec_()

    # ══════════════════════════════════════════════════
    #  通知 / 宿主契约
    # ══════════════════════════════════════════════════

    def _notify(self, text: str) -> None:
        try:
            InfoBar.success(
                title="助手中心",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
                parent=self.window(),
            )
        except Exception:
            pass

    def _notify_error(self, text: str) -> None:
        try:
            InfoBar.error(
                title="助手中心",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception:
            pass

    def refresh_style(self) -> None:
        """主题切换后刷新（宿主契约）"""
        Colors.refresh()
        self._reload_all(select_aid=self._active_aid)
